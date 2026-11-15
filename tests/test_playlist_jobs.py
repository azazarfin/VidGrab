"""A playlist expands to durable independently owned media children."""

import asyncio
import json
import sqlite3
import unittest
import threading
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

from tests.support import OfflineTestCase
import httpx


class PlaylistJobTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        self.addCleanup(self.assertEqual, self.loop_errors, [])

    async def expanded(self, **options):
        app = self.app()
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        queue = app.state.queue
        parent = await queue.enqueue("https://fixture.invalid/playlist", "best", is_playlist=True, **options)
        attempt = await queue._claim(parent, queue.in_memory_progress[parent])
        expand = getattr(queue.attempts, "expand", None)
        self.assertTrue(callable(expand), "Expansion must commit the parent attempt and child jobs together")
        result = {"playlist": {"entries": [{"position": i, "url": f"https://fixture.invalid/{i}", "title": f"Item {i}"} for i in (1, 2)]}}
        children = await expand(parent, attempt.id, result, queue._now())
        return app, parent, children

    def query(self, runtime, sql, parameters=()):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            return db.execute(sql, parameters).fetchall()

    async def test_expansion_creates_children_without_publishing_parent_media(self):
        def worker(**options):
            return {"playlist": {"entries": [
                {"position": 1, "url": "https://fixture.invalid/one", "title": "One"},
                {"position": 3, "url": "https://fixture.invalid/three", "title": "Three"},
            ]}}

        app = self.app(download_runner=worker)
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            parent = await queue.enqueue("https://fixture.invalid/playlist", "best", is_playlist=True,
                                         playlist_items="1,3", output_template="%(title)s.%(ext)s")
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            await queue._run_task(parent, "https://fixture.invalid/playlist", "best", config,
                                  {"is_playlist": True, "playlist_items": "1,3"})
            rows = self.query(queue.runtime, "SELECT id, status, file_path FROM downloads")
            self.assertEqual(len(rows), 3, "Playlist completion must persist each selected media job")
            self.assertEqual(next(row[1:] for row in rows if row[0] == parent), ("waiting_for_children", None))
            children = self.query(queue.runtime, "SELECT playlist_index, url, download_options FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
            self.assertEqual([row[:2] for row in children], [(1, "https://fixture.invalid/one"), (3, "https://fixture.invalid/three")])
            self.assertTrue(all(not json.loads(row[2])["is_playlist"] and json.loads(row[2])["playlist_items"] is None for row in children))
            self.assertEqual(self.query(queue.runtime, "SELECT COUNT(*) FROM download_artifacts"), [(0,)])

    async def test_schema_tracks_parent_identity_and_persistent_receipts(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
                self.assertTrue({"job_kind", "parent_id", "playlist_index"} <= columns,
                                "Children need explicit durable parent and selected-position authority")
                from backend.db.migrations import SCHEMA_VERSION
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    async def test_children_share_the_root_request_budget_across_attempts(self):
        from backend.services.request_budget import RequestCoordinator, MAX_REQUESTS
        from backend.engine.process_identity import ProcessIdentity
        app, parent, children = await self.expanded()
        queue = app.state.queue
        with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET request_count=? WHERE id=?", (MAX_REQUESTS - 1, parent))
        for index, child in enumerate(children):
            attempt = await queue._claim(child["id"], child)
            with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
                db.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?", (ProcessIdentity.capture().to_json(), attempt.id))
            coordinator = RequestCoordinator(queue.runtime, {"owner_id": queue.owner.id, "attempt_id": attempt.id})
            try:
                if index == 0:
                    lease = coordinator.acquire(child["url"], 1000)
                    coordinator.release(lease)
                else:
                    with self.assertRaisesRegex(Exception, "REQUEST_BUDGET_EXHAUSTED"):
                        coordinator.acquire(child["url"], 1000)
            finally:
                with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
                    db.execute("UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (attempt.id,))
        self.assertEqual(self.query(queue.runtime, "SELECT request_count FROM downloads WHERE id=?", (parent,)), [(MAX_REQUESTS,)])
        self.assertEqual(self.query(queue.runtime, "SELECT request_count FROM downloads WHERE parent_id=?", (parent,)), [(0,), (0,)])

    async def test_cookie_playlist_scopes_volatile_child_context_and_parent_retry(self):
        delivered = []

        def worker(**options):
            jar = Path(options["cookies_file_path"])
            self.assertIn("first-playlist-secret", jar.read_text())
            delivered.append(jar)
            return {"playlist": {"entries": [
                {"position": 1, "url": "https://fixture.invalid/one"},
                {"position": 2, "url": "https://other.invalid/two"},
            ]}}

        cookies = [{"domain": "fixture.invalid", "name": "session", "value": "first-playlist-secret"}]
        app = self.app(download_runner=worker)
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            parent = await queue.enqueue("https://fixture.invalid/playlist", "best", is_playlist=True, cookies=cookies)
            await queue._run_task(parent, "https://fixture.invalid/playlist", "best", await app.state.settings.get_all(), {})
            children = self.query(queue.runtime, "SELECT id, auth_required FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
            self.assertEqual(len(delivered), 1)
            self.assertFalse(delivered[0].exists(), "The parent cookie jar must exit and clean before child admission")
            self.assertEqual(self.query(queue.runtime, "SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
            self.assertEqual([row[1] for row in children], [1, 1])
            scoped, foreign = [row[0] for row in children]
            self.assertEqual(queue.task_cookies[scoped], cookies)
            self.assertNotIn(foreign, queue.task_cookies)
            self.assertNotIn(parent, queue.task_cookies)
            self.assertIsNone(await queue._claim(foreign))
            self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (foreign,)), [("waiting_for_auth",)])

            await queue.cancel(parent)
            fresh = [{**cookies[0], "value": "fresh-playlist-secret"}]
            await queue.retry(parent, cookies=fresh)
            self.assertEqual(queue.task_cookies[scoped], fresh)
            self.assertNotIn(foreign, queue.task_cookies)
            self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (scoped,)), [("queued",)])
            self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (foreign,)), [("waiting_for_auth",)])
            persisted = str(self.query(queue.runtime, "SELECT download_options FROM downloads"))
            self.assertNotIn("playlist-secret", persisted)

    async def test_parent_completes_only_after_every_child_is_terminal(self):
        app, parent, children = await self.expanded()
        queue = app.state.queue
        for index, child in enumerate(children):
            attempt = await queue._claim(child["id"], child)
            await queue._finish(child["id"], "complete", attempt_id=attempt.id, progress=100)
            status = self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (parent,))[0][0]
            self.assertEqual(status, "complete" if index == 1 else "waiting_for_children")

    async def test_parent_cancel_cancels_all_children_and_retry_keeps_child_ids(self):
        app, parent, children = await self.expanded()
        queue = app.state.queue
        await queue.cancel(parent)
        self.assertEqual(self.query(queue.runtime, "SELECT DISTINCT status FROM downloads"), [("cancelled",)])
        await queue.retry(parent)
        self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (parent,)), [("waiting_for_children",)])
        self.assertEqual({row[0] for row in self.query(queue.runtime, "SELECT id FROM downloads WHERE parent_id=?", (parent,))}, {child["id"] for child in children})
        self.assertEqual(self.query(queue.runtime, "SELECT DISTINCT status FROM downloads WHERE parent_id=?", (parent,)), [("queued",)])
        self.assertTrue(queue.wakeup_event.is_set(), "Retried children must wake the scheduler after commit")
        self.assertTrue(all(queue.in_memory_progress.get(child["id"], {}).get("status") == "queued" for child in children),
                        "Committed retries must reach the event snapshot")

    async def test_failed_child_propagates_after_siblings_finish(self):
        app, parent, children = await self.expanded()
        queue = app.state.queue
        for child, status in zip(children, ("failed", "complete")):
            attempt = await queue._claim(child["id"], child)
            await queue._finish(child["id"], status, attempt_id=attempt.id)
        self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (parent,)), [("failed",)])
        await queue.retry(children[0]["id"])
        self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (parent,)), [("waiting_for_children",)])
        self.assertEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (children[1]["id"],)), [("complete",)])

    async def test_history_removal_preserves_playlist_membership_until_whole_group_is_safe(self):
        app, parent, children = await self.expanded()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await app.state.queue.cancel(children[0]["id"])
            response = await client.delete(f"/downloads/history/{children[0]['id']}")
            self.assertEqual(response.status_code, 409, "Individual deletion must not erase parent completion authority")
            await client.delete("/downloads/history")
            self.assertEqual(self.query(app.state.runtime, "SELECT COUNT(*) FROM downloads"), [(3,)])
            await app.state.queue.cancel(parent)
            response = await client.delete(f"/downloads/history/{parent}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.query(app.state.runtime, "SELECT COUNT(*) FROM downloads"), [(0,)])

    async def test_committed_expansion_is_not_repeated_after_recovery(self):
        app, parent, children = await self.expanded()
        before = {child["id"] for child in children}
        await app.state.queue._recover()
        self.assertEqual({row[0] for row in self.query(app.state.runtime, "SELECT id FROM downloads WHERE parent_id=?", (parent,))}, before)
        self.assertIsNone(await app.state.queue.attempts.claim(parent, app.state.queue._now()))
        self.assertEqual(app.state.queue.in_memory_progress.get(parent, {}).get("status"), "waiting_for_children",
                         "Recovery must publish durable aggregate state")

    async def test_partial_child_insert_rolls_back_entire_expansion(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            parent = await queue.enqueue("https://fixture.invalid/list", "best", is_playlist=True)
            attempt = await queue._claim(parent, queue.in_memory_progress[parent])
            with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
                db.execute("CREATE TRIGGER reject_child BEFORE INSERT ON downloads WHEN NEW.playlist_index=2 BEGIN SELECT RAISE(ABORT, 'child fixture'); END")
            result = {"playlist": {"entries": [{"position": i, "url": f"https://fixture.invalid/{i}"} for i in (1, 2)]}}
            with self.assertRaisesRegex(sqlite3.IntegrityError, "child fixture"):
                await queue.attempts.expand(parent, attempt.id, result, queue._now())
            self.assertEqual(self.query(queue.runtime, "SELECT COUNT(*) FROM downloads"), [(1,)])
            self.assertEqual(self.query(queue.runtime, "SELECT status FROM download_attempts"), [("running",)])

    async def test_worker_selection_cannot_change_committed_parent_selection(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            parent = await queue.enqueue("https://fixture.invalid/list", "best", is_playlist=True, playlist_items="2")
            attempt = await queue._claim(parent, queue.in_memory_progress[parent])
            result = {"playlist": {"entries": [{"position": 1, "url": "https://fixture.invalid/one"}]}}
            with self.assertRaisesRegex(ValueError, "selection"):
                await queue.attempts.expand(parent, attempt.id, result, queue._now())
            self.assertEqual(self.query(queue.runtime, "SELECT COUNT(*) FROM downloads"), [(1,)])

    async def test_live_worker_fences_metadata_commit(self):
        from backend.engine.process_identity import ProcessIdentity
        app = self.app()
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            parent = await queue.enqueue("https://fixture.invalid/list", "best", is_playlist=True)
            attempt = await queue._claim(parent, queue.in_memory_progress[parent])
            with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
                db.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?", (ProcessIdentity.capture().to_json(), attempt.id))
            try:
                with self.assertRaisesRegex(Exception, "live.*worker"):
                    await queue.attempts.expand(parent, attempt.id, {"playlist": {"entries": [{"position": 1, "url": "https://fixture.invalid/one"}]}}, queue._now())
            finally:
                with closing(sqlite3.connect(queue.runtime.db_path)) as db, db:
                    db.execute("UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (attempt.id,))
            self.assertEqual(self.query(queue.runtime, "SELECT COUNT(*) FROM downloads"), [(1,)])

    async def test_parent_cancellation_joins_child_worker_before_aggregate_terminal_state(self):
        started = asyncio.Event()
        release = threading.Event()
        exited = threading.Event()
        loop = asyncio.get_running_loop()
        def worker(**options):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            exited.set()
            raise RuntimeError("Stopped fixture")
        app, parent, children = await self.expanded()
        from backend.engine.execution import ThreadDownloadExecutor
        queue = app.state.queue
        queue.download_executor = ThreadDownloadExecutor(worker)
        child = children[0]
        execution = asyncio.create_task(queue._run_task(child["id"], child["url"], "best", await app.state.settings.get_all(), json.loads(child["download_options"])))
        queue.active_tasks[child["id"]] = execution
        cancellation = None
        marked = asyncio.Event()
        original_cancel = queue._cancel
        async def observed_cancel(task_id, **options):
            if task_id == child["id"]:
                marked.set()
            await original_cancel(task_id, **options)
        guard = patch.object(queue, "_cancel", observed_cancel)
        guard.start()
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancellation = asyncio.create_task(queue.cancel(parent))
            await asyncio.wait_for(marked.wait(), 2)
            cancellation.cancel()
            await asyncio.sleep(0)
            cancellation.cancel()
            self.assertFalse(cancellation.done())
            self.assertFalse(exited.is_set())
            self.assertNotEqual(self.query(queue.runtime, "SELECT status FROM downloads WHERE id=?", (parent,)), [("cancelled",)])
        finally:
            release.set()
            if cancellation is not None:
                with self.assertRaises(asyncio.CancelledError):
                    await cancellation
            results = await asyncio.gather(execution, return_exceptions=True)
            guard.stop()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        self.assertTrue(exited.is_set())
        self.assertEqual(self.query(queue.runtime, "SELECT DISTINCT status FROM downloads"), [("cancelled",)])
