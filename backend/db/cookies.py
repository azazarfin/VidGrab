"""Non-secret credential ownership and the worker's cookie delivery barrier."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from backend.cookie_manager import OWNERSHIP_LIMIT, validate_cookie_record, verify_cookie_file
from backend.engine.process_identity import ProcessIdentity
from backend.runtime import RuntimeOwnershipError


def _active_attempt(db, attempt):
    row = db.execute("""SELECT o.identity FROM download_attempts a JOIN downloads d ON d.id=a.download_id
        JOIN runtime_owners o ON o.id=a.owner_id
        WHERE a.id=? AND a.download_id=? AND a.owner_id=? AND a.workspace=? AND a.status='running'
        AND d.current_attempt_id=a.id AND d.status='downloading' AND o.released_at IS NULL
        AND a.worker_owner_identity IS NULL AND a.worker_identity IS NULL""",
        (attempt.id, attempt.job_id, attempt.owner_id, str(attempt.workspace))).fetchone()
    if row is None or ProcessIdentity.from_json(row["identity"]) != ProcessIdentity.capture():
        raise RuntimeOwnershipError("Cookie creation lost its current runtime/attempt owner")


def register_cookie(runtime, attempt, ownership):
    ownership = validate_cookie_record(runtime, attempt, ownership)
    encoded = json.dumps(ownership, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > OWNERSHIP_LIMIT:
        raise ValueError("Cookie ownership record exceeds its supported size")
    with closing(sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=rw", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        _active_attempt(db, attempt)
        if db.execute("SELECT 1 FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,)).fetchone():
            raise RuntimeOwnershipError("This attempt already owns a cookie file")
        db.execute("INSERT INTO attempt_cookie_files (attempt_id, ownership) VALUES (?, ?)", (attempt.id, encoded))


def mark_cookie_ready(runtime, attempt, ownership):
    verify_cookie_file(runtime, attempt, ownership)
    encoded = json.dumps(ownership, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=rw", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        _active_attempt(db, attempt)
        cursor = db.execute("UPDATE attempt_cookie_files SET ready=1 WHERE attempt_id=? AND ownership=? AND ready=0",
                            (attempt.id, encoded))
        if not cursor.rowcount:
            raise RuntimeOwnershipError("Cookie delivery lost its durable ownership record")


def verify_cookie_delivery(db, runtime, attempt_id, owner_id, workspace, path):
    row = db.execute("SELECT ownership, ready FROM attempt_cookie_files WHERE attempt_id=?", (attempt_id,)).fetchone()
    if path is None and row is None:
        return
    attempt = SimpleNamespace(id=attempt_id, owner_id=owner_id, workspace=Path(workspace))
    if row is None or row["ready"] != 1:
        raise RuntimeOwnershipError("Cookie delivery has no ready, durable ownership record")
    try:
        ownership = verify_cookie_file(runtime, attempt, row["ownership"])
        if path != ownership["path"]:
            raise ValueError("Cookie path does not match its attempt")
    except (OSError, ValueError) as error:
        raise RuntimeOwnershipError("Cookie delivery ownership could not be verified") from error
