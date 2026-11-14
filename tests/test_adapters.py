import sqlite3
import json
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tests.support import OfflineTestCase


class AdapterTests(OfflineTestCase):
    def test_resumed_unsafe_output_does_not_run_or_clean_application_storage(self):
        runtime = self.runtime()
        invoked = []

        def download(**options):
            invoked.append(options)
            return {"filepath": str(runtime.download_dir / "missing.mp4"), "title": "Keep"}

        app = self.app(runtime, download_runner=download)
        with TestClient(app) as client:
            self.assertEqual(client.patch("/config", json={"max_retries": 0}).status_code, 200)
            response = client.post("/downloads", json={
                "url": "https://fixture.invalid/media", "format_id": "best", "title": "Keep",
            })
            task_id = response.json()["task_id"]
            sentinel = runtime.data_dir / "Keep.part"
            sentinel.write_bytes(b"unrelated application data")
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                client.portal.call(
                    app.state.queue._run_task, task_id, "https://fixture.invalid/media", "best",
                    client.get("/config").json(), {"download_dir": str(runtime.data_dir)}, 0,
                )
            self.assertEqual(invoked, [])
            self.assertEqual(sentinel.read_bytes(), b"unrelated application data")
            self.assertEqual(client.get("/downloads").json()[0]["status"], "failed")

    def test_probe_cache_and_clock_are_owned_by_each_app(self):
        calls = []
        clock = [1000.0]

        def extract(url, runtime):
            calls.append((url, runtime.data_dir))
            return {"title": "Fixture", "formats": [{"format_id": "best", "ext": "mp4"}]}

        one = self.app(self.runtime("one"), probe_runner=extract, clock=lambda: clock[0])
        two = self.app(self.runtime("two"), probe_runner=extract, clock=lambda: clock[0])
        with TestClient(one) as first, TestClient(two) as second:
            payload = {"url": "https://fixture.invalid/media"}
            for client in (first, first, second):
                self.assertEqual(client.post("/probe", json=payload).status_code, 200)
            self.assertEqual(len(calls), 2)
            clock[0] += 301
            self.assertEqual(first.post("/probe", json=payload).status_code, 200)
            self.assertEqual(len(calls), 3)

    def test_enqueue_uses_the_injected_clock(self):
        now = 1793664000.0
        with TestClient(self.app(clock=lambda: now)) as client:
            response = client.post("/downloads", json={"url": "https://fixture.invalid/media", "format_id": "best"})
            self.assertEqual(response.status_code, 200)
            created_at = client.get("/downloads").json()[0]["created_at"]
            self.assertEqual(created_at, datetime.fromtimestamp(now, timezone.utc).isoformat())

    def test_open_folder_uses_an_injected_process_adapter(self):
        opened = []
        runtime = self.runtime()
        with TestClient(self.app(runtime, open_folder=opened.append)) as client:
            response = client.post("/downloads", json={"url": "https://fixture.invalid/media", "format_id": "best"})
            task_id = response.json()["task_id"]
            media = runtime.download_dir / "fixture.mp4"
            media.write_bytes(b"fixture")
            with closing(sqlite3.connect(runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='complete', file_path=? WHERE id=?", (str(media), task_id))
            self.assertEqual(client.post(f"/downloads/{task_id}/open-folder").status_code, 200)
            self.assertEqual(opened, [str(runtime.download_dir)])

    def test_fixture_download_uses_runtime_and_cleans_volatile_cookies(self):
        runtime = self.runtime("queue বাংলা")
        observed = []

        def download(**options):
            self.assertIs(options["runtime"], runtime)
            cookie_file = options["cookies_file_path"]
            self.assertTrue(Path(cookie_file).is_relative_to(Path(options["attempt_dir"]) / ".vidgrab"))
            observed.append(cookie_file)
            media = runtime.download_dir / "fixture.mp4"
            media.write_bytes(b"fixture")
            return {"filepath": str(media), "title": "Fixture"}

        app = self.app(runtime, start_queue=True, download_runner=download)
        with TestClient(app) as client, client.websocket_connect("/ws/progress") as websocket:
            self.assertEqual(websocket.receive_json()["event"], "init_state")
            response = client.post("/downloads", json={
                "url": "https://fixture.invalid/media", "format_id": "best",
                "cookies": [{"domain": ".fixture.invalid", "name": "session", "value": "fixture-secret"}],
            })
            self.assertEqual(response.status_code, 200)
            events = [websocket.receive_json()["event"] for _ in range(3)]
            self.assertEqual(events, ["task_queued", "task_started", "task_complete"])
            job = client.get("/downloads").json()[0]
            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["file_path"], str(runtime.download_dir / "fixture.mp4"))
            self.assertTrue(Path(job["file_path"]).is_file())
            self.assertNotIn("cookies", json.loads(job["download_options"]))
            self.assertNotIn("fixture-secret", job["download_options"])
            self.assertFalse(list(runtime.temp_dir.glob("vidgrab_cookies_*")))
        self.assertEqual(len(observed), 1)
        self.assertEqual(app.state.connections.active_connections, [])
