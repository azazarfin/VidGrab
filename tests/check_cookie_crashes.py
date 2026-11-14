"""Opt-in cookie creation, delivery and cleanup crashes with actual engine recovery.

An owned source app stops at instrumented boundaries. --engine uses fresh
frozen worker modes and the entire frozen service for restart/reconciliation.
Cookie values are local fixtures, never user/browser credentials.
"""

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from backend.app import create_app
from backend import cookie_manager
from backend.db import cookies as cookie_database
from backend.engine.execution import ProcessDownloadExecutor
from backend.engine.network_guard import fixture_network_guard
from backend.engine.process_identity import ProcessIdentity
from backend.engine.resources import check_components
from backend.runtime import RuntimeConfig
from tests import check_service_recovery as recovery
from tests.check_worker_execution import WaveServer
from tests.worker_process_support import snapshot

COOKIE_VALUE = "owned-crash-fixture"
COOKIES = [{"domain": "127.0.0.1", "path": "/", "name": "vidgrab_fixture",
            "value": COOKIE_VALUE, "expirationDate": 1900000000}]
PHASES = ("before-registration", "after-registration", "during-write", "after-write-before-ready",
          "ready-before-delivery", "delivered", "cleanup-before-unlinks",
          "cleanup-between-unlinks", "cleanup-before-sqlite-delete")


class Producer(recovery.Service):
    def __init__(self, root, runtime, sequence, phase, url):
        super().__init__(root, runtime, sequence)
        self.phase, self.url = phase, url
        self.frame = None

    def start(self):
        command = [sys.executable, "-B", "-m", "tests.check_cookie_crashes", "--producer", "--tools", str(recovery.TOOLS)]
        if recovery.ENGINE:
            command.extend(["--engine", str(recovery.ENGINE)])
        with self.log.open("xb") as log:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                            close_fds=True, creationflags=0x8 if sys.platform == "win32" else 0)
        launcher = ProcessIdentity.capture(self.process.pid)
        bootstrap = {"data_dir": str(self.runtime.data_dir), "download_dir": str(self.runtime.download_dir),
                     "phase": self.phase, "url": self.url}
        self.process.stdin.write((json.dumps(bootstrap) + "\n").encode())
        self.process.stdin.flush()
        frames = queue.Queue()
        reader = threading.Thread(target=lambda: frames.put(self.process.stdout.readline(65537)), daemon=True)
        reader.start()
        try:
            line = frames.get(timeout=12)
            reader.join(timeout=1)
            if not line or not line.endswith(b"\n"):
                raise AssertionError(self.log.read_text(errors="replace")[-2500:])
            self.frame = json.loads(line)
            if self.frame.get("phase") != self.phase or self.frame.get("type") != "checkpoint":
                raise AssertionError("Invalid cookie crash checkpoint")
            identity = ProcessIdentity.capture(self.frame["pid"])
            if identity is None or launcher is None:
                raise AssertionError("Cannot verify the cookie producer")
            if identity != launcher:
                processes = {process.pid: process for process in snapshot()}
                current, seen = processes.get(identity.pid), set()
                while current and current.pid != launcher.pid and current.pid not in seen:
                    seen.add(current.pid)
                    current = processes.get(current.parent)
                if current is None or (current.pid, current.started) != (launcher.pid, launcher.started):
                    raise AssertionError("Cookie producer escaped its owned launcher")
            self.server_identity = identity.to_json()
        except BaseException:
            self.stop(hard=True)
            raise


class CookieCrashChecks(recovery.ServiceRecoveryFixture):
    def producer(self, phase, fixture):
        producer = Producer(self.root, self.runtime, len(self.services), phase, fixture.url)
        self.services.append(producer)
        producer.start()
        self.attempt = producer.frame["attempt"]
        return producer

    def crash(self, phase):
        completing = phase.startswith("cleanup-")
        with WaveServer(phase="download" if phase == "delivered" else None) as fixture:
            producer = self.producer(phase, fixture)
            frame = producer.frame
            ownership = frame["ownership"]
            path, witness = Path(ownership["path"]), Path(ownership["witness"])
            if phase == "before-registration":
                self.assertEqual(path.read_bytes(), b"")
                self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 0}])
            else:
                self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 1}])
            if phase == "during-write":
                self.assertIn(COOKIE_VALUE[:8].encode(), path.read_bytes())
            if phase == "cleanup-between-unlinks":
                self.assertFalse(path.exists())
                self.assertTrue(witness.exists())
            if phase == "cleanup-before-sqlite-delete":
                self.assertFalse(path.exists())
                self.assertFalse(witness.exists())
            if phase == "delivered" or completing:
                self.assertTrue(any(COOKIE_VALUE in header for header in fixture.cookie_headers),
                                "The real worker must load the forwarded jar")
            requests_before = len(fixture.cookie_headers)
            producer.stop(hard=True)
            self.wait_tree_exit(frame["attempt"])
            unrelated = self.runtime.temp_dir / "vidgrab_cookies_foreign.txt"
            unrelated.write_bytes(b"unrelated credential bytes")
            second = self.service()
            if phase == "before-registration":
                self.assertEqual(path.read_bytes(), b"", "Unregistered empty files stay unowned")
            else:
                self.assertFalse(path.exists())
                self.assertFalse(witness.exists())
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 0}])
            fixture.release.set()
            expected_status = "complete" if completing else "waiting_for_auth"
            recovery.wait_for(lambda: any(row["id"] == frame["attempt"]["download_id"] and row["status"] == expected_status
                                         for row in second.request("GET", "/spike/status")["jobs"]),
                              "Cookie crash restart did not reconcile its authenticated fixture", timeout=12)
            attempts = self.rows("SELECT id, number, status FROM download_attempts ORDER BY number")
            if completing:
                self.assertEqual(attempts, [{"id": frame["attempt"]["id"], "number": 1, "status": "complete"}])
                self.assertEqual(len(fixture.cookie_headers), requests_before, "Verified media must not be redownloaded")
            else:
                self.assertEqual([row["status"] for row in attempts], ["interrupted"])
                self.assertEqual(len(fixture.cookie_headers), requests_before,
                                 "A recovered authenticated job must wait without reusing discarded credentials")
            self.assertEqual(unrelated.read_bytes(), b"unrelated credential bytes")
            self.assertNotIn(COOKIE_VALUE.encode(), self.runtime.db_path.read_bytes())
            second.stop(hard=True)
            self.service()
            self.assertEqual(self.rows("SELECT id, number, status FROM download_attempts ORDER BY number"), attempts)
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 0}])
            recovery.RECEIPTS.append({"case": self.id(), "boundary": phase, "source_cookie_producer": True,
                                     "frozen_workers_and_restart": bool(recovery.ENGINE), "cookie_bytes_in_sqlite": False,
                                     "verified_worker_exit": True, "attempts": attempts})

    def test_crash_before_registration_preserves_only_empty_unowned_names(self):
        self.crash("before-registration")

    def test_crash_after_registration_before_secret_write_recovers_the_empty_owned_file(self):
        self.crash("after-registration")

    def test_crash_during_cookie_write_recovers_partial_secret_bytes(self):
        self.crash("during-write")

    def test_crash_after_cookie_write_before_ready_recovers_the_owned_file(self):
        self.crash("after-write-before-ready")

    def test_crash_before_delivery_recovers_ready_credentials(self):
        self.crash("ready-before-delivery")

    def test_crash_after_real_cookie_delivery_recovers_after_worker_tree_exit(self):
        self.crash("delivered")

    def test_crash_before_cleanup_recovers_cookies_and_the_same_completed_attempt(self):
        self.crash("cleanup-before-unlinks")

    def test_crash_between_cookie_unlinks_recovers_the_remaining_owned_witness(self):
        self.crash("cleanup-between-unlinks")

    def test_crash_after_cookie_unlinks_before_sqlite_delete_recovers_idempotently(self):
        self.crash("cleanup-before-sqlite-delete")

    def test_a_second_engine_preserves_a_live_cookie_owner(self):
        with WaveServer(phase="download") as fixture:
            producer = self.producer("delivered", fixture)
            path = Path(producer.frame["ownership"]["path"])
            self.service(refused=True)
            self.assertIsNone(producer.process.poll())
            self.assertIn(COOKIE_VALUE.encode(), path.read_bytes())
            self.assertEqual(self.rows("SELECT status FROM download_attempts"), [{"status": "running"}])

    @unittest.skipUnless(sys.platform == "linux", "POSIX stop/continue keeps the prior worker owner alive")
    def test_orphan_worker_owner_refuses_cookie_recovery_until_it_exits(self):
        with WaveServer(phase="download") as fixture:
            producer = self.producer("delivered", fixture)
            attempt = producer.frame["attempt"]
            path = Path(producer.frame["ownership"]["path"])
            owner = json.loads(attempt["worker_owner_identity"])
            os.kill(owner["pid"], signal.SIGSTOP)
            try:
                producer.stop(hard=True)
                self.service(refused=True)
                self.assertTrue(path.exists())
                self.assertEqual(self.rows("SELECT status FROM download_attempts"), [{"status": "running"}])
            finally:
                if recovery.alive_identity(attempt["worker_owner_identity"]):
                    os.kill(owner["pid"], signal.SIGCONT)
            self.wait_tree_exit(attempt)
            self.service()
            self.assertFalse(path.exists())


def producer():
    bootstrap = json.loads(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(Path(bootstrap["data_dir"]), Path(bootstrap["download_dir"]))
    phase = bootstrap["phase"]
    if phase not in PHASES:
        raise ValueError("Unknown cookie boundary")
    from backend.engine.application import FixtureRequest
    FixtureRequest(url=bootstrap["url"])
    components, _ = check_components(recovery.TOOLS)
    release = threading.Event()
    application = create_app(runtime, start_queue=True,
                             download_executor=ProcessDownloadExecutor(components=components, fixture_only=True))

    def checkpoint(ownership):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            db.row_factory = sqlite3.Row
            attempt = dict(db.execute("SELECT * FROM download_attempts WHERE id=?", (ownership["attempt_id"],)).fetchone())
        print(json.dumps({"type": "checkpoint", "phase": phase, "pid": os.getpid(),
                          "attempt": attempt, "ownership": ownership}), flush=True)

    def hold(ownership):
        checkpoint(ownership)
        if not release.wait(20):
            raise RuntimeError("Cookie crash checkpoint expired")

    register, ready, write, cleanup, unlink = (cookie_database.register_cookie, cookie_database.mark_cookie_ready,
                                               cookie_manager._write_cookie_bytes, cookie_manager.cleanup_cookie_file,
                                               Path.unlink)
    ownership = {}

    def registered(selected, attempt, record):
        ownership.update(record)
        if phase == "before-registration":
            hold(record)
        register(selected, attempt, record)
        if phase == "after-registration":
            hold(record)

    def written(file, encoded):
        if phase == "during-write":
            write(file, encoded[:-4])
            hold(ownership)
        else:
            write(file, encoded)

    def readied(selected, attempt, record):
        if phase == "after-write-before-ready":
            hold(record)
        ready(selected, attempt, record)
        if phase == "ready-before-delivery":
            hold(record)

    def cleaned(selected, attempt, record):
        if phase == "cleanup-before-unlinks":
            hold(json.loads(record))
        cleanup(selected, attempt, record)
        if phase == "cleanup-before-sqlite-delete":
            hold(json.loads(record))

    def unlinked(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if phase == "cleanup-between-unlinks" and str(path) == ownership.get("path"):
            hold(ownership)
        return result

    async def run():
        stopped = asyncio.Event()
        delivered = False
        loop = asyncio.get_running_loop()

        def control():
            getattr(sys.stdin.buffer, "raw", sys.stdin.buffer).read(1)
            release.set()
            loop.call_soon_threadsafe(stopped.set)

        threading.Thread(target=control, daemon=True).start()
        async def broadcast(message):
            nonlocal delivered
            if not delivered and phase == "delivered" and message["event"] == "task_progress" and message["data"].get("progress", 0) > 0:
                delivered = True
                checkpoint(ownership)
                await stopped.wait()

        with patch.object(application.state.connections, "broadcast", broadcast):
            async with application.router.lifespan_context(application):
                await application.state.queue.enqueue(bootstrap["url"], "best", audio_only=True, audio_format="mp3",
                                                       output_template="fixture-%(id)s.%(ext)s", cookies=COOKIES)
                await stopped.wait()

    with (patch.object(cookie_database, "register_cookie", registered),
          patch.object(cookie_database, "mark_cookie_ready", readied),
          patch.object(cookie_manager, "_write_cookie_bytes", written),
          patch.object(cookie_manager, "cleanup_cookie_file", cleaned),
          patch.object(Path, "unlink", unlinked)):
        if recovery.ENGINE:
            with patch("backend.engine.execution.entry_command", lambda _module, flag: [str(recovery.ENGINE), flag]):
                asyncio.run(run())
        else:
            asyncio.run(run())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--case", action="append", help="Run one named cookie check (repeatable)")
    parser.add_argument("--producer", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    recovery.TOOLS = args.tools.resolve(strict=True)
    recovery.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    sys.addaudithook(fixture_network_guard)
    if args.producer:
        producer()
        return
    check_components(recovery.TOOLS)
    started = time.monotonic()
    suite = (unittest.defaultTestLoader.loadTestsFromNames(args.case, CookieCrashChecks) if args.case else
             unittest.defaultTestLoader.loadTestsFromTestCase(CookieCrashChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "cases": result.testsRun,
            "seconds": round(time.monotonic() - started, 3), "engine": str(recovery.ENGINE) if recovery.ENGINE else None,
            "skipped": [(test.id(), reason) for test, reason in result.skipped],
            "errors": [(test.id(), message) for test, message in result.errors + result.failures],
            "receipts": recovery.RECEIPTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
