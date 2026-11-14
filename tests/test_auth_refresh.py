"""Fresh credentials remain volatile and cannot revive a racing cancellation."""

import asyncio
import importlib.util
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

import httpx
from tests.support import OfflineTestCase

COOKIES = [{"domain": ".fixture.invalid", "path": "/", "secure": True,
            "name": "session", "value": "fresh-secret", "expirationDate": 1900000000}]


class AuthRefreshTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    def query(self, runtime, sql, values=()):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")

    async def test_expired_auth_waits_without_automatic_retry_or_cookie_reuse(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.resume"))
        from backend.services.resume import FreshAuthRequired
        observed = []
        def download(**options):
            observed.append(options["cookies_file_path"])
            raise FreshAuthRequired()
        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best", cookies=COOKIES)
            await app.state.queue._run_task(job, "https://fixture.invalid/media", "best", await app.state.settings.get_all(), {})
            self.assertEqual(self.query(app.state.runtime, "SELECT status, retry_count FROM downloads"), [("waiting_for_auth", 0)])
            self.assertFalse(Path(observed[0]).exists())
            self.assertNotIn(job, app.state.queue.task_cookies)
            self.assertEqual(self.query(app.state.runtime, "SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_reopen_places_queued_authenticated_job_in_waiting_state(self):
        runtime = self.runtime()
        app = self.app(runtime)
        async with app.router.lifespan_context(app):
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best", cookies=COOKIES)
        reopened = self.app(runtime)
        async with reopened.router.lifespan_context(reopened):
            self.assertEqual(self.query(runtime, "SELECT status FROM downloads WHERE id=?", (job,)), [("waiting_for_auth",)])
            self.assertIsNone(await reopened.state.queue._claim(job))
            self.assertNotIn("fresh-secret", str(self.query(runtime, "SELECT download_options FROM downloads")))

    async def waiting(self, app):
        job = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='waiting_for_auth' WHERE id=?", (job,))
        app.state.queue.in_memory_progress[job]["status"] = "waiting_for_auth"
        return job

    async def test_refresh_requeues_same_job_and_supplies_new_temporary_cookie_file(self):
        observed = {}
        def download(**options):
            observed.update(options)
            self.assertIn("fresh-secret", Path(options["cookies_file_path"]).read_text())
            output = Path(options["download_dir"]) / "complete.mp3"
            output.write_bytes(b"media")
            return {"filepath": str(output)}
        app = self.app(download_runner=download)
        async with app.router.lifespan_context(app):
            job = await self.waiting(app)
            async with self.client(app) as client:
                reply = await client.post(f"/downloads/{job}/retry", json={"cookies": COOKIES})
                self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(self.query(app.state.runtime, "SELECT status FROM downloads"), [("queued",)])
            await app.state.queue._run_task(job, "https://fixture.invalid/media", "best", await app.state.settings.get_all(), {})
            self.assertEqual(self.query(app.state.runtime, "SELECT status, attempt_count FROM downloads"), [("complete", 1)])
            self.assertFalse(Path(observed["cookies_file_path"]).exists())
            self.assertNotIn("fresh-secret", str(self.query(app.state.runtime, "SELECT download_options FROM downloads")))

    async def test_missing_expired_and_wrong_scope_credentials_leave_job_waiting(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            job = await self.waiting(app)
            async with self.client(app) as client:
                for cookies in (None, [], [{**COOKIES[0], "expirationDate": 1}], [{**COOKIES[0], "domain": ".other.invalid"}]):
                    reply = await client.post(f"/downloads/{job}/retry", json={"cookies": cookies})
                    self.assertEqual(reply.status_code, 400, reply.text)
            self.assertEqual(self.query(app.state.runtime, "SELECT status FROM downloads"), [("waiting_for_auth",)])
            self.assertNotIn(job, app.state.queue.task_cookies)

    async def test_rejected_refresh_does_not_cache_credentials_for_missing_or_active_job(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            async with self.client(app) as client:
                for selected, code in (("missing", 404), (job, 400)):
                    reply = await client.post(f"/downloads/{selected}/retry", json={"cookies": COOKIES})
                    self.assertEqual(reply.status_code, code)
                    self.assertNotIn(selected, app.state.queue.task_cookies,
                                     "Rejected refresh must not install a credential context")

    async def test_cancellation_wins_refresh_race_and_no_claim_is_reused(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            job = await self.waiting(app)
            async with self.client(app) as client:
                refresh, cancel = await asyncio.gather(
                    client.post(f"/downloads/{job}/retry", json={"cookies": COOKIES}),
                    client.delete(f"/downloads/{job}"))
            self.assertEqual(cancel.status_code, 200)
            self.assertIn(refresh.status_code, (200, 400))
            self.assertEqual(self.query(app.state.runtime, "SELECT status FROM downloads"), [("cancelled",)])
            self.assertNotIn(job, app.state.queue.task_cookies)

    async def test_refresh_endpoint_cannot_revive_a_cancelled_authentication_wait(self):
        app = self.app()
        async with app.router.lifespan_context(app):
            job = await self.waiting(app)
            await app.state.queue.cancel(job)
            async with self.client(app) as client:
                reply = await client.post(f"/downloads/{job}/authentication", json={"cookies": COOKIES})
            self.assertEqual(reply.status_code, 400, reply.text)
            self.assertEqual(self.query(app.state.runtime, "SELECT status FROM downloads"), [("cancelled",)])
            self.assertNotIn(job, app.state.queue.task_cookies)
