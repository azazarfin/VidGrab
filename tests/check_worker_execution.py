"""Opt-in real loopback media, process cancellation and engine-death checks.

Requires explicitly selected verified tools. --engine selects fresh frozen
worker modes; no installed runtime or extra process library is used.
"""

import argparse
import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import wave

import aiosqlite

from backend.app import create_app
from backend.engine.execution import ProcessDownloadExecutor
from backend.engine.resources import check_components
from backend.runtime import RuntimeConfig
from tests.worker_process_support import owned_children

COMPONENTS = None
ENGINE = None


class WaveServer:
    def __init__(self, *, phase=None):
        memory = io.BytesIO()
        with wave.open(memory, "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b"\x00\x00" * 192000)
        media = memory.getvalue()
        self.requested = threading.Event()
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/fixture.wav":
                    self.send_error(404)
                    return
                fixture.requested.set()
                if phase == "extraction" and not fixture.release.wait(12):
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(media)))
                    self.end_headers()
                    for offset in range(0, len(media), 4096):
                        self.wfile.write(media[offset:offset + 4096])
                        self.wfile.flush()
                        if phase == "download" and offset == 0 and not fixture.release.wait(12):
                            return
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture.wav"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class WorkerExecutionChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-worker-media-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = RuntimeConfig(data_dir=self.root / "state", download_dir=self.root / "বাংলা media")
        self.runtime.temp_dir.mkdir(parents=True)
        self.runtime.download_dir.mkdir(parents=True)
        self.progress = []
        self.loop_errors = []
        self.loop = asyncio.get_running_loop()
        self.previous_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.children_before = {(child.pid, child.started) for child in owned_children()}
        if ENGINE:
            def command(module, flag):
                return [str(ENGINE), flag]
            replacement = patch("backend.engine.execution.entry_command", command)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.loop.set_exception_handler(self.previous_handler)
        self.assertEqual(self.loop_errors, [], "The real boundary must retrieve every task exception")
        self.assertEqual(self.owned_tree(), [], "All newly owned processes must exit before returning")

    def options(self, url, *, realtime=False):
        result = dict(runtime=self.runtime, task_id="fixture", url=url, format_id="best",
                      download_dir=str(self.runtime.download_dir), filename_template="owned-%(id)s.%(ext)s",
                      progress_callback=lambda _, data: self.progress.append(data),
                      audio_only=True, audio_format="mp3")
        if realtime:
            result["extra_args"] = {"postprocessor_args": {"extractaudio+ffmpeg_i1": ["-re"]}}
        return result

    def executor(self):
        return ProcessDownloadExecutor(components=COMPONENTS, fixture_only=True)

    def owned_tree(self):
        return owned_children(self.children_before)

    async def until(self, predicate, *, timeout=6, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        self.fail(f"Deadline exceeded waiting for {label}")

    def assert_exited(self, processes):
        for process in processes:
            self.assertFalse(process.is_running(), f"Owned process {process.pid} survived teardown")

    async def cancel_execution(self, phase):
        unrelated = self.runtime.download_dir / "owned-other-job.part"
        unrelated.write_bytes(b"unrelated partial download")
        with WaveServer(phase=phase if phase != "postprocessing" else None) as server:
            execution = asyncio.create_task(self.executor().run(**self.options(server.url, realtime=phase == "postprocessing")))
            try:
                if phase == "extraction":
                    await self.until(server.requested.is_set, label="extractor HTTP request")
                else:
                    status = "downloading" if phase == "download" else "postprocessing"
                    await self.until(lambda: any(item["status"] == status for item in self.progress), label=status)
                if phase == "postprocessing":
                    await self.until(lambda: any(self.is_ffmpeg(p) for p in self.owned_tree()), label="real FFmpeg descendant")
                processes = self.owned_tree()
                self.assertGreaterEqual(len(processes), 2)
                execution.cancel()
                await asyncio.sleep(0)
                execution.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(execution, 7)
                self.assert_exited(processes)
                self.assertEqual(unrelated.read_bytes(), b"unrelated partial download")
            finally:
                server.release.set()
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    @staticmethod
    def is_ffmpeg(process):
        return process.name.lower() in {"ffmpeg", "ffmpeg.exe"}

    async def test_real_wav_to_mp3_preserves_unicode_and_bypasses_database_startup(self):
        with WaveServer() as server:
            try:
                result = await asyncio.wait_for(self.executor().run(**self.options(server.url)), 15)
            except RuntimeError as error:
                self.fail(f"A successfully converted file needs a clean worker exit: {error}")
        output = Path(result["filepath"])
        self.assertEqual(output.parent, self.runtime.download_dir)
        self.assertEqual(output.suffix, ".mp3")
        self.assertGreater(output.stat().st_size, 100)
        probe = await asyncio.to_thread(subprocess.run,
            [COMPONENTS["ffprobe"], "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "json", str(output)],
            capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "mp3")
        self.assertFalse(self.runtime.db_path.exists(), "A worker must never initialize the service database")
        self.assertTrue(any(item["status"] == "postprocessing" for item in self.progress))

    async def test_cancel_during_extraction_joins_the_actual_worker(self):
        await self.cancel_execution("extraction")

    async def test_cancel_during_download_joins_the_actual_worker(self):
        await self.cancel_execution("download")

    async def test_cancel_during_postprocessing_joins_real_ffmpeg(self):
        await self.cancel_execution("postprocessing")

    async def test_queue_cancellation_removes_only_its_cookie_after_real_worker_exit(self):
        app = create_app(self.runtime, start_queue=True, download_executor=self.executor())
        with WaveServer(phase="download") as server:
            async with app.router.lifespan_context(app):
                task_id = await app.state.queue.enqueue(server.url, "best", audio_only=True,
                    output_template="owned-%(id)s.%(ext)s",
                    cookies=[{"domain": "127.0.0.1", "name": "fixture", "value": "temporary-test-cookie"}])
                await self.until(lambda: app.state.queue.in_memory_progress[task_id].get("progress", 0) > 0,
                                 label="real queue download progress")
                cookies = list(self.runtime.temp_dir.glob("*.txt"))
                self.assertEqual(len(cookies), 1)
                unrelated = self.runtime.temp_dir / "another-job.cookies"
                unrelated.write_bytes(b"not this worker's cookie")
                processes = self.owned_tree()
                await asyncio.wait_for(app.state.queue.cancel(task_id), 7)
                self.assert_exited(processes)
                self.assertFalse(cookies[0].exists())
                self.assertEqual(unrelated.read_bytes(), b"not this worker's cookie")
                async with aiosqlite.connect(self.runtime.db_path) as database:
                    async with database.execute("SELECT status FROM downloads WHERE id=?", (task_id,)) as cursor:
                        self.assertEqual((await cursor.fetchone())[0], "cancelled")

    async def abrupt_engine_exit(self, *, postprocessing=False):
        driver = """
import asyncio, json, sys
from backend.engine import execution
from backend.runtime import RuntimeConfig
bootstrap = json.loads(sys.stdin.readline())
if bootstrap['engine']:
    execution.entry_command = lambda module, flag: [bootstrap['engine'], flag]
options = bootstrap['options']
options['runtime'] = RuntimeConfig(**bootstrap['runtime'])
options['progress_callback'] = lambda *_: None
asyncio.run(execution.ProcessDownloadExecutor(components=bootstrap['components'], fixture_only=True).run(**options))
"""
        with WaveServer(phase=None if postprocessing else "extraction") as server, tempfile.TemporaryFile() as errors:
            options = self.options(server.url, realtime=postprocessing)
            options.pop("runtime")
            options.pop("progress_callback")
            bootstrap = dict(options=options, components=COMPONENTS, engine=str(ENGINE) if ENGINE else None,
                runtime={name: str(getattr(self.runtime, name)) for name in ("data_dir", "download_dir", "logs_dir", "temp_dir")})
            engine = subprocess.Popen([sys.executable, "-B", "-c", driver], stdin=subprocess.PIPE,
                                      stdout=errors, stderr=errors, close_fds=True)
            try:
                engine.stdin.write((json.dumps(bootstrap) + "\n").encode())
                engine.stdin.flush()
                if postprocessing:
                    await self.until(lambda: any(self.is_ffmpeg(p) for p in self.owned_tree()), label="engine-owned FFmpeg")
                else:
                    await self.until(server.requested.is_set, label="engine-owned extraction")
                processes = self.owned_tree()
                self.assertGreaterEqual(len(processes), 3)
                engine.kill()
                engine.wait(timeout=3)
                await self.until(lambda: all(not p.is_running() for p in processes), label="tree exit after abrupt engine death")
                self.assert_exited(processes)
            finally:
                server.release.set()
                if engine.poll() is None:
                    engine.kill()
                    engine.wait(timeout=3)
                engine.stdin.close()

    async def test_abrupt_engine_death_stops_extraction_without_a_service(self):
        await self.abrupt_engine_exit()

    async def test_abrupt_engine_death_stops_postprocessing_descendants(self):
        await self.abrupt_engine_exit(postprocessing=True)


def main():
    global COMPONENTS, ENGINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    COMPONENTS, versions = check_components(arguments.tools.resolve())
    ENGINE = arguments.engine.resolve() if arguments.engine else None
    if ENGINE and not ENGINE.is_file():
        parser.error("--engine must select an existing fresh frozen executable")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WorkerExecutionChecks)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if arguments.report:
        arguments.report.write_text(json.dumps(dict(ok=result.wasSuccessful(), tests=result.testsRun,
            seconds=round(time.monotonic() - started, 3), engine=str(ENGINE) if ENGINE else "source",
            versions=versions, failures=[str(test) + ": " + trace for test, trace in result.failures + result.errors]), indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
