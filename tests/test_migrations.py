import asyncio
import hashlib
import gc
import json
import sqlite3
import warnings
from contextlib import closing

from backend.db.migrations import SCHEMA_VERSION

from tests.support import OfflineTestCase


def legacy_database(path, version=0, config=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE downloads (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT,
                thumbnail_url TEXT, status TEXT NOT NULL, progress REAL DEFAULT 0,
                speed REAL DEFAULT 0, eta REAL, file_size INTEGER, file_path TEXT,
                format_selected TEXT, error_message TEXT, created_at TEXT NOT NULL,
                completed_at TEXT
            );
            INSERT INTO downloads (id, url, status, format_selected, created_at)
            VALUES ('legacy-job', 'https://fixture.invalid/media', 'queued', 'best',
                    '2026-11-03T00:00:00+00:00');
        """)
        db.execute(f"PRAGMA user_version = {version}")
        for key, value in (config or {}).items():
            db.execute("INSERT INTO config VALUES (?, ?)", (key, json.dumps(value)))


class MigrationTests(OfflineTestCase):
    def initialize(self, runtime):
        from backend.config import init_db

        asyncio.run(init_db(runtime))

    def test_empty_database_gets_versioned_schema_and_runtime_defaults(self):
        runtime = self.runtime()
        self.initialize(runtime)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            value = db.execute("SELECT value FROM config WHERE key = 'download_dir'").fetchone()[0]
            self.assertEqual(json.loads(value), str(runtime.download_dir))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 0)

    def test_each_previous_schema_retains_jobs_and_settings(self):
        for version in (0, 1):
            with self.subTest(version=version):
                runtime = self.runtime(f"version-{version}")
                legacy_database(runtime.db_path, version, {"max_concurrent": 5})
                self.initialize(runtime)
                with closing(sqlite3.connect(runtime.db_path)) as db, db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    job = db.execute("SELECT id, status, retry_count FROM downloads").fetchone()
                    self.assertEqual(job, ("legacy-job", "queued", 0))
                    value = db.execute("SELECT value FROM config WHERE key = 'max_concurrent'").fetchone()[0]
                    self.assertEqual(json.loads(value), 5)
                self.assertEqual(len(list(runtime.backups_dir.glob("*.sqlite3"))), 1)

    def test_current_schema_initialization_is_idempotent(self):
        runtime = self.runtime()
        self.initialize(runtime)
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        self.initialize(runtime)
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        runtime = self.runtime()
        legacy_database(runtime.db_path)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("""CREATE TRIGGER interrupt_defaults BEFORE INSERT ON config
                WHEN NEW.key = 'max_retries' BEGIN
                SELECT RAISE(ABORT, 'simulated migration interruption'); END""")
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(Exception, "simulated migration interruption"):
            self.initialize(runtime)
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn("retry_count", {row[1] for row in db.execute("PRAGMA table_info(downloads)")})
            db.execute("DROP TRIGGER interrupt_defaults")
        self.initialize(runtime)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_invalid_legacy_settings_fail_without_mutating_the_database(self):
        runtime = self.runtime()
        legacy_database(runtime.db_path, config={"max_concurrent": None})
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(Exception, "max_concurrent"):
            self.initialize(runtime)
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
        self.assertTrue(list(runtime.backups_dir.glob("*.sqlite3")))

    def test_newer_schema_is_rejected_before_mutation_or_backup(self):
        runtime = self.runtime()
        legacy_database(runtime.db_path, version=99)
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        with self.assertRaisesRegex(Exception, "newer|unsupported"):
            self.initialize(runtime)
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)
        self.assertFalse(runtime.backups_dir.exists())

    def test_selected_legacy_import_preserves_source_and_migrates_a_copy(self):
        runtime = self.runtime()
        source = self.root / "legacy.sqlite3"
        legacy_database(source, config={"max_concurrent": 4})
        before = hashlib.sha256(source.read_bytes()).digest()
        from backend.db.migrations import import_legacy_database

        result = asyncio.run(import_legacy_database(source, runtime))
        self.assertTrue(result.backup_path.is_file())
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT id FROM downloads").fetchone()[0], "legacy-job")

    def test_legacy_import_refuses_to_overwrite_existing_state(self):
        runtime = self.runtime()
        self.initialize(runtime)
        source = self.root / "legacy.sqlite3"
        legacy_database(source)
        before = hashlib.sha256(runtime.db_path.read_bytes()).digest()
        from backend.db.migrations import import_legacy_database

        with self.assertRaises(FileExistsError):
            asyncio.run(import_legacy_database(source, runtime))
        self.assertEqual(hashlib.sha256(runtime.db_path.read_bytes()).digest(), before)

    def test_legacy_snapshot_closes_its_database_handles(self):
        runtime = self.runtime()
        source = self.root / "legacy.sqlite3"
        legacy_database(source)
        from backend.db.migrations import import_legacy_database

        gc.collect()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            asyncio.run(import_legacy_database(source, runtime))
            gc.collect()
        leaked = [str(warning.message) for warning in captured if "unclosed database" in str(warning.message)]
        self.assertEqual(leaked, [])

    def test_legacy_import_includes_committed_wal_data(self):
        runtime = self.runtime()
        source = self.root / "legacy.sqlite3"
        legacy_database(source)
        from backend.db.migrations import import_legacy_database

        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE downloads SET title='Committed WAL fixture'")
            writer.commit()
            before = hashlib.sha256(source.read_bytes()).digest()
            asyncio.run(import_legacy_database(source, runtime))
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
            with closing(sqlite3.connect(runtime.db_path)) as copied:
                self.assertEqual(copied.execute("SELECT title FROM downloads").fetchone()[0], "Committed WAL fixture")

    def test_invalid_import_retains_backup_without_publishing_state(self):
        runtime = self.runtime()
        source = self.root / "legacy.sqlite3"
        legacy_database(source, config={"max_concurrent": None})
        from backend.db.migrations import import_legacy_database

        with self.assertRaisesRegex(Exception, "max_concurrent"):
            asyncio.run(import_legacy_database(source, runtime))
        self.assertFalse(runtime.db_path.exists())
        self.assertTrue(list(runtime.backups_dir.glob("legacy-*.sqlite3")))

    def test_import_checks_output_paths_against_final_not_staging_storage(self):
        runtime = self.runtime()
        source = self.root / "legacy.sqlite3"
        legacy_database(source, config={"filename_template": str(runtime.db_path)})
        before = hashlib.sha256(source.read_bytes()).digest()
        from backend.db.migrations import import_legacy_database

        with self.assertRaises(ValueError):
            asyncio.run(import_legacy_database(source, runtime))
        self.assertFalse(runtime.db_path.exists())
        self.assertTrue(list(runtime.backups_dir.glob("legacy-*.sqlite3")))
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_pre_migration_backups_verify_integrity_and_enforce_retention(self):
        import os
        from backend.db.migrations import MAX_PRE_MIGRATION_BACKUPS

        runtime = self.runtime("retention")
        runtime.prepare()
        # Seed 4 older schema backups with distinct mtimes.
        for idx in range(4):
            old = runtime.backups_dir / f"schema-v1-000000000{idx}-{idx:032x}.sqlite3"
            legacy_database(old, version=1)
            os.utime(old, (1000 + idx, 1000 + idx))

        legacy_database(runtime.db_path, version=1, config={"max_concurrent": 4})
        self.initialize(runtime)

        retained = sorted(p.name for p in runtime.backups_dir.glob("schema-v*.sqlite3"))
        self.assertEqual(len(retained), MAX_PRE_MIGRATION_BACKUPS)
        self.assertFalse(any("-0000000000-" in name or "-0000000001-" in name for name in retained))
        self.assertTrue(any("-0000000002-" in name for name in retained))
        self.assertTrue(any("-0000000003-" in name for name in retained))
        for path in runtime.backups_dir.glob("schema-v*.sqlite3"):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA quick_check").fetchall(), [("ok",)])

    def test_symlinked_backup_directory_or_database_is_refused(self):
        import os
        if os.name == "nt":
            self.skipTest("POSIX symlink test")
        runtime = self.runtime("symlink-backup")
        runtime.prepare()
        target = self.root / "outside.sqlite3"
        legacy_database(target, version=1)
        runtime.db_path.symlink_to(target)
        with self.assertRaisesRegex(Exception, "symlink"):
            self.initialize(runtime)
