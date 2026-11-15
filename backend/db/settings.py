"""Settings revisions and replay receipts commit with their invalidation event."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from backend.db.ownership import require_owner, transaction
from backend.api.limits import MAX_SETTINGS_SNAPSHOT_BYTES
from backend.runtime import RuntimeOwnershipError
from backend.services.control_errors import ControlError

MAX_SETTINGS_RECEIPTS = 10000


@dataclass(frozen=True)
class SettingsWrite:
    snapshot: dict
    changed: bool


async def read_settings(db):
    async with db.execute("SELECT key,value FROM config") as cursor:
        values = {row[0]: json.loads(row[1]) for row in await cursor.fetchall()}
    async with db.execute("SELECT settings_revision FROM protocol_state WHERE id=1") as cursor:
        revision = (await cursor.fetchone())[0]
    return revision, values


class SettingsRepository:
    def __init__(self, runtime, owner):
        self.runtime = runtime
        self.owner = owner

    async def patch(self, supplied, validate, *, expected_revision=None, key=None, validate_result=None):
        identity = None
        if key is not None:
            semantic = json.dumps({"expected_revision": expected_revision, "patch": supplied},
                                  sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            identity = (hashlib.sha256(key.encode("ascii")).hexdigest(),
                        hashlib.sha256(semantic.encode("utf-8")).hexdigest())
        async with transaction(self.runtime) as db:
            if self.owner is not None:
                await require_owner(db, self.owner)
            else:
                async with db.execute("SELECT 1 FROM runtime_owners WHERE released_at IS NULL") as cursor:
                    if await cursor.fetchone():
                        raise RuntimeOwnershipError("Settings updates require the active runtime owner")
            if identity is not None:
                async with db.execute("SELECT request_hash,snapshot FROM settings_receipts WHERE key_hash=?", (identity[0],)) as cursor:
                    receipt = await cursor.fetchone()
                if receipt is not None:
                    if receipt["request_hash"] != identity[1]:
                        raise ControlError("IDEMPOTENCY_CONFLICT", 409, "This key was used for a different settings update")
                    snapshot = json.loads(receipt["snapshot"])
                    if validate_result is not None:
                        validate_result(snapshot["values"])
                    return SettingsWrite(snapshot, False)
            revision, current = await read_settings(db)
            current_model = validate(current)
            if expected_revision is not None and revision != expected_revision:
                details = {"settings": {"revision": revision, "values": current_model.model_dump()}}
                if validate_result is not None:
                    try:
                        validate_result(current_model.model_dump())
                    except ControlError:
                        details = {"settings_revision": revision}
                raise ControlError("REVISION_CONFLICT", 409, "Settings changed in another client; reload before saving",
                                   details=details)
            if identity is not None:
                async with db.execute("SELECT COUNT(*) FROM settings_receipts") as cursor:
                    if (await cursor.fetchone())[0] >= MAX_SETTINGS_RECEIPTS:
                        raise ControlError("CAPACITY_EXCEEDED", 409, "The persistent settings receipt limit has been reached")
            updated = validate({**current, **supplied})
            if "download_dir" in supplied:
                supplied = {**supplied, "download_dir": str(Path(updated.download_dir).expanduser().resolve())}
                updated.download_dir = supplied["download_dir"]
            if validate_result is not None:
                validate_result(updated.model_dump())
            changed = {name: value for name, value in supplied.items() if current.get(name) != value}
            if changed:
                revision += 1
            snapshot = {"revision": revision, "values": updated.model_dump()}
            if identity is not None:
                encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > MAX_SETTINGS_SNAPSHOT_BYTES:
                    raise ControlError("MESSAGE_TOO_LARGE", 413, "Settings exceed the control message limit")
            if "download_dir" in changed:
                Path(updated.download_dir).mkdir(parents=True, exist_ok=True)
            for name, value in changed.items():
                await db.execute("INSERT INTO config(key,value) VALUES (?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name, json.dumps(value)))
            if changed:
                await db.execute("UPDATE protocol_state SET settings_revision=? WHERE id=1", (revision,))
                await db.execute("INSERT INTO protocol_events(topic,entity_id,revision,kind) VALUES ('settings',NULL,?,'changed')", (revision,))
            if identity is not None:
                await db.execute("INSERT INTO settings_receipts(key_hash,request_hash,snapshot) VALUES (?,?,?)", (*identity, encoded))
            return SettingsWrite(snapshot, bool(changed))
