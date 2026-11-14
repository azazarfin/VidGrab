"""Opt-in persisted retry deadlines across actual source/frozen engine death."""

import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest

from backend.engine.network_guard import fixture_network_guard
from backend.engine.resources import check_components
from tests import check_service_recovery as recovery
from tests.check_verified_resume import MediaServer
from tests.worker_process_support import owned_children


class BudgetServer:
    def __init__(self):
        self.limited = True
        self.requests = 0
        self.media = MediaServer.wave(0)
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                fixture.requests += 1
                if fixture.limited:
                    self.send_response(429)
                    self.send_header("Retry-After", "120")
                    body = b"fixture limited"
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("ETag", '"budget-fixture"')
                    body = fixture.media
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture.wav"
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class RequestBudgetChecks(recovery.ServiceRecoveryFixture):
    def setUp(self):
        super().setUp()
        self.previous = {(item.pid, item.started) for item in owned_children()}

    def cleanup_processes(self):
        super().cleanup_processes()
        self.assertEqual(owned_children(self.previous), [])
        warnings = ("Task exception was never retrieved", "Task was destroyed", "ResourceWarning",
                    "was never awaited", "Exception in thread", "unhandled exception during asyncio.run")
        for log in self.root.glob("*.log"):
            self.assertFalse(any(value in log.read_text(errors="replace") for value in warnings))

    def waiting(self, server):
        service = self.service()
        job = service.request("POST", "/spike/fixture", {"url": server.url})["task_id"]
        row = recovery.wait_for(lambda: next(iter(self.rows("SELECT * FROM downloads WHERE id=? AND status='retry_wait'", (job,))), None),
                                "The engine did not persist its upstream retry deadline", timeout=15)
        self.attempt = self.rows("SELECT * FROM download_attempts WHERE download_id=?", (job,))[0]
        self.wait_tree_exit(self.attempt)
        self.assertEqual((row["retry_count"], row["request_count"]), (1, 1))
        self.assertGreater(row["retry_at"], time.time()+100)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM request_leases"), [{"n": 0}])
        return service, job, row

    def test_retry_deadline_survives_engine_death_and_only_due_work_resumes(self):
        with BudgetServer() as server:
            first, job, row = self.waiting(server)
            first.stop(hard=True)
            server.limited = False
            second = self.service()
            current = self.rows("SELECT * FROM downloads WHERE id=?", (job,))[0]
            self.assertEqual((current["status"], current["retry_at"], current["request_count"], current["attempt_count"]),
                             ("retry_wait", row["retry_at"], 1, 1))
            self.assertEqual(server.requests, 1)
            second.stop(hard=True)
            # Advance only this fixture's persisted clock authority; no real wait or mutation replay.
            with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET retry_at=? WHERE id=?", (time.time()-1, job))
                db.execute("UPDATE request_hosts SET retry_at=?", (time.time()-1,))
            self.service()
            completed = recovery.wait_for(lambda: next(iter(self.rows("SELECT * FROM downloads WHERE id=? AND status='complete'", (job,))), None),
                                          "Due work did not complete after restart", timeout=20)
            self.assertEqual(completed["attempt_count"], 2)
            self.assertEqual(completed["retry_count"], 1)
            self.assertGreater(completed["request_count"], 1)
            self.assertEqual((completed["retry_at"], completed["error_code"]), (None, None))
            self.attempt = self.rows("SELECT * FROM download_attempts WHERE download_id=? ORDER BY number DESC", (job,))[0]
            self.wait_tree_exit(self.attempt)
            record = json.loads(self.rows("SELECT record FROM attempt_partials WHERE attempt_id=?", (self.attempt["id"],))[0]["record"])
            self.assertEqual((record["size"], record["sha256"]), (len(server.media), hashlib.sha256(server.media).hexdigest()))
            probe = subprocess.run([str(recovery.TOOLS/("ffprobe.exe" if sys.platform == "win32" else "ffprobe")),
                "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", completed["file_path"]],
                check=True, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "mp3")
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM request_leases"), [{"n": 0}])
            recovery.RECEIPTS.append({"case": self.id(), "original_deadline": row["retry_at"],
                "attempts": completed["attempt_count"], "spent_requests": completed["request_count"], "wave_sha256": record["sha256"]})

    def test_persisted_cancellation_wins_over_retry_deadline_after_engine_death(self):
        with BudgetServer() as server:
            first, job, row = self.waiting(server)
            first.stop(hard=True)
            with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (job,))
            self.service()
            current = self.rows("SELECT status,retry_at,error_code,request_count,attempt_count FROM downloads WHERE id=?", (job,))[0]
            self.assertEqual(current, {"status": "cancelled", "retry_at": None, "error_code": None,
                                       "request_count": row["request_count"], "attempt_count": 1})
            self.assertEqual(server.requests, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    recovery.TOOLS = args.tools.resolve(strict=True)
    recovery.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    sys.addaudithook(fixture_network_guard)
    _, versions = check_components(recovery.TOOLS)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RequestBudgetChecks))
    args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "cases": result.testsRun,
        "seconds": time.monotonic()-started, "engine": str(recovery.ENGINE) if recovery.ENGINE else "source", "versions": versions,
        "errors": [(test.id(), message) for test, message in result.errors+result.failures],
        "receipts": recovery.RECEIPTS}, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
