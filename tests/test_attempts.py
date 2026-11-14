"""SQLite claims, attempt fencing and recovery must survive interrupted writes."""

import asyncio
import hashlib
import importlib.util
import sqlite3
import unittest
from contextlib import closing

from backend.config import init_db
from tests.support import OfflineTestCase
from tests.test_migrations import legacy_database

NOW = "2026-11-14T00:00:00+00:00"
LATER = "2026-11-14T00:01:00+00:00"


class AttemptMigrationTests(OfflineTestCase):
    def schema_two(self, runtime):
        legacy_database(runtime.db_path, version=2, config={"max_concurrent": 5})
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("ALTER TABLE downloads ADD COLUMN download_options TEXT")
            db.execute("ALTER TABLE downloads ADD COLUMN scheduled_at TEXT")
            db.execute("ALTER TABLE downloads ADD COLUMN retry_count INTEGER DEFAULT 0")
            db.execute("UPDATE downloads SET status='complete', file_path=?, retry_count=2",
                       (str(runtime.download_dir / "finished বাংলা.mp3"),))

    def test_schema_two_upgrade_preserves_history_without_inventing_attempts(self):
        runtime = self.runtime()
        self.schema_two(runtime)
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            row = db.execute("SELECT status, file_path, retry_count, attempt_count, current_attempt_id FROM downloads").fetchone()
            self.assertEqual(row, ("complete", str(runtime.download_dir / "finished বাংলা.mp3"), 2, 0, None))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM download_artifacts").fetchone()[0], 0)
        self.assertEqual(len(list(runtime.backups_dir.glob("*.sqlite3"))), 1)

    def test_interrupted_schema_three_upgrade_rolls_back_tables_and_version(self):
        runtime = self.runtime()
        self.schema_two(runtime)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("""CREATE TRIGGER interrupt_defaults BEFORE INSERT ON config
                WHEN NEW.key = 'max_retries' BEGIN
                SELECT RAISE(ABORT, 'attempt migration interrupted'); END""")
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "attempt migration interrupted"):
            asyncio.run(init_db(runtime))
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='download_attempts'").fetchall())
            db.execute("DROP TRIGGER interrupt_defaults")
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_current_schema_without_the_single_attempt_index_is_refused(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP INDEX one_running_attempt")
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(Exception, "attempt.*index|index.*attempt"):
            asyncio.run(init_db(runtime))
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)

    def test_current_schema_with_an_invalid_attempt_pointer_is_refused(self):
        runtime = self.runtime()
        self.schema_two(runtime)
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET current_attempt_id='missing-attempt'")
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(Exception, "attempt|ownership"):
            asyncio.run(init_db(runtime))
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)


class AttemptStoreTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.selected_runtime = self.runtime()
        self.application = self.app(self.selected_runtime)
        context = self.application.router.lifespan_context(self.application)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)

    def store(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.db.jobs"),
                             "Durable attempts need a transactional repository")
        return self.application.state.queue.attempts

    def query(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db, db:
            db.execute(sql, values)

    def enqueue(self, job_id="job", status="queued"):
        self.execute("INSERT INTO downloads (id, url, status, format_selected, created_at) VALUES (?, ?, ?, ?, ?)",
                     (job_id, "https://fixture.invalid/media", status, "best", NOW))
        return job_id

    async def test_concurrent_claims_create_exactly_one_attempt(self):
        job_id = self.enqueue()
        claims = await asyncio.gather(*(self.store().claim(job_id, NOW) for _ in range(6)))
        actual = [claim for claim in claims if claim is not None]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].number, 1)
        self.assertEqual(actual[0].workspace.parent, self.selected_runtime.data_dir / "attempts")
        self.assertEqual(self.query("SELECT status, attempt_count, current_attempt_id FROM downloads"),
                         [("downloading", 1, actual[0].id)])
        self.assertEqual(self.query("SELECT id, status, started_at FROM download_attempts"),
                         [(actual[0].id, "running", NOW)])

    async def test_claim_failure_rolls_back_job_and_attempt_counter(self):
        job_id = self.enqueue()
        store = self.store()
        self.execute("""CREATE TRIGGER reject_attempt BEFORE INSERT ON download_attempts BEGIN
                     SELECT RAISE(ABORT, 'claim interrupted'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "claim interrupted"):
            await store.claim(job_id, NOW)
        self.assertEqual(self.query("SELECT status, attempt_count, current_attempt_id FROM downloads"),
                         [("queued", 0, None)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM download_attempts"), [(0,)])

    async def test_only_queued_jobs_can_be_claimed(self):
        store = self.store()
        for status in ("scheduled", "complete", "failed", "cancelled", "cancelling"):
            with self.subTest(status=status):
                self.assertIsNone(await store.claim(self.enqueue(status, status), NOW))
        self.assertIsNone(await store.claim("absent", NOW))
        self.assertEqual(self.query("SELECT COUNT(*) FROM download_attempts"), [(0,)])

    async def test_recovery_closes_an_interrupted_attempt_without_consuming_retries(self):
        job_id = self.enqueue()
        self.execute("UPDATE downloads SET retry_count=2 WHERE id=?", (job_id,))
        store = self.store()
        first = await store.claim(job_id, NOW)
        first.workspace.mkdir(parents=True)
        partial = first.workspace / "clip.part"
        partial.write_bytes(b"owned partial")
        unrelated = self.selected_runtime.download_dir / "clip another job.part"
        unrelated.write_bytes(b"unrelated")
        self.execute("UPDATE downloads SET status='postprocessing', speed=10, eta=5 WHERE id=?", (job_id,))
        await store.recover(LATER)
        self.assertEqual(self.query("SELECT status, current_attempt_id, retry_count, speed, eta FROM downloads"),
                         [("queued", None, 2, 0.0, None)])
        self.assertEqual(self.query("SELECT status, finished_at FROM download_attempts"), [("interrupted", LATER)])
        second = await self.store().claim(job_id, LATER)
        self.assertEqual(second.number, 2)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(partial.read_bytes(), b"owned partial")
        self.assertEqual(unrelated.read_bytes(), b"unrelated")

    async def test_recovery_honors_persisted_cancellation_and_is_idempotent(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        self.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (job_id,))
        await store.recover(LATER)
        await store.recover("2026-11-15T00:00:00+00:00")
        self.assertEqual(self.query("SELECT status, completed_at, current_attempt_id FROM downloads"),
                         [("cancelled", LATER, None)])
        self.assertEqual(self.query("SELECT id, status, finished_at FROM download_attempts"),
                         [(attempt.id, "cancelled", LATER)])

    async def test_legacy_active_rows_recover_without_fabricated_attempts(self):
        self.enqueue("legacy", "probing")
        self.enqueue("finished", "complete")
        self.enqueue("cancel-request", "cancelling")
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT id, status, attempt_count FROM downloads ORDER BY id"),
                         [("cancel-request", "cancelled", 0), ("finished", "complete", 0), ("legacy", "queued", 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM download_attempts"), [(0,)])

    async def test_previous_attempt_cannot_finish_a_new_claim(self):
        job_id = self.enqueue()
        store = self.store()
        first = await store.claim(job_id, NOW)
        self.assertTrue(await store.finish(job_id, "queued", LATER, attempt_id=first.id))
        second = await store.claim(job_id, LATER)
        self.assertFalse(await store.finish(job_id, "complete", LATER, attempt_id=first.id,
                                           file_path="stale.mp3", progress=100.0))
        self.assertEqual(self.query("SELECT status, current_attempt_id, file_path FROM downloads"),
                         [("downloading", second.id, None)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM download_artifacts"), [(0,)])

    async def test_completion_atomically_records_literal_multiple_artifacts(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        paths = [str(self.selected_runtime.download_dir / "finished বাংলা.converted"),
                 str(self.selected_runtime.download_dir / "finished বাংলা.en.srt")]
        artifacts = [{"path": paths[0], "kind": "media", "state": "complete", "size": 12},
                     {"path": paths[1], "kind": "subtitle", "state": "complete", "size": 8}]
        self.assertTrue(await store.finish(job_id, "complete", LATER, attempt_id=attempt.id,
                                          artifacts=artifacts, file_path=paths[0], progress=100.0))
        self.assertEqual(self.query("SELECT status, file_path, current_attempt_id FROM downloads"),
                         [("complete", paths[0], None)])
        self.assertEqual(self.query("SELECT status, finished_at FROM download_attempts"), [("complete", LATER)])
        self.assertEqual(self.query("SELECT path, kind, state, size FROM download_artifacts ORDER BY kind"),
                         [(paths[0], "media", "complete", 12), (paths[1], "subtitle", "complete", 8)])

    async def test_failed_artifact_insert_rolls_back_completion(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        self.execute("""CREATE TRIGGER reject_artifact BEFORE INSERT ON download_artifacts BEGIN
                     SELECT RAISE(ABORT, 'artifact write interrupted'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "artifact write interrupted"):
            await store.finish(job_id, "complete", LATER, attempt_id=attempt.id, file_path="finished.mp3",
                               artifacts=[{"path": "finished.mp3", "kind": "media", "state": "complete", "size": 1}])
        self.assertEqual(self.query("SELECT status, current_attempt_id, file_path FROM downloads"),
                         [("downloading", attempt.id, None)])
        self.assertEqual(self.query("SELECT status, finished_at FROM download_attempts"), [("running", None)])

    async def test_cancellation_request_wins_over_completion(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        self.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (job_id,))
        self.assertFalse(await store.finish(job_id, "complete", LATER, attempt_id=attempt.id))
        self.assertTrue(await store.finish(job_id, "cancelled", LATER, attempt_id=attempt.id))
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("cancelled",)])

    async def test_job_and_attempt_terminal_outcomes_cannot_disagree(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        for status, outcome in (("complete", "failed"), ("failed", "complete"),
                                ("cancelled", "interrupted"), ("queued", "complete")):
            with self.subTest(status=status, outcome=outcome):
                with self.assertRaises(ValueError):
                    await store.finish(job_id, status, LATER, attempt_id=attempt.id, outcome=outcome)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("downloading",)])
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("running",)])


    async def test_recovery_write_failure_rolls_back_both_records(self):
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        self.execute("""CREATE TRIGGER reject_recovery BEFORE UPDATE ON download_attempts
                     WHEN NEW.status='interrupted' BEGIN SELECT RAISE(ABORT, 'recovery interrupted'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "recovery interrupted"):
            await store.recover(LATER)
        self.assertEqual(self.query("SELECT status, current_attempt_id FROM downloads"), [("downloading", attempt.id)])
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("running",)])
        self.execute("DROP TRIGGER reject_recovery")

    async def test_recovery_refuses_legacy_published_artifact_ownership(self):
        from backend.services import artifacts
        self.assertTrue(callable(getattr(artifacts, "append_journal", None)),
                        "Recovery needs publication records beyond the worker result")
        job_id = self.enqueue()
        store = self.store()
        attempt = await store.claim(job_id, NOW)
        artifacts.prepare_workspace(self.selected_runtime, attempt)
        output = self.selected_runtime.download_dir / "published বাংলা.mp3"
        output.write_bytes(b"media")
        artifacts.append_journal(self.selected_runtime, attempt.workspace, [
            {"source": str(attempt.workspace / "published.mp3"), "path": str(output),
             "kind": "media", "state": "complete", "size": 5}])
        await store.recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("failed", None)])
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("failed",)])
        self.assertEqual(self.query("SELECT path, state, size FROM download_artifacts"), [])
        self.assertEqual(output.read_bytes(), b"media")
