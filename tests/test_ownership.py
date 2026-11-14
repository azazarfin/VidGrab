"""Runtime leases and process identities must fence recovery before filesystem I/O."""

import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib.util
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import aiosqlite

from backend.config import init_db
from tests.support import OfflineTestCase
from tests.test_migrations import legacy_database

NOW = 1000.0
STAMP = "2026-11-14T00:00:00+00:00"


class RuntimeOwnershipTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.previous_handler = self.loop.get_exception_handler()
        self.loop_errors = []
        self.loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.loop.set_exception_handler(self.previous_handler)
        self.assertEqual(self.loop_errors, [], "Ownership lifecycle must retrieve every task exception")

    def modules(self):
        for name in ("backend.engine.runtime_owner", "backend.engine.process_identity"):
            self.assertIsNotNone(importlib.util.find_spec(name), "Durable runtime ownership is missing")
        from backend.engine.runtime_owner import RuntimeOwner, RuntimeOwnershipError
        from backend.engine.process_identity import ProcessIdentity
        return RuntimeOwner, RuntimeOwnershipError, ProcessIdentity

    def rows(self, runtime, sql, values=()):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def execute(self, runtime, sql, values=()):
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute(sql, values)

    def seed_owner(self, runtime, identity, *, released=None, expires=NOW - 1):
        owner_id = str(uuid4())
        self.execute(runtime, """INSERT INTO runtime_owners
            (id, identity, acquired_at, heartbeat_at, lease_expires_at, released_at)
            VALUES (?, ?, ?, ?, ?, ?)""", (owner_id, identity.to_json(), NOW - 30, NOW - 30, expires, released))
        return owner_id

    async def claim(self, app):
        job_id = await app.state.queue.enqueue("https://fixture.invalid/media", "best")
        attempt = await app.state.queue.attempts.claim(job_id, STAMP)
        return job_id, attempt

    async def test_second_lifespan_is_refused_before_database_initialization(self):
        runtime = self.runtime()
        first, second = self.app(runtime), self.app(runtime)
        async with first.router.lifespan_context(first):
            before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
            with self.assertRaisesRegex(RuntimeError, "owner|already|in use"):
                async with second.router.lifespan_context(second):
                    pass
            self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
            self.assertTrue(first.state.ready)
            self.assertFalse(second.state.ready)

    async def test_reentering_the_same_app_uses_a_new_durable_owner(self):
        self.modules()
        app = self.app()
        async with app.router.lifespan_context(app):
            first = app.state.owner.id
        try:
            async with app.router.lifespan_context(app):
                self.assertNotEqual(app.state.owner.id, first)
                self.assertTrue(app.state.owner.healthy)
        except sqlite3.IntegrityError as error:
            self.fail(f"Each lifespan needs a new durable owner identity: {error}")

    async def test_overlapping_lifespans_of_the_same_app_preserve_the_first_owner(self):
        self.modules()
        app = self.app()
        async with app.router.lifespan_context(app):
            first_id, first_fd = app.state.owner.id, app.state.owner.fd
            with self.assertRaisesRegex(RuntimeError, "owner|already|in use"):
                async with app.router.lifespan_context(app):
                    pass
            self.assertEqual(app.state.owner.id, first_id)
            self.assertEqual(app.state.owner.fd, first_fd)
            self.assertTrue(app.state.ready)

    async def test_cancelled_startup_joins_the_committed_owner_before_releasing_its_lock(self):
        self.modules()
        app = self.app()
        committed, release = asyncio.Event(), asyncio.Event()
        original = app.state.owner.store.acquire

        async def acquire(owner, now):
            lease = await original(owner, now)
            committed.set()
            await release.wait()
            return lease

        context = app.router.lifespan_context(app)
        with patch.object(app.state.owner.store, "acquire", acquire):
            startup = asyncio.create_task(context.__aenter__())
            try:
                await asyncio.wait_for(committed.wait(), 1)
                startup.cancel()
                startup.cancel()
                await asyncio.sleep(0)
                self.assertIsNotNone(app.state.owner.fd, "A committed owner must remain locked until startup is joined")
            finally:
                release.set()
                await asyncio.gather(startup, return_exceptions=True)
        self.assertEqual(self.rows(app.state.runtime, "SELECT COUNT(*) FROM runtime_owners WHERE released_at IS NULL"), [(0,)])
        restarted = self.app(app.state.runtime)
        async with restarted.router.lifespan_context(restarted):
            self.assertTrue(restarted.state.ready)

    async def test_owned_attempt_missing_its_lease_is_rejected_by_schema_validation(self):
        self.modules()
        from backend.runtime import RuntimeCompatibilityError
        app = self.app()
        async with app.router.lifespan_context(app):
            _, attempt = await self.claim(app)
            self.execute(app.state.runtime, "UPDATE download_attempts SET lease_expires_at=NULL WHERE id=?", (attempt.id,))
            try:
                with self.assertRaisesRegex(RuntimeCompatibilityError, "lease|ownership"):
                    await init_db(app.state.runtime)
            finally:
                self.execute(app.state.runtime, "UPDATE download_attempts SET lease_expires_at=? WHERE id=?",
                             (app.state.owner.lease_expires_at, attempt.id))

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture; Windows reparse checks are opt-in")
    async def test_linked_owner_lock_is_refused_before_database_creation(self):
        _, error_type, _ = self.modules()
        runtime = self.runtime()
        runtime.data_dir.mkdir()
        foreign = self.root / "foreign-lock"
        foreign.write_bytes(b"foreign lock bytes")
        (runtime.data_dir / "engine-owner.lock").symlink_to(foreign)
        app = self.app(runtime)
        with self.assertRaises(error_type):
            async with app.router.lifespan_context(app):
                pass
        self.assertFalse(runtime.db_path.exists())
        self.assertEqual(foreign.read_bytes(), b"foreign lock bytes")

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture; Windows junction check is opt-in")
    async def test_runtime_directory_replaced_by_a_link_is_refused_before_creating_foreign_files(self):
        _, error_type, _ = self.modules()
        runtime = self.runtime()
        app = self.app(runtime)
        foreign = self.root / "foreign-state"
        foreign.mkdir()
        (foreign / "keep").write_bytes(b"foreign state")
        runtime.data_dir.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(error_type):
            async with app.router.lifespan_context(app):
                pass
        self.assertEqual([path.name for path in foreign.iterdir()], ["keep"])

    async def test_hard_linked_owner_lock_is_refused_without_changing_foreign_bytes(self):
        _, error_type, _ = self.modules()
        runtime = self.runtime()
        runtime.data_dir.mkdir()
        foreign = self.root / "foreign-lock"
        foreign.write_bytes(b"foreign hardlinked bytes")
        os.link(foreign, runtime.data_dir / "engine-owner.lock")
        app = self.app(runtime)
        with self.assertRaises(error_type):
            async with app.router.lifespan_context(app):
                pass
        self.assertFalse(runtime.db_path.exists())
        self.assertEqual(foreign.read_bytes(), b"foreign hardlinked bytes")

    async def test_invalid_platform_boot_identity_is_refused(self):
        _, error_type, identity_type = self.modules()
        identity = identity_type.capture()
        malformed = replace(identity, boot="" if identity.platform == "linux" else "unexpected-boot")
        with self.assertRaisesRegex(error_type, "identity"):
            identity_type.from_json(malformed.to_json())

    async def test_lease_renewal_failure_joins_workers_and_keeps_the_lock_until_they_exit(self):
        self.modules()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def runner(**options):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(4):
                raise RuntimeError("Fixture was not released")
            return {"filepath": "cancelled fixture"}

        app = self.app(start_queue=True, download_runner=runner)
        with patch("backend.engine.runtime_owner.HEARTBEAT_SECONDS", 0.01):
            async with app.router.lifespan_context(app):
                await app.state.queue.enqueue("https://fixture.invalid/media", "best")
                await asyncio.wait_for(entered.wait(), 1)
                try:
                    with patch.object(app.state.owner.store, "renew", side_effect=sqlite3.OperationalError("fixture lease failure")):
                        with self.assertLogs("vidgrab.owner", level="ERROR"):
                            async def failed():
                                while app.state.owner.failure is None:
                                    await asyncio.sleep(0.005)
                            await asyncio.wait_for(failed(), 1)
                        self.assertFalse(app.state.ready)
                        second = self.app(app.state.runtime)
                        with self.assertRaisesRegex(RuntimeError, "owner|already|in use"):
                            async with second.router.lifespan_context(second):
                                pass
                        self.assertIsNotNone(app.state.owner.fd)
                finally:
                    release.set()
                    await asyncio.wait_for(app.state.queue.stop(), 2)
        self.assertEqual(self.rows(app.state.runtime, "SELECT COUNT(*) FROM runtime_owners WHERE released_at IS NULL"), [(0,)])

    async def test_cleanup_preserves_the_original_recovery_error_and_unreleased_owner(self):
        self.modules()
        runtime = self.runtime()
        app = self.app(runtime)
        observed = None
        try:
            async with app.router.lifespan_context(app):
                await self.claim(app)
                self.execute(runtime, """CREATE TRIGGER reject_recovery BEFORE UPDATE ON download_attempts
                    WHEN NEW.status='interrupted' BEGIN SELECT RAISE(ABORT, 'recovery interrupted'); END""")
        except Exception as error:
            observed = error
        self.assertIsInstance(observed, sqlite3.IntegrityError, "Owner cleanup must preserve the first recovery error")
        self.assertRegex(str(observed), "recovery interrupted")
        self.assertIsNone(app.state.owner.fd)
        self.assertIsNone(app.state.owner.heartbeat)
        self.assertEqual(self.rows(runtime, "SELECT released_at FROM runtime_owners"), [(None,)])
        self.assertEqual(self.rows(runtime, "SELECT status FROM download_attempts"), [("running",)])

    async def test_expired_lease_does_not_replace_a_live_prior_owner(self):
        _, error_type, identity_type = self.modules()
        runtime = self.runtime()
        await init_db(runtime)
        prior = self.seed_owner(runtime, identity_type.capture())
        app = self.app(runtime, clock=lambda: NOW)
        with self.assertRaisesRegex(error_type, "live|active|owner"):
            async with app.router.lifespan_context(app):
                pass
        self.assertEqual(self.rows(runtime, "SELECT id, released_at FROM runtime_owners"), [(prior, None)])
        self.assertFalse(app.state.ready)

    async def test_confirmed_dead_owner_can_be_replaced_before_lease_expiry(self):
        _, _, identity_type = self.modules()
        runtime = self.runtime()
        await init_db(runtime)
        dead = replace(identity_type.capture(), pid=0xFFFFFFFE)
        prior = self.seed_owner(runtime, dead, expires=NOW + 60)
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            self.assertNotEqual(app.state.owner.id, prior)
            self.assertEqual(self.rows(runtime, "SELECT released_at FROM runtime_owners WHERE id=?", (prior,)), [(NOW,)])
            self.assertEqual(self.rows(runtime, "SELECT COUNT(*) FROM runtime_owners WHERE released_at IS NULL"), [(1,)])

    async def test_reused_runtime_pid_is_not_mistaken_for_its_prior_owner(self):
        _, _, identity_type = self.modules()
        runtime = self.runtime()
        await init_db(runtime)
        reused = replace(identity_type.capture(), started=identity_type.capture().started - 1)
        prior = self.seed_owner(runtime, reused)
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            self.assertEqual(self.rows(runtime, "SELECT released_at FROM runtime_owners WHERE id=?", (prior,)), [(NOW,)])

    async def test_uncertain_process_identity_refuses_takeover(self):
        _, error_type, identity_type = self.modules()
        runtime = self.runtime()
        await init_db(runtime)
        prior = self.seed_owner(runtime, identity_type.capture())
        app = self.app(runtime, clock=lambda: NOW)
        with patch.object(identity_type, "is_running", side_effect=PermissionError("identity unavailable")):
            with self.assertRaisesRegex(error_type, "identity|verify|owner"):
                async with app.router.lifespan_context(app):
                    pass
        self.assertEqual(self.rows(runtime, "SELECT id, released_at FROM runtime_owners"), [(prior, None)])

    async def test_claim_and_heartbeat_persist_the_same_owner_and_lease(self):
        self.modules()
        runtime = self.runtime()
        current = [NOW]
        app = self.app(runtime, clock=lambda: current[0])
        async with app.router.lifespan_context(app):
            _, attempt = await self.claim(app)
            lease = app.state.owner.lease_expires_at
            self.assertEqual(attempt.owner_id, app.state.owner.id)
            self.assertEqual(self.rows(runtime, "SELECT owner_id, lease_expires_at FROM download_attempts"),
                             [(app.state.owner.id, lease)])
            current[0] += 10
            await app.state.owner.renew()
            self.assertGreater(app.state.owner.lease_expires_at, lease)
            self.assertEqual(self.rows(runtime, "SELECT a.lease_expires_at=o.lease_expires_at FROM download_attempts a"
                                       " JOIN runtime_owners o ON o.id=a.owner_id"), [(1,)])

    async def test_recovery_rejects_a_live_attempt_owner_before_reading_artifacts(self):
        _, error_type, identity_type = self.modules()
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            job_id, attempt = await self.claim(app)
            prior = self.seed_owner(runtime, identity_type.capture(), released=NOW - 1)
            self.execute(runtime, "UPDATE download_attempts SET owner_id=?, lease_expires_at=? WHERE id=?",
                         (prior, NOW - 1, attempt.id))
            before = self.rows(runtime, "SELECT status, current_attempt_id FROM downloads")
            try:
                with patch("backend.db.jobs.recovery_artifacts", side_effect=AssertionError("live artifact read")):
                    with self.assertRaisesRegex(error_type, "live|active|owner"):
                        await app.state.queue.attempts.recover(STAMP)
                self.assertEqual(self.rows(runtime, "SELECT status, current_attempt_id FROM downloads"), before)
                self.assertEqual(self.rows(runtime, "SELECT status FROM download_attempts"), [("running",)])
            finally:
                self.execute(runtime, "UPDATE download_attempts SET owner_id=? WHERE id=?", (app.state.owner.id, attempt.id))

    async def test_live_worker_blocks_recovery_even_when_the_engine_owner_is_dead(self):
        _, error_type, identity_type = self.modules()
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            _, attempt = await self.claim(app)
            dead = replace(identity_type.capture(), pid=0xFFFFFFFE)
            prior = self.seed_owner(runtime, dead, released=NOW - 1)
            self.execute(runtime, "UPDATE download_attempts SET owner_id=?, worker_identity=? WHERE id=?",
                         (prior, identity_type.capture().to_json(), attempt.id))
            try:
                with self.assertRaisesRegex(error_type, "live|active|worker"):
                    await app.state.queue.attempts.recover(STAMP)
                self.assertEqual(self.rows(runtime, "SELECT status FROM downloads"), [("downloading",)])
            finally:
                self.execute(runtime, "UPDATE download_attempts SET owner_id=?, worker_identity=NULL WHERE id=?",
                             (app.state.owner.id, attempt.id))

    async def test_reused_worker_pid_allows_recovery_without_signalling_that_process(self):
        _, _, identity_type = self.modules()
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            _, attempt = await self.claim(app)
            actual = identity_type.capture()
            reused = replace(actual, started=actual.started - 1)
            self.execute(runtime, "UPDATE download_attempts SET worker_identity=? WHERE id=?", (reused.to_json(), attempt.id))
            with patch("os.kill", side_effect=AssertionError("Recovery must never signal a reused PID")):
                await app.state.queue.attempts.recover(STAMP)
            self.assertTrue(actual.is_running())
            self.assertEqual(self.rows(runtime, "SELECT status FROM downloads"), [("queued",)])

    async def test_released_owner_cannot_claim_or_finish_an_attempt(self):
        _, error_type, _ = self.modules()
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            job_id, attempt = await self.claim(app)
            self.execute(runtime, "UPDATE runtime_owners SET released_at=? WHERE id=?", (NOW, app.state.owner.id))
            try:
                with self.assertRaises(error_type):
                    await app.state.queue.attempts.claim("absent", STAMP)
                with self.assertRaises(error_type):
                    await app.state.queue.attempts.finish(job_id, "complete", STAMP, attempt_id=attempt.id)
                self.assertEqual(self.rows(runtime, "SELECT status FROM downloads"), [("downloading",)])
            finally:
                self.execute(runtime, "UPDATE runtime_owners SET released_at=NULL WHERE id=?", (app.state.owner.id,))

    async def test_worker_registration_rejects_an_owner_retired_during_launch(self):
        _, error_type, identity_type = self.modules()
        from backend.db.ownership import register_worker
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            job_id, attempt = await self.claim(app)
            self.execute(runtime, "UPDATE runtime_owners SET released_at=? WHERE id=?", (NOW, app.state.owner.id))
            try:
                with self.assertRaises(error_type):
                    register_worker(runtime, attempt.id, app.state.owner.id, job_id, str(attempt.workspace),
                                    identity_type.capture(), identity_type.capture())
                self.assertEqual(self.rows(runtime, "SELECT worker_owner_identity, worker_identity FROM download_attempts"),
                                 [(None, None)])
            finally:
                self.execute(runtime, "UPDATE runtime_owners SET released_at=NULL WHERE id=?", (app.state.owner.id,))

    async def test_legacy_running_attempt_without_identity_is_preserved_and_refused(self):
        _, error_type, _ = self.modules()
        runtime = self.runtime()
        app = self.app(runtime, clock=lambda: NOW)
        async with app.router.lifespan_context(app):
            _, attempt = await self.claim(app)
            self.execute(runtime, "UPDATE download_attempts SET owner_id=NULL, lease_expires_at=NULL WHERE id=?", (attempt.id,))
            try:
                with self.assertRaisesRegex(error_type, "identity|legacy|ownership"):
                    await app.state.queue.attempts.recover(STAMP)
                self.assertEqual(self.rows(runtime, "SELECT status FROM download_attempts"), [("running",)])
            finally:
                self.execute(runtime, "UPDATE download_attempts SET owner_id=? WHERE id=?", (app.state.owner.id, attempt.id))

    async def test_lock_is_retained_until_joined_execution_exits(self):
        self.modules()
        entered, release = asyncio.Event(), threading.Event()
        runtime = self.runtime()
        loop = asyncio.get_running_loop()

        def runner(**options):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("Fixture was not released")
            return {"filepath": None}

        app = self.app(runtime, start_queue=True, download_runner=runner)
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        exit_task = None
        try:
            await app.state.queue.enqueue("https://fixture.invalid/media", "best")
            await asyncio.wait_for(entered.wait(), 1)
            exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            await asyncio.sleep(0)
            second = self.app(runtime)
            with self.assertRaisesRegex(RuntimeError, "owner|already|in use"):
                async with second.router.lifespan_context(second):
                    pass
            self.assertFalse(exit_task.done())
        finally:
            release.set()
            if exit_task:
                await exit_task
            else:
                await context.__aexit__(None, None, None)
        third = self.app(runtime)
        async with third.router.lifespan_context(third):
            self.assertTrue(third.state.ready)


class OwnershipMigrationTests(OfflineTestCase):
    def schema_three(self, runtime, *, running=False):
        from backend.db.migrations import _migration_two, _migration_three
        legacy_database(runtime.db_path, version=2)

        async def create():
            async with aiosqlite.connect(runtime.db_path) as db:
                await _migration_two(db)
                await _migration_three(db)
                await db.execute("PRAGMA user_version=3")
                if running:
                    attempt_id = str(uuid4())
                    await db.execute("""INSERT INTO download_attempts
                        (id, download_id, number, status, workspace, started_at)
                        VALUES (?, 'legacy-job', 1, 'running', ?, ?)""",
                        (attempt_id, str(runtime.data_dir / "attempts" / attempt_id), STAMP))
                    await db.execute("UPDATE downloads SET status='downloading', current_attempt_id=?, attempt_count=1",
                                     (attempt_id,))
                await db.commit()

        asyncio.run(create())

    def test_schema_three_upgrade_preserves_running_attempt_without_inventing_an_owner(self):
        runtime = self.runtime()
        self.schema_three(runtime, running=True)
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT status, owner_id, lease_expires_at, worker_identity FROM download_attempts").fetchall(),
                             [("running", None, None, None)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runtime_owners").fetchone()[0], 0)
        backups = list(runtime.backups_dir.glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_interrupted_ownership_migration_rolls_back_the_schema_three_database(self):
        runtime = self.runtime()
        self.schema_three(runtime)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("""CREATE TRIGGER reject_defaults BEFORE INSERT ON config WHEN NEW.key='max_retries'
                          BEGIN SELECT RAISE(ABORT, 'ownership migration interrupted'); END""")
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ownership migration interrupted"):
            asyncio.run(init_db(runtime))
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='runtime_owners'").fetchall(), [])
