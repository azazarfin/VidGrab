"""P06 manager unit and contract tests: schema 11, priority, UTC scheduling, pause/resume, history delete, Bangla gate, diagnostics."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.config import init_db
from backend.db.migrations import SCHEMA_VERSION
from backend.services.artifacts import prepare_workspace
from backend.services.control_errors import ControlError
from backend.services.playlists import validate_playlist_result
from backend.services import resume as resume_service
from tests.test_migrations import legacy_database
from tests.support import OfflineTestCase


def _v1(client, operation, params=None, key=None):
    message = {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "operation": operation,
        "params": params or {},
    }
    if key is not None:
        message["idempotency_key"] = key
    return client.post("/v1/requests", json=message)


class P06SchemaAndValidationTests(OfflineTestCase):
    def test_schema_eleven_migration_from_legacy_and_rollback_safety(self):
        runtime = self.runtime("migrate-v11")
        legacy_database(runtime.db_path, version=0, config={"max_concurrent": 4})
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute(
                """CREATE TRIGGER interrupt_v11 BEFORE UPDATE ON downloads
                   WHEN NEW.status='paused' BEGIN
                   SELECT RAISE(ABORT, 'simulated v11 interruption'); END"""
            )
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            cols = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            self.assertIn("priority", cols)
            indexes = {row[1] for row in db.execute("PRAGMA index_list(downloads)")}
            self.assertIn("runnable_priority_downloads", indexes)
            self.assertIn("scheduled_deadlines", indexes)
            trig = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='protocol_download_update'"
            ).fetchone()[0]
            self.assertIn("priority", trig)
            db.execute("DROP TRIGGER interrupt_v11")

        # Validate rollback safety if schema 11 migration fails mid-way
        runtime2 = self.runtime("rollback-v11")
        asyncio.run(init_db(runtime2))
        with closing(sqlite3.connect(runtime2.db_path)) as db, db:
            db.execute("DROP TRIGGER protocol_download_update")
            db.execute("DROP INDEX scheduled_deadlines")
            db.execute("DROP INDEX runnable_priority_downloads")
            db.execute("ALTER TABLE downloads DROP COLUMN priority")
            db.execute("PRAGMA user_version = 10")
            db.execute(
                """CREATE TRIGGER fail_v11 AFTER INSERT ON protocol_events BEGIN
                   SELECT RAISE(ABORT, 'simulated trigger'); END"""
            )
            # Trigger failure on index/column migration via a failing check or simulated trigger
            db.execute(
                """CREATE TABLE _block_p11(x TEXT);"""
            )
        # Re-check _validate_schema detects missing Schema 11 artifacts
        with closing(sqlite3.connect(runtime2.db_path)) as db, db:
            db.execute("PRAGMA user_version = 11")
        from backend.runtime import RuntimeCompatibilityError
        with self.assertRaises(RuntimeCompatibilityError):
            asyncio.run(init_db(runtime2))

    def test_v1_rejects_invalid_priority_and_non_utc_schedule(self):
        from backend.engine import create_engine_app
        runtime = self.runtime("v1-validation")
        engine = create_engine_app(
            runtime, token="a" * 64, port=43210,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            media_mode="ordinary", start_queue=False,
        )
        with TestClient(engine, base_url="http://127.0.0.1:43210", headers={"Authorization": "Bearer " + "a" * 64}) as client:
            for bad_priority in (-11, 11, True, "5", 1.5):
                resp = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/a.mp4", "priority": bad_priority}, key=str(uuid4()))
                self.assertEqual(resp.status_code, 422, resp.text)
                self.assertEqual(resp.json()["error"]["code"], "BAD_REQUEST")

            for bad_ts in ("2026-12-01T12:00:00", "2026-12-01 12:00:00Z", "not-a-timestamp"):
                resp = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/b.mp4", "scheduled_at": bad_ts}, key=str(uuid4()))
                self.assertEqual(resp.status_code, 422, resp.text)
                self.assertEqual(resp.json()["error"]["code"], "BAD_REQUEST")

            # Valid negative and positive priority + offset UTC conversion
            ok_resp = _v1(
                client,
                "downloads.enqueue",
                {
                    "url": "https://fixture.invalid/c.mp4",
                    "priority": 7,
                    "scheduled_at": "2027-01-01T10:00:00+06:00",
                },
                key="valid-priority-sched",
            )
            self.assertEqual(ok_resp.status_code, 200, ok_resp.text)
            job_id = ok_resp.json()["data"]["job_id"]
            got = _v1(client, "downloads.get", {"job_id": job_id})
            self.assertEqual(got.status_code, 200, got.text)
            job = got.json()["data"]
            self.assertEqual(job["priority"], 7)
            self.assertEqual(job["status"], "scheduled")
            self.assertEqual(job["scheduled_at"], "2027-01-01T04:00:00+00:00")
            self.assertEqual(job["job_kind"], "media")
            self.assertIn(job["resume_mode"], ("verified_partial", "restart_or_unverified", "not_pausable"))

            # set_priority bumps revision and emits protocol event
            cursor_before = _v1(client, "events.read").json()["data"]["cursor"]
            prio_resp = _v1(
                client,
                "downloads.action",
                {"job_id": job_id, "expected_revision": job["revision"], "action": "set_priority", "priority": -5},
                key="set-prio-1",
            )
            self.assertEqual(prio_resp.status_code, 200, prio_resp.text)
            self.assertEqual(prio_resp.json()["data"]["accepted_revision"], job["revision"] + 1)
            updated = _v1(client, "downloads.get", {"job_id": job_id}).json()["data"]
            self.assertEqual(updated["priority"], -5)
            events = _v1(client, "events.read", {"instance_id": engine.app.state.owner.id, "cursor": cursor_before}).json()["data"]["events"]
            self.assertTrue(any(e["entity_id"] == job_id and e["revision"] == job["revision"] + 1 for e in events))

    def test_diagnostics_get_checks_components_storage_and_redacts_secrets(self):
        from backend.engine import create_engine_app
        runtime = self.runtime("diag-check")
        ffmpeg_bin = self.root / "ffmpeg"
        ffmpeg_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        token = "b" * 64
        engine = create_engine_app(
            runtime, token=token, port=43210,
            components={"ffmpeg": str(ffmpeg_bin), "ffprobe": str(self.root / "missing-ffprobe"), "deno": str(self.root / "missing-deno")},
            component_versions={"ffmpeg": "7.0", "yt_dlp": "2026.6.9"},
            media_mode="ordinary", start_queue=False,
        )
        with TestClient(engine, base_url="http://127.0.0.1:43210", headers={"Authorization": "Bearer " + token}) as client:
            resp = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/secret?token=secret123"}, key="diag-job")
            job_id = resp.json()["data"]["job_id"]
            with closing(sqlite3.connect(runtime.db_path)) as db, db:
                db.execute(
                    "UPDATE downloads SET status='failed', error_code='HTTP_ERROR', error_message=? WHERE id=?",
                    (f"Authorization: Bearer {token} failed at https://fixture.invalid/watch?token=abc", job_id),
                )
            diag = _v1(client, "diagnostics.get")
            self.assertEqual(diag.status_code, 200, diag.text)
            data = diag.json()["data"]
            self.assertEqual(data["schema_version"], SCHEMA_VERSION)
            self.assertTrue(data["download_dir_writable"])
            self.assertIsInstance(data["disk_free_bytes"], int)
            checks = {c["name"]: c for c in data["component_checks"]}
            self.assertTrue(checks["ffmpeg"]["available"])
            self.assertFalse(checks["ffprobe"]["available"])
            self.assertFalse(checks["deno"]["available"])
            self.assertTrue(checks["yt_dlp"]["available"])
            serialized = json.dumps(data)
            self.assertNotIn(token, serialized)
            self.assertNotIn("secret123", serialized)
            self.assertTrue(data["recent_errors"])

    def test_bangla_transliteration_setting_gate(self):
        from backend.engine import create_engine_app
        runtime = self.runtime("bangla-gate")
        engine = create_engine_app(runtime, token="a" * 64, port=43210, components={}, media_mode="ordinary", start_queue=False)
        bangla_title = "বাংলা গান ২০২৬"
        with TestClient(engine, base_url="http://127.0.0.1:43210", headers={"Authorization": "Bearer " + "a" * 64}) as client:
            # Default is True -> transliterated
            settings = _v1(client, "settings.get").json()["data"]
            self.assertIs(settings["values"]["transliterate_bangla_filenames"], True)
            j1 = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/b1.mp4", "title": bangla_title}, key="b1").json()["data"]["job_id"]
            got1 = _v1(client, "downloads.get", {"job_id": j1}).json()["data"]
            self.assertNotEqual(got1["title"], bangla_title)
            self.assertRegex(got1["title"], r"^[A-Za-z0-9_ .-]+$")

            # Patch to False -> native Unicode preserved
            patched = _v1(
                client,
                "settings.patch",
                {"expected_revision": settings["revision"], "patch": {"transliterate_bangla_filenames": False}},
                key="bangla-off",
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            j2 = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/b2.mp4", "title": bangla_title}, key="b2").json()["data"]["job_id"]
            got2 = _v1(client, "downloads.get", {"job_id": j2}).json()["data"]
            self.assertEqual(got2["title"], bangla_title)

        # Verify playlists validator respects transliterate flag
        raw = {"playlist": {"entries": [{"position": 1, "url": "https://fixture.invalid/v1", "title": bangla_title, "duration": 5}]}}
        self.assertEqual(validate_playlist_result(raw, transliterate=False)["playlist"]["entries"][0]["title"], bangla_title)
        self.assertNotEqual(validate_playlist_result(raw, transliterate=True)["playlist"]["entries"][0]["title"], bangla_title)

        # Verify downloader honors transliterate_bangla_filenames=False while keeping OutputPathError safety
        from backend.downloader import run_download
        media_dir = runtime.download_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        out_path = media_dir / f"{bangla_title}.mp4"
        out_path.write_bytes(b"ok")

        class FakeYDL:
            last_opts = None
            def __init__(self, opts):
                FakeYDL.last_opts = dict(opts)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def add_post_processor(self, *_a, **_kw):
                pass
            def extract_info(self, url, download=True):
                return {"title": bangla_title, "ext": "mp4", "filepath": str(out_path), "_filename": str(out_path)}
            def prepare_filename(self, info):
                return str(out_path)

        import backend.downloader as dl_mod
        orig_ydl = dl_mod.yt_dlp.YoutubeDL
        dl_mod.yt_dlp.YoutubeDL = FakeYDL
        try:
            res = run_download(
                "bangla-task",
                "https://fixture.invalid/v.mp4",
                "best",
                str(media_dir),
                "%(title)s.%(ext)s",
                lambda *_a, **_kw: None,
                runtime,
                transliterate_bangla_filenames=False,
            )
            self.assertFalse(FakeYDL.last_opts["restrictfilenames"])
            self.assertEqual(res["title"], bangla_title)
            from backend.runtime import OutputPathError
            with self.assertRaises(OutputPathError):
                run_download(
                    "unsafe-task",
                    "https://fixture.invalid/v.mp4",
                    "best",
                    str(media_dir),
                    f"../{runtime.data_dir.name}/%(title)s.%(ext)s",
                    lambda *_a, **_kw: None,
                    runtime,
                    transliterate_bangla_filenames=False,
                )
        finally:
            dl_mod.yt_dlp.YoutubeDL = orig_ydl


class P06QueueLifecycleTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_priority_worker_ordering_and_scheduled_promotion(self):
        started_urls = []
        gate = threading.Event()
        now = [1000.0]

        def runner(url, *args, **kwargs):
            started_urls.append(url)
            gate.wait(timeout=5)
            out_dir = Path(args[1]) if len(args) > 1 else Path(kwargs["download_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{len(started_urls)}.mp4"
            out_file.write_bytes(b"media")
            return {"filepath": str(out_file), "title": url, "filesize": 5}

        app = self.app(download_runner=runner, clock=lambda: now[0], start_queue=False)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        self.addAsyncCleanup(ctx.__aexit__, None, None, None)
        queue = app.state.queue
        await app.state.settings.patch({"max_concurrent": 1})

        # Enqueue low first, then high, then normal (at same/later timestamps)
        low_id = await queue.enqueue("https://fixture.invalid/low.mp4", "best", priority=-5)
        high_id = await queue.enqueue("https://fixture.invalid/high.mp4", "best", priority=10)
        norm_id = await queue.enqueue("https://fixture.invalid/norm.mp4", "best", priority=0)
        sched_iso = datetime.fromtimestamp(1500.0, timezone.utc).isoformat()
        sched_id = await queue.enqueue("https://fixture.invalid/sched.mp4", "best", priority=10, scheduled_at=sched_iso)

        # Scheduled job stays scheduled until deadline passes
        self.assertEqual(await queue.attempts.promote_due_scheduled(datetime.fromtimestamp(1499.0, timezone.utc).isoformat()), [])

        gate.set()
        queue.start()
        for _ in range(100):
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                done = db.execute("SELECT COUNT(*) FROM downloads WHERE id IN (?,?,?) AND status='complete'", (low_id, high_id, norm_id)).fetchone()[0]
            if done == 3:
                break
            await asyncio.sleep(0.05)

        self.assertEqual(started_urls, [
            "https://fixture.invalid/high.mp4",
            "https://fixture.invalid/norm.mp4",
            "https://fixture.invalid/low.mp4",
        ])

        # Verify scheduled job did not run prematurely and promotes once clock passes 1500
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (sched_id,)).fetchone()[0], "scheduled")
        now[0] = 1500.0
        queue.wakeup_event.set()
        for _ in range(100):
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                if db.execute("SELECT status FROM downloads WHERE id=?", (sched_id,)).fetchone()[0] == "complete":
                    break
            await asyncio.sleep(0.05)
        self.assertEqual(started_urls[-1], "https://fixture.invalid/sched.mp4")

    async def test_pause_resume_lifecycle_and_partial_preservation(self):
        entered = threading.Event()
        release = threading.Event()
        now = [1000.0]

        def blocking_runner(url, *args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            raise asyncio.CancelledError()

        app = self.app(download_runner=blocking_runner, clock=lambda: now[0], start_queue=False)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        self.addAsyncCleanup(ctx.__aexit__, None, None, None)
        queue = app.state.queue

        # 1. Pause queued and scheduled (preserves scheduled_at)
        q_id = await queue.enqueue("https://fixture.invalid/q.mp4", "best")
        future_iso = datetime.fromtimestamp(2000.0, timezone.utc).isoformat()
        s_id = await queue.enqueue("https://fixture.invalid/s.mp4", "best", scheduled_at=future_iso)
        await queue.pause(q_id)
        await queue.pause(s_id)
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (q_id,)).fetchone()[0], "paused")
            self.assertEqual(db.execute("SELECT status, scheduled_at FROM downloads WHERE id=?", (s_id,)).fetchone(), ("paused", future_iso))

        # Resume scheduled-paused restores scheduled when deadline still in future
        await queue.resume(s_id)
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status, scheduled_at FROM downloads WHERE id=?", (s_id,)).fetchone(), ("scheduled", future_iso))

        # 2. Active downloading job -> pausing -> paused with attempt_partials preserved
        await queue.resume(q_id)
        queue.start()
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            row = db.execute("SELECT current_attempt_id FROM downloads WHERE id=?", (q_id,)).fetchone()
            attempt_id = row[0]
        # Seed a verified partial witness on the active attempt (workspace already prepared by _run_task)
        attempt_obj = queue.active_attempts[q_id]
        attempt_obj.workspace.mkdir(parents=True, exist_ok=True)
        part_file = attempt_obj.workspace / "q.part"
        part_file.write_bytes(b"partial-bytes")
        source = resume_service.source_validator("https://fixture.invalid/q.mp4", "media", '"etag1"', 1024)
        await asyncio.to_thread(
            resume_service.checkpoint,
            app.state.runtime,
            attempt_obj,
            part_file,
            source,
            13,
            hashlib.sha256(b"partial-bytes").hexdigest(),
        )

        pause_task = asyncio.create_task(queue.pause(q_id))
        await asyncio.sleep(0.05)
        release.set()
        await pause_task
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (q_id,)).fetchone()[0], "paused")
            self.assertEqual(db.execute("SELECT status FROM download_attempts WHERE id=?", (attempt_id,)).fetchone()[0], "interrupted")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_partials WHERE attempt_id=?", (attempt_id,)).fetchone()[0], 1)

        # 3. Refuse pause during postprocessing or waiting_for_children with STATE_CONFLICT
        pp_id = await queue.enqueue("https://fixture.invalid/pp.mp4", "best")
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='postprocessing' WHERE id=?", (pp_id,))
        with self.assertRaises(ControlError) as err_ctx:
            await queue.pause(pp_id)
        self.assertEqual(err_ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(err_ctx.exception.status, 409)

        # 4. Resume auth_required job when volatile context absent -> waiting_for_auth
        auth_id = await queue.enqueue("https://fixture.invalid/auth.mp4", "best", requires_auth=True)
        await queue.pause(auth_id)
        queue.task_cookies.pop(auth_id, None)
        queue.task_headers.pop(auth_id, None)
        await queue.resume(auth_id)
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            status, code = db.execute("SELECT status, error_code FROM downloads WHERE id=?", (auth_id,)).fetchone()
            self.assertEqual(status, "waiting_for_auth")
            self.assertIn(code, ("AUTH_REQUIRED", "BROWSER_CONTEXT_REQUIRED"))

        # 5. AttemptStore.recover preserves paused and scheduled, settles pausing -> paused
        await queue.stop()
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='pausing' WHERE id=?", (q_id,))
        await queue.attempts.recover(queue._now())
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (q_id,)).fetchone()[0], "paused")
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (s_id,)).fetchone()[0], "scheduled")

    async def test_pause_queued_playlist_child_syncs_parent(self):
        now = [1000.0]
        app = self.app(clock=lambda: now[0], start_queue=False)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        self.addAsyncCleanup(ctx.__aexit__, None, None, None)
        queue = app.state.queue

        parent_id = await queue.enqueue("https://fixture.invalid/playlist", "best", is_playlist=True)
        child1 = str(uuid4())
        child2 = str(uuid4())
        now_iso = datetime.fromtimestamp(now[0], timezone.utc).isoformat()
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='complete', progress=100.0, job_kind='playlist' WHERE id=?", (parent_id,))
            db.execute(
                """INSERT INTO downloads(id,url,title,status,progress,speed,created_at,format_selected,download_options,
                   auth_required,job_kind,parent_id,playlist_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (child1, "https://fixture.invalid/item-1.mp4", "Item 1", "queued", 0.0, 0.0, now_iso, "best", "{}", 0, "media", parent_id, 1),
            )
            db.execute(
                """INSERT INTO downloads(id,url,title,status,progress,speed,created_at,format_selected,download_options,
                   auth_required,job_kind,parent_id,playlist_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (child2, "https://fixture.invalid/item-2.mp4", "Item 2", "complete", 100.0, 0.0, now_iso, "best", "{}", 0, "media", parent_id, 2),
            )
        await queue.pause(child1)
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (child1,)).fetchone()[0], "paused")
            parent_status, parent_progress = db.execute("SELECT status, progress FROM downloads WHERE id=?", (parent_id,)).fetchone()
            self.assertEqual(parent_status, "waiting_for_children")
            self.assertEqual(parent_progress, 50.0)

    async def test_delete_history_and_clear_history_preserve_files_and_active_rows(self):
        from backend.engine import create_engine_app
        runtime = self.runtime("history-delete")
        engine = create_engine_app(runtime, token="a" * 64, port=43210, components={}, media_mode="ordinary", start_queue=False)
        with TestClient(engine, base_url="http://127.0.0.1:43210", headers={"Authorization": "Bearer " + "a" * 64}) as client:
            runtime.download_dir.mkdir(parents=True, exist_ok=True)
            kept_media = runtime.download_dir / "saved-video.mp4"
            kept_media.write_bytes(b"video-content")
            j_done = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/done.mp4"}, key="j-done").json()["data"]["job_id"]
            j_fail = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/fail.mp4"}, key="j-fail").json()["data"]["job_id"]
            j_active = _v1(client, "downloads.enqueue", {"url": "https://fixture.invalid/active.mp4"}, key="j-active").json()["data"]["job_id"]
            with closing(sqlite3.connect(runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='complete', file_path=?, completed_at='2026-11-20T00:00:00+00:00' WHERE id=?", (str(kept_media), j_done))
                db.execute("UPDATE downloads SET status='failed', completed_at='2026-11-20T00:00:00+00:00' WHERE id=?", (j_fail,))

            # Cannot delete_history on active job
            rev_active = _v1(client, "downloads.get", {"job_id": j_active}).json()["data"]["revision"]
            conflict = _v1(client, "downloads.action", {"job_id": j_active, "expected_revision": rev_active, "action": "delete_history"}, key="del-active")
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["error"]["code"], "STATE_CONFLICT")

            # Delete single history entry -> metadata gone, media file untouched
            rev_done = _v1(client, "downloads.get", {"job_id": j_done}).json()["data"]["revision"]
            del_one = _v1(client, "downloads.action", {"job_id": j_done, "expected_revision": rev_done, "action": "delete_history"}, key="del-one")
            self.assertEqual(del_one.status_code, 200, del_one.text)
            self.assertEqual(del_one.json()["data"]["accepted_status"], "deleted")
            self.assertTrue(kept_media.is_file())
            self.assertEqual(_v1(client, "downloads.get", {"job_id": j_done}).status_code, 404)

            # Clear remaining history with idempotent replay
            clear1 = _v1(client, "downloads.clear_history", {}, key="clear-all-1")
            self.assertEqual(clear1.status_code, 200, clear1.text)
            self.assertEqual(clear1.json()["data"], {"deleted_count": 1, "retained_count": 0})
            clear2 = _v1(client, "downloads.clear_history", {}, key="clear-all-1")
            self.assertEqual(clear2.json()["data"], {"deleted_count": 1, "retained_count": 0})
            self.assertEqual(_v1(client, "downloads.get", {"job_id": j_active}).status_code, 200)
