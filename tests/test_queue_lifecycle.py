"""Queue transitions must wait for their execution boundary to finish."""

import asyncio
import threading
from pathlib import Path

import aiosqlite

from tests.support import OfflineTestCase
import unittest
from unittest.mock import patch


class QueueLifecycleTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_errors = []
        self.loop = asyncio.get_running_loop()
        self.previous_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.loop.set_exception_handler(self.previous_handler)
        self.assertEqual(self.loop_errors, [], "Execution exceptions must be retrieved during teardown")

    async def test_repeated_cancellation_keeps_publication_verification_joined_until_terminal_write(self):
        from backend.services.publication import reconcile_publication
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def download(**options):
            output = Path(options["download_dir"]) / "finished.mp3"
            output.write_bytes(b"media")
            return {"filepath": str(output)}

        def gated(runtime, attempt):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return reconcile_publication(runtime, attempt)

        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            with patch("backend.queue_manager.reconcile_publication", side_effect=gated):
                task = asyncio.create_task(app.state.queue._run_task(job_id, "https://fixture.invalid/media", "best",
                                                                    await app.state.settings.get_all(), {}))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "Caller cancellation must wait for the owned verification thread")
                    self.assertEqual((await self.row(app.state.runtime, job_id))["status"], "downloading")
                finally:
                    release.set()
                    await task
            self.assertEqual((await self.row(app.state.runtime, job_id))["status"], "complete")

    async def row(self, runtime, task_id):
        async with aiosqlite.connect(runtime.db_path) as database:
            database.row_factory = aiosqlite.Row
            async with database.execute("SELECT * FROM downloads WHERE id = ?", (task_id,)) as cursor:
                return dict(await cursor.fetchone())

    async def blocking_download(self, *, shutdown=False, repeat=False, interrupt=False):
        runtime = self.runtime()
        started = asyncio.Event()
        cancellation_requested = asyncio.Event()
        release = threading.Event()
        exited = threading.Event()
        observed = {}
        loop = asyncio.get_running_loop()

        def download(**options):
            observed.update(options)
            loop.call_soon_threadsafe(started.set)
            while not release.wait(0.01):
                if options["is_cancelled_cb"]():
                    loop.call_soon_threadsafe(cancellation_requested.set)
            exited.set()
            raise RuntimeError("Fixture stopped")

        app = self.app(runtime, start_queue=True, download_runner=download)
        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue(
                "https://fixture.invalid/media", "best", title="Keep",
                cookies=[{"domain": ".fixture.invalid", "name": "session", "value": "secret"}],
            )
            await asyncio.wait_for(started.wait(), 2)
            cookie = Path(observed["cookies_file_path"])
            action = asyncio.create_task(app.state.queue.stop() if shutdown else app.state.queue.cancel(task_id))
            second_action = None
            try:
                # Give the action time to request stop while the fixture remains gated.
                await asyncio.sleep(0.05)
                self.assertFalse(action.done(), "Terminal cancellation/shutdown must wait for execution exit")
                self.assertTrue(cookie.exists(), "Cookies must remain available while execution is alive")
                self.assertNotIn((await self.row(runtime, task_id))["status"], {"complete", "failed", "cancelled"})
                if repeat:
                    second_action = asyncio.create_task(app.state.queue.stop() if shutdown else app.state.queue.cancel(task_id))
                    await asyncio.sleep(0.02)
                    self.assertFalse(second_action.done(), "Concurrent teardown must join the same execution")
                    self.assertTrue(cookie.exists(), "Repeated teardown must not detach the execution")
                if interrupt:
                    action.cancel()
                    await asyncio.sleep(0.02)
                    action.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(action.done(), "Interrupted teardown must finish joining before returning")
                    self.assertTrue(cookie.exists(), "Interrupted teardown must keep the owned cookie alive")
            finally:
                release.set()
                actions = [action] + ([second_action] if second_action else [])
                outcomes = await asyncio.wait_for(asyncio.gather(*actions, return_exceptions=True), 2)
            if interrupt:
                self.assertIsInstance(outcomes[0], asyncio.CancelledError)
            else:
                self.assertEqual(outcomes, [None] * len(actions))
            self.assertTrue(exited.is_set())
            self.assertFalse(cookie.exists())
            self.assertEqual((await self.row(runtime, task_id))["status"], "queued" if shutdown else "cancelled")

    async def test_cancel_waits_for_execution_before_terminal_state_and_cookie_cleanup(self):
        await self.blocking_download()

    async def test_shutdown_joins_active_execution_and_preserves_a_restartable_job(self):
        await self.blocking_download(shutdown=True)

    async def test_concurrent_shutdowns_join_before_cookie_cleanup(self):
        await self.blocking_download(shutdown=True, repeat=True)

    async def test_concurrent_cancellations_join_before_cookie_cleanup(self):
        await self.blocking_download(repeat=True)

    async def test_interrupted_shutdown_still_joins_execution(self):
        await self.blocking_download(shutdown=True, interrupt=True)

    async def test_interrupted_cancel_still_finishes_cancellation(self):
        await self.blocking_download(interrupt=True)

    async def test_finish_cannot_overwrite_a_persisted_cancellation_request(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            for status in ("complete", "failed", "queued"):
                with self.subTest(status=status):
                    task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                    async with aiosqlite.connect(app.state.runtime.db_path) as database:
                        await database.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (task_id,))
                        await database.commit()
                    app.state.queue.in_memory_progress[task_id]["status"] = "cancelling"
                    await app.state.queue._finish(task_id, status, file_path="stale.mp3")
                    row = await self.row(app.state.runtime, task_id)
                    self.assertEqual(row["status"], "cancelling", "Cancellation must win over stale completion/requeue")
                    self.assertIsNone(row["file_path"])
                    self.assertEqual(app.state.queue.in_memory_progress[task_id]["status"], "cancelling")

    async def test_cancelled_claim_does_not_start_after_a_delayed_broadcast(self):
        started = asyncio.Event()
        release_broadcast = asyncio.Event()
        invoked = []

        def download(**options):
            invoked.append(options["task_id"])
            raise RuntimeError("Cancelled job must not execute")

        app = self.app(start_queue=True, download_runner=download)

        async def broadcast(message):
            if message["event"] == "task_started":
                started.set()
                await release_broadcast.wait()

        with patch.object(app.state.connections, "broadcast", broadcast):
            async with app.router.lifespan_context(app):
                try:
                    task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                    await asyncio.wait_for(started.wait(), 2)
                    await app.state.queue.cancel(task_id)
                    release_broadcast.set()
                    await asyncio.sleep(0.1)
                    self.assertEqual(invoked, [], "A cancelled claim must be rechecked before opening resources")
                    self.assertEqual((await self.row(app.state.runtime, task_id))["status"], "cancelled")
                finally:
                    release_broadcast.set()

    async def test_finish_and_cancel_keep_database_and_memory_consistent(self):
        app = self.app()
        committed = asyncio.Event()
        release_commit = asyncio.Event()
        original_commit = aiosqlite.Connection.commit
        finish = None

        async def pause_finish_commit(database):
            await original_commit(database)
            if asyncio.current_task() is finish:
                committed.set()
                await release_commit.wait()

        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            with patch.object(aiosqlite.Connection, "commit", pause_finish_commit):
                finish = asyncio.create_task(app.state.queue._finish(task_id, "queued"))
                cancel = None
                try:
                    await asyncio.wait_for(committed.wait(), 2)
                    cancel = asyncio.create_task(app.state.queue.cancel(task_id))
                    await asyncio.wait({cancel}, timeout=0.1)
                finally:
                    release_commit.set()
                    await asyncio.gather(finish, *([cancel] if cancel else []))
            self.assertEqual((await self.row(app.state.runtime, task_id))["status"], "cancelled")
            self.assertEqual(app.state.queue.in_memory_progress[task_id]["status"], "cancelled",
                             "A committed retry must not publish stale memory after cancellation")

    async def test_scheduler_cannot_requeue_a_cancelled_scheduled_job(self):
        app = self.app()
        due_read = asyncio.Event()
        release_read = asyncio.Event()
        next_cycle = asyncio.Event()
        original_fetchall = aiosqlite.Cursor.fetchall
        original_sleep = asyncio.sleep
        cycles = 0

        async def scheduler_sleep(seconds):
            nonlocal cycles
            if seconds == 30:
                cycles += 1
                if cycles > 1:
                    next_cycle.set()
                    await asyncio.Event().wait()
                return
            await original_sleep(seconds)

        async def pause_due_read(cursor):
            rows = await original_fetchall(cursor)
            if rows and cursor.description[0][0] == "id":
                due_read.set()
                await release_read.wait()
            return rows

        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best",
                                                   scheduled_at="2000-01-01T00:00:00+00:00")
            with patch("asyncio.sleep", scheduler_sleep), patch.object(aiosqlite.Cursor, "fetchall", pause_due_read):
                scheduler = asyncio.create_task(app.state.queue._scheduler_loop())
                try:
                    await asyncio.wait_for(due_read.wait(), 2)
                    await app.state.queue.cancel(task_id)
                    release_read.set()
                    await asyncio.wait_for(next_cycle.wait(), 2)
                    self.assertEqual((await self.row(app.state.runtime, task_id))["status"], "cancelled")
                    self.assertEqual(app.state.queue.in_memory_progress[task_id]["status"], "cancelled")
                finally:
                    release_read.set()
                    scheduler.cancel()
                    await asyncio.gather(scheduler, return_exceptions=True)

    async def test_late_progress_cannot_overwrite_terminal_memory_state(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            await app.state.queue.cancel(task_id)
            app.state.queue.handle_progress(task_id, {"status": "downloading", "progress": 75})
            self.assertEqual(app.state.queue.in_memory_progress[task_id]["status"], "cancelled")

    async def test_cancel_does_not_rewrite_completed_history(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            async with aiosqlite.connect(app.state.runtime.db_path) as database:
                await database.execute("UPDATE downloads SET status='complete', completed_at='original' WHERE id=?", (task_id,))
                await database.commit()
            await app.state.queue.cancel(task_id)
            row = await self.row(app.state.runtime, task_id)
            self.assertEqual((row["status"], row["completed_at"]), ("complete", "original"))

    async def test_failed_job_preserves_other_files_with_the_same_title(self):
        def fail(**options):
            raise RuntimeError("Fixture permanent failure")

        app = self.app(download_runner=fail)
        async with app.router.lifespan_context(app):
            task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best", title="Keep")
            unrelated = app.state.runtime.download_dir / "Keep another job.part"
            unrelated.write_bytes(b"unrelated partial download")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                await app.state.queue._run_task(task_id, "https://fixture.invalid/media", "best", config, {})
            self.assertTrue(unrelated.exists(), "A title prefix does not establish artifact ownership")
            self.assertEqual(unrelated.read_bytes(), b"unrelated partial download")
