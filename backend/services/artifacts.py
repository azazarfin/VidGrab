"""Literal output records and exclusively created, retained attempt workspaces."""

import json
import os
import stat
from pathlib import Path
from uuid import UUID

from backend.runtime import OutputPathError

ARTIFACT_KINDS = frozenset({"media", "subtitle", "thumbnail", "auxiliary"})
JOURNAL_DIRECTORY = ".vidgrab"
JOURNAL_LIMIT = 1024 * 1024


def is_link(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def validate_workspace(runtime, directory):
    path = Path(directory)
    if path.parent != runtime.data_dir / "attempts":
        raise OutputPathError("Worker workspace must be an owned runtime attempt directory")
    try:
        if str(UUID(path.name)) != path.name:
            raise ValueError
        if is_link(path.parent) or is_link(path) or not path.is_dir():
            raise ValueError
    except (ValueError, OSError) as error:
        raise OutputPathError("Worker workspace is missing or has an unsafe link") from error
    return path


def prepare_workspace(runtime, attempt):
    expected = runtime.data_dir / "attempts" / attempt.id
    if attempt.workspace != expected:
        raise OutputPathError("Attempt workspace does not match its runtime identity")
    parent = expected.parent
    parent.mkdir(mode=0o700, exist_ok=True)
    if is_link(parent):
        raise OutputPathError("Attempt workspace root must not be a link")
    expected.mkdir(mode=0o700)  # An existing tree is not proof of ownership.
    return validate_workspace(runtime, str(expected))


def result_artifacts(result):
    filepath = result.get("filepath") if isinstance(result, dict) else None
    if not isinstance(filepath, str) or not filepath or "\0" in filepath:
        raise ValueError("Worker result requires a literal file path")
    records = result.get("artifacts", [{"path": filepath, "kind": "media"}])
    if not isinstance(records, list) or not records:
        raise ValueError("Worker artifacts must be a nonempty list")
    seen = set()
    for record in records:
        path = record.get("path") if isinstance(record, dict) else None
        kind = record.get("kind") if isinstance(record, dict) else None
        if not isinstance(path, str) or not path or "\0" in path or path in seen:
            raise ValueError("Artifact paths must be unique literal strings")
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise ValueError("Invalid artifact kind")
        seen.add(path)
    if not any(record["path"] == filepath and record["kind"] == "media" for record in records):
        raise ValueError("The primary file path must identify a media artifact")
    return [{"path": record["path"], "kind": record["kind"]} for record in records]


def validate_output_path(runtime, literal):
    path = Path(literal)
    if not path.is_absolute():
        raise OutputPathError("Final output records must contain absolute paths")
    runtime.validate_output(path.parent, path.name)
    if os.path.lexists(path) and is_link(path):
        raise OutputPathError("Final outputs must not be symbolic links or reparse points")
    return path


def completed_artifacts(runtime, result):
    artifacts = []
    for record in result_artifacts(result):
        path = validate_output_path(runtime, record["path"])
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OutputPathError("A reported final output is not a regular file")
        artifacts.append({**record, "state": "complete", "size": metadata.st_size})
    return artifacts


def retained_artifacts(runtime, attempt):
    workspace = validate_workspace(runtime, str(attempt.workspace))
    records = []
    for directory, directories, files in os.walk(workspace, followlinks=False):
        parent = Path(directory)
        directories[:] = sorted(name for name in directories if not is_link(parent / name)
                                and not (parent == workspace and name == JOURNAL_DIRECTORY))
        for name in sorted(files):
            path = parent / name
            if is_link(path):
                continue
            metadata = path.stat()
            if stat.S_ISREG(metadata.st_mode):
                records.append({"path": str(path), "kind": "auxiliary", "state": "partial", "size": metadata.st_size})
    return records


def validate_journal_record(runtime, workspace, record):
    if not isinstance(record, dict):
        raise ValueError("Artifact journal records must be objects")
    source = record.get("source")
    if not isinstance(source, str) or not source or "\0" in source:
        raise ValueError("Artifact journal sources must be literal paths")
    source_path = Path(source)
    if not source_path.is_absolute() or not source_path.resolve().is_relative_to(workspace):
        raise OutputPathError("Artifact journal source is outside its owned workspace")
    if source_path.resolve().is_relative_to(workspace / JOURNAL_DIRECTORY):
        raise OutputPathError("Artifact journal metadata is not a media output")
    path = record.get("path")
    kind = record.get("kind")
    state = record.get("state")
    size = record.get("size")
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Artifact journal output must be a literal path")
    validate_output_path(runtime, path)
    if not isinstance(kind, str) or kind not in ARTIFACT_KINDS or state not in ("partial", "complete"):
        raise ValueError("Invalid artifact journal kind/state")
    if type(size) is not int or size < 0:
        raise ValueError("Artifact journal size must be a nonnegative integer")
    return {"source": source, "path": path, "kind": kind, "state": state, "size": size}


def journal_path(workspace, *, create=False):
    directory = workspace / JOURNAL_DIRECTORY
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if is_link(directory) or not directory.is_dir():
        raise OutputPathError("Artifact journal directory must not be a link")
    path = directory / "artifacts.jsonl"
    if os.path.lexists(path) and (is_link(path) or path.stat().st_nlink != 1):
        raise OutputPathError("Artifact journal must be an unshared regular file")
    return path


def append_journal(runtime, directory, records):
    workspace = validate_workspace(runtime, str(directory))
    records = [validate_journal_record(runtime, workspace, record) for record in records]
    encoded = b"".join((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8") for record in records)
    path = journal_path(workspace, create=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "ab") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OutputPathError("Artifact journal must be an unshared regular file")
        if metadata.st_size + len(encoded) > JOURNAL_LIMIT:
            raise ValueError("Artifact journal exceeds its size limit")
        file.write(encoded)
        file.flush()
        os.fsync(file.fileno())


def journal_artifacts(runtime, attempt):
    workspace = validate_workspace(runtime, str(attempt.workspace))
    try:
        path = journal_path(workspace)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > JOURNAL_LIMIT:
                raise ValueError("Invalid or oversized artifact journal")
            encoded = file.read(JOURNAL_LIMIT + 1)
    except FileNotFoundError:
        return []
    if len(encoded) > JOURNAL_LIMIT:
        raise ValueError("Artifact journal exceeds its size limit")
    records = {}
    for line in encoded.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # An interrupted final append does not invalidate earlier fsynced frames.
        record = validate_journal_record(runtime, workspace, json.loads(line))
        records[record["path"]] = {key: record[key] for key in ("path", "kind", "state", "size")}
    return list(records.values())


def recovery_artifacts(runtime, attempt):
    if attempt.workspace != runtime.data_dir / "attempts" / attempt.id:
        raise OutputPathError("Persisted workspace does not match the attempt identity")
    try:
        attempt.workspace.lstat()
    except FileNotFoundError:
        return []  # A claimed worker may never have opened its resources.
    records = {record["path"]: record for record in journal_artifacts(runtime, attempt)}
    records.update({record["path"]: record for record in retained_artifacts(runtime, attempt)})
    return list(records.values())
