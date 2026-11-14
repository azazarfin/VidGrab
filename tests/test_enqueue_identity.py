"""Enqueue identity is durable, atomic and independent of transport retries."""

import asyncio
import inspect
import json
import sqlite3
import unittest
from contextlib import closing

from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import patch

from tests.support import OfflineTestCase


class EnqueueIdentityTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.application = self.app()
        self.context = self.application.router.lifespan_context(self.application)
        await self.context.__aenter__()
        self.queue = self.application.state.queue
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        self.addCleanup(self.assertEqual, self.loop_errors, [])
        self.addAsyncCleanup(self.close_context)

    async def close_context(self):
        await self.context.__aexit__(None, None, None)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.assertEqual(self.loop_errors, [])

    def query(self, sql, parameters=()):
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db:
            return db.execute(sql, parameters).fetchall()

    async def enqueue(self, key="request-one", **options):
        self.assertIn("idempotency_key", inspect.signature(self.queue.enqueue).parameters,
                      "Enqueue must accept a persistent replay identity")
        return await self.queue.enqueue("https://fixture.invalid/media?signature=one", "best",
                                        idempotency_key=key, **options)

    async def test_concurrent_replays_create_one_job_and_one_receipt(self):
        jobs = await asyncio.gather(*(self.enqueue() for _ in range(8)))
        self.assertEqual(len(set(jobs)), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM enqueue_receipts"), [(1,)])

    async def test_changed_request_conflicts_without_mutating_original(self):
        original = await self.enqueue(audio_only=True)
        with self.assertRaises(HTTPException) as caught:
            await self.enqueue(audio_only=False)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.query("SELECT id FROM downloads"), [(original,)])

    async def test_no_key_preserves_independent_submissions(self):
        first = await self.queue.enqueue("https://fixture.invalid/media", "best")
        second = await self.queue.enqueue("https://fixture.invalid/media", "best")
        self.assertNotEqual(first, second)

    async def test_replay_does_not_replace_volatile_credentials(self):
        first = [{"domain": "fixture.invalid", "name": "session", "value": "first-secret"}]
        second = [{"domain": "fixture.invalid", "name": "session", "value": "second-secret"}]
        job = await self.enqueue(cookies=first)
        self.assertEqual(await self.enqueue(cookies=second), job)
        self.assertEqual(self.queue.task_cookies[job], first)
        persisted = str(self.query("SELECT * FROM enqueue_receipts")) + str(self.query("SELECT download_options FROM downloads"))
        self.assertNotIn("secret", persisted)
        self.assertNotIn("request-one", persisted)

    async def test_replay_after_engine_reopen_uses_same_job(self):
        job = await self.enqueue()
        await self.context.__aexit__(None, None, None)
        self.context = self.application.router.lifespan_context(self.application)
        await self.context.__aenter__()
        self.assertEqual(await self.enqueue(), job)

    async def test_deleted_history_retains_receipt_and_refuses_recreation(self):
        job = await self.enqueue()
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("DELETE FROM downloads WHERE id=?", (job,))
        with self.assertRaises(HTTPException) as caught:
            await self.enqueue()
        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM enqueue_receipts"), [(1,)])

    async def test_invalid_or_oversized_keys_are_rejected_before_insert(self):
        for key in ("", "a" * 129, "contains space", "newline\n", "বাংলা"):
            with self.subTest(key=key):
                with self.assertRaises(HTTPException) as caught:
                    await self.enqueue(key)
                self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(0,)])

    async def test_retired_owner_cannot_admit_jobs_or_receipts(self):
        owner = self.application.state.owner
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE runtime_owners SET released_at=1 WHERE id=?", (owner.id,))
        try:
            with self.assertRaisesRegex(Exception, "retired|owner"):
                await self.enqueue()
        finally:
            with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
                db.execute("UPDATE runtime_owners SET released_at=NULL WHERE id=?", (owner.id,))
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(0,)])

    async def test_receipt_failure_rolls_back_job_and_volatile_credentials(self):
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON enqueue_receipts BEGIN SELECT RAISE(ABORT, 'receipt fixture'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt fixture"):
            await self.enqueue(cookies=[{"domain": "fixture.invalid", "name": "session", "value": "secret"}])
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(0,)])
        self.assertEqual(self.queue.task_cookies, {})
        self.assertEqual(self.queue.in_memory_progress, {})

    async def test_repeated_cancellation_joins_committed_admission_and_replay(self):
        started = asyncio.Event()
        release = asyncio.Event()
        original = self.queue.enqueues.admit
        async def gated(*args):
            result = await original(*args)
            started.set()
            await release.wait()
            return result
        with patch.object(self.queue.enqueues, "admit", gated):
            task = asyncio.create_task(self.enqueue())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Admission may not detach after SQLite commits")
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        job = await self.enqueue()
        self.assertEqual(self.query("SELECT id FROM downloads"), [(job,)])
        self.assertIn(job, self.queue.in_memory_progress)

    async def test_transport_user_agent_does_not_change_replay_identity(self):
        job = await self.enqueue(user_agent="Chrome fixture")
        self.assertEqual(await self.enqueue(user_agent="Firefox fixture"), job)

    async def test_receipt_capacity_refuses_new_keys_but_allows_existing_replays(self):
        job = await self.enqueue()
        with patch("backend.db.enqueue.MAX_RECEIPTS", 1):
            self.assertEqual(await self.enqueue(), job)
            with self.assertRaises(HTTPException) as caught:
                await self.enqueue("request-two")
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(1,)])

    async def test_nonfinite_or_oversized_requests_fail_before_admission_without_a_key(self):
        for arguments in ({"invalid": float("nan")}, {"oversized": "a" * 65536}):
            with self.subTest(arguments=list(arguments)):
                with self.assertRaises(HTTPException) as caught:
                    await self.queue.enqueue("https://fixture.invalid/media", "best", extra_args=arguments)
                self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.query("SELECT COUNT(*) FROM downloads"), [(0,)])


class EnqueueEndpointTests(OfflineTestCase):
    def test_legacy_endpoint_accepts_key_header_and_reports_conflicts(self):
        app = self.app()
        with TestClient(app) as client:
            request = {"url": "https://fixture.invalid/media", "format_id": "best"}
            first = client.post("/downloads", json=request, headers={"Idempotency-Key": "request-one"})
            replay = client.post("/downloads", json=request, headers={"Idempotency-Key": "request-one"})
            self.assertEqual(replay.json(), first.json(), "A lost enqueue reply must not create a replacement")
            conflict = client.post("/downloads", json={**request, "format_id": "worst"},
                                   headers={"Idempotency-Key": "request-one"})
            self.assertEqual(conflict.status_code, 409)
