"""Action acceptance and replay receipts share the job's owned transaction."""

from dataclasses import dataclass
import hashlib
import json

from backend.services.control_errors import ControlError

MAX_ACTION_RECEIPTS = 10000


@dataclass(frozen=True)
class ControlAction:
    key_hash: str
    request_hash: str
    action: str
    expected_revision: int

    @classmethod
    def from_request(cls, request, params, action):
        encoded = json.dumps({"operation": request.operation, "params": params.model_dump()},
                             sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        return cls(hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest(),
                   hashlib.sha256(encoded).hexdigest(), action, params.expected_revision)


class ControlActionStore:
    async def replay(self, db, action):
        async with db.execute("SELECT request_hash,result FROM control_action_receipts WHERE key_hash=?", (action.key_hash,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if row["request_hash"] != action.request_hash:
            raise ControlError("IDEMPOTENCY_CONFLICT", 409, "This key was used for a different download action")
        return json.loads(row["result"])

    async def validate(self, db, action, row):
        if row is None:
            raise ControlError("NOT_FOUND", 404, "The selected download no longer exists")
        if row["revision"] != action.expected_revision:
            raise ControlError("REVISION_CONFLICT", 409, "The download changed; reload before applying this action",
                               details={"job": {"id": row["id"], "revision": row["revision"], "status": row["status"]}})
        async with db.execute("SELECT COUNT(*) FROM control_action_receipts") as cursor:
            if (await cursor.fetchone())[0] >= MAX_ACTION_RECEIPTS:
                raise ControlError("CAPACITY_EXCEEDED", 409, "The persistent download-action receipt limit has been reached")

    async def record(self, db, action, job_id):
        async with db.execute("SELECT status,revision FROM downloads WHERE id=?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        result = {"job_id": job_id, "action": action.action, "accepted_status": row["status"], "accepted_revision": row["revision"]}
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        await db.execute("INSERT INTO control_action_receipts(key_hash,request_hash,result) VALUES (?,?,?)",
                         (action.key_hash, action.request_hash, encoded))
        return result
