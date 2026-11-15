"""Budget schema upgrades preserve prior history and roll back invalid state."""

import asyncio
from contextlib import closing
import sqlite3

from backend.config import init_db
from tests.support import OfflineTestCase, drop_protocol_schema


class RequestMigrationTests(OfflineTestCase):
    def schema_seven(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            drop_protocol_schema(db)
            for table in ("request_leases", "request_probes", "request_hosts"):
                db.execute(f"DROP TABLE {table}")
            db.execute("DROP INDEX retry_deadlines")
            for column in ("retry_at", "error_code", "request_count"):
                db.execute(f"ALTER TABLE downloads DROP COLUMN {column}")
            db.execute("PRAGMA user_version=7")
            db.execute("INSERT INTO downloads(id,url,status,created_at) VALUES ('history','fixture','complete','now')")
        return runtime

    def test_schema_seven_upgrade_preserves_history_and_zero_spent_requests(self):
        runtime = self.schema_seven()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("SELECT id,status,retry_at,error_code,request_count FROM downloads").fetchall(),
                             [("history", "complete", None, None, 0)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM request_leases").fetchone()[0], 0)

    def test_schema_eight_settings_failure_rolls_back_every_budget_table(self):
        runtime = self.schema_seven()
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DELETE FROM config WHERE key='max_retries'")
            db.execute("CREATE TRIGGER reject_defaults BEFORE INSERT ON config BEGIN SELECT RAISE(ABORT, 'fixture budget'); END")
        before = runtime.db_path.read_bytes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture budget"):
            asyncio.run(init_db(runtime))
        self.assertEqual(runtime.db_path.read_bytes(), before)

    def test_missing_request_host_index_is_refused_without_mutation(self):
        runtime = self.runtime()
        asyncio.run(init_db(runtime))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP INDEX request_host_leases")
        before = runtime.db_path.read_bytes()
        with self.assertRaisesRegex(Exception, "request.*index"):
            asyncio.run(init_db(runtime))
        self.assertEqual(runtime.db_path.read_bytes(), before)
