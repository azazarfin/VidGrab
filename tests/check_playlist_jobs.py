"""Opt-in owned source/frozen playlist workers against private loopback media."""

import argparse
import asyncio
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import threading
import time
import unittest
import wave
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.app import create_app
from backend.engine.network_guard import fixture_network_guard
from backend.engine.resources import check_components
from backend.services.diagnostics import redact_diagnostic
from tests import check_worker_execution as media
from tests import check_service_recovery as services
from backend.engine.process_identity import ProcessIdentity

RECEIPTS = []


class PlaylistServer:
    def __init__(self, *, gate=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gate = gate
        self.requests = []
        self.pcm = {}
        resources = {"/list.html": ("text/html", b'<html><title>Owned playlist</title><video src="/one.wav"></video><video src="/two.wav"></video></html>')}
        for name, frequency in (("one", 220), ("two", 440)):
            pcm = b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * frequency * i / 16000))) for i in range(32000))
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(pcm)
            self.pcm[name] = hashlib.sha256(pcm).hexdigest()
            resources[f"/{name}.wav"] = ("audio/wav", buffer.getvalue())
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.respond(False)
            def do_GET(self):
                self.respond(True)
            def respond(self, body):
                fixture.requests.append((self.command, self.path))
                if self.path not in resources:
                    self.send_error(404)
                    return
                if self.path == fixture.gate:
                    fixture.entered.set()
                    if not fixture.release.wait(12):
                        return
                content_type, data = resources[self.path]
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("ETag", '"' + hashlib.sha256(data).hexdigest() + '"')
                    self.end_headers()
                    if body:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/list.html"
    def __enter__(self):
        self.thread.start()
        return self
    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class PlaylistWorkerChecks(unittest.IsolatedAsyncioTestCase):
    owned_tree = media.WorkerExecutionChecks.owned_tree
    executor = media.WorkerExecutionChecks.executor

    async def asyncSetUp(self):
        await media.WorkerExecutionChecks.asyncSetUp(self)
        self.app = create_app(self.runtime, start_queue=False, download_executor=self.executor())
        self.context = self.app.router.lifespan_context(self.app)
        await self.context.__aenter__()
        self.queue = self.app.state.queue
        self.config = await self.app.state.settings.get_all()
        self.config.update(max_retries=0, filename_template="%(id)s.%(ext)s")

    async def asyncTearDown(self):
        try:
            RECEIPTS.append(self.diagnostic("teardown"))
            await self.context.__aexit__(None, None, None)
        finally:
            await media.WorkerExecutionChecks.asyncTearDown(self)

    async def until(self, predicate, *, timeout=6, label="condition"):
        RECEIPTS.append(self.diagnostic("wait-start", label=label, timeout=timeout))
        try:
            result = await media.WorkerExecutionChecks.until(self, predicate, timeout=timeout, label=label)
        except AssertionError:
            RECEIPTS.append(self.diagnostic("deadline", label=label, timeout=timeout))
            raise
        RECEIPTS.append(self.diagnostic("wait-complete", label=label, timeout=timeout))
        return result

    def diagnostic(self, phase, **fields):
        record = {"case": self.id(), "phase": phase, "root": str(self.root), "time": time.time(), **fields}
        try:
            record["jobs"] = self.rows("""SELECT id,parent_id,status,current_attempt_id,auth_required,
                error_code,error_message,attempt_count,retry_count,retry_at,request_count,download_options
                FROM downloads ORDER BY created_at LIMIT 256""")
            record["attempts"] = self.rows("""SELECT id,download_id,status,number,error_message,worker_identity
                FROM download_attempts ORDER BY started_at LIMIT 256""")
            record["request_leases"] = self.rows("SELECT * FROM request_leases LIMIT 256")
            record["request_hosts"] = self.rows("SELECT * FROM request_hosts LIMIT 256")
            record["logs"] = {path.name: redact_diagnostic(path.read_text(errors="replace")[-12000:])
                              for path in self.root.glob("service-*.log")}
        except (OSError, sqlite3.Error) as error:
            record["diagnostic_error"] = redact_diagnostic(error)
        return record

    def rows(self, sql, values=()):
        with closing(sqlite3.connect(self.runtime.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, values)]

    async def expand(self, server, selection=None):
        parent = await self.queue.enqueue(server.url, "best", is_playlist=True, playlist_items=selection,
                                          audio_only=True, audio_format="wav", output_template="%(id)s.%(ext)s",
                                          idempotency_key="owned-playlist")
        options = json.loads(self.queue.in_memory_progress[parent]["download_options"])
        await self.queue._run_task(parent, server.url, "best", self.config, options)
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (parent,))[0]["status"], "waiting_for_children")
        return parent, self.rows("SELECT * FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))

    async def finish_child(self, child):
        await self.queue._run_task(child["id"], child["url"], child["format_selected"], self.config, json.loads(child["download_options"]))
        row = self.rows("SELECT * FROM downloads WHERE id=?", (child["id"],))[0]
        self.assertEqual(row["status"], "complete", row["error_message"])
        with wave.open(row["file_path"], "rb") as source:
            self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()), (1, 2, 16000, 32000))
            digest = hashlib.sha256(source.readframes(source.getnframes())).hexdigest()
        return row, digest

    async def test_playlist_publishes_two_independent_verified_media_outputs(self):
        with PlaylistServer() as server:
            parent, children = await self.expand(server)
            self.assertEqual(len(children), 2)
            paths = []
            for child, name in zip(children, ("one", "two")):
                row, digest = await self.finish_child(child)
                self.assertEqual(digest, server.pcm[name])
                paths.append(row["file_path"])
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(self.rows("SELECT status,file_path FROM downloads WHERE id=?", (parent,)), [{"status": "complete", "file_path": None}])
            self.assertEqual(len(self.rows("SELECT * FROM download_attempts WHERE status='complete'")), 3)
            RECEIPTS.append({"case": self.id(), "pcm_sha256": server.pcm, "outputs": paths, "requests": server.requests})

    async def test_selected_position_downloads_only_its_bound_media(self):
        with PlaylistServer() as server:
            parent, children = await self.expand(server, "2")
            self.assertEqual([child["playlist_index"] for child in children], [2])
            _, digest = await self.finish_child(children[0])
            self.assertEqual(digest, server.pcm["two"])
            self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (parent,))[0]["status"], "complete")

    async def test_replay_and_reopen_do_not_repeat_expansion_or_completed_child(self):
        with PlaylistServer() as server:
            parent, children = await self.expand(server)
            completed, _ = await self.finish_child(children[0])
            before = Path(completed["file_path"]).read_bytes()
            await self.context.__aexit__(None, None, None)
            self.context = self.app.router.lifespan_context(self.app)
            await self.context.__aenter__()
            replay = await self.queue.enqueue(server.url, "best", is_playlist=True, audio_only=True,
                                              audio_format="wav", output_template="%(id)s.%(ext)s", idempotency_key="owned-playlist")
            self.assertEqual(replay, parent)
            self.assertIsNone(await self.queue.attempts.claim(parent, self.queue._now()))
            self.assertEqual([child["id"] for child in self.rows("SELECT id FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))], [child["id"] for child in children])
            await self.finish_child(children[1])
            self.assertEqual(Path(completed["file_path"]).read_bytes(), before)

    async def test_cancel_during_metadata_extraction_joins_tree_without_children(self):
        with PlaylistServer(gate="/list.html") as server:
            parent = await self.queue.enqueue(server.url, "best", is_playlist=True)
            execution = asyncio.create_task(self.queue._run_task(parent, server.url, "best", self.config, {"is_playlist": True}))
            self.queue.active_tasks[parent] = execution
            try:
                await self.until(server.entered.is_set, timeout=8, label="playlist metadata request")
                await self.queue.cancel(parent)
            finally:
                server.release.set()
                results = await asyncio.gather(execution, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self.assertEqual(self.rows("SELECT status FROM downloads"), [{"status": "cancelled"}])
            self.assertEqual(self.owned_tree(), [])

    async def test_parent_retry_preserves_completed_output_and_reuses_cancelled_child_id(self):
        with PlaylistServer() as server:
            parent, children = await self.expand(server)
            completed, _ = await self.finish_child(children[0])
            before = Path(completed["file_path"]).read_bytes()
            await self.queue.cancel(parent)
            await self.queue.retry(parent)
            current = self.rows("SELECT * FROM downloads WHERE id=?", (children[1]["id"],))[0]
            self.assertEqual(current["status"], "queued")
            await self.finish_child(current)
            self.assertEqual(Path(completed["file_path"]).read_bytes(), before)
            self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (parent,))[0]["status"], "complete")

    async def test_media_worker_refuses_nested_playlist_without_publication(self):
        with PlaylistServer() as server:
            job = await self.queue.enqueue(server.url, "best", audio_only=True, audio_format="wav")
            await self.queue._run_task(job, server.url, "best", self.config, {"audio_only": True, "audio_format": "wav"})
            self.assertEqual(self.rows("SELECT status,file_path FROM downloads WHERE id=?", (job,)), [{"status": "failed", "file_path": None}],
                             "A media job must not publish an inline nested playlist")
            self.assertEqual(self.rows("SELECT * FROM download_artifacts"), [])
            self.assertEqual(list(self.runtime.download_dir.iterdir()), [])

    async def test_engine_death_before_child_commit_requeues_expansion_once(self):
        with PlaylistServer(gate="/list.html") as server:
            parent = await self.queue.enqueue(server.url, "best", is_playlist=True, audio_only=True,
                                              audio_format="wav", output_template="%(id)s.%(ext)s", idempotency_key="crash-parent")
            await self.context.__aexit__(None, None, None)
            first = services.Service(self.root, self.runtime, "before-commit-1", hold_expansion=True)
            second = services.Service(self.root, self.runtime, "before-commit-2")
            try:
                await asyncio.to_thread(first.start)
                await self.until(server.entered.is_set, timeout=10, label="gated metadata extraction")
                attempt = self.rows("SELECT * FROM download_attempts WHERE download_id=? AND status='running'", (parent,))[0]
                server.release.set()
                await self.until((self.root / "expansion-held").is_file, timeout=10, label="selected child-expansion gate")
                await self.until(lambda: not ProcessIdentity.from_json(attempt["worker_identity"]).worker_tree_is_running(), timeout=10, label="metadata worker tree exit before commit")
                self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM request_leases"), [{"n": 0}],
                                 "The metadata producer must finish its responses before the child-commit gate")
                await asyncio.to_thread(first.stop, hard=True)
                self.assertEqual(self.rows("SELECT * FROM downloads WHERE parent_id=?", (parent,)), [])
                await asyncio.to_thread(second.start)
                await self.until(lambda: self.rows("SELECT status FROM downloads WHERE id=?", (parent,))[0]["status"] == "complete", timeout=15, label="recovered playlist completion")
                children = self.rows("SELECT * FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
                self.assertEqual(len(children), 2)
                self.assertEqual(self.rows("SELECT attempt_count FROM downloads WHERE id=?", (parent,))[0]["attempt_count"], 2)
                for child, name in zip(children, ("one", "two")):
                    with wave.open(child["file_path"], "rb") as source:
                        self.assertEqual(hashlib.sha256(source.readframes(source.getnframes())).hexdigest(), server.pcm[name])
                self.assertEqual(len(self.rows("SELECT * FROM enqueue_receipts")), 1)
            finally:
                server.release.set()
                await asyncio.to_thread(first.stop, hard=True)
                await asyncio.to_thread(second.stop, hard=True)
                self.context = self.app.router.lifespan_context(self.app)
                await self.context.__aenter__()
            RECEIPTS.append({"case": self.id(), "parent_attempts": self.rows("SELECT number,status FROM download_attempts WHERE download_id=? ORDER BY number", (parent,)),
                "source_expansion_gate": True, "frozen_worker_and_recovery": bool(media.ENGINE),
                "logs": {path.name: path.read_text(errors="replace")[-6000:] for path in self.root.glob("service-before-commit-*.log")}})

    async def test_engine_death_after_expansion_preserves_children_and_persisted_cancel(self):
        with PlaylistServer(gate="/two.wav") as server:
            parent = await self.queue.enqueue(server.url, "best", is_playlist=True, audio_only=True,
                                              audio_format="wav", output_template="%(id)s.%(ext)s", idempotency_key="crash-parent")
            await self.context.__aexit__(None, None, None)
            first = services.Service(self.root, self.runtime, "after-commit-1")
            second = services.Service(self.root, self.runtime, "after-commit-2")
            try:
                await asyncio.to_thread(first.start)
                await self.until(server.entered.is_set, timeout=12, label="committed child media request")
                children = self.rows("SELECT * FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
                self.assertEqual(len(children), 2)
                def settled_sibling():
                    sibling = self.rows("SELECT * FROM downloads WHERE id=?", (children[0]["id"],))[0]
                    if sibling["status"] == "complete" or (sibling["status"] == "retry_wait"
                            and sibling["error_code"] == "ADMISSION_LIMITED" and sibling["retry_at"] > time.time()):
                        return sibling
                    return None
                # Crashing an unrelated publisher before its success footer is
                # deliberately ambiguous. Select a committed sibling boundary
                # while the cancelled child is still held before its response.
                sibling = await self.until(settled_sibling, timeout=8,
                    label="sibling completion or persisted admission retry before crash")
                completed_bytes = Path(sibling["file_path"]).read_bytes() if sibling["status"] == "complete" else None
                self.assertEqual(sibling["retry_count"], 0)
                cancelled = children[1]["id"]
                attempts = self.rows("SELECT worker_identity FROM download_attempts WHERE status='running'")
                with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                    db.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (cancelled,))
                await asyncio.to_thread(first.stop, hard=True)
                server.release.set()
                for attempt in attempts:
                    await self.until(lambda selected=attempt: not ProcessIdentity.from_json(selected["worker_identity"]).worker_tree_is_running(), timeout=10, label="interrupted child tree exit")
                await asyncio.to_thread(second.start)
                await self.until(lambda: self.rows("SELECT status FROM downloads WHERE id=?", (parent,))[0]["status"] == "cancelled", timeout=15, label="persisted child cancellation and sibling completion")
                after = self.rows("SELECT * FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
                self.assertEqual([child["id"] for child in after], [child["id"] for child in children])
                self.assertEqual([child["status"] for child in after], ["complete", "cancelled"])
                if completed_bytes is not None:
                    self.assertEqual(Path(after[0]["file_path"]).read_bytes(), completed_bytes)
                self.assertIsNone(after[1]["file_path"])
                self.assertEqual(self.rows("SELECT attempt_count FROM downloads WHERE id=?", (parent,))[0]["attempt_count"], 1)
            finally:
                server.release.set()
                await asyncio.to_thread(first.stop, hard=True)
                await asyncio.to_thread(second.stop, hard=True)
                self.context = self.app.router.lifespan_context(self.app)
                await self.context.__aenter__()
            RECEIPTS.append({"case": self.id(), "child_ids": [child["id"] for child in children],
                "sibling_crash_boundary": sibling["status"], "preserved_completed_output": completed_bytes is not None,
                "logs": {path.name: path.read_text(errors="replace")[-6000:] for path in self.root.glob("service-after-commit-*.log")}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    media.COMPONENTS, _ = check_components(args.tools.resolve(strict=True))
    media.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    services.ENGINE = media.ENGINE
    services.TOOLS = args.tools.resolve(strict=True)
    sys.addaudithook(fixture_network_guard)
    started = time.monotonic()
    suite = unittest.TestSuite(PlaylistWorkerChecks(name) for name in args.case) if args.case else unittest.defaultTestLoader.loadTestsFromTestCase(PlaylistWorkerChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    with args.report.open("x", encoding="utf-8") as output:
        json.dump({"ok": result.wasSuccessful(), "cases": result.testsRun, "seconds": round(time.monotonic() - started, 3),
                   "engine": str(media.ENGINE) if media.ENGINE else None, "skipped": [(case.id(), reason) for case, reason in result.skipped],
                   "errors": [(case.id(), message) for case, message in result.errors + result.failures], "receipts": RECEIPTS}, output, indent=2)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
