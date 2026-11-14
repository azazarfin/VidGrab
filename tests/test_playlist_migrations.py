"""Schema seven adds bounded child authority without fabricating past jobs."""

import asyncio
import json
import sqlite3
from contextlib import closing

from backend.config import init_db
from tests.support import OfflineTestCase


class PlaylistMigrationTests(OfflineTestCase):
    def schema_six(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP TABLE enqueue_receipts")
            db.execute("DROP INDEX playlist_positions")
            db.execute("DROP TRIGGER playlist_parent_insert")
            db.execute("DROP TRIGGER playlist_parent_update")
            for column in ("playlist_index", "parent_id", "job_kind"):
                db.execute(f"ALTER TABLE downloads DROP COLUMN {column}")
            db.execute("PRAGMA user_version=6")
            for job, status in (("unfinished", "queued"), ("history", "complete")):
                db.execute("INSERT INTO downloads (id,url,status,created_at,download_options) VALUES (?,?,?,?,?)",
                           (job, "https://fixture.invalid/list", status, "2026-11-14T00:00:00+00:00", json.dumps({"is_playlist": True})))
        return runtime

    def test_schema_six_upgrade_preserves_history_and_only_marks_unfinished_selection(self):
        runtime = self.schema_six()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            from backend.db.migrations import SCHEMA_VERSION
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT id,job_kind,parent_id,attempt_count FROM downloads ORDER BY id").fetchall(),
                             [("history", "media", None, 0), ("unfinished", "playlist", None, 0)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM enqueue_receipts").fetchone()[0], 0)
        backups = list(runtime.backups_dir.glob("schema-v6-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_failed_schema_seven_migration_rolls_back_every_new_record(self):
        runtime = self.schema_six()
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DELETE FROM config WHERE key='max_retries'")
            db.execute("CREATE TRIGGER reject_defaults BEFORE INSERT ON config BEGIN SELECT RAISE(ABORT, 'migration fixture'); END")
        before = runtime.db_path.read_bytes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "migration fixture"):
            asyncio.run(init_db(runtime))
        self.assertEqual(runtime.db_path.read_bytes(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertNotIn("job_kind", {row[1] for row in db.execute("PRAGMA table_info(downloads)")})

    def test_child_without_a_position_is_rejected_by_the_database(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("INSERT INTO downloads(id,url,status,created_at,job_kind) VALUES ('parent','fixture','queued','now','playlist')")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO downloads(id,url,status,created_at,parent_id) VALUES ('child','fixture','queued','now','parent')")

    def test_missing_position_index_is_refused_without_mutation(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP INDEX playlist_positions")
        before = runtime.db_path.read_bytes()
        with self.assertRaisesRegex(Exception, "playlist.*index"):
            asyncio.run(init_db(runtime))
        self.assertEqual(runtime.db_path.read_bytes(), before)
