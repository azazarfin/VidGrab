"""Atomic, runtime-scoped enqueue receipts; deleted jobs leave replay tombstones."""

import hashlib
import json
import re

from fastapi import HTTPException

from backend.db.ownership import require_owner, transaction

MAX_RECEIPTS = 10000
MAX_REQUEST_BYTES = 65536


def request_identity(key, record, *, intent=None):
    if key is not None and (not isinstance(key, str) or re.fullmatch(r"[!-~]{1,128}", key) is None):
        raise HTTPException(400, "Idempotency-Key must contain 1–128 visible ASCII characters")
    semantic = {name: record[name] for name in
                ("url", "format_selected", "title", "thumbnail_url", "scheduled_at", "auth_required", "job_kind")}
    options = json.loads(record["download_options"])
    options.pop("user_agent", None)
    semantic["options"] = options
    try:
        encoded = json.dumps(semantic, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError) as error:
        raise HTTPException(400, "Enqueue options must contain finite JSON values") from error
    if len(encoded) > MAX_REQUEST_BYTES:
        raise HTTPException(400, "Enqueue identity exceeds its 64 KiB limit")
    if intent is not None:
        # Validate the full durable record above even when v1 binds logical
        # intent, so this seam cannot weaken P03's finite JSON/size limits.
        try:
            encoded = json.dumps({"control_intent": intent}, sort_keys=True, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError) as error:
            raise HTTPException(400, "Control intent must contain finite JSON values") from error
        if len(encoded) > MAX_REQUEST_BYTES:
            raise HTTPException(400, "Control intent exceeds its 64 KiB limit")
    if key is None:
        return None
    return hashlib.sha256(key.encode("ascii")).hexdigest(), hashlib.sha256(encoded).hexdigest()


async def insert_download(db, record):
    fields = tuple(record)
    await db.execute(f"INSERT INTO downloads ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                     tuple(record[field] for field in fields))


class EnqueueStore:
    def __init__(self, runtime, owner):
        self.runtime = runtime
        self.owner = owner

    async def admit(self, record, key=None, *, intent=None):
        identity = request_identity(key, record, intent=intent)
        async with transaction(self.runtime) as db:
            await require_owner(db, self.owner)
            if identity is not None:
                key_hash, request_hash = identity
                async with db.execute("SELECT request_hash, job_id FROM enqueue_receipts WHERE key_hash=?", (key_hash,)) as cursor:
                    receipt = await cursor.fetchone()
                if receipt is not None:
                    if receipt["request_hash"] != request_hash:
                        raise HTTPException(409, "Idempotency-Key was already used for a different enqueue request")
                    async with db.execute("SELECT * FROM downloads WHERE id=?", (receipt["job_id"],)) as cursor:
                        existing = await cursor.fetchone()
                    if existing is None:
                        raise HTTPException(410, "Enqueue history was removed; use a new Idempotency-Key")
                    return dict(existing), False
                async with db.execute("SELECT COUNT(*) FROM enqueue_receipts") as cursor:
                    if (await cursor.fetchone())[0] >= MAX_RECEIPTS:
                        raise HTTPException(409, "Persistent enqueue receipt capacity has been reached")
            await insert_download(db, record)
            if identity is not None:
                await db.execute("INSERT INTO enqueue_receipts (key_hash, request_hash, job_id, created_at) VALUES (?, ?, ?, ?)",
                                 (*identity, record["id"], record["created_at"]))
        return record, True
