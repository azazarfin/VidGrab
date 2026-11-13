"""Workspace ownership must never follow a foreign path into cleanup or tracking."""

import importlib.util
import sys
import unittest
from uuid import uuid4

from tests.support import OfflineTestCase


class ArtifactOwnershipTests(OfflineTestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.artifacts"),
                             "Attempt workspaces need explicit ownership validation")
        from backend.services import artifacts
        return artifacts

    def attempt(self, runtime):
        from backend.db.jobs import Attempt
        identity = str(uuid4())
        return Attempt(identity, "job", 1, runtime.data_dir / "attempts" / identity)

    def journal_service(self):
        service = self.service()
        self.assertTrue(callable(getattr(service, "append_journal", None)),
                        "Output publication needs an owned durable journal")
        return service

    def test_journal_retains_completed_outputs_without_a_worker_result(self):
        service = self.journal_service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        output = runtime.download_dir / "published বাংলা.mp3"
        output.write_bytes(b"media")
        record = {"source": str(attempt.workspace / "published.mp3"), "path": str(output),
                  "kind": "media", "state": "complete", "size": 5}
        service.append_journal(runtime, attempt.workspace, [record])
        self.assertEqual(service.journal_artifacts(runtime, attempt),
                         [{"path": str(output), "kind": "media", "state": "complete", "size": 5}])

    def test_truncated_last_journal_frame_keeps_preceding_durable_records(self):
        service = self.journal_service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        output = runtime.download_dir / "planned.mp3"
        record = {"source": str(attempt.workspace / "planned.mp3"), "path": str(output),
                  "kind": "media", "state": "partial", "size": 5}
        service.append_journal(runtime, attempt.workspace, [record])
        journal = attempt.workspace / ".vidgrab" / "artifacts.jsonl"
        with journal.open("ab") as file:
            file.write(b'{"interrupted":')
        self.assertEqual(service.journal_artifacts(runtime, attempt),
                         [{"path": str(output), "kind": "media", "state": "partial", "size": 5}])

    def test_journal_refuses_a_source_outside_the_owned_workspace(self):
        service = self.journal_service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        with self.assertRaises(ValueError):
            service.append_journal(runtime, attempt.workspace, [{"source": str(runtime.download_dir / "foreign.part"),
                "path": str(runtime.download_dir / "foreign.mp3"), "kind": "media", "state": "complete", "size": 1}])
        self.assertEqual(service.retained_artifacts(runtime, attempt), [])

    @unittest.skipIf(sys.platform == "win32", "This fixture requires POSIX symlink creation")
    def test_journal_metadata_link_is_refused_without_reading_foreign_files(self):
        service = self.journal_service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "artifacts.jsonl"
        sentinel.write_bytes(b"private foreign data")
        (attempt.workspace / ".vidgrab").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ValueError):
            service.journal_artifacts(runtime, attempt)
        self.assertEqual(sentinel.read_bytes(), b"private foreign data")

    def test_workspace_creation_refuses_an_existing_directory(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        sentinel = attempt.workspace / "unrelated.part"
        sentinel.write_bytes(b"existing")
        with self.assertRaises(Exception):
            service.prepare_workspace(runtime, attempt)
        self.assertEqual(sentinel.read_bytes(), b"existing")

    def test_workspace_must_be_the_exact_runtime_attempt_directory(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        with self.assertRaises(ValueError):
            service.validate_workspace(runtime, str(runtime.download_dir))

    @unittest.skipIf(sys.platform == "win32", "This fixture requires POSIX symlink creation")
    def test_dangling_output_symlink_is_refused_without_creating_its_target(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        missing = self.root / "missing.mp3"
        output = runtime.download_dir / "finished.mp3"
        output.symlink_to(missing)
        with self.assertRaises(ValueError):
            service.validate_output_path(runtime, str(output))
        self.assertTrue(output.is_symlink())
        self.assertFalse(missing.exists())

    @unittest.skipIf(sys.platform == "win32", "This fixture requires POSIX symlink creation")
    def test_dangling_journal_symlink_is_refused_before_open(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        metadata = attempt.workspace / ".vidgrab"
        metadata.mkdir()
        missing = self.root / "missing-journal"
        journal = metadata / "artifacts.jsonl"
        journal.symlink_to(missing)
        with self.assertRaises(ValueError):
            service.journal_path(attempt.workspace)
        self.assertTrue(journal.is_symlink())
        self.assertFalse(missing.exists())

    @unittest.skipIf(sys.platform == "win32", "This fixture requires POSIX symlink creation")
    def test_workspace_root_symlink_is_refused(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (runtime.data_dir / "attempts").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ValueError):
            service.prepare_workspace(runtime, self.attempt(runtime))
        self.assertEqual(list(foreign.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "This fixture requires POSIX symlink creation")
    def test_retained_artifact_scan_does_not_follow_foreign_links(self):
        service = self.service()
        runtime = self.runtime()
        runtime.prepare()
        attempt = self.attempt(runtime)
        service.prepare_workspace(runtime, attempt)
        partial = attempt.workspace / "owned.part"
        partial.write_bytes(b"partial")
        foreign = self.root / "foreign"
        foreign.mkdir()
        secret = foreign / "other.part"
        secret.write_bytes(b"foreign data")
        (attempt.workspace / "linked-directory").symlink_to(foreign, target_is_directory=True)
        (attempt.workspace / "linked-file").symlink_to(secret)
        self.assertEqual(service.retained_artifacts(runtime, attempt),
                         [{"path": str(partial), "kind": "auxiliary", "state": "partial", "size": 7}])
        self.assertEqual(secret.read_bytes(), b"foreign data")
