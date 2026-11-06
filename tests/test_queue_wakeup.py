import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from tests.support import OfflineTestCase


class QueueWakeupTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_during_idle_transition_runs_without_another_wakeup(self):
        runtime = self.runtime()
        observed = []
        events = []
        idle_read = asyncio.Event()
        continue_worker = asyncio.Event()
        completed = asyncio.Event()

        def download(**options):
            observed.append(options["url"])
            media = Path(options["download_dir"]) / "fixture.mp4"
            media.write_bytes(b"fixture")
            return {"filepath": str(media), "title": "Fixture"}

        app = self.app(runtime, start_queue=True, download_runner=download)
        original_fetchone = aiosqlite.Cursor.fetchone

        async def pause_empty_download_read(cursor):
            row = await original_fetchone(cursor)
            if row is None and cursor.description[0][0] == "id" and not idle_read.is_set():
                idle_read.set()
                await continue_worker.wait()
            return row

        async def broadcast(message):
            events.append(message["event"])
            if message["event"] == "task_complete":
                completed.set()

        with patch.object(aiosqlite.Cursor, "fetchone", pause_empty_download_read), \
                patch.object(app.state.connections, "broadcast", broadcast):
            async with app.router.lifespan_context(app):
                try:
                    await asyncio.wait_for(idle_read.wait(), timeout=2)
                    task_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                    continue_worker.set()
                    try:
                        await asyncio.wait_for(completed.wait(), timeout=2)
                    except TimeoutError:
                        self.fail("An enqueue during the idle transition must run without a second notification")
                    async with aiosqlite.connect(runtime.db_path) as database:
                        async with database.execute("SELECT status, file_path FROM downloads WHERE id = ?", (task_id,)) as cursor:
                            status, file_path = await cursor.fetchone()
                    self.assertEqual(status, "complete")
                    self.assertEqual(Path(file_path).read_bytes(), b"fixture")
                    self.assertEqual(observed, ["https://fixture.invalid/media"])
                    self.assertEqual(events, ["task_queued", "task_started", "task_complete"])
                finally:
                    continue_worker.set()
