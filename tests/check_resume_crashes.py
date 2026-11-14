"""Opt-in whole-engine crashes with witnessed loopback WAV resume.

Source and frozen services use their actual owner/worker/recovery paths.
Cookie delivery and the private legacy refresh service are source fixtures;
--engine selects frozen workers and the entire frozen recovery service.
"""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import uvicorn

from backend.engine import create_engine_app
from backend.engine.__main__ import Bootstrap, ReadyServer
from backend.engine.network_guard import fixture_network_guard
from backend.engine.resources import check_components
from backend.runtime import RuntimeConfig
from tests import check_cookie_crashes as cookies
from tests import check_service_recovery as recovery
from tests.check_verified_resume import MediaServer
from tests.worker_process_support import owned_children


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class AuthenticationService(recovery.Service):
    def __init__(self, root, runtime, sequence, job):
        super().__init__(root, runtime, sequence)
        self.job = job

    def command(self):
        command = [sys.executable, "-B", "-m", "tests.check_resume_crashes", "--serve-auth",
                   "--tools", str(recovery.TOOLS), "--job", self.job]
        if recovery.ENGINE:
            command.extend(["--engine", str(recovery.ENGINE)])
        return command


class ResumeCrashChecks(recovery.ServiceRecoveryFixture):
    def setUp(self):
        super().setUp()
        self.previous = {(item.pid, item.started) for item in owned_children()}
        self.stopped_partials = {}

    def cleanup_processes(self):
        super().cleanup_processes()
        self.assertEqual(owned_children(self.previous), [], "Every owned service/worker must exit")
        for path, expected in self.stopped_partials.items():
            self.assertEqual(digest(Path(path)), expected, "A stopped partial must remain untouched")
        warnings = ("Task exception was never retrieved", "Task was destroyed", "ResourceWarning",
                    "was never awaited", "Exception in thread", "unhandled exception during asyncio.run")
        for log in self.root.glob("*.log"):
            text = log.read_text(errors="replace")
            self.assertFalse(any(warning in text for warning in warnings), text[-4000:])

    def prefix(self, job, number):
        def committed():
            rows = self.rows("""SELECT a.*, p.record FROM download_attempts a
                JOIN attempt_partials p ON p.attempt_id=a.id WHERE a.download_id=? AND a.number=?
                AND a.status='running' AND a.worker_owner_identity IS NOT NULL
                AND a.worker_identity IS NOT NULL AND json_extract(p.record, '$.size')>0""", (job, number))
            return rows[0] if rows else None

        attempt = recovery.wait_for(committed, "The service did not commit its owned HTTP prefix", timeout=12)
        self.attempt = attempt
        self.assertTrue(recovery.alive_identity(attempt["worker_owner_identity"]))
        self.assertTrue(recovery.tree_alive(attempt["worker_identity"]))
        return attempt

    def active(self, server):
        service = self.service()
        job = service.request("POST", "/spike/fixture", {"url": server.url})["task_id"]
        return service, job, self.prefix(job, 1)

    def crash(self, service, attempt):
        self.assertIsNone(service.process.poll())
        service.stop(hard=True)
        self.assertIsNotNone(service.process.poll())
        self.assertFalse(recovery.alive_identity(service.server_identity))
        self.wait_tree_exit(attempt)
        record = json.loads(self.rows("SELECT record FROM attempt_partials WHERE attempt_id=?", (attempt["id"],))[0]["record"])
        path = Path(attempt["workspace"]) / record["name"]
        self.assertGreater(record["size"], 0)
        with path.open("rb") as file:
            self.assertEqual(hashlib.sha256(file.read(record["size"])).hexdigest(), record["sha256"])
        self.stopped_partials[str(path)] = digest(path)
        recovery.RECEIPTS.append({"case": self.id(), "boundary": "engine-killed-after-prefix",
            "service_identity": service.server_identity, "attempt_id": attempt["id"],
            "worker_owner_identity": attempt["worker_owner_identity"], "worker_identity": attempt["worker_identity"],
            "prefix": record, "retained_partial_sha256": digest(path), "verified_tree_exit": True})
        return path, record

    def terminal(self, job, status):
        def finished():
            rows = self.rows("SELECT * FROM downloads WHERE id=?", (job,))
            if rows and rows[0]["status"] in ("complete", "failed", "cancelled", "waiting_for_auth"):
                return rows[0]
            return None
        row = recovery.wait_for(finished, "The recovered job did not reach a terminal/waiting state", timeout=15)
        self.assertEqual(row["status"], status, row.get("error_message"))
        return row

    def complete(self, job, server, attempts):
        row = self.terminal(job, "complete")
        self.attempt = self.rows("SELECT * FROM download_attempts WHERE download_id=? ORDER BY number DESC LIMIT 1", (job,))[0]
        self.wait_tree_exit(self.attempt)
        self.assertEqual(row["attempt_count"], attempts)
        self.assertEqual(row["retry_count"], 0)
        all_attempts = self.rows("SELECT id,number,status,workspace,owner_id FROM download_attempts ORDER BY number")
        self.assertEqual([item["number"] for item in all_attempts], list(range(1, attempts+1)))
        self.assertEqual([item["status"] for item in all_attempts], ["interrupted"]*(attempts-1)+["complete"])
        self.assertEqual(len({item["workspace"] for item in all_attempts}), attempts)
        self.assertEqual(len({item["owner_id"] for item in all_attempts}), attempts)
        record = json.loads(self.rows("SELECT record FROM attempt_partials WHERE attempt_id=?", (all_attempts[-1]["id"],))[0]["record"])
        self.assertEqual(record["size"], len(server.media))
        self.assertEqual(record["sha256"], hashlib.sha256(server.media).hexdigest())
        output = Path(row["file_path"])
        self.assertTrue(output.is_relative_to(self.runtime.download_dir))
        probe = subprocess.run([str(recovery.TOOLS / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")),
            "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", str(output)],
            capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "mp3")
        recovery.RECEIPTS.append({"case": self.id(), "output_sha256": digest(output),
            "downloaded_wav_sha256": record["sha256"], "attempts": all_attempts})
        return output

    def test_repeated_engine_death_resumes_new_attempts_and_retains_every_old_partial(self):
        with MediaServer() as server:
            first, job, attempt = self.active(server)
            unrelated = self.runtime.download_dir / "another job.part"
            unrelated.write_bytes(b"unrelated bytes")
            _, initial = self.crash(first, attempt)
            second = self.service()
            next_attempt = self.prefix(job, 2)
            recovery.wait_for(lambda: f"bytes={initial['size']}-" in server.ranges, "The second attempt did not request its prefix range")
            _, next_record = self.crash(second, next_attempt)
            third = self.service()
            server.gated = False
            server.release.set()
            output = self.complete(job, server, 3)
            self.assertIn(f"bytes={next_record['size']}-", server.ranges)
            self.assertEqual(unrelated.read_bytes(), b"unrelated bytes")
            output_hash, requests = digest(output), server.requests
            third.stop(hard=True)
            self.service()
            self.assertEqual(self.rows("SELECT status,attempt_count FROM downloads"), [{"status": "complete", "attempt_count": 3}])
            self.assertEqual(server.requests, requests)
            self.assertEqual(digest(output), output_hash)

    def test_changed_source_starts_fresh_after_engine_death(self):
        with MediaServer() as server:
            first, job, attempt = self.active(server)
            self.crash(first, attempt)
            server.etag = '"changed-after-crash"'
            server.media = server.wave(100)
            server.ranges.clear()
            server.gated = False
            server.release.set()
            self.service()
            self.complete(job, server, 2)
            self.assertTrue(server.ranges)
            self.assertTrue(all(value is None for value in server.ranges))

    def test_uncommitted_tail_is_preserved_and_only_the_witnessed_prefix_resumes(self):
        with MediaServer() as server:
            first, job, attempt = self.active(server)
            partial, record = self.crash(first, attempt)
            # Simulate bytes written after the durable checkpoint; tree exit is already verified.
            with partial.open("ab") as file:
                file.write(b"uncommitted crash-tail fixture")
                file.flush()
                os.fsync(file.fileno())
            self.stopped_partials[str(partial)] = digest(partial)
            server.gated = False
            server.release.set()
            self.service()
            self.complete(job, server, 2)
            self.assertIn(f"bytes={record['size']}-", server.ranges)

    def test_foreign_partial_fails_without_retry_and_survives_another_restart(self):
        with MediaServer() as server:
            first, job, attempt = self.active(server)
            partial, _ = self.crash(first, attempt)
            original = partial.with_name(partial.name+".preserved")
            partial.rename(original)
            partial.write_bytes(b"foreign replacement")
            self.stopped_partials[str(original)] = self.stopped_partials.pop(str(partial))
            self.stopped_partials[str(partial)] = digest(partial)
            second = self.service()
            server.gated = False
            server.release.set()
            row = self.terminal(job, "failed")
            self.assertEqual((row["attempt_count"], row["retry_count"], row["file_path"]), (2, 0, None))
            requests = server.requests
            second.stop(hard=True)
            self.service()
            self.assertEqual(self.rows("SELECT status,attempt_count,retry_count FROM downloads"),
                             [{"status": "failed", "attempt_count": 2, "retry_count": 0}])
            self.assertEqual(server.requests, requests)
            self.assertFalse(list(self.runtime.download_dir.glob("*.mp3")))

    def test_persisted_cancellation_wins_after_engine_death(self):
        with MediaServer() as server:
            first, job, attempt = self.active(server)
            # Persist cancellation while the real transfer is gated, then kill the engine.
            with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='cancelling' WHERE id=? AND current_attempt_id=?", (job, attempt["id"]))
            self.crash(first, attempt)
            requests = server.requests
            self.service()
            row = self.terminal(job, "cancelled")
            self.assertEqual(row["attempt_count"], 1)
            self.assertEqual(server.requests, requests)
            self.assertEqual(self.rows("SELECT status FROM download_attempts"), [{"status": "cancelled"}])
            self.assertFalse(list(self.runtime.download_dir.glob("*.mp3")))

    def test_cookie_crash_waits_for_fresh_auth_then_resumes_the_same_job(self):
        with MediaServer() as server:
            producer = cookies.Producer(self.root, self.runtime, len(self.services), "delivered", server.url)
            self.services.append(producer)
            producer.start()
            job = producer.frame["attempt"]["download_id"]
            attempt = self.prefix(job, 1)
            cookie_path = Path(producer.frame["ownership"]["path"])
            self.assertIn(cookies.COOKIE_VALUE.encode(), cookie_path.read_bytes())
            self.crash(producer, attempt)
            requests = server.requests
            server.auth = True
            restored = self.service()
            row = self.terminal(job, "waiting_for_auth")
            self.assertEqual(row["attempt_count"], 1)
            self.assertFalse(cookie_path.exists())
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 0}])
            self.assertEqual(server.requests, requests)
            restored.stop(hard=True)
            refresh = AuthenticationService(self.root, self.runtime, len(self.services), job)
            self.services.append(refresh)
            refresh.start()
            server.gated = False
            server.release.set()
            refresh.request("POST", f"/downloads/{job}/authentication", {"cookies": [
                {"domain": "127.0.0.1", "path": "/", "name": "session", "value": "fresh",
                 "expirationDate": time.time()+3600}]})
            self.complete(job, server, 2)
            self.assertTrue(server.cookie_seen)
            self.assertTrue(any(value is not None for value in server.ranges))
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM attempt_cookie_files"), [{"n": 0}])
            for value in [cookies.COOKIE_VALUE, "session\tfresh"]:
                self.assertNotIn(value.encode(), self.runtime.db_path.read_bytes())
            recovery.RECEIPTS.append({"case": self.id(), "source_cookie_producer_and_auth_service": True,
                "frozen_workers_and_recovery_service": bool(recovery.ENGINE), "fresh_auth_same_job": job})


def serve_auth(job):
    # Expose only one existing legacy action in a private source test service.
    import uuid
    if str(uuid.UUID(job)) != job:
        raise ValueError("Authentication fixture requires a canonical job UUID")
    paths, versions = check_components(recovery.TOOLS)
    bootstrap = Bootstrap.model_validate_json(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(Path(bootstrap.data_dir), Path(bootstrap.download_dir))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_engine_app(runtime, token=bootstrap.token, port=port, components=paths)
        app.allowed = app.allowed | {("POST", f"/downloads/{job}/authentication")}
        server = ReadyServer(uvicorn.Config(app, log_config=None, access_log=False, loop="asyncio", lifespan="on"))
        server.component_versions = versions
        if recovery.ENGINE:
            with patch("backend.engine.execution.entry_command", lambda _module, flag: [str(recovery.ENGINE), flag]):
                asyncio.run(server.serve(sockets=[listener]))
        else:
            asyncio.run(server.serve(sockets=[listener]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--case", action="append")
    parser.add_argument("--serve-auth", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--job", help=argparse.SUPPRESS)
    args = parser.parse_args()
    recovery.TOOLS = args.tools.resolve(strict=True)
    recovery.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    sys.addaudithook(fixture_network_guard)
    if args.serve_auth:
        serve_auth(args.job)
        return
    _, versions = check_components(recovery.TOOLS)
    started = time.monotonic()
    suite = (unittest.defaultTestLoader.loadTestsFromNames(args.case, ResumeCrashChecks) if args.case else
             unittest.defaultTestLoader.loadTestsFromTestCase(ResumeCrashChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "cases": result.testsRun,
            "seconds": round(time.monotonic()-started, 3), "engine": str(recovery.ENGINE) if recovery.ENGINE else "source",
            "versions": versions, "skipped": [(test.id(), reason) for test, reason in result.skipped],
            "errors": [(test.id(), message) for test, message in result.errors+result.failures],
            "receipts": recovery.RECEIPTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
