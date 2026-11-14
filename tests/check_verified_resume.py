"""Opt-in direct HTTP interruption/resume and fresh-auth checks on owned workers."""

import argparse
import asyncio
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import httpx
from backend.app import create_app
from backend.engine.execution import ProcessDownloadExecutor
from backend.engine.resources import check_components
from backend.runtime import RuntimeConfig
from tests.worker_process_support import owned_children

ENGINE = None
COMPONENTS = None


class MediaServer:
    def __init__(self):
        self.media = self.wave(0)
        self.etag = '"fixture-one"'
        self.release = threading.Event()
        self.gated = True
        self.auth = False
        self.ranges = []
        self.requests = 0
        self.cookie_seen = False
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests += 1
                if self.path != "/fixture.wav":
                    self.send_error(404)
                    return
                cookie = self.headers.get("Cookie", "")
                fixture.cookie_seen |= "session=fresh" in cookie
                if fixture.auth and "session=fresh" not in cookie:
                    self.send_error(401)
                    return
                range_header = self.headers.get("Range")
                fixture.ranges.append(range_header)
                offset = int(range_header[6:-1]) if range_header else 0
                self.send_response(206 if range_header else 200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("ETag", fixture.etag)
                self.send_header("Content-Length", str(len(fixture.media)-offset))
                if range_header:
                    self.send_header("Content-Range", f"bytes {offset}-{len(fixture.media)-1}/{len(fixture.media)}")
                self.end_headers()
                try:
                    for start in range(offset, len(fixture.media), 65536):
                        self.wfile.write(fixture.media[start:start+65536])
                        self.wfile.flush()
                        if fixture.gated and fixture.requests >= 2 and not fixture.release.wait(15):
                            return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def wave(sample):
        memory = io.BytesIO()
        with wave.open(memory, "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(bytes((sample, 0)) * 600000)
        return memory.getvalue()

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/fixture.wav"

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class VerifiedResumeChecks(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sys.platform == "win32", "Exact Windows Job Object membership check")
    async def test_uncontained_windows_execution_identity_is_rejected(self):
        from types import SimpleNamespace
        from backend.engine.windows_job import WindowsJob
        from backend.engine.process_identity import ProcessIdentity
        from backend.engine.worker_owner import executing_identity
        job = WindowsJob()
        try:
            actual = ProcessIdentity.capture()
            tree = SimpleNamespace(job=job, process=SimpleNamespace(pid=actual.pid))
            with self.assertRaisesRegex(RuntimeError, "Job Object"):
                executing_identity(tree, {"type": "identity", "identity": actual.to_json()})
        finally:
            job.close()

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-resume-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = RuntimeConfig(self.root / "state", self.root / "বাংলা media")
        self.before = {(item.pid, item.started) for item in owned_children()}
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        if ENGINE:
            replacement = patch("backend.engine.execution.entry_command", lambda module, flag: [str(ENGINE), flag])
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = create_app(self.runtime, start_queue=False,
                              download_executor=ProcessDownloadExecutor(components=COMPONENTS, fixture_only=True))
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.queue = self.app.state.queue
        self.config = await self.app.state.settings.get_all()
        self.config["max_retries"] = 0

    async def asyncTearDown(self):
        self.assertEqual(owned_children(self.before), [], "Owned workers must exit before terminal state")
        self.assertEqual(self.loop_errors, [])

    def rows(self, sql, values=()):
        with closing(sqlite3.connect(self.runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    async def interrupt(self, server):
        job = await self.queue.enqueue(server.url, "best")
        execution = asyncio.create_task(self.queue._run_task(job, server.url, "best", self.config, {}))
        try:
            deadline = time.monotonic()+12
            while not self.rows("SELECT record FROM attempt_partials WHERE json_extract(record, '$.size')>0"):
                if execution.done():
                    await execution
                    self.fail(str(self.rows("SELECT status,error_message FROM downloads")))
                self.assertLess(time.monotonic(), deadline, "Timed out waiting for a committed HTTP prefix")
                await asyncio.sleep(0.02)
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            self.assertEqual(self.rows("SELECT status FROM downloads"), [("queued",)])
            record = json.loads(self.rows("SELECT record FROM attempt_partials")[0][0])
            workspace = Path(self.rows("SELECT workspace FROM download_attempts")[0][0])
            partial = workspace / record["name"]
            self.assertEqual(owned_children(self.before), [])
            return job, partial, record
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            server.gated = False
            server.release.set()

    async def finish(self, job, server):
        await self.queue._run_task(job, server.url, "best", self.config, {})
        status, path, error = self.rows("SELECT status,file_path,error_message FROM downloads WHERE id=?", (job,))[0]
        self.assertEqual(status, "complete", error)
        self.assertEqual(Path(path).read_bytes(), server.media)
        self.assertEqual(self.rows("SELECT attempt_count FROM downloads"), [(2,)])

    async def test_interrupted_http_prefix_resumes_in_new_attempt_after_repeated_cancellation(self):
        with MediaServer() as server:
            job, partial, record = await self.interrupt(server)
            before = hashlib.sha256(partial.read_bytes()).hexdigest()
            await self.finish(job, server)
            self.assertIn(f"bytes={record['size']}-", server.ranges)
            self.assertEqual(hashlib.sha256(partial.read_bytes()).hexdigest(), before)

    async def test_changed_source_restarts_in_fresh_workspace_and_retains_old_partial(self):
        with MediaServer() as server:
            job, partial, record = await self.interrupt(server)
            before = partial.read_bytes()
            server.etag = '"fixture-two"'
            server.media = server.wave(1)
            server.ranges.clear()
            await self.finish(job, server)
            self.assertTrue(all(value is None for value in server.ranges))
            self.assertEqual(partial.read_bytes(), before)

    async def test_foreign_partial_fails_without_retry_and_preserves_replacement(self):
        with MediaServer() as server:
            job, partial, record = await self.interrupt(server)
            replacement = partial.with_suffix(".foreign")
            replacement.write_bytes(partial.read_bytes())
            os.replace(replacement, partial)
            before = partial.read_bytes()
            await self.queue._run_task(job, server.url, "best", self.config, {})
            self.assertEqual(self.rows("SELECT status,retry_count FROM downloads"), [("failed", 0)])
            self.assertEqual(partial.read_bytes(), before)

    async def test_expired_auth_waits_then_fresh_browser_cookies_resume_same_job(self):
        with MediaServer() as server:
            job, partial, record = await self.interrupt(server)
            before = partial.read_bytes()
            server.auth = True
            await self.queue._run_task(job, server.url, "best", self.config, {})
            self.assertEqual(self.rows("SELECT status,retry_count FROM downloads"), [("waiting_for_auth", 0)])
            cookies = [{"domain": "127.0.0.1", "path": "/", "name": "session", "value": "fresh",
                        "secure": False, "expirationDate": 1900000000}]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://fixture") as client:
                reply = await client.post(f"/downloads/{job}/authentication", json={"cookies": cookies})
                self.assertEqual(reply.status_code, 200, reply.text)
            await self.queue._run_task(job, server.url, "best", self.config, {})
            status, path = self.rows("SELECT status,file_path FROM downloads")[0]
            self.assertEqual(status, "complete", self.rows("SELECT error_message FROM downloads"))
            self.assertEqual(Path(path).read_bytes(), server.media)
            self.assertTrue(server.cookie_seen)
            self.assertIn(f"bytes={record['size']}-", server.ranges)
            self.assertEqual(partial.read_bytes(), before)
            self.assertEqual(self.rows("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])


def main():
    global ENGINE, COMPONENTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    COMPONENTS, versions = check_components(args.tools.resolve())
    ENGINE = args.engine.resolve(strict=True) if args.engine else None
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(VerifiedResumeChecks))
    args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "tests": result.testsRun,
        "seconds": round(time.monotonic()-started, 3), "engine": str(ENGINE) if ENGINE else "source", "versions": versions,
        "failures": [str(test)+": "+trace for test, trace in result.failures+result.errors]}, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
