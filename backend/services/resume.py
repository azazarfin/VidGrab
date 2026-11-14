"""Bounded non-secret HTTP witnesses; never adopt a partial by its filename."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import urlsplit
from types import MethodType

from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import parse_http_range

from backend.db.ownership import require_recoverable
from backend.engine.process_identity import ProcessIdentity
from backend.runtime import OutputPathError, RuntimeOwnershipError
from backend.services.artifacts import is_link, validate_workspace

RECORD_LIMIT = 4096
STREAM_LIMIT = 32
COPY_BLOCK = 1024 * 1024


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_validator(url, format_id, etag, length):
    """A strong ETag and exact identity-coded length are required for appending."""
    if (not isinstance(url, str) or len(url) > 65536 or urlsplit(url).scheme not in ("http", "https")
            or not isinstance(format_id, str) or len(format_id) > 1024
            or not isinstance(etag, str) or not 2 <= len(etag) <= 1024 or not etag.startswith('"')
            or not etag.endswith('"') or any(ord(c) < 33 or c == '"' or ord(c) == 127 for c in etag[1:-1])
            or type(length) is not int or not 0 < length <= 2**63 - 1):
        return None
    return {"key": _digest(json.dumps([url, format_id], ensure_ascii=False)),
            "etag": _digest(etag), "length": length}


def _identity(metadata):
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_source(source):
    if (not isinstance(source, dict) or set(source) != {"key", "etag", "length"}
            or not _valid_digest(source["key"]) or not _valid_digest(source["etag"])
            or type(source["length"]) is not int or not 0 < source["length"] <= 2**63 - 1):
        raise ValueError("Invalid partial source validator")


def _validate_record(record):
    if isinstance(record, str):
        if len(record.encode("utf-8")) > RECORD_LIMIT:
            raise ValueError("Partial record exceeds its size limit")
        record = json.loads(record)
    if not isinstance(record, dict) or set(record) != {"version", "source", "name", "workspace_identity", "identity", "size", "sha256"}:
        raise ValueError("Invalid partial record")
    _validate_source(record["source"])
    name = record["name"]
    if (type(record["version"]) is not int or record["version"] != 1
            or not isinstance(name, str) or not name or len(name) > 255 or "\0" in name
            or "/" in name or "\\" in name or ":" in name or name in (".", "..") or not name.endswith(".part")
            or type(record["size"]) is not int or not 0 <= record["size"] <= record["source"]["length"]
            or not _valid_digest(record["sha256"])):
        raise ValueError("Invalid partial path, size or digest")
    for key in ("identity", "workspace_identity"):
        identity = record[key]
        if (not isinstance(identity, dict) or set(identity) != {"device", "inode"}
                or any(type(v) is not int or v < 0 for v in identity.values()) or identity["inode"] == 0):
            raise ValueError("Invalid partial file identity")
    return record


def _target(runtime, attempt, path):
    if attempt.workspace != runtime.data_dir / "attempts" / attempt.id:
        raise ValueError("Partial workspace does not match its attempt identity")
    workspace = validate_workspace(runtime, str(attempt.workspace))
    path = Path(path)
    if path.parent != workspace or not path.name.endswith(".part") or ":" in path.name or "\\" in path.name:
        raise ValueError("Partial path must be directly inside its owned workspace")
    return path


def _active(db, attempt):
    row = db.execute("""SELECT a.*, o.identity AS runtime_identity FROM download_attempts a
        JOIN downloads d ON d.id=a.download_id JOIN runtime_owners o ON o.id=a.owner_id
        WHERE a.id=? AND a.download_id=? AND a.owner_id=? AND a.workspace=? AND a.status='running'
        AND d.current_attempt_id=a.id AND d.status='downloading' AND o.released_at IS NULL""",
        (attempt.id, attempt.job_id, attempt.owner_id, str(attempt.workspace))).fetchone()
    if row is None or not ProcessIdentity.from_json(row["runtime_identity"]).is_running():
        raise RuntimeOwnershipError("Partial operation lost its active runtime/attempt owner")
    expected = row["worker_identity"] or row["runtime_identity"]
    if ProcessIdentity.from_json(expected) != ProcessIdentity.capture():
        raise RuntimeOwnershipError("Partial operation belongs to another worker or runtime owner")
    return row


def _connect(runtime):
    db = sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=rw", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("BEGIN IMMEDIATE")
    return db


def _file(path):
    if is_link(path):
        raise ValueError("Partial file must not be a link or reparse point")
    file = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb")
    metadata = os.fstat(file.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        file.close()
        raise ValueError("Partial file must be a regular file without extra hardlinks")
    return file


def checkpoint(runtime, attempt, path, source, size, sha256):
    """Commit an already fsynced prefix; ownership remains immutable across updates."""
    _validate_source(source)
    with closing(_connect(runtime)) as db, db:
        _active(db, attempt)
        path = _target(runtime, attempt, path)
        with _file(path) as file:
            metadata = os.fstat(file.fileno())
        record = _validate_record({"version": 1, "source": source, "name": path.name,
                                   "workspace_identity": _identity(attempt.workspace.stat()),
                                   "identity": _identity(metadata), "size": size, "sha256": sha256})
        if size > metadata.st_size:
            raise ValueError("Partial checkpoint extends beyond its file")
        previous = db.execute("SELECT record FROM attempt_partials WHERE attempt_id=? AND source_key=?",
                              (attempt.id, source["key"])).fetchone()
        if previous:
            prior = _validate_record(previous["record"])
            if any(prior[key] != record[key] for key in ("source", "name", "workspace_identity", "identity")) or size < prior["size"]:
                raise ValueError("Partial checkpoint changed its durable ownership")
        elif db.execute("SELECT COUNT(*) FROM attempt_partials WHERE attempt_id=?", (attempt.id,)).fetchone()[0] >= STREAM_LIMIT:
            raise ValueError("Attempt exceeds its partial stream limit")
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > RECORD_LIMIT:
            raise ValueError("Partial record exceeds its size limit")
        db.execute("""INSERT INTO attempt_partials (attempt_id, source_key, record) VALUES (?, ?, ?)
            ON CONFLICT(attempt_id, source_key) DO UPDATE SET record=excluded.record""", (attempt.id, source["key"], encoded))


def _prefix(file, size, output=None):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = file.read(min(remaining, COPY_BLOCK))
        if not block:
            raise ValueError("Partial prefix is shorter than its checkpoint")
        digest.update(block)
        if output is not None:
            output.write(block)
        remaining -= len(block)
    return digest.hexdigest()


def restore(runtime, attempt, target, source):
    """Copy only a verified prefix to an exclusive new attempt file, never a hardlink."""
    _validate_source(source)
    with closing(_connect(runtime)) as db, db:
        _active(db, attempt)
        target = _target(runtime, attempt, target)
        if os.path.lexists(target):
            raise FileExistsError("Existing partial destination is not this attempt's ownership")
        prior = db.execute("""SELECT a.*, o.identity AS runtime_identity, o.released_at AS owner_released_at, p.record
            FROM attempt_partials p JOIN download_attempts a ON a.id=p.attempt_id
            JOIN runtime_owners o ON o.id=a.owner_id
            WHERE a.download_id=? AND a.number<? AND p.source_key=? ORDER BY a.number DESC LIMIT 1""",
            (attempt.job_id, attempt.number, source["key"])).fetchone()
        if prior is None:
            return 0
        require_recoverable(prior, attempt.owner_id)
    record = _validate_record(prior["record"])
    if record["source"] != source or record["size"] == 0:
        return 0
    if Path(prior["workspace"]) != runtime.data_dir / "attempts" / prior["id"]:
        raise ValueError("Partial workspace does not match its attempt identity")
    workspace = validate_workspace(runtime, prior["workspace"])
    if _identity(workspace.stat()) != record["workspace_identity"]:
        raise ValueError("Partial workspace identity changed")
    path = workspace / record["name"]
    with _file(path) as file:
        before = os.fstat(file.fileno())
        if _identity(before) != record["identity"] or before.st_size < record["size"]:
            raise ValueError("Partial file identity or prefix size changed")
        if _prefix(file, record["size"]) != record["sha256"]:
            raise ValueError("Partial prefix digest changed")
        file.seek(0)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "wb") as output:
            copied = _prefix(file, record["size"], output)
            output.flush()
            os.fsync(output.fileno())
            after = os.fstat(file.fileno())
            if (copied != record["sha256"] or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or after.st_nlink != 1
                    or is_link(path) or _identity(path.stat()) != record["identity"]):
                raise ValueError("Partial file changed during verified copying")
    with closing(_connect(runtime)) as db, db:
        _active(db, attempt)
    return record["size"]


class FreshAuthRequired(RuntimeError):
    def __init__(self):
        super().__init__("Fresh browser authentication is required")


def requires_fresh_auth(error):
    """Follow bounded typed extractor causes; never classify by secret-bearing text."""
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        selected = pending.pop()
        if not isinstance(selected, BaseException) or id(selected) in seen:
            continue
        seen.add(id(selected))
        if isinstance(selected, FreshAuthRequired) or isinstance(selected, HTTPError) and selected.status in (401, 403):
            return True
        pending.extend(getattr(selected, name, None) for name in ("cause", "__cause__", "__context__"))
        info = getattr(selected, "exc_info", None)
        if isinstance(info, tuple) and len(info) == 3:
            pending.append(info[1])
    return False


def install_http_resume(ydl, runtime, attempt):
    """Install verified direct/static-fragment transfers only in this owned worker."""
    from backend.services.fragment_resume import VerifiedDashFD, VerifiedHlsFD
    from yt_dlp.downloader.dash import DashSegmentsFD
    from yt_dlp.downloader.hls import HlsFD
    original = ydl.dl
    def download(self, name, info, subtitle=False, test=False):
        downloader = get_suitable_downloader(info, self.params, to_stdout=name == "-")
        if (not subtitle and not test and not info.get("request_data")
                and name != "-" and downloader in (HttpFD, HlsFD, DashSegmentsFD)):
            copied = self._copy_infodict(info)
            if copied.get("http_headers") is None:
                copied["http_headers"] = self._calc_headers(copied)
            selected = {HttpFD: VerifiedHttpFD, HlsFD: VerifiedHlsFD, DashSegmentsFD: VerifiedDashFD}[downloader]
            fd = selected(self, self.params, runtime, attempt)
            for hook in self._progress_hooks:
                fd.add_progress_hook(hook)
            if os.path.lexists(name):
                raise OutputPathError("Existing download output is not owned by this transfer")
            return fd.download(name, copied)
        return original(name, info, subtitle, test)
    ydl.dl = MethodType(download, ydl)


class VerifiedHttpFD(HttpFD):
    """Direct identity-coded HTTP only; every resumed response is verified."""

    def __init__(self, ydl, params, runtime, attempt):
        super().__init__(ydl, params)
        self.runtime = runtime
        self.attempt = attempt

    def _open(self, request):
        try:
            return self.ydl.urlopen(request)
        except HTTPError as error:
            if error.status in (401, 403):
                error.response.close()
                raise FreshAuthRequired() from None
            raise

    def real_download(self, filename, info_dict):
        filename = Path(filename)
        partial = _target(self.runtime, self.attempt, str(filename) + ".part")
        if os.path.lexists(filename):
            raise OutputPathError("Existing download output is not owned by this transfer")
        headers = dict(info_dict.get("http_headers") or {})
        if any(key.lower() in ("range", "if-range") for key in headers):
            raise OutputPathError("Caller-supplied HTTP ranges cannot use verified resume")
        headers["Accept-Encoding"] = "identity"
        response = self._open(Request(info_dict["url"], headers=headers))
        offset = 0
        digest = hashlib.sha256()
        try:
            if response.status != 200:
                raise OutputPathError("Unexpected initial HTTP response; partial files retained")
            length = response.headers.get("Content-Length")
            length = int(length) if isinstance(length, str) and length.isdecimal() else None
            etag = response.headers.get("ETag")
            source = source_validator(response.url, str(info_dict.get("format_id") or ""), etag, length)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                source = None
                length = None
            if source is not None:
                try:
                    offset = restore(self.runtime, self.attempt, partial, source)
                except (OSError, ValueError) as error:
                    raise OutputPathError("Partial ownership could not be verified; files retained") from error
            if offset and offset < length:
                response.close()
                response = self._open(Request(info_dict["url"], headers={**headers, "Range": f"bytes={offset}-", "If-Range": etag}))
                start, end, total = parse_http_range(response.headers.get("Content-Range"))
                current = source_validator(response.url, str(info_dict.get("format_id") or ""), response.headers.get("ETag"), total)
                if (response.status != 206 or current != source or start != offset or end != length - 1
                        or response.headers.get("Content-Length") != str(length - offset)
                        or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                    raise OutputPathError("HTTP source or Range response changed; partial files retained")
            if offset:
                with _file(partial) as file:
                    while block := file.read(COPY_BLOCK):
                        digest.update(block)
            elif os.path.lexists(partial):
                raise OutputPathError("Existing partial is not owned by this transfer")
            flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | (os.O_APPEND if offset else os.O_CREAT | os.O_EXCL)
            with os.fdopen(os.open(partial, flags, 0o600), "ab") as output:
                metadata = os.fstat(output.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != offset:
                    raise OutputPathError("Partial file changed before append")
                output.flush()
                os.fsync(output.fileno())
                if source is not None:
                    checkpoint(self.runtime, self.attempt, partial, source, offset, digest.hexdigest())
                else:
                    with closing(_connect(self.runtime)) as db, db:
                        _active(db, self.attempt)
                count = offset
                last_checkpoint = offset
                start_time = time.time()
                block_size = 1024
                while count != length and (block := response.read(block_size)):
                    block_size = 65536
                    if length is not None and count + len(block) > length:
                        raise OutputPathError("HTTP source exceeds its declared length; partial retained")
                    output.write(block)
                    digest.update(block)
                    count += len(block)
                    # Keep one rolling digest. Recovery verifies only the fsynced prefix,
                    # retaining any later crash tail without adopting it.
                    if source is not None and (last_checkpoint == offset or count - last_checkpoint >= COPY_BLOCK):
                        output.flush()
                        os.fsync(output.fileno())
                        checkpoint(self.runtime, self.attempt, partial, source, count, digest.hexdigest())
                        last_checkpoint = count
                    now = time.time()
                    self.slow_down(start_time, now, count - offset)
                    self._hook_progress({"status": "downloading", "downloaded_bytes": count,
                                         "total_bytes": length, "filename": str(filename), "tmpfilename": str(partial),
                                         "speed": self.calc_speed(start_time, now, count-offset),
                                         "eta": self.calc_eta(start_time, now, length-offset, count-offset) if length else None}, info_dict)
                output.flush()
                os.fsync(output.fileno())
                if source is not None:
                    checkpoint(self.runtime, self.attempt, partial, source, count, digest.hexdigest())
                if length is not None and count != length:
                    raise RuntimeError("HTTP download interrupted before the declared length")
            os.link(partial, filename)  # Preserve no-replace semantics inside the attempt too.
            partial.unlink()
            self._hook_progress({"status": "finished", "downloaded_bytes": count,
                                 "total_bytes": count, "filename": str(filename)}, info_dict)
            return True
        finally:
            response.close()
