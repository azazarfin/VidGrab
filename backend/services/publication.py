"""No-replace publication with retained file witnesses and crash reconciliation."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from backend.runtime import OutputPathError
from backend.services.artifacts import (append_journal, append_journal_result, is_link, read_journal,
                                        retained_artifacts, result_artifacts, validate_journal_record,
                                        validate_output_path, validate_workspace)


def file_identity(metadata):
    if metadata.st_ino == 0:
        raise OutputPathError("The filesystem does not provide verifiable file identities")
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


@contextmanager
def owned_file(path, expected=None, *, unshared=False):
    before = path.lstat()
    if is_link(path) or not stat.S_ISREG(before.st_mode) or unshared and before.st_nlink != 1:
        raise OutputPathError("Publication files must be regular, owned files without unsafe links")
    identity = file_identity(before)
    if expected is not None and identity != expected:
        raise OutputPathError("Publication file ownership no longer matches its journal")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        opened = os.fstat(file.fileno())
        if file_identity(opened) != identity or not stat.S_ISREG(opened.st_mode) or (
                getattr(opened, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise OutputPathError("Publication file changed while it was opened")
        if unshared and opened.st_nlink != 1:
            raise OutputPathError("Publication source has a foreign hard link")
        yield file


def fingerprint(path, expected=None, *, unshared=False):
    with owned_file(path, expected, unshared=unshared) as file:
        before = os.fstat(file.fileno())
        digest = hashlib.file_digest(file, "sha256").hexdigest()
        after = os.fstat(file.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OutputPathError("Publication file changed while its content was verified")
        return {"identity": file_identity(after), "size": after.st_size, "sha256": digest}


def verify_parent(runtime, record):
    output = validate_output_path(runtime, record["path"])
    if str(output.parent.resolve(strict=True)) != record["parent"] or file_identity(output.parent.stat()) != record["parent_identity"]:
        raise OutputPathError("Publication destination directory no longer matches its journal")
    return output


def verify_witness(record):
    evidence = fingerprint(Path(record["witness"]), record["identity"])
    if evidence["size"] != record["size"] or evidence["sha256"] != record["sha256"]:
        raise OutputPathError("Publication content no longer matches its journal")


def verify_published(runtime, record):
    output = verify_parent(runtime, record)
    metadata = output.lstat()
    if not stat.S_ISREG(metadata.st_mode) or is_link(output) or file_identity(metadata) != record["identity"]:
        raise OutputPathError("Publication destination has foreign file ownership")
    verify_witness(record)
    return {key: record[key] for key in ("path", "kind", "size")} | {"state": "complete"}


def sync_directory(directory):
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def plan_publications(runtime, directory, moves):
    """Journal every ready witness before any final filename can be created."""
    workspace = validate_workspace(runtime, str(directory))
    planned = []
    destinations = set()
    for move in moves:
        source = Path(move["source"])
        output = validate_output_path(runtime, move["path"])
        if os.path.lexists(output):
            raise OutputPathError("Output already exists; choose a different filename before retrying")
        resolved = os.path.normcase(str(output.resolve()))
        if resolved in destinations:
            raise OutputPathError("Multiple artifacts select the same output destination")
        destinations.add(resolved)
        validate_journal_record(runtime, workspace, {**move, "state": "partial", "size": 0})
        evidence = fingerprint(source, unshared=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        parent = output.parent.stat()
        record = {**move, **evidence, "state": "partial", "version": 2, "phase": "ready",
                  "source_identity": evidence["identity"], "witness": str(source),
                  "parent": str(output.parent.resolve(strict=True)), "parent_identity": file_identity(parent)}
        if evidence["identity"]["device"] != parent.st_dev:
            # Hard links never cross volumes. Copy exclusively on the selected
            # destination volume, then retain that inode as the publication witness.
            witness = output.parent / f".vidgrab-{workspace.name}-{uuid4()}.part"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(witness, flags, 0o600), "wb") as target:
                record.update(witness=str(witness), identity=file_identity(os.fstat(target.fileno())), phase="copying")
                append_journal(runtime, workspace, [record])
                with owned_file(source, record["source_identity"], unshared=True) as original:
                    shutil.copyfileobj(original, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            copied = fingerprint(witness, record["identity"])
            if copied["size"] != record["size"] or copied["sha256"] != record["sha256"]:
                raise OutputPathError("Publication source changed during its cross-volume copy")
            sync_directory(output.parent)
            record["phase"] = "ready"
        planned.append(record)
    if not planned:
        raise ValueError("Publication requires at least one artifact")
    append_journal(runtime, workspace, planned)
    return planned


def publish_artifact(runtime, directory, record):
    workspace = validate_workspace(runtime, str(directory))
    record = validate_journal_record(runtime, workspace, record)
    if record.get("phase") != "ready":
        raise OutputPathError("Publication requires a ready, journaled ownership witness")
    output = verify_parent(runtime, record)
    verify_witness(record)
    try:
        os.link(record["witness"], output, follow_symlinks=False)
    except OSError as error:
        raise OutputPathError("Cannot publish without replacing an existing output; files retained") from error
    sync_directory(output.parent)
    completed = {**record, "state": "complete", "phase": "published"}
    verify_published(runtime, completed)
    append_journal(runtime, workspace, [completed])
    return completed


def record_result(runtime, directory, result):
    records, previous = read_journal(runtime, directory)
    if previous is not None:
        raise ValueError("Publication already has a terminal result")
    reported = result_artifacts(result)
    if {(record["path"], record["kind"]) for record in records} != {(record["path"], record["kind"]) for record in reported}:
        raise OutputPathError("Worker result does not match its full publication journal")
    for record in records:
        if record.get("version") != 2 or record["state"] != "complete":
            raise OutputPathError("Worker result contains an incomplete or unverifiable publication")
        verify_published(runtime, record)
    append_journal_result(runtime, directory, result)


@dataclass
class PublicationRecovery:
    artifacts: list
    result: dict | None = None
    error: str | None = None
    witness_paths: frozenset = frozenset()


def reconcile_publication(runtime, attempt):
    """Read-only reconciliation; callers must first verify all worker trees exited."""
    if attempt.workspace != runtime.data_dir / "attempts" / attempt.id:
        raise OutputPathError("Persisted workspace does not match the attempt identity")
    try:
        attempt.workspace.lstat()
    except FileNotFoundError:
        return PublicationRecovery([])  # The claimed worker may never have started.
    retained = retained_artifacts(runtime, attempt)
    recovery = PublicationRecovery(retained)
    try:
        records, result = read_journal(runtime, attempt.workspace)
    except (OSError, ValueError) as error:
        recovery.error = f"Interrupted publication journal is unverifiable; files retained: {error}"
        return recovery
    published = []
    for record in records:
        try:
            output = Path(record["path"])
            if record.get("version") != 2:
                if os.path.lexists(output) or record["state"] == "complete":
                    raise OutputPathError("Interrupted publication has no verifiable file ownership")
                continue
            output = verify_parent(runtime, record)
            if record["phase"] == "copying":
                with owned_file(Path(record["witness"]), record["identity"]) as file:
                    size = os.fstat(file.fileno()).st_size
                recovery.artifacts.append({"path": record["witness"], "kind": "auxiliary", "state": "partial", "size": size})
                if os.path.lexists(output):
                    raise OutputPathError("Incomplete publication copy has an occupied destination")
                continue
            verify_witness(record)
            if record["witness"] != record["source"]:
                recovery.artifacts.append({"path": record["witness"], "kind": "auxiliary", "state": "partial", "size": record["size"]})
            if os.path.lexists(output):
                published.append(verify_published(runtime, record))
            elif record["state"] == "complete":
                raise OutputPathError("A journaled completed publication is missing its output")
        except (OSError, ValueError) as error:
            recovery.error = f"Interrupted publication ownership is unverifiable; files retained: {error}"
    recovery.artifacts.extend(published)
    if result is not None and recovery.error is None:
        expected = {(record["path"], record["kind"]) for record in result_artifacts(result)}
        if expected != {(record["path"], record["kind"]) for record in published} or any(
                record.get("version") != 2 or record["state"] != "complete" for record in records):
            recovery.error = "Interrupted worker result has missing or incomplete publications; files retained"
        else:
            recovery.result = result
            recovery.witness_paths = frozenset(path for record in records for path in (record["source"], record["witness"]))
    if published and result is None and recovery.error is None:
        recovery.error = "Download interrupted after publishing outputs; files retained. Inspect them and choose a different filename before retrying"
    return recovery


def cleanup_publication(runtime, attempt):
    """Release verified witnesses only after the caller's success commit."""
    if attempt.workspace != runtime.data_dir / "attempts" / attempt.id:
        raise OutputPathError("Persisted workspace does not match the attempt identity")
    if not os.path.lexists(attempt.workspace):
        return
    records, result = read_journal(runtime, attempt.workspace)
    if result is None:
        return
    if any(record.get("version") != 2 or record["state"] != "complete" for record in records) or (
            {(record["path"], record["kind"]) for record in records}
            != {(record["path"], record["kind"]) for record in result_artifacts(result)}):
        raise OutputPathError("Retaining publication witnesses with an unverifiable completion journal")
    for record in records:
        witness = Path(record["witness"])
        if not os.path.lexists(witness):
            continue  # Repeated cleanup never adopts a subsequently created file.
        verify_published(runtime, record)
        if record["source"] != record["witness"]:
            source = Path(record["source"])
            if os.path.lexists(source):
                original = fingerprint(source, record["source_identity"], unshared=True)
                if original["sha256"] != record["sha256"] or original["size"] != record["size"]:
                    raise OutputPathError("Retaining a changed publication source after completion")
                source.unlink()
        if file_identity(witness.lstat()) != record["identity"] or is_link(witness):
            raise OutputPathError("Retaining a changed publication witness after completion")
        witness.unlink()
