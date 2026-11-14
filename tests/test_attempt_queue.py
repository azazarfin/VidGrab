"""Queue execution must use durable attempt identities and literal artifacts."""

import asyncio
import inspect
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import OfflineTestCase


class AttemptQueueTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    def rows(self, runtime, sql):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            return db.execute(sql).fetchall()

    async def test_execution_claim_is_durable_before_runner_starts(self):
        started = asyncio.Event()
        completed = asyncio.Event()
        release = threading.Event()
        observed = {}
        loop = asyncio.get_running_loop()

        def download(**options):
            observed.update(options)
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
            output = Path(options["download_dir"]) / "finished বাংলা.converted"
            subtitle = output.with_suffix(".en.srt")
            output.write_bytes(b"media")
            subtitle.write_bytes(b"subtitle")
            return {"filepath": str(output), "artifacts": [{"path": str(output), "kind": "media"},
                                                            {"path": str(subtitle), "kind": "subtitle"}]}

        app = self.app(start_queue=True, download_runner=download)

        async def broadcast(message):
            if message["event"] == "task_complete":
                completed.set()

        with patch.object(app.state.connections, "broadcast", broadcast):
            async with app.router.lifespan_context(app):
                try:
                    await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                    await asyncio.wait_for(started.wait(), 2)
                    attempts = self.rows(app.state.runtime, "SELECT id, status, workspace FROM download_attempts")
                    self.assertEqual(len(attempts), 1, "A worker must have a committed attempt before it starts")
                    attempt_id, status, workspace = attempts[0]
                    self.assertEqual(status, "running")
                    self.assertEqual(observed.get("attempt_dir"), workspace)
                    self.assertEqual(Path(workspace).name, attempt_id)
                    self.assertTrue(Path(workspace).is_dir())
                finally:
                    release.set()
                await asyncio.wait_for(completed.wait(), 2)
                self.assertEqual(self.rows(app.state.runtime, "SELECT status, current_attempt_id FROM downloads"),
                                 [("complete", None)])
                self.assertEqual(self.rows(app.state.runtime, "SELECT kind, state, size FROM download_artifacts ORDER BY kind"),
                                 [("media", "complete", 5), ("subtitle", "complete", 8)])

    async def test_missing_reported_output_does_not_complete_from_a_neighbor_extension(self):
        def download(**options):
            output = Path(options["download_dir"]) / "fixture.mp3"
            output.write_bytes(b"another file")
            return {"filepath": str(output.with_suffix(".wav"))}

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best", config, {})
            self.assertEqual(self.rows(app.state.runtime, "SELECT status, file_path FROM downloads"), [("failed", None)])
            self.assertEqual(self.rows(app.state.runtime, "SELECT status FROM download_attempts"), [("failed",)])
            self.assertEqual(self.rows(app.state.runtime, "SELECT COUNT(*) FROM download_artifacts"), [(0,)])

    async def test_reported_application_storage_is_refused_without_cleanup(self):
        app = None

        def download(**options):
            return {"filepath": str(app.state.runtime.data_dir / "unrelated.mp3")}

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            sentinel = app.state.runtime.data_dir / "unrelated.mp3"
            sentinel.write_bytes(b"protected")
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best", config, {})
            self.assertEqual(self.rows(app.state.runtime, "SELECT status FROM downloads"), [("failed",)])
            self.assertEqual(sentinel.read_bytes(), b"protected")
            self.assertEqual(self.rows(app.state.runtime, "SELECT COUNT(*) FROM download_artifacts"), [(0,)])

    async def test_failed_attempt_records_only_its_retained_workspace_files(self):
        observed = {}

        def download(**options):
            observed.update(options)
            if options.get("attempt_dir"):
                (Path(options["attempt_dir"]) / "fixture.part").write_bytes(b"partial")
            raise RuntimeError("Fixture interrupted")

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            unrelated = app.state.runtime.download_dir / "fixture other job.part"
            unrelated.write_bytes(b"other partial")
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best", config, {})
            self.assertIn("attempt_dir", observed, "Each attempt needs an isolated workspace")
            partial = Path(observed["attempt_dir"]) / "fixture.part"
            self.assertEqual(self.rows(app.state.runtime, "SELECT path, state, size FROM download_artifacts"),
                             [(str(partial), "partial", 7)])
            self.assertEqual(partial.read_bytes(), b"partial")
            self.assertEqual(unrelated.read_bytes(), b"other partial")

    async def test_invalid_journal_fails_and_closes_the_attempt_without_deleting_outputs(self):
        observed = {}

        def download(**options):
            output = Path(options["download_dir"]) / "finished.mp3"
            output.write_bytes(b"media")
            metadata = Path(options["attempt_dir"]) / ".vidgrab"
            metadata.mkdir()
            journal = metadata / "artifacts.jsonl"
            journal.write_bytes(b'{"malformed":}\n')
            observed.update(output=output, journal=journal)
            return {"filepath": str(output)}

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best", config, {})
            self.assertEqual(self.rows(app.state.runtime, "SELECT status, current_attempt_id FROM downloads"),
                             [("failed", None)])
            self.assertEqual(self.rows(app.state.runtime, "SELECT status FROM download_attempts"), [("failed",)])
            self.assertEqual(self.rows(app.state.runtime, "SELECT path, state FROM download_artifacts"), [])
            self.assertEqual(observed["output"].read_bytes(), b"media")
            self.assertEqual(observed["journal"].read_bytes(), b'{"malformed":}\n')

    async def test_shutdown_closes_a_claim_blocked_before_execution(self):
        started = asyncio.Event()
        release = asyncio.Event()
        invoked = []
        app = self.app(start_queue=True, download_runner=lambda **options: invoked.append(options))

        async def broadcast(message):
            if message["event"] == "task_started":
                started.set()
                await release.wait()

        with patch.object(app.state.connections, "broadcast", broadcast):
            async with app.router.lifespan_context(app):
                try:
                    await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                    await asyncio.wait_for(started.wait(), 2)
                    await app.state.queue.stop()
                    self.assertEqual(self.rows(app.state.runtime, "SELECT status, current_attempt_id FROM downloads"),
                                     [("queued", None)])
                    self.assertEqual(self.rows(app.state.runtime, "SELECT status FROM download_attempts"), [("interrupted",)])
                    self.assertEqual(invoked, [])
                finally:
                    release.set()

    async def test_stale_progress_cannot_change_a_new_attempt(self):
        from backend.db.jobs import AttemptStore
        app = self.app()
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            store = app.state.queue.attempts
            first = await store.claim(job_id, "2026-11-14T00:00:00+00:00")
            await store.finish(job_id, "queued", "2026-11-14T00:01:00+00:00", attempt_id=first.id)
            second = await store.claim(job_id, "2026-11-14T00:02:00+00:00")
            self.assertIn("attempt_id", inspect.signature(app.state.queue.handle_progress).parameters,
                          "Progress must carry the attempt identity")
            app.state.queue.active_attempts[job_id] = second
            app.state.queue.in_memory_progress[job_id].update(status="downloading", progress=0)
            app.state.queue.handle_progress(job_id, {"status": "postprocessing", "progress": 100}, attempt_id=first.id)
            self.assertEqual(app.state.queue.in_memory_progress[job_id]["progress"], 0)

    async def test_lost_worker_success_reply_completes_from_the_durable_result(self):
        from backend.services.publication import plan_publications, publish_artifact, record_result

        def download(**options):
            workspace = Path(options["attempt_dir"])
            source = workspace / "fixture.mp3"
            source.write_bytes(b"media")
            output = Path(options["download_dir"]) / source.name
            moves = plan_publications(options["runtime"], workspace,
                                      [{"source": str(source), "path": str(output), "kind": "media"}])
            publish_artifact(options["runtime"], workspace, moves[0])
            record_result(options["runtime"], workspace, {"filepath": str(output)})
            raise RuntimeError("The worker success reply was lost")

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            config = await app.state.settings.get_all()
            with self.assertLogs("vidgrab.queue_manager", level="WARNING"):
                await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best", config, {})
            self.assertEqual(self.rows(app.state.runtime, "SELECT status, retry_count, file_path FROM downloads"),
                             [("complete", 0, str(app.state.runtime.download_dir / "fixture.mp3"))])
            self.assertEqual(self.rows(app.state.runtime, "SELECT status FROM download_attempts"), [("complete",)])

    async def test_lost_reply_after_publication_stops_automatic_retry(self):
        from backend.services.publication import plan_publications, publish_artifact

        def download(**options):
            workspace = Path(options["attempt_dir"])
            source = workspace / "fixture.mp3"
            source.write_bytes(b"media")
            output = Path(options["download_dir"]) / source.name
            moves = plan_publications(options["runtime"], workspace,
                                      [{"source": str(source), "path": str(output), "kind": "media"}])
            publish_artifact(options["runtime"], workspace, moves[0])
            raise RuntimeError("The worker exited after publication")

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            with self.assertLogs("vidgrab.queue_manager", level="WARNING"):
                await app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best",
                                                await app.state.settings.get_all(), {})
            self.assertEqual(self.rows(app.state.runtime, "SELECT status, retry_count FROM downloads"), [("failed", 0)])
            self.assertEqual(self.rows(app.state.runtime, "SELECT kind, state FROM download_artifacts WHERE kind='media'"),
                             [("media", "complete")])


class AttemptHistoryTests(OfflineTestCase):
    def test_history_deletion_cascades_attempt_metadata_and_preserves_media(self):
        def download(**options):
            output = Path(options["download_dir"]) / "fixture.mp3"
            output.write_bytes(b"media")
            return {"filepath": str(output)}

        app = self.app(download_runner=download)
        with TestClient(app) as client:
            job_id = client.post("/downloads", json={"url": "https://fixture.invalid/media", "format_id": "best"}).json()["task_id"]
            client.portal.call(app.state.queue._run_task, job_id, "https://fixture.invalid/media", "best",
                               client.get("/config").json(), {})
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0], 1)
            self.assertEqual(client.delete(f"/downloads/history/{job_id}").status_code, 200)
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM download_artifacts").fetchone()[0], 0)
            self.assertEqual((app.state.runtime.download_dir / "fixture.mp3").read_bytes(), b"media")
