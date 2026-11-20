"""Opt-in renderer boundary: actual v1 service, fake browser/Tauri delivery."""

import json
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

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
    permissions: {request(value, callback) {call({type:'fixture.permissions.request', value}).then(callback);}},
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
                            "cookieForwardingEnabled": False, "requestObservationEnabled": False, "notificationPreference": "none"}
        self.permission_requests = []
        self.permission_approval = True
        self.is_firefox = False
        self._was_ungranted = True
        self._pending_restart = False
        self.permissions = {"cookies_granted": True, "native_granted": True, "notifications_granted": True,
                            "request_observation_granted": False, "site_granted": True, "permission_api_available": True, "origins": ["<all_urls>"]}
        self.contexts = {}
        self.context_calls = []
        self.now = time.time()
        self.probe_error = None
        self.probe_formats = [{"format_id": "720", "ext": "mp4", "resolution": "720p", "vcodec": "avc1", "acodec": "none"}]
        self.probe_extra = {}
        self.application = {"kind": "appimage", "installed": False, "app": "/fixture/VidGrab.AppImage", "shortcut": None}
        self.connection = {"enabled": True, "bridge_ready": True, "chrome": "registered", "firefox": "registered"}
        self.desktop_preferences = {
            "preferences_version": 1,
            "start_at_login": False,
            "close_to_tray": True,
            "start_minimized": False,
            "onboarding_completed": False,
            "tray_available": False,
            "packaged": True,
            "autostart_state": "missing",
        }
        self.activated = 0
        self.restarts = 0
        self.revealed = []
        self.opened = []
        self.picked_folder = None
        self.url_list_pick = None
        self.exported_diagnostics_path = None
        self.engine_log_tail = ["INFO engine ready"]
        self.update_status = {
            "app_version": "0.1.0",
            "engine_bundle_version": "0.1.0",
            "protocol_version": 1,
            "platform": "linux-x86_64",
            "channel": "stable",
            "rollback_available": False,
            "previous_version": "",
            "pending_update": None,
            "rollback_notice": None,
            "last_check": None,
            "backups_count": 0,
            "backups": [],
        }
        self.next_update_check = None
        self.legacy_config_import = {
            "source_path": "/tmp/legacy/config.json",
            "patch": {"max_concurrent": 5, "default_format": "best[height<=720]"},
            "imported_keys": ["max_concurrent", "default_format"],
            "skipped_keys": ["custom_ytdlp_args", "auto_update_ytdlp"],
            "warnings": [
                "Skipped 'custom_ytdlp_args': raw command-line arguments are disabled in the packaged VidGrab engine for safety.",
                "Skipped 'auto_update_ytdlp': signed app/engine bundle updates replace runtime pip self-updates.",
            ],
        }

        def probe(url, runtime, *, auth_context=None):
            self.probe_urls.append(url)
            if self.probe_error is not None:
                raise self.probe_error
            return {"title": "Fixture <img src=x onerror=window.pwned=1>", "duration": 12,
                    "formats": self.probe_formats,
                    "subtitles": {"en": [{"ext": "vtt", "name": "English"}]}, **self.probe_extra}

        self.engine = create_engine_app(self.runtime, token="a" * 64, port=43210, media_mode="ordinary",
                                        components={"ffmpeg": "/fixture/ffmpeg", "ffprobe": "/fixture/ffprobe", "deno": "/fixture/deno"},
                                        probe_runner=probe, clock=lambda: self.now, start_queue=False)
        self.client = self.stack.enter_context(TestClient(self.engine, base_url="http://127.0.0.1:43210",
                                                         headers={"Authorization": "Bearer " + "a" * 64}))
        self.engine.app.state.control.component_versions = {"ffmpeg": "fixture", "yt_dlp": "2026.6.9"}

    def restart_browser(self):
        self._pending_restart = False

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

    def set_status(self, job_id, status, *, error_code=None):
        with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status=?,auth_required=?,error_code=COALESCE(?,error_code),revision=revision+1 WHERE id=?",
                       (status, int(status == "waiting_for_auth"), error_code, job_id))
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
            granted = bool(self.permissions.get("request_observation_granted"))
            enabled = bool(self.preferences["requestObservationEnabled"])
            restart_required = bool(not self.is_firefox and enabled and granted and self._pending_restart)
            return {"ok": True, "data": {**self.permissions, "cookie_forwarding_enabled": self.preferences["cookieForwardingEnabled"],
                                          "request_observation_enabled": enabled,
                                          "request_observation_restart_required": restart_required,
                                          "notification_preference": self.preferences["notificationPreference"]}}
        if kind == "fixture.permissions.request":
            self.permission_requests.append(message["value"])
            previous = bool(self.permissions.get("request_observation_granted"))
            next_granted = bool(self.permission_approval)
            self.permissions["request_observation_granted"] = next_granted
            if not self.is_firefox:
                if not next_granted:
                    self._was_ungranted = True
                    self._pending_restart = False
                elif not previous or self._was_ungranted:
                    self._was_ungranted = False
                    self._pending_restart = True
            else:
                self._was_ungranted = not next_granted
                self._pending_restart = False
            return self.permission_approval
        if kind == "companion.pending":
            return {"ok": True, "data": self.pending}
        if kind == "companion.dismiss":
            self.pending = [item for item in self.pending if item["key"] != message["key"]]
            return {"ok": True}
        if kind == "companion.context":
            candidate = None
            if message.get("candidate_id"):
                candidate = next((item for item in self.preferences.get("detected_videos_0", [])
                                  if item.get("id") == message["candidate_id"] and item["url"] == message["url"]), None)
                if candidate is None:
                    return {"ok": False, "error": {"code": "BROWSER_CONTEXT_EXPIRED", "message": "Select media from the current tab"}}
            if not self.preferences["cookieForwardingEnabled"] and candidate is None:
                return {"ok": True, "data": None}
            if self.preferences["cookieForwardingEnabled"] and (not self.permissions["cookies_granted"] or not self.permissions["site_granted"]):
                return {"ok": False, "error": {"code": "BROWSER_CONNECTION_UNAVAILABLE", "message": "Review browser site permissions"}}
            scope = (message["url"], message.get("job_id"), message.get("candidate_id"))
            if message.get("fresh") or scope not in self.contexts:
                params = {"url": scope[0], "instance_id": self.engine.app.state.owner.id,
                          "cookies": [{"domain": ".fixture.invalid", "path": "/", "name": "session", "value": "fixture-cookie-secret", "secure": True}]
                                     if self.preferences["cookieForwardingEnabled"] else []}
                if candidate is not None:
                    frame = urlsplit(candidate["frame_url"])
                    params["referer"] = f"{frame.scheme}://{frame.netloc}/"
                if scope[1]:
                    params["job_id"] = scope[1]
                self.context_calls.append(params)
                self.contexts[scope] = self.request("auth.create", params, str(uuid4()))
            return {"ok": True, "data": self.contexts[scope]}
        if kind == "companion.discovery.remove":
            key = "detected_videos_" + str(message["tab_id"])
            self.preferences[key] = [item for item in self.preferences.get(key, [])
                                     if item.get("id", item["url"]) not in message["ids"]]
            return {"ok": True}
        if kind == "companion.activate":
            self.activated += 1
            return {"ok": True, "data": {"activated": True}}
        if kind == "fixture.invoke":
            command = message["command"]
            arguments = message.get("arguments") or {}
            if command == "protocol_request":
                return self.control(arguments["request"])
            if command == "companion_status":
                return {"state": "ready", "tray_available": self.desktop_preferences["tray_available"],
                        "supervisor_pid": 1001, "engine_pid": 2001 + self.restarts,
                        "engine_generation": 1 + self.restarts,
                        "jobs": [{"id": "legacy-decoy", "title": "Legacy decoy", "status": "complete"}],
                        "browser_connection": self.connection, "application": self.application,
                        "downloads_active": bool(self.request("downloads.list")["counts"]["unfinished"])}
            if command == "set_browser_connection":
                self.connection["enabled"] = arguments["enabled"]
                self.connection["chrome"] = "registered" if arguments["enabled"] else "missing"
                self.connection["firefox"] = "registered" if arguments["enabled"] else "missing"
                return self.connection
            if command == "get_desktop_preferences":
                return dict(self.desktop_preferences)
            if command == "set_desktop_preferences":
                patch = arguments.get("preferences") or {}
                for key in ("start_at_login", "close_to_tray", "start_minimized", "onboarding_completed"):
                    if key in patch and patch[key] is not None:
                        self.desktop_preferences[key] = bool(patch[key])
                self.desktop_preferences["autostart_state"] = (
                    "registered" if self.desktop_preferences["start_at_login"] else "missing"
                )
                return dict(self.desktop_preferences)
            if command == "restart_engine":
                self.restarts += 1
                return {"state": "ready", "tray_available": self.desktop_preferences["tray_available"],
                        "supervisor_pid": 1001, "engine_pid": 2001 + self.restarts,
                        "engine_generation": 1 + self.restarts,
                        "browser_connection": self.connection, "application": self.application,
                        "downloads_active": bool(self.request("downloads.list")["counts"]["unfinished"])}
            if command == "pick_download_folder":
                return self.picked_folder
            if command == "pick_url_list_file":
                return self.url_list_pick
            if command == "reveal_download":
                self.revealed.append(arguments.get("jobId"))
                return None
            if command == "open_download":
                self.opened.append(arguments.get("jobId"))
                return None
            if command == "diagnostics_report":
                diag = self.request("diagnostics.get")
                return {
                    "supervisor": {"state": "ready", "tray_available": self.desktop_preferences["tray_available"],
                                   "supervisor_pid": 1001, "engine_pid": 2001 + self.restarts,
                                   "engine_generation": 1 + self.restarts,
                                   "browser_connection": self.connection, "application": self.application},
                    "engine": diag,
                    "desktop_preferences": dict(self.desktop_preferences),
                    "updates": dict(self.update_status),
                    "browser_connection": dict(self.connection),
                    "application": dict(self.application),
                    "engine_log_tail": list(self.engine_log_tail),
                }
            if command == "update_status":
                return dict(self.update_status)
            if command == "check_for_updates":
                if self.next_update_check is not None:
                    self.update_status["last_check"] = dict(self.next_update_check)
                else:
                    self.update_status["last_check"] = {
                        "checked_at": int(self.now),
                        "status": "offline",
                        "available_version": None,
                        "channel": self.update_status["channel"],
                        "notes": None,
                        "message": "Update server unreachable",
                    }
                return dict(self.update_status)
            if command == "apply_update":
                if self.request("downloads.list")["counts"]["unfinished"]:
                    raise RuntimeError("Finish active downloads before applying a VidGrab update")
                target = (
                    (self.update_status.get("pending_update") or {}).get("version")
                    or (self.update_status.get("last_check") or {}).get("available_version")
                    or "0.2.0"
                )
                prev = self.update_status["engine_bundle_version"]
                self.update_status["previous_version"] = prev
                self.update_status["engine_bundle_version"] = target
                self.update_status["rollback_available"] = True
                self.update_status["pending_update"] = None
                self.update_status["last_check"] = {
                    "checked_at": int(self.now),
                    "status": "up_to_date",
                    "available_version": target,
                    "channel": self.update_status["channel"],
                    "notes": None,
                    "message": None,
                }
                self.update_status["backups_count"] = self.update_status.get("backups_count", 0) + 1
                self.restarts += 1
                return dict(self.update_status)
            if command == "rollback_update":
                if self.request("downloads.list")["counts"]["unfinished"]:
                    raise RuntimeError("Finish active downloads before rolling back VidGrab")
                current = self.update_status["engine_bundle_version"]
                prev = self.update_status.get("previous_version") or self.update_status["app_version"]
                self.update_status["engine_bundle_version"] = prev
                self.update_status["rollback_available"] = False
                self.update_status["previous_version"] = ""
                self.update_status["rollback_notice"] = f"Rolled back manually from v{current} to v{prev}."
                self.restarts += 1
                return dict(self.update_status)
            if command == "pick_and_import_legacy_config":
                return dict(self.legacy_config_import) if self.legacy_config_import else None
            if command == "export_diagnostics_file":
                return self.exported_diagnostics_path
            if command.startswith("plugin:"):
                return 1 if command.endswith("|listen") else None
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
