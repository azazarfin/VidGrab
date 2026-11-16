"""Opt-in renderer boundary: actual v1 service, fake browser/Tauri delivery."""

import json
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


BOOTSTRAP = r"""
(() => {
  const events = [];
  const call = message => window.__vidgrabControl(message);
  const chrome = window.chrome = {
    runtime: {
      id: 'ikjpekhbbaehdhigakbahlhafaojbjpm', lastError: null,
      getURL: path => 'chrome-extension://ikjpekhbbaehdhigakbahlhafaojbjpm/' + path,
      openOptionsPage() {},
      sendMessage(message, callback) {
        call(message).then(response => {
          if (response?.lost_reply) {
            chrome.runtime.lastError = {message: 'Fixture lost reply'};
            callback(); chrome.runtime.lastError = null;
          } else callback(response);
        });
      },
    },
    tabs: { query(_query, callback) { callback([{id: 0, url: 'https://fixture.invalid/media.mp4'}]); } },
    storage: {
      local: {
        get(keys, callback) { call({type:'fixture.storage.get', keys}).then(callback); },
        set(values, callback) { call({type:'fixture.storage.set', values}).then(changes => {
          events.forEach(listener => listener(changes, 'local')); callback?.();
        }); },
        remove(keys, callback) { call({type:'fixture.storage.remove', keys}).then(() => callback?.()); },
      },
      onChanged: {addListener(listener) {events.push(listener);}},
    },
  };
  window.__TAURI_INTERNALS__ = {
    invoke: async (command, arguments) => {
      const result = await call({type:'fixture.invoke', command, arguments});
      if (result?.lost_reply) throw new Error('Fixture lost reply');
      return result;
    },
    transformCallback() {return 1;},
  };
})();
"""


class ClientFixture:
    def __init__(self, root):
        from backend.engine import create_engine_app
        from backend.runtime import RuntimeConfig
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.stack = ExitStack()
        temporary = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="vidgrab-p04-client-ui-")))
        self.runtime = RuntimeConfig(data_dir=temporary / "state", download_dir=temporary / "media")
        self.probe_urls = []
        self.requests = []
        self.lost_operations = set()
        self.excluded_features = set()
        self.pending = []
        self.preferences = {"backendUrl": "http://untrusted.invalid:8000", "defaultFormat": "old-browser-default",
                            "cookieForwardingEnabled": False, "notificationPreference": "none"}
        self.permissions = {"cookies_granted": True, "native_granted": True, "notifications_granted": True,
                            "site_granted": True, "permission_api_available": True, "origins": ["<all_urls>"]}
        self.contexts = {}
        self.context_calls = []
        self.application = {"kind": "appimage", "installed": False, "app": "/fixture/VidGrab.AppImage", "shortcut": None}
        self.connection = {"enabled": True, "bridge_ready": True, "chrome": "registered", "firefox": "registered"}

        def probe(url, runtime, *, auth_context=None):
            self.probe_urls.append(url)
            return {"title": "Fixture <img src=x onerror=window.pwned=1>", "duration": 12,
                    "formats": [{"format_id": "720", "ext": "mp4", "resolution": "720p", "vcodec": "avc1", "acodec": "none"}],
                    "subtitles": {"en": [{"ext": "vtt", "name": "English"}]}}

        self.engine = create_engine_app(self.runtime, token="a" * 64, port=43210, media_mode="ordinary",
                                        components={"ffmpeg": "/fixture/ffmpeg", "ffprobe": "/fixture/ffprobe", "deno": "/fixture/deno"},
                                        probe_runner=probe, start_queue=False)
        self.client = self.stack.enter_context(TestClient(self.engine, base_url="http://127.0.0.1:43210",
                                                         headers={"Authorization": "Bearer " + "a" * 64}))
        self.engine.app.state.control.component_versions = {"ffmpeg": "fixture", "yt_dlp": "2026.6.9"}

    def close(self):
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def request(self, operation, params=None, key=None):
        message = {"protocol_version": 1, "request_id": str(uuid4()), "operation": operation, "params": params or {}}
        if key is not None:
            message["idempotency_key"] = key
        result = self.client.post("/v1/requests", json=message)
        assert result.status_code == 200, result.text
        return result.json()["data"]

    def settings(self):
        return self.request("settings.get")

    def patch(self, values):
        return self.request("settings.patch", {"expected_revision": self.settings()["revision"], "patch": values}, str(uuid4()))

    def jobs(self):
        return self.request("downloads.list", {"limit": 50})["jobs"]

    def set_status(self, job_id, status):
        with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status=?,auth_required=?,revision=revision+1 WHERE id=?",
                       (status, int(status == "waiting_for_auth"), job_id))
            revision = db.execute("SELECT revision FROM downloads WHERE id=?", (job_id,)).fetchone()[0]
            db.execute("INSERT INTO protocol_events(topic,entity_id,revision,kind) VALUES('downloads',?,?,'changed')", (job_id, revision))
        if job_id in self.engine.app.state.queue.in_memory_progress:
            self.engine.app.state.queue.in_memory_progress[job_id]["status"] = status

    def install(self, context):
        context.expose_binding("__vidgrabControl", self.handle)
        context.add_init_script(BOOTSTRAP)

    def handle(self, source, message):
        kind = message["type"]
        if kind == "fixture.storage.get":
            keys = message.get("keys")
            return self.preferences if keys is None else {key: self.preferences.get(key) for key in (keys if isinstance(keys, list) else [keys])}
        if kind == "fixture.storage.set":
            values = message["values"]
            changes = {key: {"oldValue": self.preferences.get(key), "newValue": value} for key, value in values.items()}
            self.preferences.update(values)
            return changes
        if kind == "fixture.storage.remove":
            keys = message["keys"]
            for key in keys if isinstance(keys, list) else [keys]:
                self.preferences.pop(key, None)
            return None
        if kind == "companion.permissions":
            return {"ok": True, "data": {**self.permissions, "cookie_forwarding_enabled": self.preferences["cookieForwardingEnabled"],
                                          "notification_preference": self.preferences["notificationPreference"]}}
        if kind == "companion.pending":
            return {"ok": True, "data": self.pending}
        if kind == "companion.dismiss":
            self.pending = [item for item in self.pending if item["key"] != message["key"]]
            return {"ok": True}
        if kind == "companion.context":
            if not self.preferences["cookieForwardingEnabled"]:
                return {"ok": True, "data": None}
            if not self.permissions["cookies_granted"] or not self.permissions["site_granted"]:
                return {"ok": False, "error": {"code": "BROWSER_CONNECTION_UNAVAILABLE", "message": "Review browser site permissions"}}
            scope = (message["url"], message.get("job_id"))
            if message.get("fresh") or scope not in self.contexts:
                params = {"url": scope[0], "instance_id": self.engine.app.state.owner.id,
                          "cookies": [{"domain": ".fixture.invalid", "path": "/", "name": "session", "value": "fixture-cookie-secret", "secure": True}]}
                if scope[1]:
                    params["job_id"] = scope[1]
                self.context_calls.append(params)
                self.contexts[scope] = self.request("auth.create", params, str(uuid4()))
            return {"ok": True, "data": self.contexts[scope]}
        if kind == "fixture.invoke":
            command = message["command"]
            arguments = message.get("arguments") or {}
            if command == "protocol_request":
                return self.control(arguments["request"])
            if command == "companion_status":
                return {"state": "ready", "tray_available": False, "jobs": [{"id": "legacy-decoy", "title": "Legacy decoy", "status": "complete"}],
                        "browser_connection": self.connection, "application": self.application, "downloads_active": bool(self.request("downloads.list")["counts"]["unfinished"])}
            if command == "set_browser_connection":
                self.connection["enabled"] = arguments["enabled"]
                return self.connection
            if command in ("quit_companion", "open_software_manager"):
                return None
            raise AssertionError("Unmigrated desktop command: " + command)
        if kind == "companion.request":
            return self.control(message["request"])
        raise AssertionError("Unsupported fixture message: " + kind)

    def control(self, request):
        self.requests.append(request)
        response = self.client.post("/v1/requests", json=request).json()
        operation = request["operation"]
        if response["ok"] and operation == "capabilities.get":
            response["data"]["features"] = [feature for feature in response["data"]["features"] if feature not in self.excluded_features]
        if response["ok"] and operation in self.lost_operations:
            self.lost_operations.remove(operation)
            self.pending.append({"key": request["idempotency_key"], "operation": operation})
            return {"lost_reply": True}
        if response["ok"] and request.get("idempotency_key"):
            self.pending = [item for item in self.pending if item["key"] != request["idempotency_key"]]
        return response

    def write_report(self, path):
        # Request payloads may contain URLs/references. Retain only operation/ID/key summaries.
        report = {"request_count": len(self.requests), "requests": [{key: request[key] for key in ("operation", "request_id", "idempotency_key") if key in request}
                                                                  for request in self.requests],
                  "job_count": len(self.jobs()), "settings_revision": self.settings()["revision"],
                  "context_count": len(self.context_calls), "pending_count": len(self.pending)}
        Path(path).write_text(json.dumps(report, indent=2) + "\n")
