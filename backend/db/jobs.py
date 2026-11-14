"""Transactional job claims and attempt-fenced lifecycle persistence."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from backend.runtime import RuntimeConfig
from backend.db.ownership import require_owner, require_recoverable, running_attempts, transaction
from backend.services.job_state import ACTIVE, FINISH_FROM, TERMINAL
from backend.services.artifacts import recovery_artifacts

JOB_FIELDS = frozenset({"progress", "speed", "eta", "file_size", "file_path", "title", "thumbnail_url",
                        "error_message", "retry_count"})
ATTEMPT_OUTCOMES = frozenset({"complete", "failed", "cancelled", "interrupted"})


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

    async def claim(self, job_id: str, now: str) -> Attempt | None:
        async with self.transaction() as db:
            lease = await require_owner(db, self.owner)
            async with db.execute("SELECT attempt_count FROM downloads WHERE id=? AND status='queued' AND current_attempt_id IS NULL",
                                  (job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            attempt_id = str(uuid4())
            attempt = Attempt(attempt_id, job_id, row["attempt_count"] + 1,
                              self.runtime.data_dir / "attempts" / attempt_id, self.owner.id)
            await db.execute("""UPDATE downloads SET status='downloading', attempt_count=?, current_attempt_id=?,
                                progress=0, speed=0, eta=NULL, file_path=NULL, completed_at=NULL, error_message=NULL
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
        outcome = outcome or ("interrupted" if status == "queued" else status)
        permitted = {"failed", "interrupted"} if status == "queued" else {status}
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
            assignments = ", ".join(f"{key}=?" for key in fields)
            await db.execute(f"UPDATE downloads SET {assignments} WHERE id=?", (*fields.values(), job_id))
        return True

    async def recover(self, now: str) -> list[dict]:
        """Fence every owner/tree before reading artifacts or changing recovery state."""
        async with self.transaction() as db:
            await require_owner(db, self.owner)
            attempts = await running_attempts(db)
            for attempt in attempts:
                require_recoverable(attempt, self.owner.id)
            for attempt in attempts:
                owned = Attempt(attempt["id"], attempt["download_id"], attempt["number"], Path(attempt["workspace"]))
                artifacts = await asyncio.to_thread(recovery_artifacts, self.runtime, owned)
                await record_artifacts(db, owned.id, now, artifacts)
                outcome = "cancelled" if attempt["job_status"] == "cancelling" else "interrupted"
                await db.execute("UPDATE download_attempts SET status=?, finished_at=? WHERE id=?",
                                 (outcome, now, attempt["id"]))
            states = (*sorted(ACTIVE), "cancelling")
            placeholders = ", ".join("?" for _ in states)
            async with db.execute(f"SELECT id FROM downloads WHERE status IN ({placeholders})", states) as cursor:
                recovered_ids = [row["id"] for row in await cursor.fetchall()]
            await db.execute(f"""UPDATE downloads SET
                                 status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE 'queued' END,
                                 completed_at=CASE WHEN status='cancelling' THEN ? ELSE NULL END,
                                 speed=0, eta=NULL, current_attempt_id=NULL WHERE status IN ({placeholders})""",
                             (now, *states))
            if not recovered_ids:
                return []
            placeholders = ", ".join("?" for _ in recovered_ids)
            async with db.execute(f"SELECT * FROM downloads WHERE id IN ({placeholders})", recovered_ids) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
