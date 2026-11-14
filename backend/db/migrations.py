"""Transactional migrations with snapshots; never migrate the selected import source."""

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Callable
from uuid import uuid4

import aiosqlite

from backend.runtime import RuntimeCompatibilityError, RuntimeConfig

SCHEMA_VERSION = 8
BASE_COLUMNS = {
    "id", "url", "title", "thumbnail_url", "status", "progress", "speed", "eta",
    "file_size", "file_path", "format_selected", "error_message", "created_at", "completed_at",
}
EXTRA_COLUMNS = {
    "download_options": "TEXT", "scheduled_at": "TEXT", "retry_count": "INTEGER DEFAULT 0",
}
ATTEMPT_COLUMNS = {
    "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)",
    "current_attempt_id": "TEXT",
}
AUTH_COLUMNS = {"auth_required": "INTEGER NOT NULL DEFAULT 0 CHECK (auth_required IN (0, 1))"}
PLAYLIST_COLUMNS = {
    "job_kind": "TEXT NOT NULL DEFAULT 'media' CHECK (job_kind IN ('media', 'playlist'))",
    "parent_id": "TEXT REFERENCES downloads(id) ON DELETE RESTRICT",
    "playlist_index": "INTEGER CHECK ((parent_id IS NULL AND playlist_index IS NULL) OR "
                      "(parent_id IS NOT NULL AND playlist_index IS NOT NULL AND typeof(playlist_index)='integer' "
                      "AND playlist_index BETWEEN 1 AND 4096 AND job_kind='media'))",
}
REQUEST_COLUMNS = {
    "retry_at": "REAL CHECK (retry_at IS NULL OR (typeof(retry_at) IN ('real','integer') AND retry_at BETWEEN 0 AND 253402300799))",
    "error_code": "TEXT CHECK (error_code IS NULL OR length(error_code) <= 64)",
    "request_count": "INTEGER NOT NULL DEFAULT 0 CHECK (typeof(request_count)='integer' AND request_count >= 0)",
}
OWNERSHIP_COLUMNS = {
    "owner_id": "TEXT REFERENCES runtime_owners(id)",
    "lease_expires_at": "REAL",
    "worker_owner_identity": "TEXT",
    "worker_identity": "TEXT",
}
RELATED_COLUMNS = {
    "download_attempts": {"id", "download_id", "number", "status", "workspace", "started_at", "finished_at", "error_message"} | OWNERSHIP_COLUMNS.keys(),
    "download_artifacts": {"attempt_id", "path", "kind", "state", "size", "updated_at"},
    "runtime_owners": {"id", "identity", "acquired_at", "heartbeat_at", "lease_expires_at", "released_at"},
    "attempt_cookie_files": {"attempt_id", "ownership", "ready"},
    "attempt_partials": {"attempt_id", "source_key", "record"},
    "enqueue_receipts": {"key_hash", "request_hash", "job_id", "created_at"},
    "request_hosts": {"host_key", "retry_at"},
    "request_probes": {"id", "owner_id", "identity", "host_key", "request_count", "started_at"},
    "request_leases": {"id", "owner_id", "attempt_id", "probe_id", "identity", "host_key", "created_at"},
}


def _snapshot(source: Path, destination: Path):
    """SQLite's backup API includes committed WAL data, unlike a plain file copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as backup:
            original.backup(backup)


async def schema_version(path: Path) -> int:
    async with aiosqlite.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        async with db.execute("PRAGMA user_version") as cursor:
            return (await cursor.fetchone())[0]


def _check_version(version: int):
    if version < 0 or version > SCHEMA_VERSION:
        raise RuntimeCompatibilityError(
            f"Database schema {version} is newer than or unsupported by this engine "
            f"(maximum {SCHEMA_VERSION}); install a compatible build"
        )


async def _migration_one(db):
    await db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, thumbnail_url TEXT,
            status TEXT NOT NULL, progress REAL DEFAULT 0, speed REAL DEFAULT 0,
            eta REAL, file_size INTEGER, file_path TEXT, format_selected TEXT,
            error_message TEXT, created_at TEXT NOT NULL, completed_at TEXT
        )
    """)


async def _migration_two(db):
    async with db.execute("PRAGMA table_info(downloads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    for column, definition in EXTRA_COLUMNS.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")


async def _migration_three(db):
    for column, definition in ATTEMPT_COLUMNS.items():
        await db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")
    await db.execute("""
        CREATE TABLE download_attempts (
            id TEXT PRIMARY KEY, download_id TEXT NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
            number INTEGER NOT NULL CHECK (number > 0),
            status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed', 'cancelled', 'interrupted')),
            workspace TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error_message TEXT,
            UNIQUE (download_id, number)
        )
    """)
    await db.execute("""CREATE UNIQUE INDEX one_running_attempt ON download_attempts(download_id)
                        WHERE status='running'""")
    await db.execute("""
        CREATE TABLE download_artifacts (
            attempt_id TEXT NOT NULL REFERENCES download_attempts(id) ON DELETE CASCADE,
            path TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('media', 'subtitle', 'thumbnail', 'auxiliary')),
            state TEXT NOT NULL CHECK (state IN ('partial', 'complete')),
            size INTEGER CHECK (size >= 0), updated_at TEXT NOT NULL,
            PRIMARY KEY (attempt_id, path)
        )
    """)
    await db.execute("CREATE INDEX runnable_downloads ON downloads(status, created_at)")


async def _migration_four(db):
    await db.execute("""CREATE TABLE runtime_owners (
        id TEXT PRIMARY KEY, identity TEXT NOT NULL, acquired_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL, lease_expires_at REAL NOT NULL, released_at REAL
    )""")
    await db.execute("CREATE UNIQUE INDEX one_active_runtime_owner ON runtime_owners((1)) WHERE released_at IS NULL")
    for column, definition in OWNERSHIP_COLUMNS.items():
        await db.execute(f"ALTER TABLE download_attempts ADD COLUMN {column} {definition}")


async def _migration_five(db):
    await db.execute("""CREATE TABLE attempt_cookie_files (
        attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES download_attempts(id) ON DELETE RESTRICT,
        ownership TEXT NOT NULL CHECK (length(CAST(ownership AS BLOB)) <= 8192),
        ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1))
    )""")


async def _migration_six(db):
    for column, definition in AUTH_COLUMNS.items():
        await db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")
    await db.execute("""CREATE TABLE attempt_partials (
        attempt_id TEXT NOT NULL REFERENCES download_attempts(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL CHECK (length(source_key)=64),
        record TEXT NOT NULL CHECK (length(CAST(record AS BLOB)) <= 4096),
        PRIMARY KEY (attempt_id, source_key)
    )""")


async def _migration_seven(db):
    async with db.execute("PRAGMA table_info(downloads)") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    for column, definition in PLAYLIST_COLUMNS.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")
    await db.execute("""CREATE TABLE IF NOT EXISTS enqueue_receipts (
        key_hash TEXT PRIMARY KEY NOT NULL CHECK (length(key_hash)=64),
        request_hash TEXT NOT NULL CHECK (length(request_hash)=64),
        job_id TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS playlist_positions ON downloads(parent_id, playlist_index) WHERE parent_id IS NOT NULL")
    await db.execute("""CREATE TRIGGER IF NOT EXISTS playlist_parent_insert BEFORE INSERT ON downloads
        WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM downloads WHERE id=NEW.parent_id AND job_kind='playlist' AND parent_id IS NULL)
        BEGIN SELECT RAISE(ABORT, 'Invalid playlist parent'); END""")
    await db.execute("""CREATE TRIGGER IF NOT EXISTS playlist_parent_update BEFORE UPDATE OF parent_id, job_kind ON downloads
        WHEN (NEW.parent_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM downloads WHERE id=NEW.parent_id AND job_kind='playlist' AND parent_id IS NULL))
        OR (NEW.job_kind<>'playlist' AND EXISTS (SELECT 1 FROM downloads WHERE parent_id=NEW.id))
        BEGIN SELECT RAISE(ABORT, 'Invalid playlist parent'); END""")
    async with db.execute("SELECT id, download_options FROM downloads WHERE status NOT IN ('complete', 'failed', 'cancelled') AND parent_id IS NULL") as cursor:
        rows = await cursor.fetchall()
    for job_id, raw in rows:
        try:
            options = json.loads(raw or "{}")
            if not isinstance(options, dict):
                raise ValueError("expected an object")
        except (ValueError, TypeError) as error:
            raise RuntimeCompatibilityError(f"Invalid download_options for job {job_id}") from error
        if options.get("is_playlist") or options.get("playlist_items"):
            await db.execute("UPDATE downloads SET job_kind='playlist' WHERE id=?", (job_id,))


async def _migration_eight(db):
    async with db.execute("PRAGMA table_info(downloads)") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    for column, definition in REQUEST_COLUMNS.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")
    await db.execute("CREATE INDEX IF NOT EXISTS retry_deadlines ON downloads(retry_at) WHERE status='retry_wait'")
    await db.execute("""CREATE TABLE IF NOT EXISTS request_hosts (
        host_key TEXT PRIMARY KEY NOT NULL CHECK (length(host_key)=64),
        retry_at REAL NOT NULL CHECK (retry_at BETWEEN 0 AND 253402300799))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS request_probes (
        id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL REFERENCES runtime_owners(id) ON DELETE RESTRICT,
        identity TEXT NOT NULL CHECK (length(CAST(identity AS BLOB)) <= 8192),
        host_key TEXT NOT NULL CHECK (length(host_key)=64),
        request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count BETWEEN 0 AND 64), started_at REAL NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS request_leases (
        id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL REFERENCES runtime_owners(id) ON DELETE RESTRICT,
        attempt_id TEXT REFERENCES download_attempts(id) ON DELETE RESTRICT,
        probe_id TEXT REFERENCES request_probes(id) ON DELETE RESTRICT,
        identity TEXT NOT NULL CHECK (length(CAST(identity AS BLOB)) <= 8192),
        host_key TEXT NOT NULL CHECK (length(host_key)=64), created_at REAL NOT NULL,
        CHECK ((attempt_id IS NOT NULL AND probe_id IS NULL) OR (attempt_id IS NULL AND probe_id IS NOT NULL)))""")
    await db.execute("CREATE INDEX IF NOT EXISTS request_host_leases ON request_leases(host_key)")


async def _validate_schema(db):
    async with db.execute("PRAGMA quick_check") as cursor:
        if await cursor.fetchall() != [("ok",)]:
            raise RuntimeCompatibilityError("Database integrity check failed")
    async with db.execute("PRAGMA table_info(downloads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    missing = (BASE_COLUMNS | EXTRA_COLUMNS.keys() | ATTEMPT_COLUMNS.keys() | AUTH_COLUMNS.keys() | PLAYLIST_COLUMNS.keys() | REQUEST_COLUMNS.keys()) - columns
    if missing:
        raise RuntimeCompatibilityError(f"Database is missing download columns: {', '.join(sorted(missing))}")
    async with db.execute("PRAGMA table_info(config)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if not {"key", "value"}.issubset(columns):
        raise RuntimeCompatibilityError("Database is missing config columns")
    for table, required in RELATED_COLUMNS.items():
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if not required.issubset(columns):
            raise RuntimeCompatibilityError(f"Database is missing {table} columns")
    async with db.execute("PRAGMA foreign_key_check") as cursor:
        if await cursor.fetchone():
            raise RuntimeCompatibilityError("Database contains orphan attempt/artifact records")
    for table, index, columns in (("downloads", "retry_deadlines", ["retry_at"]),
                                  ("request_leases", "request_host_leases", ["host_key"])):
        async with db.execute(f"PRAGMA index_list({table})") as cursor:
            if not any(row[1] == index for row in await cursor.fetchall()):
                raise RuntimeCompatibilityError("Database is missing its request budget index")
        async with db.execute(f"PRAGMA index_info({index})") as cursor:
            if [row[2] for row in await cursor.fetchall()] != columns:
                raise RuntimeCompatibilityError("Database has an invalid request budget index")
    async with db.execute("PRAGMA index_list(downloads)") as cursor:
        indexes = await cursor.fetchall()
    if not any(row[1] == "playlist_positions" and row[2] == 1 and row[4] == 1 for row in indexes):
        raise RuntimeCompatibilityError("Database is missing its unique playlist position index")
    async with db.execute("PRAGMA index_info(playlist_positions)") as cursor:
        if [row[2] for row in await cursor.fetchall()] != ["parent_id", "playlist_index"]:
            raise RuntimeCompatibilityError("Database has an invalid playlist position index")
    async with db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ('playlist_parent_insert','playlist_parent_update')") as cursor:
        if len(await cursor.fetchall()) != 2:
            raise RuntimeCompatibilityError("Database is missing its playlist parent guards")
    async with db.execute("""SELECT 1 FROM downloads c LEFT JOIN downloads p ON p.id=c.parent_id WHERE
        (c.parent_id IS NULL AND c.playlist_index IS NOT NULL) OR
        (c.parent_id IS NOT NULL AND (c.job_kind<>'media' OR c.playlist_index IS NULL OR
         c.playlist_index NOT BETWEEN 1 AND 4096 OR p.job_kind<>'playlist' OR p.parent_id IS NOT NULL)) LIMIT 1""") as cursor:
        if await cursor.fetchone():
            raise RuntimeCompatibilityError("Database contains inconsistent playlist children")
    async with db.execute("PRAGMA index_list(download_attempts)") as cursor:
        indexes = await cursor.fetchall()
    if not any(row[1] == "one_running_attempt" and row[2] == 1 and row[4] == 1 for row in indexes):
        raise RuntimeCompatibilityError("Database is missing its single running attempt index")
    async with db.execute("PRAGMA index_list(runtime_owners)") as cursor:
        indexes = await cursor.fetchall()
    if not any(row[1] == "one_active_runtime_owner" and row[2] == 1 and row[4] == 1 for row in indexes):
        raise RuntimeCompatibilityError("Database is missing its single runtime owner index")
    from backend.engine.process_identity import ProcessIdentity
    async with db.execute("""SELECT 1 FROM download_attempts a JOIN runtime_owners o ON o.id=a.owner_id
        WHERE a.lease_expires_at IS NULL OR typeof(a.lease_expires_at) NOT IN ('integer', 'real')
        OR a.lease_expires_at<=0 OR (a.status='running' AND a.lease_expires_at<>o.lease_expires_at) LIMIT 1""") as cursor:
        if await cursor.fetchone():
            raise RuntimeCompatibilityError("Database contains inconsistent attempt ownership/lease information")
    async with db.execute("""SELECT identity FROM runtime_owners
        UNION ALL SELECT worker_owner_identity FROM download_attempts WHERE worker_owner_identity IS NOT NULL
        UNION ALL SELECT worker_identity FROM download_attempts WHERE worker_identity IS NOT NULL
        UNION ALL SELECT identity FROM request_leases UNION ALL SELECT identity FROM request_probes""") as cursor:
        for (identity,) in await cursor.fetchall():
            ProcessIdentity.from_json(identity)
    async with db.execute("""SELECT 1 FROM downloads d LEFT JOIN download_attempts a ON a.id=d.current_attempt_id
                             WHERE d.current_attempt_id IS NOT NULL AND
                             (a.id IS NULL OR a.download_id<>d.id OR a.status<>'running')
                             UNION ALL SELECT 1 FROM download_attempts a JOIN downloads d ON d.id=a.download_id
                             WHERE a.status='running' AND d.current_attempt_id IS NOT a.id LIMIT 1""") as cursor:
        if await cursor.fetchone():
            raise RuntimeCompatibilityError("Database contains inconsistent attempt ownership")
    async with db.execute("SELECT id, download_options FROM downloads WHERE download_options IS NOT NULL") as cursor:
        for task_id, options in await cursor.fetchall():
            try:
                if not isinstance(json.loads(options), dict):
                    raise ValueError("expected an object")
            except (ValueError, TypeError) as error:
                raise RuntimeCompatibilityError(f"Invalid download_options for job {task_id}") from error


async def migrate_database(runtime: RuntimeConfig, defaults: dict, validate_settings: Callable[[dict], object]):
    runtime.validate()
    existed = runtime.db_path.is_file()
    version = await schema_version(runtime.db_path) if existed else 0
    _check_version(version)
    runtime.prepare()
    if existed and version < SCHEMA_VERSION:
        backup = runtime.backups_dir / f"schema-v{version}-{uuid4().hex}.sqlite3"
        await asyncio.to_thread(_snapshot, runtime.db_path, backup)
    async with aiosqlite.connect(runtime.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Recheck under the write lock: concurrent initialization must not regress it.
            async with db.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            _check_version(version)
            for target, migration in ((1, _migration_one), (2, _migration_two), (3, _migration_three),
                                      (4, _migration_four), (5, _migration_five), (6, _migration_six), (7, _migration_seven), (8, _migration_eight)):
                if version < target:
                    await migration(db)
                    await db.execute(f"PRAGMA user_version = {target}")
            await _validate_schema(db)
            for key, value in defaults.items():
                await db.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, json.dumps(value)),
                )
            async with db.execute("SELECT key, value FROM config") as cursor:
                values = {}
                for key, value in await cursor.fetchall():
                    try:
                        values[key] = json.loads(value)
                    except (ValueError, TypeError) as error:
                        raise RuntimeCompatibilityError(f"Invalid JSON setting: {key}") from error
            validate_settings(values)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


@dataclass(frozen=True)
class LegacyImportResult:
    database_path: Path
    backup_path: Path


async def import_legacy_database(source: Path, runtime: RuntimeConfig) -> LegacyImportResult:
    source = Path(source).expanduser().resolve()
    runtime.validate()
    if not source.is_file():
        raise FileNotFoundError(source)
    if runtime.db_path.exists():
        raise FileExistsError("Legacy import requires a new, empty runtime database location")
    runtime.prepare()
    backup = runtime.backups_dir / f"legacy-{uuid4().hex}.sqlite3"
    await asyncio.to_thread(_snapshot, source, backup)
    from backend.config import SettingsStore, init_db

    # Stage on the target filesystem. Linking publishes atomically and refuses to
    # replace a target created concurrently by another initializer.
    with tempfile.TemporaryDirectory(prefix="legacy-import-", dir=runtime.data_dir) as staging:
        staged_runtime = RuntimeConfig(
            data_dir=Path(staging), download_dir=runtime.download_dir,
            logs_dir=runtime.logs_dir, temp_dir=runtime.temp_dir,
        )
        await asyncio.to_thread(shutil.copyfile, backup, staged_runtime.db_path)
        await init_db(staged_runtime)
        # The staging directory is narrower than final application storage.
        SettingsStore(runtime).validate_settings(await SettingsStore(staged_runtime).get_all())
        os.link(staged_runtime.db_path, runtime.db_path)
    return LegacyImportResult(runtime.db_path, backup)
