"""Durable owner leases and the worker's transactional startup barrier."""

from contextlib import asynccontextmanager, closing
from pathlib import Path
import sqlite3

import aiosqlite

from backend.engine.process_identity import ProcessIdentity
from backend.runtime import RuntimeOwnershipError

LEASE_SECONDS = 30.0


@asynccontextmanager
async def transaction(runtime):
    async with aiosqlite.connect(runtime.db_path.resolve().as_uri() + "?mode=rw", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


def require_dead(value, label, *, tree=False):
    try:
        identity = ProcessIdentity.from_json(value)
        live = identity.worker_tree_is_running() if tree else identity.is_running()
    except (OSError, ValueError) as error:
        raise RuntimeOwnershipError(f"Cannot verify {label} process identity") from error
    if live:
        raise RuntimeOwnershipError(f"A live {label} still owns this runtime or attempt")


def require_recoverable(attempt, current_owner):
    if attempt["owner_id"] is None:
        raise RuntimeOwnershipError("Legacy running attempt has no verifiable owner identity; recovery refused")
    if attempt["owner_id"] != current_owner and (attempt["status"] == "running" or attempt["owner_released_at"] is None):
        require_dead(attempt["runtime_identity"], "attempt owner")
    for column, label, tree in (("worker_owner_identity", "worker owner", False), ("worker_identity", "worker tree", True)):
        if attempt[column] is not None:
            require_dead(attempt[column], label, tree=tree)


async def running_attempts(db):
    async with db.execute("""SELECT a.*, d.status AS job_status, o.identity AS runtime_identity
                             FROM download_attempts a JOIN downloads d ON d.id=a.download_id
                             LEFT JOIN runtime_owners o ON o.id=a.owner_id WHERE a.status='running'""") as cursor:
        return await cursor.fetchall()


async def recovery_attempts(db):
    async with db.execute("""SELECT a.*, d.status AS job_status, o.identity AS runtime_identity,
        o.released_at AS owner_released_at, c.ownership AS cookie_ownership
        FROM download_attempts a JOIN downloads d ON d.id=a.download_id
        LEFT JOIN runtime_owners o ON o.id=a.owner_id LEFT JOIN attempt_cookie_files c ON c.attempt_id=a.id
        WHERE a.status='running' OR c.attempt_id IS NOT NULL""") as cursor:
        return await cursor.fetchall()


async def require_owner(db, owner):
    owner.require_lock()
    async with db.execute("SELECT identity, lease_expires_at FROM runtime_owners WHERE id=? AND released_at IS NULL",
                          (owner.id,)) as cursor:
        row = await cursor.fetchone()
    if row is None or row["identity"] != owner.identity.to_json():
        raise RuntimeOwnershipError("Runtime owner has been retired or replaced")
    return row["lease_expires_at"]


class OwnerStore:
    def __init__(self, runtime):
        self.runtime = runtime

    async def acquire(self, owner, now):
        async with transaction(self.runtime) as db:
            async with db.execute("SELECT identity FROM runtime_owners WHERE released_at IS NULL") as cursor:
                previous = await cursor.fetchall()
            for prior in previous:
                require_dead(prior["identity"], "runtime owner")
            # Check every attempt before retiring owners or changing any recovery state.
            for attempt in await recovery_attempts(db):
                require_recoverable(attempt, owner.id)
            await db.execute("UPDATE runtime_owners SET released_at=? WHERE released_at IS NULL", (now,))
            await db.execute("""INSERT INTO runtime_owners
                                (id, identity, acquired_at, heartbeat_at, lease_expires_at)
                                VALUES (?, ?, ?, ?, ?)""",
                             (owner.id, owner.identity.to_json(), now, now, now + LEASE_SECONDS))
        return now + LEASE_SECONDS

    async def renew(self, owner, now):
        async with transaction(self.runtime) as db:
            await require_owner(db, owner)
            await db.execute("UPDATE runtime_owners SET heartbeat_at=?, lease_expires_at=? WHERE id=?",
                             (now, now + LEASE_SECONDS, owner.id))
            await db.execute("UPDATE download_attempts SET lease_expires_at=? WHERE owner_id=? AND status='running'",
                             (now + LEASE_SECONDS, owner.id))
        return now + LEASE_SECONDS

    async def release(self, owner, now):
        async with transaction(self.runtime) as db:
            await require_owner(db, owner)
            async with db.execute("SELECT 1 FROM download_attempts WHERE owner_id=? AND status='running' LIMIT 1",
                                  (owner.id,)) as cursor:
                if await cursor.fetchone():
                    raise RuntimeOwnershipError("Cannot release an owner with unfinished attempts")
            await db.execute("UPDATE runtime_owners SET released_at=? WHERE id=?", (now, owner.id))


def register_worker(runtime, attempt_id, owner_id, job_id, workspace, worker_owner, worker, *, cookies_file_path=None):
    """Commit identities before the child receives any bootstrap or may touch outputs."""
    if Path(workspace) != runtime.data_dir / "attempts" / attempt_id:
        raise RuntimeOwnershipError("Worker workspace does not match its attempt identity")
    uri = runtime.db_path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT o.identity FROM download_attempts a
                            JOIN downloads d ON d.id=a.download_id JOIN runtime_owners o ON o.id=a.owner_id
                            WHERE a.id=? AND a.owner_id=? AND a.download_id=? AND a.workspace=? AND a.status='running'
                            AND d.current_attempt_id=a.id AND d.status='downloading' AND o.released_at IS NULL
                            AND a.worker_owner_identity IS NULL AND a.worker_identity IS NULL""",
                         (attempt_id, owner_id, job_id, workspace)).fetchone()
        if row is None or not ProcessIdentity.from_json(row["identity"]).is_running():
            raise RuntimeOwnershipError("Worker launch lost its active runtime/attempt owner")
        from backend.db.cookies import verify_cookie_delivery
        verify_cookie_delivery(db, runtime, attempt_id, owner_id, workspace, cookies_file_path)
        db.execute("UPDATE download_attempts SET worker_owner_identity=?, worker_identity=? WHERE id=?",
                   (worker_owner.to_json(), worker.to_json(), attempt_id))
