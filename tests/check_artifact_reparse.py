"""Opt-in Windows junction refusals, using temporary paths and no processes/network."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.db.jobs import Attempt
from backend.runtime import OutputPathError
from backend.services import artifacts
from tests.support import OfflineTestCase


@unittest.skipUnless(sys.platform == "win32", "Windows junction fixtures")
class ArtifactReparseChecks(OfflineTestCase):
    def prepared(self):
        runtime = self.runtime()
        runtime.prepare()
        identity = str(uuid4())
        return runtime, Attempt(identity, "fixture", 1, runtime.data_dir / "attempts" / identity)

    @staticmethod
    def junction(source, destination):
        import _winapi
        _winapi.CreateJunction(str(source), str(destination))

    def test_workspace_root_junction_is_refused_before_creating_foreign_files(self):
        runtime, attempt = self.prepared()
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "unrelated.part"
        sentinel.write_bytes(b"preserve")
        self.junction(foreign, runtime.data_dir / "attempts")
        with self.assertRaises(OutputPathError):
            artifacts.prepare_workspace(runtime, attempt)
        self.assertEqual(list(foreign.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_journal_directory_junction_is_refused_before_reading(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "artifacts.jsonl"
        sentinel.write_bytes(b"foreign metadata")
        self.junction(foreign, attempt.workspace / ".vidgrab")
        original_open = artifacts.os.open

        def guarded_open(path, *args, **kwargs):
            self.assertFalse(Path(path).resolve().is_relative_to(foreign), "Foreign metadata must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(artifacts.os, "open", guarded_open):
            with self.assertRaises(OutputPathError):
                artifacts.journal_artifacts(runtime, attempt)
        self.assertEqual(sentinel.read_bytes(), b"foreign metadata")

    def test_retained_scan_prunes_a_foreign_directory_junction(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        partial = attempt.workspace / "owned.part"
        partial.write_bytes(b"partial")
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "other.part"
        sentinel.write_bytes(b"foreign data")
        self.junction(foreign, attempt.workspace / "linked-directory")
        self.assertEqual(artifacts.retained_artifacts(runtime, attempt),
                         [{"path": str(partial), "kind": "auxiliary", "state": "partial", "size": 7}])
        self.assertEqual(sentinel.read_bytes(), b"foreign data")

    def test_dangling_output_junction_is_refused_before_publication(self):
        runtime, _ = self.prepared()
        missing = self.root / "missing-output"
        missing.mkdir()
        output = runtime.download_dir / "finished.mp3"
        self.junction(missing, output)
        missing.rmdir()
        with self.assertRaises(OutputPathError):
            artifacts.validate_output_path(runtime, str(output))
        self.assertTrue(artifacts.os.path.lexists(output))
        self.assertFalse(missing.exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ArtifactReparseChecks))
    args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "tests": result.testsRun,
        "skipped": [str(case) for case, _ in result.skipped], "seconds": round(time.monotonic() - started, 3),
        "module_sha256": hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest()}, indent=2))
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
