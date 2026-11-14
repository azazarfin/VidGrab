"""Opt-in actual ASGI ownership, startup-barrier and gated-download restart checks.

Only explicitly verified tools, private temporary runtimes and loopback WAVs
are used. Linux orphan-group and before/after-link publication cases use
owned synthetic processes. A real media result is held before SQLite's
terminal write. Verified resume and power-loss durability remain separate.
"""

import argparse
import asyncio
from contextlib import closing
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from backend.app import create_app
from backend.engine import create_engine_app
from backend.engine.__main__ import Bootstrap, ReadyServer
from backend.engine.network_guard import fixture_network_guard
from backend.engine.process_identity import ProcessIdentity
from backend.engine.resources import check_components
from backend.db.migrations import SCHEMA_VERSION
from backend.runtime import RuntimeConfig
from backend.services.artifacts import prepare_workspace
from backend.services.artifacts import read_journal
from backend.services.publication import plan_publications, publish_artifact
from tests.check_worker_execution import WaveServer
from tests.worker_process_support import linux_identity, windows_identity, snapshot, windows_kernel

ENGINE = None
TOOLS = None
RECEIPTS = []


def wait_for(condition, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError(message)


def alive_identity(value):
    identity = json.loads(value)
    if sys.platform == "win32":
        return windows_identity(identity["pid"]) == identity["started"]
    current = linux_identity(identity["pid"])
    if current is None or current.started != identity["started"]:
        return False
    try:
        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[0] not in {"Z", "X"}


def tree_alive(value):
    if alive_identity(value):
        return True
    if sys.platform != "linux":
        return False
    identity = json.loads(value)
    current = linux_identity(identity["pid"])
    if current is not None and current.started != identity["started"]:
        return False
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if fields[0] not in {"Z", "X"} and int(fields[2]) == identity["pid"] and int(fields[3]) == identity["pid"]:
                return True
    return False


def terminate_windows_identity(value):
    """Terminate through a creation-time-verified handle, never an unbound PID."""
    identity = json.loads(value)
    kernel = windows_kernel()
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x101001, False, identity["pid"])
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if started != identity["started"] or kernel.WaitForSingleObject(handle, 0) == 0:
            return
        if not kernel.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 7000) != 0:
            raise TimeoutError("The selected service process did not exit")
    finally:
        kernel.CloseHandle(handle)


class Service:
    def __init__(self, root, runtime, sequence):
        self.runtime = runtime
        self.root = root
        self.token = secrets.token_hex(32)
        self.process = None
        self.server_identity = None
        self.port = None
        self.log = root / f"service-{sequence}.log"

    def start(self, *, refused=False):
        command = ([str(ENGINE)] if ENGINE else [sys.executable, "-B", "-m", "tests.check_service_recovery",
                                                "--serve", "--tools", str(TOOLS)])
        with self.log.open("xb") as log:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                            close_fds=True,
                                            creationflags=0x08000000 if sys.platform == "win32" else 0)
        launcher = ProcessIdentity.capture(self.process.pid)
        bootstrap = {"token": self.token, "data_dir": str(self.runtime.data_dir),
                     "download_dir": str(self.runtime.download_dir)}
        self.process.stdin.write((json.dumps(bootstrap) + "\n").encode())
        self.process.stdin.flush()
        output = queue.Queue()
        reader = threading.Thread(target=lambda: output.put(self.process.stdout.readline(65537)), daemon=True)
        reader.start()
        try:
            line = output.get(timeout=10)
            reader.join(timeout=1)
            if refused:
                if line:
                    raise AssertionError("A second service became ready against live ownership")
                self.process.wait(timeout=5)
                if "owner" not in self.log.read_text(errors="replace").lower():
                    raise AssertionError("Startup failed without an ownership refusal")
                return
            if not line:
                raise AssertionError(self.log.read_text(errors="replace")[-2500:])
            ready = json.loads(line)
            pid = ready.get("pid")
            if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or ready.get("spike_version") != 1:
                raise AssertionError("Invalid service readiness identity")
            identity = ProcessIdentity.capture(pid)
            if identity is None or launcher is None:
                raise AssertionError("Readiness process identity could not be verified")
            if pid != self.process.pid:
                # Windows venv redirectors can host the interpreter in a child.
                processes = {process.pid: process for process in snapshot()}
                current, seen = processes.get(pid), set()
                while current and current.pid not in seen and current.pid != launcher.pid:
                    seen.add(current.pid)
                    current = processes.get(current.parent)
                if current is None or current.pid != launcher.pid or current.started != launcher.started:
                    raise AssertionError("The readiness process is outside the owned launcher tree")
            self.server_identity = identity.to_json()
            self.port = ready["port"]
            if self.request("GET", "/health")["schema_version"] != SCHEMA_VERSION:
                raise AssertionError("Service is not using the ownership schema")
        except BaseException:
            self.stop(hard=True)
            raise

    def request(self, method, path, body=None):
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.request(method, f"http://127.0.0.1:{self.port}{path}", json=body,
                                      headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            return response.json()

    def stop(self, *, hard=False):
        if self.process is None:
            return
        if sys.platform == "win32" and self.server_identity:
            terminate_windows_identity(self.server_identity)
        if self.process.poll() is None:
            self.process.kill() if hard else self.process.terminate()
            try:
                self.process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()


class ServiceRecoveryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-service-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = RuntimeConfig(self.root / "state", self.root / "media বাংলা")
        self.services = []
        self.attempt = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for service in self.services:
            service.stop(hard=True)
        if self.attempt:
            self.wait_tree_exit(self.attempt)
        RECEIPTS.append({"case": self.id(), "logs": {path.name: path.read_text(errors="replace")[-4000:]
                                                    for path in self.root.glob("*.log")}})

    def service(self, *, refused=False):
        service = Service(self.root, self.runtime, len(self.services))
        self.services.append(service)
        service.start(refused=refused)
        return service

    def rows(self, sql, values=()):
        with closing(sqlite3.connect(self.runtime.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, values)]

    def active(self, server):
        first = self.service()
        job = first.request("POST", "/spike/fixture", {"url": server.url})["task_id"]

        def registered():
            rows = self.rows("SELECT * FROM download_attempts WHERE download_id=?", (job,))
            return rows[0] if rows and rows[0]["worker_owner_identity"] and rows[0]["worker_identity"] else None

        attempt = wait_for(registered, "Worker identities were not committed")
        self.attempt = attempt
        wait_for(lambda: server.requested.is_set(), "The local download did not start")
        wait_for(lambda: list(Path(attempt["workspace"]).glob("*.part")), "The gated attempt did not create a partial file")
        self.assertEqual(self.rows("SELECT a.owner_id, a.lease_expires_at=o.lease_expires_at AS aligned FROM download_attempts a"
                                  " JOIN runtime_owners o ON o.id=a.owner_id WHERE a.id=?", (attempt["id"],)),
                         [{"owner_id": attempt["owner_id"], "aligned": 1}])
        return first, job, attempt

    def wait_tree_exit(self, attempt):
        wait_for(lambda: (attempt["worker_owner_identity"] is None or not alive_identity(attempt["worker_owner_identity"]))
                 and (attempt["worker_identity"] is None or not tree_alive(attempt["worker_identity"])),
                 "The original attempt still has live processes")


class ServiceRecoveryChecks(ServiceRecoveryFixture):

    def test_second_service_refuses_a_live_runtime_without_resetting_its_attempt(self):
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            self.service(refused=True)
            self.assertIsNone(first.process.poll())
            self.assertEqual(self.rows("SELECT status, current_attempt_id, attempt_count FROM downloads WHERE id=?", (job,)),
                             [{"status": "downloading", "current_attempt_id": attempt["id"], "attempt_count": 1}])
            first.stop(hard=True)
            self.wait_tree_exit(attempt)

    def test_killed_service_restarts_in_a_new_workspace_and_retains_the_prior_partial(self):
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            unrelated = self.runtime.download_dir / "fixture another job.part"
            unrelated.write_bytes(b"unrelated download bytes")
            first.stop(hard=True)
            self.wait_tree_exit(attempt)
            partials = {str(path): path.read_bytes() for path in Path(attempt["workspace"]).glob("*.part")}
            self.assertTrue(partials)
            second = self.service()
            fixture.release.set()
            wait_for(lambda: any(row["id"] == job and row["status"] == "complete"
                                 for row in second.request("GET", "/spike/status")["jobs"]),
                     "The restarted service did not complete its local download", timeout=12)
            attempts = self.rows("SELECT id, number, status, workspace, owner_id FROM download_attempts ORDER BY number")
            self.assertEqual([row["status"] for row in attempts], ["interrupted", "complete"])
            self.assertEqual([row["number"] for row in attempts], [1, 2])
            self.assertNotEqual(attempts[0]["workspace"], attempts[1]["workspace"])
            self.assertNotEqual(attempts[0]["owner_id"], attempts[1]["owner_id"])
            self.assertEqual({path: Path(path).read_bytes() for path in partials}, partials)
            self.assertEqual(unrelated.read_bytes(), b"unrelated download bytes")
            row = self.rows("SELECT file_path, retry_count FROM downloads WHERE id=?", (job,))[0]
            self.assertEqual(row["retry_count"], 0)
            self.assertTrue(Path(row["file_path"]).is_file())
            self.assertIn("বাংলা", row["file_path"])
            self.assertTrue(self.rows("SELECT * FROM download_artifacts WHERE attempt_id=? AND state='partial'", (attempt["id"],)))

    def publication_checkpoint(self, phase):
        """Kill an owned diagnostic publisher at a known abandoned-job boundary."""
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            first.stop(hard=True)
            self.wait_tree_exit(attempt)
            owner_command = ([str(ENGINE), "--worker-owner"] if ENGINE else
                             [sys.executable, "-B", "-m", "backend.engine.worker_owner"])
            command = [*owner_command, "--", sys.executable, "-B", "-m", "tests.check_service_recovery",
                       "--tools", str(TOOLS), "--publication-worker"]
            bootstrap = {"runtime": {key: str(getattr(self.runtime, key)) for key in
                                       ("data_dir", "download_dir", "logs_dir", "temp_dir")},
                         "options": {"attempt_dir": attempt["workspace"], "phase": phase}}
            publisher = None
            identities = None
            try:
                with (self.root / f"publication-{phase}.log").open("xb") as log:
                    publisher = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=log, close_fds=True, creationflags=0x08000000 if sys.platform == "win32" else 0)
                launcher = ProcessIdentity.capture(publisher.pid)
                publisher.stdin.write((json.dumps(bootstrap) + "\n").encode())
                publisher.stdin.flush()
                frames = queue.Queue()

                def read_checkpoint():
                    frames.put(publisher.stdout.readline(65537))
                    frames.put(publisher.stdout.readline(65537))

                reader = threading.Thread(target=read_checkpoint, daemon=True)
                reader.start()
                started = json.loads(frames.get(timeout=10))
                checkpoint = json.loads(frames.get(timeout=10))
                reader.join(timeout=1)
                self.assertEqual(started["type"], "started")
                self.assertEqual(checkpoint["type"], "progress")
                self.assertEqual(checkpoint["data"]["phase"], phase)
                # Windows venv redirectors introduce launcher/interpreter pairs.
                # The worker's kernel parent identifies the actual owner holding
                # the Job Object; never kill an unverified launcher alone.
                processes = {process.pid: process for process in snapshot()}
                owner_pid = processes[started["pid"]].parent
                current, seen = processes.get(owner_pid), set()
                while current and current.pid != launcher.pid and current.pid not in seen:
                    seen.add(current.pid)
                    current = processes.get(current.parent)
                self.assertIsNotNone(current)
                self.assertEqual((current.pid, current.started), (launcher.pid, launcher.started))
                identities = {"worker_owner_identity": ProcessIdentity.capture(owner_pid).to_json(),
                              "worker_identity": ProcessIdentity.capture(started["pid"]).to_json()}
                with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                    db.execute("UPDATE download_attempts SET worker_owner_identity=?, worker_identity=? WHERE id=?",
                               (identities["worker_owner_identity"], identities["worker_identity"], attempt["id"]))
                attempt.update(identities)
                if sys.platform == "win32":
                    terminate_windows_identity(identities["worker_owner_identity"])
                else:
                    self.assertTrue(alive_identity(identities["worker_owner_identity"]))
                    os.kill(owner_pid, signal.SIGKILL)
                publisher.wait(timeout=7)
                self.wait_tree_exit(attempt)
                records, result = read_journal(self.runtime, Path(attempt["workspace"]))
                self.assertIsNone(result)
                source = Path(records[0]["source"])
                before = source.read_bytes()
                second = self.service()
                if phase == "before-link":
                    fixture.release.set()
                    wait_for(lambda: any(row["id"] == job and row["status"] == "complete"
                                         for row in second.request("GET", "/spike/status")["jobs"]),
                             "The unpublished attempt was not safely retried", timeout=12)
                    self.assertEqual(self.rows("SELECT number, status FROM download_attempts ORDER BY number"),
                                     [{"number": 1, "status": "interrupted"}, {"number": 2, "status": "complete"}])
                else:
                    self.assertEqual(self.rows("SELECT status, attempt_count, retry_count, file_path FROM downloads"),
                                     [{"status": "failed", "attempt_count": 1, "retry_count": 0, "file_path": None}])
                    self.assertEqual(Path(records[0]["path"]).read_bytes(), before)
                    self.assertEqual(self.rows("SELECT state FROM download_artifacts WHERE kind='media'"), [{"state": "complete"}])
                    second.stop(hard=True)
                    self.service()
                    self.assertEqual(self.rows("SELECT status, attempt_count FROM downloads"), [{"status": "failed", "attempt_count": 1}])
                self.assertEqual(source.read_bytes(), before)
                RECEIPTS.append({"case": self.id(), "boundary": phase, "synthetic_publication": True,
                                 "source": str(source), "output": records[0]["path"], "identities": identities,
                                 "attempts": self.rows("SELECT id, number, status FROM download_attempts ORDER BY number")})
            finally:
                if publisher:
                    if identities and alive_identity(identities["worker_owner_identity"]):
                        if sys.platform == "win32":
                            terminate_windows_identity(identities["worker_owner_identity"])
                        else:
                            os.kill(json.loads(identities["worker_owner_identity"])["pid"], signal.SIGTERM)
                    if publisher.poll() is None:
                        publisher.stdin.close()
                        try:
                            publisher.wait(timeout=7)
                        except subprocess.TimeoutExpired:
                            publisher.kill()
                            publisher.wait(timeout=3)
                    if not publisher.stdin.closed:
                        publisher.stdin.close()
                    publisher.stdout.close()

    def test_owned_publisher_crash_before_link_safely_retries_the_abandoned_job(self):
        self.publication_checkpoint("before-link")

    def test_owned_publisher_crash_after_link_preserves_the_output_and_stops_retry(self):
        self.publication_checkpoint("after-link")

    def test_real_media_result_before_sqlite_terminal_write_recovers_without_redownload(self):
        import hashlib
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            workspace = Path(attempt["workspace"])
            with closing(sqlite3.connect(self.runtime.db_path)) as gate:
                gate.execute("BEGIN IMMEDIATE")
                fixture.release.set()

                def durable_result():
                    _, result = read_journal(self.runtime, workspace)
                    return result

                result = wait_for(durable_result, "Real worker result was not durably journaled", timeout=4)
                self.wait_tree_exit(attempt)
                self.assertEqual(self.rows("SELECT status, current_attempt_id FROM downloads"),
                                 [{"status": "downloading", "current_attempt_id": attempt["id"]}])
                output = Path(result["filepath"])
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                probe = subprocess.run([str(TOOLS / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")),
                                        "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", str(output)],
                                       capture_output=True, text=True, timeout=5, check=True)
                self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "mp3")
                first.stop(hard=True)
                gate.rollback()
            second = self.service()
            self.assertEqual(self.rows("SELECT status, attempt_count, retry_count, file_path, error_message FROM downloads"),
                             [{"status": "complete", "attempt_count": 1, "retry_count": 0, "file_path": str(output), "error_message": None}])
            self.assertEqual(self.rows("SELECT id, status FROM download_attempts"), [{"id": attempt["id"], "status": "complete"}])
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
            second.stop(hard=True)
            self.service()
            self.assertEqual(self.rows("SELECT status, attempt_count FROM downloads"), [{"status": "complete", "attempt_count": 1}])
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
            RECEIPTS.append({"case": self.id(), "boundary": "result-before-terminal-write", "synthetic_publication": False,
                             "sha256": digest, "output": str(output), "attempt_id": attempt["id"], "worker_tree_exited": True})

    @unittest.skipUnless(sys.platform == "linux", "Uses POSIX stop/continue to hold the actual prior worker owner")
    def test_live_orphan_owner_blocks_recovery_after_engine_death(self):
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            owner = json.loads(attempt["worker_owner_identity"])
            self.assertTrue(alive_identity(attempt["worker_owner_identity"]))
            os.kill(owner["pid"], signal.SIGSTOP)
            try:
                first.stop(hard=True)
                self.service(refused=True)
                self.assertEqual(self.rows("SELECT status, current_attempt_id FROM downloads WHERE id=?", (job,)),
                                 [{"status": "downloading", "current_attempt_id": attempt["id"]}])
            finally:
                if alive_identity(attempt["worker_owner_identity"]):
                    os.kill(owner["pid"], signal.SIGCONT)
            self.wait_tree_exit(attempt)
            second = self.service()
            fixture.release.set()
            wait_for(lambda: any(row["id"] == job and row["status"] == "complete"
                                 for row in second.request("GET", "/spike/status")["jobs"]),
                     "Service did not recover after the prior owner exited", timeout=12)

    @unittest.skipUnless(sys.platform == "linux", "Linux session membership after its leader exits")
    def test_live_orphan_group_blocks_recovery_after_its_leader_exits(self):
        with WaveServer(phase="download") as fixture:
            first, job, attempt = self.active(fixture)
            first.stop(hard=True)
            self.wait_tree_exit(attempt)
            code = """import json, os, subprocess, sys
from backend.engine.process_identity import ProcessIdentity
child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(25)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
print(json.dumps({'leader': ProcessIdentity.capture().to_json(), 'child': ProcessIdentity.capture(child.pid).to_json()}), flush=True)
"""
            leader = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
            output, error = leader.communicate(timeout=5)
            self.assertEqual(leader.returncode, 0, error.decode(errors="replace"))
            identities = json.loads(output)
            try:
                self.assertFalse(alive_identity(identities["leader"]))
                self.assertTrue(alive_identity(identities["child"]))
                with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                    db.execute("UPDATE download_attempts SET worker_owner_identity=NULL, worker_identity=? WHERE id=?",
                               (identities["leader"], attempt["id"]))
                self.service(refused=True)
                self.assertEqual(self.rows("SELECT status FROM download_attempts WHERE id=?", (attempt["id"],)), [{"status": "running"}])
            finally:
                if alive_identity(identities["child"]):
                    os.kill(json.loads(identities["child"])["pid"], signal.SIGKILL)
                wait_for(lambda: not tree_alive(identities["leader"]), "Synthetic orphan group did not exit")

    def test_retired_launch_cannot_bootstrap_its_worker_after_recovery_wins_the_transaction(self):
        async def check():
            app = create_app(self.runtime, start_queue=False)
            async with app.router.lifespan_context(app):
                job = await app.state.queue.enqueue("http://127.0.0.1:1/fixture.wav", "best")
                attempt = await app.state.queue._claim(job)
                prepare_workspace(self.runtime, attempt)
                spawned, executed = self.root / "spawned.json", self.root / "executed"
                code = ("import json, os, sys\nfrom pathlib import Path\n"
                        f"Path({str(spawned)!r}).write_text(json.dumps({{'pid': os.getpid()}}))\n"
                        f"if sys.stdin.buffer.readline(): Path({str(executed)!r}).write_text('executed')\n")
                owner_command = ([str(ENGINE), "--worker-owner"] if ENGINE else
                                 [sys.executable, "-B", "-m", "backend.engine.worker_owner"])
                bootstrap = {"runtime": {key: str(getattr(self.runtime, key)) for key in
                                           ("data_dir", "download_dir", "logs_dir", "temp_dir")},
                             "options": {"task_id": job, "attempt_dir": str(attempt.workspace),
                                         "ownership": {"attempt_id": attempt.id, "owner_id": app.state.owner.id}}}
                process = None
                try:
                    with closing(sqlite3.connect(self.runtime.db_path)) as db:
                        db.execute("BEGIN IMMEDIATE")
                        with (self.root / "retired-launch.log").open("xb") as log:
                            process = subprocess.Popen([*owner_command, "--", sys.executable, "-B", "-c", code],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, close_fds=True,
                                creationflags=0x08000000 if sys.platform == "win32" else 0)
                        process.stdin.write((json.dumps(bootstrap) + "\n").encode())
                        process.stdin.flush()
                        await asyncio.to_thread(wait_for, spawned.is_file, "The pending worker did not launch")
                        db.execute("UPDATE runtime_owners SET released_at=? WHERE id=?", (time.time(), app.state.owner.id))
                        db.execute("UPDATE download_attempts SET status='interrupted', finished_at=? WHERE id=?", ("fixture", attempt.id))
                        db.execute("UPDATE downloads SET status='queued', current_attempt_id=NULL WHERE id=?", (job,))
                        db.commit()
                    await asyncio.to_thread(process.wait, 5)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertFalse(executed.exists(), "A retired attempt received its download bootstrap")
                    self.assertEqual(self.rows("SELECT worker_identity FROM download_attempts WHERE id=?", (attempt.id,)),
                                     [{"worker_identity": None}])
                finally:
                    if process:
                        if process.poll() is None:
                            process.kill()
                            await asyncio.to_thread(process.wait, 3)
                        process.stdin.close()
                        process.stdout.close()
                    with closing(sqlite3.connect(self.runtime.db_path)) as db, db:
                        db.execute("UPDATE runtime_owners SET released_at=NULL WHERE id=?", (app.state.owner.id,))

        asyncio.run(check())


def serve(tools):
    paths, versions = check_components(tools)
    bootstrap = Bootstrap.model_validate_json(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(Path(bootstrap.data_dir), Path(bootstrap.download_dir))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        app = create_engine_app(runtime, token=bootstrap.token, port=listener.getsockname()[1], components=paths)
        server = ReadyServer(uvicorn.Config(app, log_config=None, access_log=False, loop="asyncio", lifespan="on"))
        server.component_versions = versions
        asyncio.run(server.serve(sockets=[listener]))


def publication_worker():
    bootstrap = json.loads(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(**bootstrap["runtime"])
    workspace = Path(bootstrap["options"]["attempt_dir"])
    source = workspace / "fixture.mp3"
    source.write_bytes(b"owned synthetic publication fixture")
    record = plan_publications(runtime, workspace,
                               [{"source": str(source), "path": str(runtime.download_dir / source.name), "kind": "media"}])[0]
    phase = bootstrap["options"]["phase"]

    def checkpoint(*_args, **_options):
        print(json.dumps({"type": "progress", "data": {"status": "postprocessing", "progress": 100, "phase": phase}}), flush=True)
        getattr(sys.stdin.buffer, "raw", sys.stdin.buffer).read(1)
        raise SystemExit(0)  # EOF/crash never produces a completed append or result.

    if phase == "after-link":
        # Hold the diagnostic publisher after its actual link but before the
        # completed journal append. Production/frozen recovery code is unchanged.
        with patch("backend.services.publication.append_journal", side_effect=checkpoint):
            publish_artifact(runtime, workspace, record)
    elif phase != "before-link":
        raise ValueError("Unknown publication fixture boundary")
    else:
        checkpoint()


def main():
    global ENGINE, TOOLS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--publication-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    TOOLS = args.tools.resolve(strict=True)
    ENGINE = args.engine.resolve(strict=True) if args.engine else None
    sys.addaudithook(fixture_network_guard)
    if args.publication_worker:
        publication_worker()
        return
    if args.serve:
        serve(TOOLS)
        return
    check_components(TOOLS)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ServiceRecoveryChecks))
    if args.report:
        args.report.write_text(json.dumps({"ok": result.wasSuccessful(), "cases": result.testsRun,
            "seconds": round(time.monotonic() - started, 3), "engine": str(ENGINE) if ENGINE else None,
            "skipped": [(test.id(), reason) for test, reason in result.skipped],
            "errors": [(test.id(), message) for test, message in result.errors + result.failures],
            "receipts": RECEIPTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
