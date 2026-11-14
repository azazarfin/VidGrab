"""Transactional job claims and attempt-fenced lifecycle persistence."""

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from backend.runtime import RuntimeConfig
from backend.engine.execution import join_owned_task
from backend.db.ownership import require_owner, require_recoverable, recovery_attempts, recover_requests, transaction
from backend.db.playlists import PlaylistStore, sync_parent, sync_parents
from backend import cookie_manager
from backend.services.job_state import ACTIVE, FINISH_FROM, TERMINAL
from backend.services.publication import cleanup_publication, reconcile_publication

logger = logging.getLogger("vidgrab.attempts")

JOB_FIELDS = frozenset({"progress", "speed", "eta", "file_size", "file_path", "title", "thumbnail_url",
                        "error_message", "retry_count", "auth_required", "retry_at", "error_code"})
ATTEMPT_OUTCOMES = frozenset({"complete", "failed", "cancelled", "interrupted"})
MAX_ATTEMPTS = 64


async def record_artifacts(db, attempt_id, now, artifacts):
    for artifact in artifacts:
        await db.execute("""INSERT INTO download_artifacts (attempt_id, path, kind, state, size, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_id, path) DO UPDATE SET
                            kind=excluded.kind, state=excluded.state, size=excluded.size, updated_at=excluded.updated_at""",
                         (attempt_id, artifact["path"], artifact["kind"], artifact["state"], artifact.get("size"), now))


@dataclass(frozen=True)
class Attempt:
    id: str
    job_id: str
    number: int
    workspace: Path
    owner_id: str | None = None


class AttemptStore:
    def __init__(self, runtime: RuntimeConfig, owner):
        self.runtime = runtime
        self.owner = owner

    def transaction(self):
        return transaction(self.runtime)

    async def promote_due_retries(self, now):
        async with self.transaction() as db:
            await require_owner(db, self.owner)
            cursor = await db.execute("""UPDATE downloads SET status='queued', retry_at=NULL
                WHERE status='retry_wait' AND retry_at<=? RETURNING *""", (now,))
            return [dict(row) for row in await cursor.fetchall()]

    async def expand(self, job_id, attempt_id, result, now):
        return await PlaylistStore(self.runtime, self.owner).expand(job_id, attempt_id, result, now)

    async def _cleanup_cookies(self, db, attempt, ownership):
        cleanup = asyncio.create_task(asyncio.to_thread(cookie_manager.cleanup_cookie_file, self.runtime, attempt, ownership))
        await join_owned_task(cleanup)
        await db.execute("DELETE FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,))

    async def cleanup_cookies(self, attempt):
        async with self.transaction() as db:
            await require_owner(db, self.owner)
            for owned in await recovery_attempts(db):
                if owned["id"] == attempt.id and owned["cookie_ownership"] is not None:
                    require_recoverable(owned, self.owner.id)
                    selected = Attempt(owned["id"], owned["download_id"], owned["number"],
                                       Path(owned["workspace"]), owned["owner_id"])
                    await self._cleanup_cookies(db, selected, owned["cookie_ownership"])
                    break

    async def _cleanup_completed(self, attempt):
        cleanup = asyncio.create_task(asyncio.to_thread(cleanup_publication, self.runtime, attempt))
        try:
            await join_owned_task(cleanup)
        except (OSError, ValueError) as error:
            logger.warning("Retained publication witnesses for completed attempt %s: %s", attempt.id, error)

    async def claim(self, job_id: str, now: str) -> Attempt | None:
        async with self.transaction() as db:
            lease = await require_owner(db, self.owner)
            async with db.execute("""SELECT attempt_count, parent_id FROM downloads WHERE id=? AND status='queued'
                AND current_attempt_id IS NULL AND (retry_at IS NULL OR retry_at<=?)""",
                                  (job_id, datetime.fromisoformat(now).timestamp())) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            if row["attempt_count"] >= MAX_ATTEMPTS:
                await db.execute("""UPDATE downloads SET status='failed', retry_at=NULL,
                    error_code='ATTEMPT_BUDGET_EXHAUSTED', error_message='Attempt budget exhausted',
                    speed=0, eta=NULL, completed_at=? WHERE id=?""", (now, job_id))
                if row["parent_id"] is not None:
                    await sync_parent(db, row["parent_id"], now)
                return None
            attempt_id = str(uuid4())
            attempt = Attempt(attempt_id, job_id, row["attempt_count"] + 1,
                              self.runtime.data_dir / "attempts" / attempt_id, self.owner.id)
            await db.execute("""UPDATE downloads SET status='downloading', attempt_count=?, current_attempt_id=?,
                                progress=0, speed=0, eta=NULL, file_path=NULL, completed_at=NULL, error_message=NULL,
                                retry_at=NULL, error_code=NULL
                                WHERE id=?""", (attempt.number, attempt.id, job_id))
            await db.execute("""INSERT INTO download_attempts
                                (id, download_id, number, status, workspace, started_at, owner_id, lease_expires_at)
                                VALUES (?, ?, ?, 'running', ?, ?, ?, ?)""",
                             (attempt.id, job_id, attempt.number, str(attempt.workspace), now, self.owner.id, lease))
        return attempt

    async def finish(self, job_id: str, status: str, now: str, *, attempt_id: str | None = None,
                     outcome: str | None = None, artifacts=(), **fields) -> bool:
        if not fields.keys() <= JOB_FIELDS:
            raise ValueError("Unsupported job transition fields")
        expected = FINISH_FROM[status]
        outcome = outcome or ("interrupted" if status in ("queued", "waiting_for_auth", "retry_wait") else status)
        permitted = {"failed", "interrupted"} if status in ("queued", "waiting_for_auth", "retry_wait") else {status}
        if outcome not in ATTEMPT_OUTCOMES or outcome not in permitted:
            raise ValueError("Invalid attempt outcome")
        async with self.transaction() as db:
            await require_owner(db, self.owner)
            async with db.execute("SELECT status, current_attempt_id FROM downloads WHERE id=?", (job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None or row["status"] not in expected or row["current_attempt_id"] != attempt_id:
                return False
            if attempt_id is not None:
                async with db.execute("SELECT * FROM download_attempts WHERE id=? AND owner_id=?", (attempt_id, self.owner.id)) as cursor:
                    owned = await cursor.fetchone()
                if owned is None:
                    return False
                require_recoverable(owned, self.owner.id)
                await recover_requests(db, self.owner.id, attempt_id=attempt_id)
                cursor = await db.execute("""UPDATE download_attempts SET status=?, finished_at=?, error_message=?
                                             WHERE id=? AND download_id=? AND status='running'""",
                                          (outcome, now, fields.get("error_message"), attempt_id, job_id))
                if not cursor.rowcount:
                    return False
                await record_artifacts(db, attempt_id, now, artifacts)
            elif artifacts:
                raise ValueError("Artifacts require an owned attempt")
            fields.update(status=status, speed=0.0, eta=None, current_attempt_id=None,
                          completed_at=now if status in TERMINAL else None)
            if status != "retry_wait":
                fields["retry_at"] = None
            if status in ("complete", "cancelled", "queued"):
                fields["error_code"] = None
            assignments = ", ".join(f"{key}=?" for key in fields)
            await db.execute(f"UPDATE downloads SET {assignments} WHERE id=?", (*fields.values(), job_id))
            async with db.execute("SELECT parent_id FROM downloads WHERE id=?", (job_id,)) as cursor:
                parent_id = (await cursor.fetchone())[0]
            if parent_id is not None:
                await sync_parent(db, parent_id, now)
        if status == "complete" and attempt_id is not None:
            await self._cleanup_completed(Attempt(attempt_id, job_id, owned["number"], Path(owned["workspace"])))
        return True

    async def recover(self, now: str) -> list[dict]:
        """Fence every owner/tree before reading artifacts or changing recovery state."""
        completed = []
        async with self.transaction() as db:
            await require_owner(db, self.owner)
            candidates = await recovery_attempts(db)
            for attempt in candidates:
                require_recoverable(attempt, self.owner.id)
            await recover_requests(db, self.owner.id)
            for attempt in candidates:
                if attempt["cookie_ownership"] is not None:
                    selected = Attempt(attempt["id"], attempt["download_id"], attempt["number"],
                                       Path(attempt["workspace"]), attempt["owner_id"])
                    await self._cleanup_cookies(db, selected, attempt["cookie_ownership"])
            attempts = [attempt for attempt in candidates if attempt["status"] == "running"]
            for attempt in attempts:
                owned = Attempt(attempt["id"], attempt["download_id"], attempt["number"], Path(attempt["workspace"]))
                verification = asyncio.create_task(asyncio.to_thread(reconcile_publication, self.runtime, owned))
                recovery = await join_owned_task(verification)
                if attempt["job_status"] == "cancelling":
                    status, outcome = "cancelled", "cancelled"
                    fields = {"error_message": None}
                elif recovery.error:
                    status, outcome = "failed", "failed"
                    fields = {"error_message": recovery.error, "file_path": None}
                elif recovery.result is not None:
                    status, outcome = "complete", "complete"
                    completed.append(owned)
                    recovery.artifacts = [record for record in recovery.artifacts if record["path"] not in recovery.witness_paths]
                    fields = {"file_path": recovery.result["filepath"], "progress": 100.0, "error_message": None}
                    for key in ("title", "thumbnail_url"):
                        if recovery.result.get(key) is not None:
                            fields[key] = recovery.result[key]
                else:
                    status, outcome = "queued", "interrupted"
                    fields = {"file_path": None, "error_message": None}
                await record_artifacts(db, owned.id, now, recovery.artifacts)
                await db.execute("UPDATE download_attempts SET status=?, finished_at=?, error_message=? WHERE id=?",
                                 (outcome, now, fields.get("error_message"), attempt["id"]))
                fields.update(status=status, current_attempt_id=None, speed=0, eta=None,
                              completed_at=now if status in TERMINAL else None, retry_at=None, error_code=None)
                assignments = ", ".join(f"{key}=?" for key in fields)
                await db.execute(f"UPDATE downloads SET {assignments} WHERE id=?", (*fields.values(), owned.job_id))
            states = (*sorted(ACTIVE), "cancelling")
            placeholders = ", ".join("?" for _ in states)
            async with db.execute(f"SELECT id FROM downloads WHERE status IN ({placeholders})", states) as cursor:
                recovered_ids = [row["download_id"] for row in attempts] + [row["id"] for row in await cursor.fetchall()]
            await db.execute(f"""UPDATE downloads SET
                                 status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE 'queued' END,
                                 completed_at=CASE WHEN status='cancelling' THEN ? ELSE NULL END,
                                 speed=0, eta=NULL, current_attempt_id=NULL, retry_at=NULL, error_code=NULL
                                 WHERE status IN ({placeholders})""",
                             (now, *states))
            cursor = await db.execute("""UPDATE downloads SET status='waiting_for_auth',
                error_message='Fresh browser authentication is required'
                WHERE status='queued' AND auth_required=1 RETURNING id""")
            recovered_ids.extend(row["id"] for row in await cursor.fetchall())
            recovered_ids.extend(await sync_parents(db, now))
            if not recovered_ids:
                return []
            placeholders = ", ".join("?" for _ in recovered_ids)
            async with db.execute(f"SELECT * FROM downloads WHERE id IN ({placeholders})", recovered_ids) as cursor:
                recovered = [dict(row) for row in await cursor.fetchall()]
        for attempt in completed:
            await self._cleanup_completed(attempt)
        return recovered
