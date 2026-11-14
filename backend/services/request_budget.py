"""SQLite request admission shared by probes, media workers and fragments."""

import hashlib
import math
import sqlite3
from contextlib import closing, contextmanager
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.engine.process_identity import ProcessIdentity
from backend.runtime import RuntimeOwnershipError
from backend.services.retry_policy import MAX_DEADLINE, RequestFailure

MAX_REQUESTS = 32768
MAX_ACTIVE = 2
MAX_PER_HOST = 1
MAX_HOSTS = 4096


def host_key(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        parsed.port
        hostname = parsed.hostname.lower().encode("idna")
        if len(hostname) > 253:
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise RequestFailure("REQUEST_REJECTED") from error
    return hashlib.sha256(hostname).hexdigest()


@contextmanager
def request_transaction(runtime):
    with closing(sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


class RequestCoordinator:
    def __init__(self, runtime, context, *, max_requests=MAX_REQUESTS, max_active=MAX_ACTIVE, max_per_host=MAX_PER_HOST):
        if not isinstance(context, dict) or set(context) not in ({"owner_id", "attempt_id"}, {"owner_id", "probe_id"}):
            raise RuntimeOwnershipError("Invalid request authority context")
        for value in context.values():
            if str(UUID(value)) != value:
                raise RuntimeOwnershipError("Invalid request authority identifier")
        if any(type(limit) is not int or limit < 1 for limit in (max_requests, max_active, max_per_host)):
            raise ValueError("Request limits must be positive integers")
        self.runtime, self.context = runtime, dict(context)
        self.identity = ProcessIdentity.capture().to_json()
        self.max_requests, self.max_active, self.max_per_host = max_requests, max_active, max_per_host

    def _owner(self, db):
        owner = db.execute("SELECT identity FROM runtime_owners WHERE id=? AND released_at IS NULL", (self.context["owner_id"],)).fetchone()
        if owner is None or not ProcessIdentity.from_json(owner["identity"]).is_running():
            raise RuntimeOwnershipError("Request owner has been retired or replaced")
        return owner

    def _authority(self, db):
        owner = self._owner(db)
        if "attempt_id" in self.context:
            row = db.execute("""SELECT a.worker_identity, COALESCE(d.parent_id,d.id) AS root_id
                FROM download_attempts a JOIN downloads d ON d.id=a.download_id
                WHERE a.id=? AND a.owner_id=? AND a.status='running' AND d.current_attempt_id=a.id
                AND d.status IN ('downloading','probing','postprocessing')""",
                (self.context["attempt_id"], self.context["owner_id"])).fetchone()
            if row is None or row["worker_identity"] != self.identity:
                raise RuntimeOwnershipError("Request worker identity or attempt authority changed")
            return "downloads", row["root_id"], self.max_requests
        row = db.execute("SELECT identity FROM request_probes WHERE id=? AND owner_id=?",
                         (self.context["probe_id"], self.context["owner_id"])).fetchone()
        if row is None or row["identity"] != self.identity or owner["identity"] != self.identity:
            raise RuntimeOwnershipError("Probe request identity or authority changed")
        return "request_probes", self.context["probe_id"], 64

    def acquire(self, url, now):
        key = host_key(url)
        with request_transaction(self.runtime) as db:
            table, job, limit = self._authority(db)
            cooldown = db.execute("SELECT retry_at FROM request_hosts WHERE host_key=?", (key,)).fetchone()
            if cooldown is not None and cooldown[0] > now:
                raise RequestFailure("UPSTREAM_RATE_LIMITED", retry_at=cooldown[0], consumes_retry=False)
            total, per_host = db.execute("SELECT COUNT(*),COALESCE(SUM(host_key=?),0) FROM request_leases", (key,)).fetchone()
            if total >= self.max_active or per_host >= self.max_per_host:
                raise RequestFailure("ADMISSION_LIMITED", retry_at=now + 1, consumes_retry=False)
            count = db.execute(f"SELECT request_count FROM {table} WHERE id=?", (job,)).fetchone()
            if count is None or count[0] >= limit:
                raise RequestFailure("REQUEST_BUDGET_EXHAUSTED")
            lease = str(uuid4())
            db.execute(f"UPDATE {table} SET request_count=request_count+1 WHERE id=?", (job,))
            db.execute("""INSERT INTO request_leases (id,owner_id,attempt_id,probe_id,identity,host_key,created_at)
                VALUES (?,?,?,?,?,?,?)""", (lease, self.context["owner_id"], self.context.get("attempt_id"),
                self.context.get("probe_id"), self.identity, key, now))
        return lease

    def release(self, lease):
        with request_transaction(self.runtime) as db:
            self._owner(db)
            db.execute("DELETE FROM request_leases WHERE id=? AND owner_id=? AND identity=?",
                       (lease, self.context["owner_id"], self.identity))

    def cooldown(self, url, deadline, now):
        key = host_key(url)
        if type(deadline) not in (float, int) or not math.isfinite(deadline) or not 0 <= deadline <= MAX_DEADLINE:
            raise RequestFailure("REQUEST_REJECTED")
        with request_transaction(self.runtime) as db:
            self._authority(db)
            db.execute("DELETE FROM request_hosts WHERE retry_at<=? AND host_key NOT IN (SELECT host_key FROM request_leases)", (now,))
            if db.execute("SELECT 1 FROM request_hosts WHERE host_key=?", (key,)).fetchone() is None:
                if db.execute("SELECT COUNT(*) FROM request_hosts").fetchone()[0] >= MAX_HOSTS:
                    raise RequestFailure("REQUEST_BUDGET_EXHAUSTED")
            db.execute("""INSERT INTO request_hosts (host_key,retry_at) VALUES (?,?)
                ON CONFLICT(host_key) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)""", (key, deadline))


def begin_probe(runtime, owner, url, now):
    owner.require_lock()
    identity, key = ProcessIdentity.capture().to_json(), host_key(url)
    with request_transaction(runtime) as db:
        row = db.execute("SELECT identity FROM runtime_owners WHERE id=? AND released_at IS NULL", (owner.id,)).fetchone()
        if row is None or row[0] != identity:
            raise RuntimeOwnershipError("Probe owner identity changed")
        total, per_host = db.execute("SELECT COUNT(*),COALESCE(SUM(host_key=?),0) FROM request_probes", (key,)).fetchone()
        if total >= 2 or per_host >= 1:
            raise RequestFailure("ADMISSION_LIMITED", retry_at=now + 1, consumes_retry=False)
        probe = str(uuid4())
        db.execute("INSERT INTO request_probes (id,owner_id,identity,host_key,started_at) VALUES (?,?,?,?,?)",
                   (probe, owner.id, identity, key, now))
    return {"owner_id": owner.id, "probe_id": probe}


def end_probe(runtime, context):
    coordinator = RequestCoordinator(runtime, context)
    with request_transaction(runtime) as db:
        coordinator._authority(db)
        if db.execute("SELECT 1 FROM request_leases WHERE probe_id=?", (context["probe_id"],)).fetchone():
            raise RuntimeOwnershipError("A probe still owns a request response")
        db.execute("DELETE FROM request_probes WHERE id=?", (context["probe_id"],))
