"""Opt-in Windows junction refusals, using temporary paths and no processes/network."""

import argparse
import asyncio
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.db.jobs import Attempt
from backend import cookie_manager
from backend.app import create_app
from backend.runtime import OutputPathError, RuntimeOwnershipError
from backend.engine.runtime_owner import RuntimeOwner
from backend.services import artifacts
from backend.services import publication
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

    def test_runtime_owner_directory_junction_is_refused_before_creating_foreign_files(self):
        runtime = self.runtime()
        foreign = self.root / "foreign-state"
        foreign.mkdir()
        sentinel = foreign / "preserved"
        sentinel.write_bytes(b"foreign state bytes")
        self.junction(foreign, runtime.data_dir)
        owner = RuntimeOwner(runtime)
        with self.assertRaises(RuntimeOwnershipError):
            owner.lock()
        self.assertIsNone(owner.fd)
        self.assertEqual(list(foreign.iterdir()), [sentinel])

    def test_runtime_owner_lock_junction_is_refused_before_opening_foreign_files(self):
        runtime, _ = self.prepared()
        foreign = self.root / "foreign-lock"
        foreign.mkdir()
        sentinel = foreign / "preserved"
        sentinel.write_bytes(b"foreign lock bytes")
        self.junction(foreign, runtime.data_dir / "engine-owner.lock")
        owner = RuntimeOwner(runtime)
        with self.assertRaises(RuntimeOwnershipError):
            owner.lock()
        self.assertIsNone(owner.fd)
        self.assertEqual(sentinel.read_bytes(), b"foreign lock bytes")

    def test_publication_parent_junction_change_preserves_foreign_destination(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        source = attempt.workspace / "finished.mp3"
        source.write_bytes(b"owned")
        output = runtime.download_dir / source.name
        record = publication.plan_publications(runtime, attempt.workspace,
                                              [{"source": str(source), "path": str(output), "kind": "media"}])[0]
        moved = self.root / "previous-media"
        runtime.download_dir.rename(moved)
        foreign = self.root / "foreign-media"
        foreign.mkdir()
        sentinel = foreign / source.name
        sentinel.write_bytes(b"foreign")
        self.junction(foreign, runtime.download_dir)
        with self.assertRaises(OutputPathError):
            publication.publish_artifact(runtime, attempt.workspace, record)
        self.assertEqual(sentinel.read_bytes(), b"foreign")
        self.assertEqual(source.read_bytes(), b"owned")

    def test_source_parent_junction_refuses_foreign_publication_input(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        foreign = self.root / "foreign-source"
        foreign.mkdir()
        sentinel = foreign / "finished.mp3"
        sentinel.write_bytes(b"foreign")
        linked = attempt.workspace / "linked-source"
        self.junction(foreign, linked)
        output = runtime.download_dir / sentinel.name
        with self.assertRaises(OutputPathError):
            publication.plan_publications(runtime, attempt.workspace,
                                          [{"source": str(linked / sentinel.name), "path": str(output), "kind": "media"}])
        self.assertFalse(output.exists())
        self.assertEqual(sentinel.read_bytes(), b"foreign")

    def test_published_destination_junction_is_not_adopted_by_recovery(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        source = attempt.workspace / "finished.mp3"
        source.write_bytes(b"owned")
        output = runtime.download_dir / source.name
        record = publication.plan_publications(runtime, attempt.workspace,
                                              [{"source": str(source), "path": str(output), "kind": "media"}])[0]
        publication.publish_artifact(runtime, attempt.workspace, record)
        publication.record_result(runtime, attempt.workspace, {"filepath": str(output)})
        output.unlink()
        foreign = self.root / "foreign-output"
        foreign.mkdir()
        sentinel = foreign / "preserved"
        sentinel.write_bytes(b"foreign")
        self.junction(foreign, output)
        recovery = publication.reconcile_publication(runtime, attempt)
        self.assertIsNotNone(recovery.error)
        self.assertFalse(any(record["kind"] == "media" for record in recovery.artifacts))
        self.assertEqual(sentinel.read_bytes(), b"foreign")
        self.assertEqual(source.read_bytes(), b"owned")

    def cookie_check(self, check):
        async def run():
            runtime = self.runtime("cookie-state")
            application = create_app(runtime, start_queue=False)
            async with application.router.lifespan_context(application):
                job = await application.state.queue.enqueue("https://fixture.invalid/media", "best")
                attempt = await application.state.queue._claim(job)
                artifacts.prepare_workspace(runtime, attempt)
                path = Path(cookie_manager.create_cookie_file(
                    [{"domain": ".fixture.invalid", "name": "session", "value": "owned-fixture"}], runtime, attempt))
                with closing(sqlite3.connect(runtime.db_path)) as db:
                    record = json.loads(db.execute("SELECT ownership FROM attempt_cookie_files WHERE attempt_id=?", (attempt.id,)).fetchone()[0])
                await check(application, attempt, path, record)
        asyncio.run(run())

    def test_cookie_metadata_junction_is_refused_before_creating_any_foreign_file(self):
        runtime, attempt = self.prepared()
        artifacts.prepare_workspace(runtime, attempt)
        foreign = self.root / "foreign-cookie-metadata"
        foreign.mkdir()
        sentinel = foreign / "preserved"
        sentinel.write_bytes(b"foreign bytes")
        self.junction(foreign, attempt.workspace / ".vidgrab")
        with self.assertRaises(OutputPathError):
            cookie_manager.create_cookie_file([{"name": "session", "value": "owned-fixture"}], runtime, attempt)
        self.assertEqual(list(foreign.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"foreign bytes")

    def test_replaced_cookie_directory_junction_refuses_cleanup_and_preserves_both_trees(self):
        async def check(application, attempt, path, _record):
            directory = path.parent
            saved = directory.with_name("saved-cookie-directory")
            directory.rename(saved)
            foreign = self.root / "foreign-cookie-directory"
            foreign.mkdir()
            sentinel = foreign / path.name
            sentinel.write_bytes(b"foreign cookie bytes")
            self.junction(foreign, directory)
            try:
                with self.assertRaises(OutputPathError):
                    await application.state.queue.attempts.cleanup_cookies(attempt)
                self.assertEqual(sentinel.read_bytes(), b"foreign cookie bytes")
                self.assertIn(b"owned-fixture", (saved / path.name).read_bytes())
            finally:
                directory.rmdir()
                saved.rename(directory)
        self.cookie_check(check)

    def test_cookie_file_junction_refuses_cleanup_before_touching_its_target(self):
        async def check(application, attempt, path, _record):
            saved = path.with_name("saved-cookie")
            path.rename(saved)
            foreign = self.root / "foreign-cookie-target"
            foreign.mkdir()
            sentinel = foreign / "preserved"
            sentinel.write_bytes(b"foreign bytes")
            self.junction(foreign, path)
            try:
                with self.assertRaises(OutputPathError):
                    await application.state.queue.attempts.cleanup_cookies(attempt)
                self.assertEqual(sentinel.read_bytes(), b"foreign bytes")
                self.assertIn(b"owned-fixture", saved.read_bytes())
            finally:
                path.rmdir()
                saved.rename(path)
        self.cookie_check(check)

    def test_cookie_dacl_grants_access_only_to_the_current_account_and_system(self):
        async def check(_application, _attempt, path, record):
            account = current_windows_sid()
            for selected in (path.parent, path, Path(record["witness"])):
                descriptor = windows_dacl(selected)
                identities = re.findall(r";([^;()]*)\)", descriptor)
                self.assertEqual(set(identities), {account, "SY"})
                self.assertEqual(len(identities), 2)
                if selected == path.parent:
                    self.assertTrue(descriptor.startswith("D:P"), "The cookie directory must reject inherited access")
        self.cookie_check(check)


def current_windows_sid():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    text = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertSidToStringSidW(ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if token.value:
            kernel.CloseHandle(token)


def windows_dacl(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    data = ctypes.create_string_buffer(size.value)
    if not advapi.GetFileSecurityW(str(path), 4, data, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    text = wintypes.LPWSTR()
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(data, 1, 4, ctypes.byref(text), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ArtifactReparseChecks))
    args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "tests": result.testsRun,
        "skipped": [str(case) for case, _ in result.skipped], "seconds": round(time.monotonic() - started, 3),
        "module_sha256": hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest(),
        "owner_module_sha256": hashlib.sha256(Path(sys.modules[RuntimeOwner.__module__].__file__).read_bytes()).hexdigest(),
        "publication_module_sha256": hashlib.sha256(Path(publication.__file__).read_bytes()).hexdigest()}, indent=2))
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
