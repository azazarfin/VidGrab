"""Cookie bytes remain volatile; only verified file ownership survives a crash."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import aiosqlite
import httpx

from tests.support import OfflineTestCase

NOW = "2026-11-14T00:00:00+00:00"
LATER = "2026-11-14T00:01:00+00:00"
COOKIES = [{"domain": ".fixture.invalid", "path": "/", "secure": True,
            "expirationDate": 1900000000, "name": "session", "value": "fixture-secret বাংলা"}]


class Crash(BaseException):
    """Stop at an owned boundary without pretending a process was gracefully shut down."""


class CookieRecoveryTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))

        async def check_loop():
            await asyncio.sleep(0)
            loop.set_exception_handler(previous)
            self.assertEqual(self.loop_errors, [], "Cookie operations must retrieve joined task exceptions")

        self.addAsyncCleanup(check_loop)
        self.selected_runtime = self.runtime("cookie বাংলা")
        self.application = self.app(self.selected_runtime)
        context = self.application.router.lifespan_context(self.application)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.store = self.application.state.queue.attempts

    def query(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db, db:
            db.execute(sql, values)

    def service(self):
        module = importlib.import_module("backend.cookie_manager")
        self.assertTrue(callable(getattr(module, "create_cookie_file", None)),
                        "Cookie creation must persist attempt ownership before writing secrets")
        self.assertTrue(callable(getattr(self.store, "cleanup_cookies", None)),
                        "Cookie cleanup must pass durable owner and worker fences")
        return module

    async def attempt(self):
        from backend.services.artifacts import prepare_workspace
        job = await self.application.state.queue.enqueue("https://fixture.invalid/media", "best")
        attempt = await self.store.claim(job, NOW)
        prepare_workspace(self.selected_runtime, attempt)
        return attempt

    async def cookie(self):
        module = self.service()
        attempt = await self.attempt()
        path = Path(module.create_cookie_file(COOKIES, self.selected_runtime, attempt))
        return attempt, path

    def record(self, attempt):
        rows = self.query("SELECT ownership FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,))
        self.assertEqual(len(rows), 1)
        return json.loads(rows[0][0])

    def retained(self, attempt):
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,)), [(1,)])

    async def test_queue_cookie_is_private_attempt_metadata_before_execution(self):
        observed = {}

        def download(**options):
            path = Path(options["cookies_file_path"])
            self.assertTrue(path.is_relative_to(Path(options["attempt_dir"]) / ".vidgrab"),
                            "Forwarded cookies must live in the exclusive attempt metadata tree")
            observed["path"] = path
            with closing(sqlite3.connect(options["runtime"].db_path)) as db:
                records = db.execute("SELECT ownership FROM attempt_cookie_files").fetchall()
            self.assertEqual(len(records), 1, "Ownership must commit before cookie delivery")
            self.assertNotIn("fixture-secret", records[0][0])
            self.assertIn("fixture-secret বাংলা", path.read_text(encoding="utf-8"))
            output = Path(options["download_dir"]) / "complete.mp3"
            output.write_bytes(b"media")
            return {"filepath": str(output)}

        app = self.app(self.runtime("queue-cookie"), download_runner=download)
        async with app.router.lifespan_context(app):
            queue = app.state.queue
            job = await queue.enqueue("https://fixture.invalid/media", "best", cookies=COOKIES)
            config = await app.state.settings.get_all()
            config["max_retries"] = 0
            await queue._run_task(job, "https://fixture.invalid/media", "best", config, {})
            self.assertIn("path", observed, "The runner must receive the durably owned cookie")
            self.assertFalse(observed["path"].exists())
            with closing(sqlite3.connect(app.state.runtime.db_path)) as db:
                self.assertEqual(db.execute("SELECT status FROM downloads").fetchall(), [("complete",)])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_cookie_files").fetchone()[0], 0)
                self.assertNotIn("fixture-secret", db.execute("SELECT download_options FROM downloads").fetchone()[0])
            self.assertNotIn(b"fixture-secret", app.state.runtime.db_path.read_bytes())

    async def test_cookie_metadata_keeps_secrets_out_of_sqlite_and_media_artifacts(self):
        attempt, path = await self.cookie()
        record = self.record(attempt)
        self.assertNotIn("fixture-secret", json.dumps(record))
        self.assertNotIn(b"fixture-secret", self.selected_runtime.db_path.read_bytes())
        self.assertTrue(path.is_relative_to(attempt.workspace / ".vidgrab"))
        self.assertEqual(Path(record["witness"]).stat().st_ino, path.stat().st_ino)
        self.assertEqual(path.stat().st_nlink, 2)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        await self.store.recover(LATER)
        self.assertFalse(path.exists())
        self.assertEqual(self.query("SELECT COUNT(*) FROM download_artifacts"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_cookie_ownership_commits_while_both_file_names_are_empty(self):
        module = self.service()
        from backend.db import cookies as database
        attempt = await self.attempt()
        original = database.register_cookie
        observed = []

        def register(runtime, owned, ownership):
            self.assertEqual(Path(ownership["path"]).read_bytes(), b"")
            self.assertEqual(Path(ownership["witness"]).read_bytes(), b"")
            original(runtime, owned, ownership)
            self.retained(owned)
            observed.append(ownership)

        with patch.object(database, "register_cookie", register):
            path = Path(module.create_cookie_file(COOKIES, self.selected_runtime, attempt))
        self.assertEqual(len(observed), 1)
        self.assertIn("fixture-secret", path.read_text(encoding="utf-8"))

    async def test_crash_before_registration_leaves_only_empty_unowned_files(self):
        module = self.service()
        attempt = await self.attempt()
        observed = {}

        def crash(_runtime, _attempt, ownership):
            observed.update(ownership)
            raise Crash()

        with patch("backend.db.cookies.register_cookie", crash):
            with self.assertRaises(Crash):
                module.create_cookie_file(COOKIES, self.selected_runtime, attempt)
        path = Path(observed["path"])
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
        await self.store.recover(LATER)
        self.assertTrue(path.exists(), "Unregistered names are not cleanup authority")
        self.assertEqual(path.read_bytes(), b"")

    async def test_crash_after_registration_before_secret_write_is_recoverable(self):
        module = self.service()
        attempt = await self.attempt()
        with patch.object(module, "_write_cookie_bytes", side_effect=Crash()):
            with self.assertRaises(Crash):
                module.create_cookie_file(COOKIES, self.selected_runtime, attempt)
        record = self.record(attempt)
        self.assertEqual(Path(record["path"]).read_bytes(), b"")
        await self.store.recover(LATER)
        self.assertFalse(Path(record["path"]).exists())
        self.assertFalse(Path(record["witness"]).exists())
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_crash_during_secret_write_recovers_partial_cookie_bytes(self):
        module = self.service()
        attempt = await self.attempt()

        def crash(file, encoded):
            file.write(encoded[:-4])
            file.flush()
            os.fsync(file.fileno())
            raise Crash()

        with patch.object(module, "_write_cookie_bytes", crash):
            with self.assertRaises(Crash):
                module.create_cookie_file(COOKIES, self.selected_runtime, attempt)
        record = self.record(attempt)
        self.assertIn(b"fixture-secret", Path(record["path"]).read_bytes())
        await self.store.recover(LATER)
        self.assertFalse(Path(record["path"]).exists())
        self.assertFalse(Path(record["witness"]).exists())

    async def test_recovery_cleans_written_cookie_without_reusing_it_for_the_next_attempt(self):
        attempt, path = await self.cookie()
        unrelated = self.selected_runtime.temp_dir / "vidgrab_cookies_foreign.txt"
        unrelated.write_bytes(b"another credential")
        await self.store.recover(LATER)
        next_attempt = await self.store.claim(attempt.job_id, LATER)
        self.assertNotEqual(next_attempt.id, attempt.id)
        self.assertFalse(path.exists())
        self.assertEqual(unrelated.read_bytes(), b"another credential")
        self.assertEqual(self.application.state.queue.task_cookies, {})

    async def test_terminal_attempt_cookie_is_recovered_without_changing_history(self):
        attempt, path = await self.cookie()
        await self.store.finish(attempt.job_id, "failed", NOW, attempt_id=attempt.id, error_message="fixture")
        before = self.query("SELECT status, attempt_count, completed_at, error_message FROM downloads")
        await self.store.recover(LATER)
        self.assertFalse(path.exists())
        self.assertEqual(self.query("SELECT status, attempt_count, completed_at, error_message FROM downloads"), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_every_worker_fence_precedes_cleanup_of_any_attempt(self):
        from backend.runtime import RuntimeOwnershipError
        first, first_path = await self.cookie()
        second, second_path = await self.cookie()
        self.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?",
                     (self.application.state.owner.identity.to_json(), second.id))
        self.addCleanup(self.execute, "UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (second.id,))
        with patch("backend.cookie_manager.cleanup_cookie_file", side_effect=AssertionError("Cleanup preceded fencing")):
            with self.assertRaises(RuntimeOwnershipError):
                await self.store.recover(LATER)
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(self.query("SELECT status FROM download_attempts ORDER BY number"), [("running",), ("running",)])

    async def test_unverifiable_worker_identity_preserves_cookie_and_job_state(self):
        from backend.runtime import RuntimeOwnershipError
        attempt, path = await self.cookie()
        self.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?", ("{}", attempt.id))
        self.addCleanup(self.execute, "UPDATE download_attempts SET worker_identity=NULL WHERE id=?", (attempt.id,))
        with self.assertRaises(RuntimeOwnershipError):
            await self.store.cleanup_cookies(attempt)
        self.assertTrue(path.exists())
        self.retained(attempt)

    async def test_foreign_replacement_preserves_both_foreign_and_owned_cookie_bytes(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        saved = path.with_name("saved-original")
        path.rename(saved)
        path.write_bytes(b"foreign file")

        def restore():
            path.unlink()
            saved.rename(path)

        self.addCleanup(restore)
        with self.assertRaises(OutputPathError):
            await self.store.recover(LATER)
        self.assertEqual(path.read_bytes(), b"foreign file")
        self.assertIn(b"fixture-secret", saved.read_bytes())
        self.retained(attempt)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("downloading",)])

    async def test_additional_foreign_hardlink_refuses_cleanup(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        foreign = self.root / "foreign-hardlink"
        os.link(path, foreign)
        self.addCleanup(foreign.unlink)
        with self.assertRaises(OutputPathError):
            await self.store.cleanup_cookies(attempt)
        self.assertTrue(path.exists())
        self.assertIn(b"fixture-secret", foreign.read_bytes())
        self.retained(attempt)

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink fixture; Windows reparse checks are opt-in")
    async def test_cookie_symlink_is_preserved_without_reading_its_target(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        saved = path.with_name("saved-original")
        path.rename(saved)
        target = self.root / "foreign-target"
        target.write_bytes(b"foreign target")
        path.symlink_to(target)

        def restore():
            path.unlink()
            saved.rename(path)

        self.addCleanup(restore)
        with self.assertRaises(OutputPathError):
            await self.store.cleanup_cookies(attempt)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b"foreign target")

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink fixture; Windows reparse checks are opt-in")
    async def test_replaced_cookie_directory_is_preserved(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        directory = path.parent
        saved = directory.with_name("saved-directory")
        directory.rename(saved)
        target = self.root / "foreign-directory"
        target.mkdir()
        (target / path.name).write_bytes(b"foreign bytes")
        directory.symlink_to(target, target_is_directory=True)

        def restore():
            directory.unlink()
            saved.rename(directory)

        self.addCleanup(restore)
        with self.assertRaises(OutputPathError):
            await self.store.cleanup_cookies(attempt)
        self.assertEqual((target / path.name).read_bytes(), b"foreign bytes")
        self.assertIn(b"fixture-secret", (saved / path.name).read_bytes())

    async def test_worker_cookie_jar_rewrite_retains_verifiable_file_ownership(self):
        attempt, path = await self.cookie()
        record = self.record(attempt)
        path.write_text("# Netscape HTTP Cookie File\n.fixture.invalid\tTRUE\t/\tFALSE\t1900000000\tnew\tupdated\n",
                        encoding="utf-8")
        self.assertEqual(path.stat().st_ino, Path(record["witness"]).stat().st_ino)
        await self.store.cleanup_cookies(attempt)
        self.assertFalse(path.exists())
        self.assertFalse(Path(record["witness"]).exists())

    async def test_cleanup_crash_between_unlinks_is_idempotently_recovered(self):
        attempt, path = await self.cookie()
        witness = Path(self.record(attempt)["witness"])
        original = Path.unlink

        def crash(selected, *args, **kwargs):
            if selected == witness:
                raise Crash()
            return original(selected, *args, **kwargs)

        with patch.object(Path, "unlink", crash):
            with self.assertRaises(Crash):
                await self.store.cleanup_cookies(attempt)
        self.assertFalse(path.exists())
        self.assertTrue(witness.exists())
        self.retained(attempt)
        await self.store.recover(LATER)
        self.assertFalse(witness.exists())
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_cleanup_crash_after_unlinks_before_sqlite_delete_is_recoverable(self):
        attempt, path = await self.cookie()
        witness = Path(self.record(attempt)["witness"])
        original = aiosqlite.Connection.execute

        def crash(database, sql, *args, **kwargs):
            if sql.startswith("DELETE FROM attempt_cookie_files"):
                raise Crash()
            return original(database, sql, *args, **kwargs)

        with patch.object(aiosqlite.Connection, "execute", crash):
            with self.assertRaises(Crash):
                await self.store.cleanup_cookies(attempt)
        self.assertFalse(path.exists())
        self.assertFalse(witness.exists())
        self.retained(attempt)
        await self.store.recover(LATER)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_repeated_cancellation_joins_cleanup_before_transaction_rollback(self):
        module = self.service()
        attempt, path = await self.cookie()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = module.cleanup_cookie_file

        def held(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError("Cookie cleanup gate was not released")
            return original(*args)

        with patch.object(module, "cleanup_cookie_file", held):
            operation = asyncio.create_task(self.store.cleanup_cookies(attempt))
            try:
                await asyncio.wait_for(started.wait(), 2)
                for _ in range(2):
                    operation.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(operation.done())
                    self.assertTrue(path.exists())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
        self.assertFalse(path.exists())
        self.retained(attempt)
        await self.store.recover(LATER)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_retired_attempt_cannot_write_cookie_secrets(self):
        from backend.runtime import RuntimeOwnershipError
        module = self.service()
        attempt = await self.attempt()
        await self.store.finish(attempt.job_id, "failed", NOW, attempt_id=attempt.id)
        with self.assertRaises(RuntimeOwnershipError):
            module.create_cookie_file(COOKIES, self.selected_runtime, attempt)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
        self.assertFalse(any(b"fixture-secret" in path.read_bytes() for path in attempt.workspace.rglob("*") if path.is_file()))

    async def test_worker_delivery_refuses_a_foreign_cookie_path(self):
        from backend.db.ownership import register_worker
        from backend.engine.process_identity import ProcessIdentity
        from backend.runtime import RuntimeOwnershipError
        attempt, path = await self.cookie()
        foreign = self.root / "foreign-cookie.txt"
        foreign.write_bytes(b"foreign credential")
        identity = ProcessIdentity.capture()
        with self.assertRaises(RuntimeOwnershipError):
            register_worker(self.selected_runtime, attempt.id, attempt.owner_id, attempt.job_id,
                            str(attempt.workspace), identity, identity, cookies_file_path=str(foreign))
        self.assertEqual(foreign.read_bytes(), b"foreign credential")
        self.assertTrue(path.exists())
        self.assertEqual(self.query("SELECT worker_identity FROM download_attempts"), [(None,)])

    async def test_worker_delivery_refuses_a_cookie_with_no_durable_record(self):
        from backend.db.ownership import register_worker
        from backend.engine.process_identity import ProcessIdentity
        from backend.runtime import RuntimeOwnershipError
        attempt, path = await self.cookie()
        encoded = self.query("SELECT ownership FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,))[0][0]
        self.execute("DELETE FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,))
        self.addCleanup(self.execute, "INSERT INTO attempt_cookie_files (attempt_id, ownership) VALUES (?, ?)",
                        (attempt.id, encoded))
        identity = ProcessIdentity.capture()
        with self.assertRaises(RuntimeOwnershipError):
            register_worker(self.selected_runtime, attempt.id, attempt.owner_id, attempt.job_id,
                            str(attempt.workspace), identity, identity, cookies_file_path=str(path))
        self.assertTrue(path.exists())
        self.assertEqual(self.query("SELECT worker_identity FROM download_attempts"), [(None,)])

    async def test_a_second_cookie_cannot_replace_an_attempt_ownership_record(self):
        from backend.runtime import RuntimeOwnershipError
        module = self.service()
        attempt, path = await self.cookie()
        before = self.record(attempt)
        with self.assertRaises(RuntimeOwnershipError):
            module.create_cookie_file(COOKIES, self.selected_runtime, attempt)
        self.assertEqual(self.record(attempt), before)
        self.assertIn(b"fixture-secret", path.read_bytes())

    async def test_cookie_field_injection_is_refused_without_persisting_secret_bytes(self):
        module = self.service()
        attempt = await self.attempt()
        for field in ("domain", "path", "name", "value"):
            for value in ("secret\nforeign\trow", "secret\0", ["secret"]):
                with self.subTest(field=field, value_type=type(value).__name__):
                    cookies = [{**COOKIES[0], field: value}]
                    with self.assertRaisesRegex(ValueError, "cookie|Cookie"):
                        module.create_cookie_file(cookies, self.selected_runtime, attempt)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
        self.assertFalse(list(attempt.workspace.rglob("cookies.txt")))

    async def test_malformed_cookie_ownership_cannot_select_a_foreign_file(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        original = self.query("SELECT ownership FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,))[0][0]
        record = json.loads(original)
        foreign = self.root / "foreign-secret"
        foreign.write_bytes(b"foreign")
        record["path"] = str(foreign)
        self.execute("UPDATE attempt_cookie_files SET ownership=? WHERE attempt_id=?", (json.dumps(record), attempt.id))
        self.addCleanup(self.execute, "UPDATE attempt_cookie_files SET ownership=? WHERE attempt_id=?", (original, attempt.id))
        with self.assertRaises(OutputPathError):
            await self.store.cleanup_cookies(attempt)
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertTrue(path.exists())

    async def test_history_deletion_preserves_pending_cookie_ownership(self):
        attempt, path = await self.cookie()
        await self.store.finish(attempt.job_id, "failed", NOW, attempt_id=attempt.id)
        transport = httpx.ASGITransport(app=self.application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            self.assertEqual((await client.delete(f"/downloads/history/{attempt.job_id}")).status_code, 409)
            self.assertEqual((await client.delete("/downloads/history")).status_code, 200)
            self.retained(attempt)
            self.assertTrue(path.exists())
            await self.store.cleanup_cookies(attempt)
            self.assertEqual((await client.delete(f"/downloads/history/{attempt.job_id}")).status_code, 200)

    async def test_schema_four_upgrade_adds_empty_cookie_ownership_without_inventing_credentials(self):
        from backend.config import init_db
        selected = self.runtime("old-cookie-schema")
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db, db:
            db.execute("DROP TABLE IF EXISTS attempt_cookie_files")
            db.execute("PRAGMA user_version=4")
            db.execute("INSERT INTO downloads (id, url, status, created_at) VALUES ('history', 'fixture', 'complete', ?)", (NOW,))
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5,
                             "Cookie ownership needs an explicit transactional schema migration")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_cookie_files").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status, attempt_count FROM downloads").fetchall(), [("complete", 0)])
        self.assertEqual(len(list(selected.backups_dir.glob("*.sqlite3"))), 1)

    async def test_schema_five_creation_rolls_back_with_failed_settings_validation(self):
        from backend.config import init_db
        selected = self.runtime("interrupted-cookie-schema")
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db, db:
            db.execute("DROP TABLE IF EXISTS attempt_cookie_files")
            db.execute("PRAGMA user_version=4")
            db.execute("DELETE FROM config WHERE key='max_retries'")
            db.execute("""CREATE TRIGGER interrupt_cookie_migration BEFORE INSERT ON config
                WHEN NEW.key='max_retries' BEGIN SELECT RAISE(ABORT, 'cookie migration interrupted'); END""")
        before = selected.db_path.read_bytes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cookie migration interrupted"):
            await init_db(selected)
        self.assertEqual(selected.db_path.read_bytes(), before)
        with closing(sqlite3.connect(selected.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='attempt_cookie_files'").fetchall(), [])

    async def test_queue_cookie_refusal_finishes_failed_without_automatic_retry_or_unhandled_error(self):
        selected = self.runtime("foreign-queue-cookie")
        observed = {}

        def download(**options):
            path = Path(options["cookies_file_path"])
            saved = path.with_name("saved-original")
            path.rename(saved)
            path.write_bytes(b"foreign bytes")
            observed.update(path=path, saved=saved)
            output = selected.download_dir / "fixture.mp3"
            output.write_bytes(b"media")
            return {"filepath": str(output)}

        app = self.app(selected, download_runner=download)
        async with app.router.lifespan_context(app):
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best", cookies=COOKIES)
            try:
                with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                    try:
                        await app.state.queue._run_task(job, "https://fixture.invalid/media", "best",
                                                      await app.state.settings.get_all(), {})
                    except Exception as error:
                        self.fail(f"Cookie refusal must persist failure and retrieve execution: {type(error).__name__}")
                with closing(sqlite3.connect(selected.db_path)) as db:
                    self.assertEqual(db.execute("SELECT status, retry_count FROM downloads").fetchall(), [("failed", 0)])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_cookie_files").fetchone()[0], 1)
                self.assertEqual(observed["path"].read_bytes(), b"foreign bytes")
                self.assertIn(b"fixture-secret", observed["saved"].read_bytes())
            finally:
                if observed:
                    observed["path"].unlink()
                    observed["saved"].rename(observed["path"])

    async def test_cancellation_during_cookie_creation_joins_the_writer_before_cleanup(self):
        module = self.service()
        from backend.db import cookies as database
        selected = self.runtime("cancel-creation")
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        observed = {}
        original = database.register_cookie

        def held(runtime, attempt, ownership):
            original(runtime, attempt, ownership)
            observed.update(ownership)
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError("Cookie writer gate was not released")

        def download(**_options):
            self.fail("Cancelled creation must never deliver cookies to execution")

        app = self.app(selected, download_runner=download)
        async with app.router.lifespan_context(app):
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best", cookies=COOKIES)
            with patch.object(database, "register_cookie", held):
                operation = asyncio.create_task(app.state.queue._run_task(
                    job, "https://fixture.invalid/media", "best", await app.state.settings.get_all(), {}))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    for _ in range(2):
                        operation.cancel()
                        await asyncio.sleep(0.01)
                        self.assertFalse(operation.done())
                        self.assertTrue(Path(observed["path"]).exists())
                finally:
                    release.set()
                    await operation
            self.assertFalse(Path(observed["path"]).exists())
            self.assertFalse(Path(observed["witness"]).exists())
            with closing(sqlite3.connect(selected.db_path)) as db:
                self.assertEqual(db.execute("SELECT status FROM downloads").fetchall(), [("queued",)])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_cookie_files").fetchone()[0], 0)

    async def test_unready_cookie_is_refused_at_worker_delivery(self):
        from backend.db.ownership import register_worker
        from backend.engine.process_identity import ProcessIdentity
        from backend.runtime import RuntimeOwnershipError
        attempt, path = await self.cookie()
        self.execute("UPDATE attempt_cookie_files SET ready=0 WHERE attempt_id=?", (attempt.id,))
        identity = ProcessIdentity.capture()
        with self.assertRaises(RuntimeOwnershipError):
            register_worker(self.selected_runtime, attempt.id, attempt.owner_id, attempt.job_id,
                            str(attempt.workspace), identity, identity, cookies_file_path=str(path))
        self.assertTrue(path.exists())
        self.assertEqual(self.query("SELECT worker_identity FROM download_attempts"), [(None,)])

    async def test_startup_cancellation_joins_cookie_recovery_before_rollback(self):
        module = self.service()
        attempt, path = await self.cookie()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = module.cleanup_cookie_file

        def held(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError("Cookie recovery gate was not released")
            return original(*args)

        with patch.object(module, "cleanup_cookie_file", held):
            operation = asyncio.create_task(self.store.recover(LATER))
            try:
                await asyncio.wait_for(started.wait(), 2)
                for _ in range(2):
                    operation.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(operation.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
        self.assertFalse(path.exists())
        self.retained(attempt)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("downloading",)])
        await self.store.recover(LATER)

    async def test_invalid_expiration_and_oversized_cookie_inputs_fail_before_creating_secret_files(self):
        module = self.service()
        attempt = await self.attempt()
        for expiration in (True, "secret", float("nan"), float("inf"), -1, 10 ** 1000):
            with self.subTest(expiration_type=type(expiration).__name__):
                try:
                    module.create_cookie_file([{**COOKIES[0], "expirationDate": expiration}], self.selected_runtime, attempt)
                except ValueError as error:
                    self.assertIn("Cookie", str(error))
                except Exception as error:
                    self.fail(f"Invalid expiration must report a bounded cookie error: {type(error).__name__}")
                else:
                    self.fail("Invalid expiration was accepted")
        with self.assertRaisesRegex(ValueError, "Cookie"):
            module.create_cookie_file([{**COOKIES[0], "value": "x" * (1024 * 1024)}], self.selected_runtime, attempt)
        with self.assertRaisesRegex(ValueError, "Cookie"):
            module.create_cookie_file(COOKIES * 4097, self.selected_runtime, attempt)
        self.assertIsNone(module.create_cookie_file([], self.selected_runtime, attempt))
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
        self.assertFalse(list(attempt.workspace.rglob("cookies.txt")))

    async def test_replaced_witness_with_identical_bytes_is_preserved_and_refuses_cleanup(self):
        from backend.runtime import OutputPathError
        attempt, path = await self.cookie()
        witness = Path(self.record(attempt)["witness"])
        saved = witness.with_name("saved-witness")
        witness.rename(saved)
        witness.write_bytes(saved.read_bytes())

        def restore():
            witness.unlink()
            saved.rename(witness)

        self.addCleanup(restore)
        with self.assertRaises(OutputPathError):
            await self.store.cleanup_cookies(attempt)
        self.assertTrue(path.exists())
        self.assertEqual(witness.read_bytes(), saved.read_bytes())
        self.retained(attempt)

    async def test_cookie_ownership_requires_a_canonical_attempt_uuid_before_path_checks(self):
        from backend.db.jobs import Attempt
        from backend.runtime import OutputPathError
        module = self.service()
        attempt, path = await self.cookie()
        record = self.record(attempt)
        invalid_id = "../foreign"
        workspace = self.selected_runtime.data_dir / "attempts" / invalid_id
        invalid = Attempt(invalid_id, attempt.job_id, 1, workspace, attempt.owner_id)
        metadata = workspace / ".vidgrab"
        directory = metadata / ("cookies-" + record["cookie_id"])
        record.update(attempt_id=invalid_id, path=str(directory / "cookies.txt"), witness=str(directory / "cookies.witness"))
        for entry, selected in zip(record["directories"],
                                   (self.selected_runtime.data_dir, workspace.parent, workspace, metadata, directory)):
            entry["path"] = str(selected)
        with self.assertRaises(OutputPathError):
            module.validate_cookie_record(self.selected_runtime, invalid, record)
        self.assertTrue(path.exists())
