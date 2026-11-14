"""Request counts and slots share durable authority across worker attempts."""

import importlib.util
import sqlite3
import unittest
from contextlib import closing

from tests.support import OfflineTestCase


class RequestBudgetTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.application = self.app()
        context = self.application.router.lifespan_context(self.application)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.queue = self.application.state.queue
        self.job = await self.queue.enqueue("https://fixture.invalid/media?private=secret", "best")
        self.attempt = await self.queue._claim(self.job)
        self.addAsyncCleanup(self.clear_fixture_identity)

    async def clear_fixture_identity(self):
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("DELETE FROM request_leases")
            db.execute("DELETE FROM request_probes")
            db.execute("UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (self.attempt.id,))

    def coordinator(self, **limits):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.request_budget"), "Requests need a shared durable coordinator")
        from backend.services.request_budget import RequestCoordinator
        from backend.engine.process_identity import ProcessIdentity
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?", (ProcessIdentity.capture().to_json(), self.attempt.id))
        return RequestCoordinator(self.queue.runtime, {"owner_id": self.queue.owner.id, "attempt_id": self.attempt.id}, **limits)

    def query(self, sql, args=()):
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db:
            return db.execute(sql, args).fetchall()

    async def test_slot_release_preserves_finite_request_count(self):
        coordinator = self.coordinator(max_requests=2)
        for _ in range(2):
            lease = coordinator.acquire("https://fixture.invalid/media", 1000)
            coordinator.release(lease)
        with self.assertRaisesRegex(Exception, "REQUEST_BUDGET_EXHAUSTED"):
            coordinator.acquire("https://fixture.invalid/media", 1000)
        self.assertEqual(self.query("SELECT request_count FROM downloads WHERE id=?", (self.job,)), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM request_leases"), [(0,)])

    def seed_lease(self, identity):
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("""INSERT INTO request_leases (id,owner_id,attempt_id,identity,host_key,created_at)
                VALUES (?,?,?,?,?,?)""", ("fixture-lease", self.queue.owner.id, self.attempt.id, identity, "a" * 64, 1000))

    async def test_recovery_refuses_live_or_unknown_request_identity_before_reset(self):
        from backend.engine.process_identity import ProcessIdentity
        for identity in (ProcessIdentity.capture().to_json(), "{}"):
            self.seed_lease(identity)
            with self.assertRaisesRegex(Exception, "live|identity|request"):
                await self.queue._recover()
            self.assertEqual(self.query("SELECT status FROM download_attempts WHERE id=?", (self.attempt.id,)), [("running",)])
            with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
                db.execute("DELETE FROM request_leases")

    async def test_recovery_removes_only_a_verified_dead_request_lease(self):
        import json
        from backend.engine.process_identity import ProcessIdentity
        identity = json.loads(ProcessIdentity.capture().to_json())
        identity["pid"] = 2147483647
        self.seed_lease(json.dumps(identity))
        await self.queue._recover()
        self.assertEqual(self.query("SELECT COUNT(*) FROM request_leases"), [(0,)])

    async def test_owner_release_refuses_retained_request_authority(self):
        from backend.engine.process_identity import ProcessIdentity
        from backend.db.ownership import OwnerStore
        self.seed_lease(ProcessIdentity.capture().to_json())
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE download_attempts SET status='failed' WHERE id=?", (self.attempt.id,))
        with self.assertRaisesRegex(Exception, "request"):
            await OwnerStore(self.queue.runtime).release(self.queue.owner, 2000)

    async def test_probe_authority_is_fenced_before_recovery(self):
        from backend.services.request_budget import begin_probe, end_probe
        context = begin_probe(self.queue.runtime, self.queue.owner, "https://fixture.invalid/list", 1000)
        try:
            with self.assertRaisesRegex(Exception, "live|probe"):
                await self.queue._recover()
            self.assertEqual(self.query("SELECT status FROM download_attempts WHERE id=?", (self.attempt.id,)), [("running",)])
        finally:
            end_probe(self.queue.runtime, context)

    async def test_owner_acquisition_refuses_live_request_before_retirement(self):
        import json
        from types import SimpleNamespace
        from uuid import uuid4
        from backend.db.ownership import OwnerStore
        from backend.engine.process_identity import ProcessIdentity
        identity = ProcessIdentity.capture()
        self.seed_lease(identity.to_json())
        dead = json.loads(identity.to_json())
        dead["pid"] = 2147483647
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE runtime_owners SET identity=? WHERE id=?", (json.dumps(dead), self.queue.owner.id))
        replacement = SimpleNamespace(id=str(uuid4()), identity=identity)
        try:
            with self.assertRaisesRegex(Exception, "live.*request"):
                await OwnerStore(self.queue.runtime).acquire(replacement, 2000)
            self.assertEqual(self.query("SELECT released_at FROM runtime_owners WHERE id=?", (self.queue.owner.id,)), [(None,)])
        finally:
            with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
                db.execute("UPDATE runtime_owners SET identity=? WHERE id=?", (identity.to_json(), self.queue.owner.id))

    async def test_retained_request_lease_returns_history_conflict(self):
        from fastapi import HTTPException
        from backend.engine.process_identity import ProcessIdentity
        self.seed_lease(ProcessIdentity.capture().to_json())
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE download_attempts SET status='failed' WHERE id=?", (self.attempt.id,))
            db.execute("UPDATE downloads SET status='failed',current_attempt_id=NULL WHERE id=?", (self.job,))
        with self.assertRaises(HTTPException) as caught:
            await self.queue.delete_history(self.job)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.queue.delete_history(), [])

    async def test_host_and_global_slots_reject_without_consuming_a_request(self):
        coordinator = self.coordinator(max_active=2, max_per_host=1)
        first = coordinator.acquire("https://fixture.invalid/one", 1000)
        with self.assertRaisesRegex(Exception, "ADMISSION_LIMITED"):
            coordinator.acquire("https://fixture.invalid/two", 1000)
        second = coordinator.acquire("https://other.invalid/media", 1000)
        with self.assertRaisesRegex(Exception, "ADMISSION_LIMITED"):
            coordinator.acquire("https://third.invalid/media", 1000)
        coordinator.release(first)
        coordinator.release(second)
        self.assertEqual(self.query("SELECT request_count FROM downloads WHERE id=?", (self.job,)), [(2,)])

    async def test_cooldown_is_shared_across_instances_and_does_not_shorten(self):
        one = self.coordinator()
        one.cooldown("https://fixture.invalid/media", 1200, 1000)
        two = self.coordinator()
        two.cooldown("https://fixture.invalid/other", 1100, 1000)
        with self.assertRaisesRegex(Exception, "UPSTREAM_RATE_LIMITED"):
            two.acquire("https://fixture.invalid/media", 1199)
        lease = two.acquire("https://fixture.invalid/media", 1200)
        two.release(lease)
        persisted = str(self.query("SELECT * FROM request_hosts"))
        self.assertNotIn("fixture.invalid", persisted)
        self.assertNotIn("secret", persisted)

    async def test_retired_owner_and_changed_worker_identity_are_refused(self):
        coordinator = self.coordinator()
        with closing(sqlite3.connect(self.queue.runtime.db_path)) as db, db:
            db.execute("UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (self.attempt.id,))
        with self.assertRaisesRegex(Exception, "identity|authority"):
            coordinator.acquire("https://fixture.invalid/media", 1000)
        self.assertEqual(self.query("SELECT COUNT(*) FROM request_leases"), [(0,)])
