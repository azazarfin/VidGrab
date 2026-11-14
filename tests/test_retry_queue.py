"""Automatic failures wait durably instead of spinning or losing deadlines."""

import asyncio
import sqlite3
import unittest
from contextlib import closing

from tests.support import OfflineTestCase


class RetryQueueTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, error):
        def worker(**options):
            raise error
        app = self.app(download_runner=worker, clock=lambda: 1000.0)
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        queue = app.state.queue
        job = await queue.enqueue("https://fixture.invalid/media", "best")
        await queue._run_task(job, "https://fixture.invalid/media", "best", await app.state.settings.get_all(), {})
        return app, job

    def query(self, app, sql, args=()):
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
            return db.execute(sql, args).fetchall()

    async def test_transient_failure_waits_with_a_persisted_deadline(self):
        app, job = await self.run_failure(TimeoutError("fixture timeout"))
        state = self.query(app, "SELECT status FROM downloads WHERE id=?", (job,))[0][0]
        self.assertEqual(state, "retry_wait", "Transient failures must not requeue immediately")
        state, deadline, retries = self.query(app, "SELECT status, retry_at, retry_count FROM downloads WHERE id=?", (job,))[0]
        self.assertTrue(1000 < deadline <= 1030)
        self.assertEqual(retries, 1)
        self.assertIsNone(await app.state.queue.attempts.claim(job, app.state.queue._now()))
        await app.state.queue._recover()
        self.assertEqual(self.query(app, "SELECT status, retry_at FROM downloads WHERE id=?", (job,)), [(state, deadline)])

    async def test_unknown_failure_is_permanent_without_an_automatic_retry(self):
        app, job = await self.run_failure(RuntimeError("unclassified fixture"))
        self.assertEqual(self.query(app, "SELECT status, retry_count FROM downloads WHERE id=?", (job,)), [("failed", 0)])

    async def test_retry_wait_is_cancellable_and_never_promoted_after_cancel(self):
        app, job = await self.run_failure(TimeoutError("fixture timeout"))
        await app.state.queue.cancel(job)
        promote = getattr(app.state.queue.attempts, "promote_due_retries", None)
        self.assertTrue(callable(promote), "Due promotion needs durable owner-fenced authority")
        await promote(2000.0)
        self.assertEqual(self.query(app, "SELECT status, retry_at FROM downloads WHERE id=?", (job,)), [("cancelled", None)])

    async def test_due_promotion_preserves_future_jobs_and_requires_current_owner(self):
        app, job = await self.run_failure(TimeoutError("fixture timeout"))
        promote = getattr(app.state.queue.attempts, "promote_due_retries", None)
        self.assertTrue(callable(promote), "Due retries must be promoted through one owner-fenced transaction")
        self.assertEqual(await promote(1000.0), [])
        rows = await promote(2000.0)
        self.assertEqual([row["id"] for row in rows], [job])
        self.assertEqual(self.query(app, "SELECT status, retry_at FROM downloads WHERE id=?", (job,)), [("queued", None)])

    async def test_total_attempt_budget_stops_repeated_admission_or_crash_attempts(self):
        app, job = await self.run_failure(TimeoutError("fixture timeout"))
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='queued', retry_at=NULL, attempt_count=64 WHERE id=?", (job,))
        self.assertIsNone(await app.state.queue.attempts.claim(job, app.state.queue._now()),
                          "Admission/recovery retries must not create unlimited owned attempts")
        self.assertEqual(self.query(app, "SELECT status,error_code FROM downloads WHERE id=?", (job,)),
                         [("failed", "ATTEMPT_BUDGET_EXHAUSTED")])

    async def test_exhausted_claim_updates_queue_snapshot(self):
        app, job = await self.run_failure(TimeoutError("fixture timeout"))
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET status='queued', retry_at=NULL, attempt_count=64 WHERE id=?", (job,))
        self.assertIsNone(await app.state.queue._claim(job))
        self.assertEqual(app.state.queue.in_memory_progress[job]["error_code"], "ATTEMPT_BUDGET_EXHAUSTED")

    async def test_manual_retry_clears_deadline_and_code_but_preserves_spent_budget(self):
        app, job = await self.run_failure(RuntimeError("fixture permanent"))
        with closing(sqlite3.connect(app.state.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET retry_at=2000,request_count=21 WHERE id=?", (job,))
        await app.state.queue.retry(job)
        self.assertEqual(self.query(app, "SELECT retry_at,error_code,request_count,attempt_count FROM downloads WHERE id=?", (job,)),
                         [(None, None, 21, 1)])
