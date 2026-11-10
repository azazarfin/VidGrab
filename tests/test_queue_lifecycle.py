"""Queue transitions must wait for their execution boundary to finish."""

import asyncio
import threading
from pathlib import Path

import aiosqlite

from tests.support import OfflineTestCase
import unittest


class QueueLifecycleTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def row(self, runtime, task_id):
        async with aiosqlite.connect(runtime.db_path) as database:
            database.row_factory = aiosqlite.Row
            async with database.execute("SELECT * FROM downloads WHERE id = ?", (task_id,)) as cursor:
                return dict(await cursor.fetchone())

    async def blocking_download(self, *, shutdown=False):
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
            try:
                # Give the action time to request stop while the fixture remains gated.
                await asyncio.sleep(0.05)
                self.assertFalse(action.done(), "Terminal cancellation/shutdown must wait for execution exit")
                self.assertTrue(cookie.exists(), "Cookies must remain available while execution is alive")
                self.assertNotIn((await self.row(runtime, task_id))["status"], {"complete", "failed", "cancelled"})
            finally:
                release.set()
                await asyncio.wait_for(action, 2)
            self.assertTrue(exited.is_set())
            self.assertFalse(cookie.exists())
            self.assertEqual((await self.row(runtime, task_id))["status"], "queued" if shutdown else "cancelled")

    async def test_cancel_waits_for_execution_before_terminal_state_and_cookie_cleanup(self):
        await self.blocking_download()

    async def test_shutdown_joins_active_execution_and_preserves_a_restartable_job(self):
        await self.blocking_download(shutdown=True)

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
