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

SCHEMA_VERSION = 2
BASE_COLUMNS = {
    "id", "url", "title", "thumbnail_url", "status", "progress", "speed", "eta",
    "file_size", "file_path", "format_selected", "error_message", "created_at", "completed_at",
}
EXTRA_COLUMNS = {
    "download_options": "TEXT", "scheduled_at": "TEXT", "retry_count": "INTEGER DEFAULT 0",
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


async def _validate_schema(db):
    async with db.execute("PRAGMA quick_check") as cursor:
        if await cursor.fetchall() != [("ok",)]:
            raise RuntimeCompatibilityError("Database integrity check failed")
    async with db.execute("PRAGMA table_info(downloads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    missing = (BASE_COLUMNS | EXTRA_COLUMNS.keys()) - columns
    if missing:
        raise RuntimeCompatibilityError(f"Database is missing download columns: {', '.join(sorted(missing))}")
    async with db.execute("PRAGMA table_info(config)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if not {"key", "value"}.issubset(columns):
        raise RuntimeCompatibilityError("Database is missing config columns")
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
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Recheck under the write lock: concurrent initialization must not regress it.
            async with db.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            _check_version(version)
            for target, migration in ((1, _migration_one), (2, _migration_two)):
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
