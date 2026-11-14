"""Opt-in static HLS/DASH resume, auth and whole-engine crash fixtures.

Serve manifest content at the existing loopback fixture.wav endpoint; the frozen
spike's URL allowlist remains unchanged. Real bundled FFmpeg creates clear fMP4
segments. No public media, user runtime or browser registration is involved.
"""

import argparse
import asyncio
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import httpx
from backend.engine.network_guard import fixture_network_guard
from backend.engine.resources import check_components
from tests import check_service_recovery as recovery
from tests import check_verified_resume as direct
from tests.worker_process_support import owned_children


def digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class FragmentServer:
    def __init__(self, protocol):
        self.temporary = tempfile.TemporaryDirectory(prefix="vidgrab-fragments-media-")
        self.root = Path(self.temporary.name)
        command = [direct.COMPONENTS["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=8", "-c:a", "aac", "-b:a", "64k",
            "-f", "hls", "-hls_time", "1", "-hls_segment_type", "fmp4", "-hls_playlist_type", "vod",
            "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "segment%d.m4s", "fixture.m3u8"]
        try:
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=25, check=True, close_fds=True, cwd=self.root,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
            self.segments = sorted(self.root.glob("segment*.m4s"), key=lambda path: int(path.stem[7:]))
            if len(self.segments) < 3:
                raise AssertionError("The fragment fixture needs multiple media segments")
            self.fragments = [self.root / "init.mp4", *self.segments]
            self.parts = {"/" + path.name: path.read_bytes() for path in self.fragments}
            self.media = b"".join(self.parts["/" + path.name] for path in self.fragments)
            self.expected = hashlib.sha256(self.media).hexdigest()
            reference = self.root / "reference.mp4"
            reference.write_bytes(self.media)
            self.decoded = self.decoded_hash(reference)
            if protocol == "hls":
                self.manifest = (self.root / "fixture.m3u8").read_bytes()
                self.content_type = "application/vnd.apple.mpegurl"
            else:
                segment_list = "".join('<SegmentURL media="' + path.name + '"/>' for path in self.segments)
                self.manifest = ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT8S">'
                    '<Period duration="PT8S"><AdaptationSet mimeType="audio/mp4" contentType="audio" codecs="mp4a.40.2">'
                    '<Representation id="audio" bandwidth="64000" audioSamplingRate="16000">'
                    '<SegmentList timescale="16000" duration="16000"><Initialization sourceURL="init.mp4"/>'
                    + segment_list + '</SegmentList></Representation></AdaptationSet></Period></MPD>').encode()
                self.content_type = "application/dash+xml"
            self.protocol = protocol
            self.release = threading.Event()
            self.entered = threading.Event()
            self.gated = True
            self.gate_path = "/" + self.segments[0].name
            self.auth = False
            self.cookie_seen = False
            self.requests = []
            fixture = self
            class Handler(BaseHTTPRequestHandler):
                def do_HEAD(self):
                    self.respond(False)

                def do_GET(self):
                    self.respond(True)

                def respond(self, body):
                    fixture.requests.append((self.command, self.path))
                    fresh = "session=fresh" in self.headers.get("Cookie", "")
                    fixture.cookie_seen |= fresh
                    if fixture.auth and not fresh:
                        self.send_error(401)
                        return
                    payload = fixture.manifest if self.path == "/fixture.wav" else fixture.parts.get(self.path)
                    if payload is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Content-Type", fixture.content_type if self.path == "/fixture.wav" else "audio/mp4")
                    self.send_header("ETag", '"' + hashlib.sha256(payload).hexdigest() + '"')
                    self.end_headers()
                    if not body:
                        return
                    if fixture.gated and self.path == fixture.gate_path:
                        fixture.entered.set()
                        if not fixture.release.wait(18):
                            return
                    try:
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

                def log_message(self, *_):
                    pass
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        except BaseException:
            self.temporary.cleanup()
            raise

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/fixture.wav"

    @staticmethod
    def decoded_hash(path):
        output = subprocess.run([direct.COMPONENTS["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"], capture_output=True, timeout=8, check=True,
            creationflags=0x08000000 if sys.platform == "win32" else 0)
        return hashlib.sha256(output.stdout).hexdigest()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.gated = False
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()


class FragmentWorkerChecks(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = direct.VerifiedResumeChecks.asyncSetUp
    asyncTearDown = direct.VerifiedResumeChecks.asyncTearDown
    rows = direct.VerifiedResumeChecks.rows

    async def interrupt(self, server, job=None, *, after=0):
        job = job or await self.queue.enqueue(server.url, "best")
        execution = asyncio.create_task(self.queue._run_task(job, server.url, "best", self.config, {}))
        try:
            deadline = time.monotonic() + 12
            while True:
                rows = self.rows("""SELECT a.workspace,p.record FROM attempt_partials p JOIN download_attempts a ON a.id=p.attempt_id
                    WHERE a.download_id=? AND a.status='running' AND json_extract(p.record,'$.size')>?""", (job, after))
                if rows and server.entered.is_set():
                    break
                if execution.done():
                    await execution
                    self.fail(str(self.rows("SELECT status,error_message FROM downloads")))
                self.assertLess(time.monotonic(), deadline, "A complete fragment prefix was not committed")
                await asyncio.sleep(0.02)
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            self.assertEqual(self.rows("SELECT status FROM downloads"), [("queued",)])
            self.assertEqual(owned_children(self.before), [])
            record = json.loads(rows[0][1])
            path = Path(rows[0][0]) / record["name"]
            with path.open("rb") as file:
                self.assertEqual(hashlib.sha256(file.read(record["size"])).hexdigest(), record["sha256"])
            return job, path, record
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
        records = self.rows("""SELECT p.record FROM attempt_partials p JOIN download_attempts a ON a.id=p.attempt_id
            WHERE a.download_id=? ORDER BY a.number DESC LIMIT 1""", (job,))
        self.assertEqual(json.loads(records[0][0])["sha256"], server.expected)
        self.assertEqual(json.loads(records[0][0])["size"], len(server.media))
        self.assertEqual(server.decoded_hash(path), server.decoded)
        output = subprocess.run([direct.COMPONENTS["ffprobe"], "-v", "error", "-show_entries",
            "stream=codec_name:format=duration", "-of", "json", path], capture_output=True, check=True, timeout=8,
            creationflags=0x08000000 if sys.platform == "win32" else 0)
        probe = json.loads(output.stdout)
        self.assertEqual(probe["streams"][0]["codec_name"], "aac")
        self.assertAlmostEqual(float(probe["format"]["duration"]), 8, delta=0.2)

    async def repeated(self, protocol):
        with FragmentServer(protocol) as server:
            job, first, record = await self.interrupt(server)
            first_hash = digest(first)
            server.gate_path = "/" + server.segments[1].name
            server.gated = True
            server.release.clear()
            server.entered.clear()
            job, second, next_record = await self.interrupt(server, job, after=record["size"])
            second_hash = digest(second)
            self.assertNotEqual(first.parent, second.parent)
            server.requests.clear()
            await self.finish(job, server)
            self.assertNotIn(("GET", "/init.mp4"), server.requests)
            self.assertNotIn(("GET", "/" + server.segments[0].name), server.requests)
            self.assertEqual(digest(first), first_hash)
            self.assertEqual(digest(second), second_hash)
            self.assertEqual(self.rows("SELECT attempt_count FROM downloads"), [(3,)])

    async def changed(self, protocol):
        with FragmentServer(protocol) as server:
            job, path, record = await self.interrupt(server)
            before = digest(path)
            server.manifest += b"\n#changed\n" if protocol == "hls" else b"\n<!-- changed -->\n"
            server.requests.clear()
            await self.finish(job, server)
            self.assertIn(("GET", "/init.mp4"), server.requests)
            self.assertEqual(digest(path), before)

    async def foreign(self, protocol):
        with FragmentServer(protocol) as server:
            job, path, record = await self.interrupt(server)
            replacement = path.with_suffix(".foreign")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
            before = digest(path)
            await self.queue._run_task(job, server.url, "best", self.config, {})
            self.assertEqual(self.rows("SELECT status,retry_count FROM downloads"), [("failed", 0)])
            self.assertEqual(digest(path), before)

    async def authentication(self, protocol):
        with FragmentServer(protocol) as server:
            job, path, record = await self.interrupt(server)
            before = digest(path)
            server.auth = True
            await self.queue._run_task(job, server.url, "best", self.config, {})
            self.assertEqual(self.rows("SELECT status,retry_count FROM downloads"), [("waiting_for_auth", 0)])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://fixture") as client:
                reply = await client.post(f"/downloads/{job}/authentication", json={"cookies": [
                    {"domain": "127.0.0.1", "path": "/", "name": "session", "value": "fresh", "expirationDate": time.time() + 3600}]})
                self.assertEqual(reply.status_code, 200, reply.text)
            server.requests.clear()
            await self.finish(job, server)
            self.assertTrue(server.cookie_seen)
            self.assertNotIn(("GET", "/init.mp4"), server.requests)
            self.assertEqual(digest(path), before)
            self.assertEqual(self.rows("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])

    async def test_hls_repeated_cancellation_resumes_in_distinct_attempts(self):
        await self.repeated("hls")

    async def test_dash_repeated_cancellation_resumes_in_distinct_attempts(self):
        await self.repeated("dash")

    async def test_hls_changed_manifest_restarts_fresh(self):
        await self.changed("hls")

    async def test_dash_changed_manifest_restarts_fresh(self):
        await self.changed("dash")

    async def test_hls_foreign_prefix_fails_without_retry(self):
        await self.foreign("hls")

    async def test_dash_foreign_prefix_fails_without_retry(self):
        await self.foreign("dash")

    async def test_hls_fresh_authentication_resumes_same_job(self):
        await self.authentication("hls")

    async def test_dash_fresh_authentication_resumes_same_job(self):
        await self.authentication("dash")


class FragmentServiceChecks(recovery.ServiceRecoveryFixture):
    def setUp(self):
        super().setUp()
        self.before = {(item.pid, item.started) for item in owned_children()}
        self.retained = {}

    def cleanup_processes(self):
        super().cleanup_processes()
        self.assertEqual(owned_children(self.before), [])
        for path, expected in self.retained.items():
            self.assertEqual(digest(path), expected)
        for log in self.root.glob("*.log"):
            contents = log.read_text(errors="replace")
            self.assertFalse(any(text in contents for text in ("Task exception was never retrieved", "Task was destroyed",
                "ResourceWarning", "was never awaited", "Exception in thread", "unhandled exception during asyncio.run")), contents[-3500:])

    def prefix(self, job, after=0):
        def committed():
            rows = self.rows("""SELECT a.*,p.record FROM download_attempts a JOIN attempt_partials p ON p.attempt_id=a.id
                WHERE a.download_id=? AND a.status='running' AND json_extract(p.record,'$.size')>?""", (job, after))
            return rows[0] if rows else None
        self.attempt = recovery.wait_for(committed, "Service did not commit a complete fragment prefix", timeout=12)
        return self.attempt

    def terminal(self, job, expected):
        def finished():
            rows = self.rows("SELECT * FROM downloads WHERE id=?", (job,))
            return rows[0] if rows and rows[0]["status"] in ("complete", "failed", "cancelled", "waiting_for_auth") else None
        row = recovery.wait_for(finished, "The fragment job did not finish after restart", timeout=15)
        self.assertEqual(row["status"], expected, row.get("error_message"))
        return row

    def crash(self, service, attempt):
        service.stop(hard=True)
        self.wait_tree_exit(attempt)
        record = json.loads(self.rows("SELECT record FROM attempt_partials WHERE attempt_id=?", (attempt["id"],))[0]["record"])
        path = Path(attempt["workspace"]) / record["name"]
        self.retained[path] = digest(path)
        return record

    def repeated(self, protocol):
        with FragmentServer(protocol) as server:
            first = self.service()
            job = first.request("POST", "/spike/fixture", {"url": server.url})["task_id"]
            attempt = self.prefix(job)
            record = self.crash(first, attempt)
            server.gate_path = "/" + server.segments[1].name
            second = self.service()
            current = self.prefix(job, record["size"])
            self.assertNotEqual(current["id"], attempt["id"])
            self.assertNotEqual(current["owner_id"], attempt["owner_id"])
            self.crash(second, current)
            server.gated = False
            server.release.set()
            server.requests.clear()
            third = self.service()
            terminal = self.terminal(job, "complete")
            self.assertEqual(terminal["id"], job)
            self.attempt = self.rows("SELECT * FROM download_attempts WHERE download_id=? ORDER BY number DESC LIMIT 1", (job,))[0]
            self.wait_tree_exit(self.attempt)
            self.assertEqual(terminal["attempt_count"], 3)
            self.assertEqual(terminal["retry_count"], 0)
            self.assertNotIn(("GET", "/init.mp4"), server.requests)
            self.assertNotIn(("GET", "/" + server.segments[0].name), server.requests)
            rows = self.rows("SELECT a.number,p.record FROM download_attempts a JOIN attempt_partials p ON p.attempt_id=a.id ORDER BY a.number")
            self.assertEqual([row["number"] for row in rows], [1, 2, 3])
            self.assertEqual(json.loads(rows[-1]["record"])["sha256"], server.expected)
            self.assertEqual(json.loads(rows[-1]["record"])["size"], len(server.media))
            probe = subprocess.run([direct.COMPONENTS["ffprobe"], "-v", "error", "-show_entries", "stream=codec_name", "-of", "json",
                terminal["file_path"]], capture_output=True, check=True, timeout=8,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
            self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "mp3")

    def cancellation(self, protocol):
        with FragmentServer(protocol) as server:
            first = self.service()
            job = first.request("POST", "/spike/fixture", {"url": server.url})["task_id"]
            attempt = self.prefix(job)
            with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='cancelling' WHERE id=? AND current_attempt_id=?", (job, attempt["id"]))
            self.crash(first, attempt)
            requests = list(server.requests)
            restored = self.service()
            self.terminal(job, "cancelled")
            self.assertEqual(server.requests, requests)
            self.assertEqual(self.rows("SELECT attempt_count FROM downloads"), [{"attempt_count": 1}])
            self.assertFalse(list(self.runtime.download_dir.glob("*.mp3")))

    def test_hls_repeated_whole_engine_death_resumes_verified_fragments(self):
        self.repeated("hls")

    def test_dash_repeated_whole_engine_death_resumes_verified_fragments(self):
        self.repeated("dash")

    def test_hls_persisted_cancellation_wins_on_restart(self):
        self.cancellation("hls")

    def test_dash_persisted_cancellation_wins_on_restart(self):
        self.cancellation("dash")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    direct.COMPONENTS, versions = check_components(args.tools.resolve(strict=True))
    direct.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    recovery.TOOLS, recovery.ENGINE = args.tools.resolve(strict=True), direct.ENGINE
    sys.addaudithook(fixture_network_guard)
    loader = unittest.defaultTestLoader
    cases = {cls.__name__: cls for cls in (FragmentWorkerChecks, FragmentServiceChecks)}
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(cases[name]) if "." not in name else
        unittest.TestSuite([cases[name.split(".")[0]](name.split(".")[1])]) for name in args.case) if args.case else unittest.TestSuite(
        loader.loadTestsFromTestCase(cls) for cls in cases.values())
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "cases": result.testsRun,
        "seconds": round(time.monotonic() - started, 3), "engine": str(direct.ENGINE) if direct.ENGINE else "source",
        "versions": versions, "skipped": [(test.id(), reason) for test, reason in result.skipped],
        "receipts": recovery.RECEIPTS,
        "errors": [(test.id(), message) for test, message in result.errors + result.failures],
        "limits": ["Static clear full-file fragments only", "Seeded database cancellation in service fixtures",
                   "Legacy auth app with source or frozen workers", "Process crashes; no power-loss or browser evidence"]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
