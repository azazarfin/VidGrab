"""Opt-in execution-tree checks. Only local fixture processes are launched."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ENGINE = None


def alive(pid):
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class WorkerLifecycleChecks(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.engine.worker_owner"),
                             "Each attempt needs an owned process-tree execution boundary")
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-worker-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def start(self, *, complete=False, response=None):
        # The child creates a stubborn descendant resembling a stuck media tool.
        code = """
import json, os, signal, subprocess, sys, time
bootstrap = json.loads(sys.stdin.readline())
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN) if sys.platform != "win32" else None; time.sleep(60)'], close_fds=True)
with open(bootstrap['receipt'] + '.tmp', 'w', encoding='utf-8') as stream:
    json.dump({'worker': os.getpid(), 'descendant': child.pid}, stream)
os.replace(bootstrap['receipt'] + '.tmp', bootstrap['receipt'])
if bootstrap['complete']:
    if bootstrap.get('response') is not None:
        sys.stdout.write(bootstrap['response'])
        sys.stdout.flush()
    else:
        print(json.dumps({'type': 'result', 'data': {'filepath': 'literal বাংলা/fixture.mp3'}}), flush=True)
else:
    time.sleep(60)
"""
        self.receipt = self.root / "processes.json"
        owner = [str(ENGINE), "--worker-owner"] if ENGINE else [sys.executable, "-B", "-m", "backend.engine.worker_owner"]
        command = [*owner, "--", sys.executable, "-B", "-c", code]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        self.addCleanup(self.cleanup, process)
        process.stdin.write((json.dumps({"receipt": str(self.receipt), "complete": complete, "response": response}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while not self.receipt.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            time.sleep(0.01)
        self.assertTrue(self.receipt.exists(), "Worker did not enter its controlled fixture")
        self.pids = json.loads(self.receipt.read_text(encoding="utf-8"))
        return process

    def cleanup(self, process):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    def assert_stopped(self):
        for label, pid in self.pids.items():
            self.assertFalse(alive(pid), f"Owned {label} {pid} survived the execution boundary")

    def test_control_eof_stops_worker_and_stubborn_descendant(self):
        process = self.start()
        self.assertTrue(all(alive(pid) for pid in self.pids.values()))
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0, "Owner control EOF must exit without an interpreter abort")
        self.assert_stopped()

    def test_result_is_forwarded_only_after_descendant_exit(self):
        process = self.start(complete=True)
        self.assertEqual(process.wait(timeout=5), 0, "Successful ownership teardown must exit cleanly")
        messages = [json.loads(line) for line in process.stdout]
        self.assertEqual(messages[-1], {"type": "result", "data": {"filepath": "literal বাংলা/fixture.mp3"}})
        self.assert_stopped()

    def assert_rejected(self, response):
        process = self.start(complete=True, response=response)
        try:
            self.assertEqual(process.wait(timeout=5), 0, "Rejected replies still need a clean owner exit")
        except subprocess.TimeoutExpired:
            self.fail("An exited worker's inherited pipe must not keep its owner alive")
        messages = [json.loads(line) for line in process.stdout]
        self.assertEqual(messages[-1]["type"], "error", "Invalid worker messages must fail before forwarding")
        self.assertIsInstance(messages[-1].get("message"), str, "Only bounded textual errors may cross the boundary")
        self.assertFalse(any(message["type"] == "result" for message in messages))
        self.assert_stopped()

    def test_result_payload_must_include_a_literal_path(self):
        self.assert_rejected(json.dumps({"type": "result", "data": ["invalid"]}) + "\n")

    def test_error_payload_must_be_text(self):
        self.assert_rejected(json.dumps({"type": "error", "message": {"unexpected": "object"}}) + "\n")

    def test_terminal_state_cannot_arrive_as_progress(self):
        response = json.dumps({"type": "progress", "data": {"status": "complete", "progress": 100}}) + "\n"
        response += json.dumps({"type": "result", "data": {"filepath": "fixture.mp3"}}) + "\n"
        self.assert_rejected(response)

    def test_nontext_progress_state_is_refused_and_the_tree_exits(self):
        self.assert_rejected(json.dumps({"type": "progress", "data": {"status": []}}) + "\n")

    def test_malformed_json_is_refused_and_the_tree_exits(self):
        self.assert_rejected("{malformed}\n")

    def test_oversized_message_is_refused_and_the_tree_exits(self):
        self.assert_rejected(json.dumps({"type": "progress", "data": {"status": "downloading", "extra": "x" * 65536}}) + "\n")

    def test_truncated_message_is_refused_and_the_tree_exits(self):
        self.assert_rejected(json.dumps({"type": "result", "data": {"filepath": "fixture.mp3"}}))

    @unittest.skipIf(sys.platform == "win32", "Windows owner exit closes its Job Object")
    def test_owner_sigterm_stops_a_separate_worker_process_group(self):
        process = self.start()
        process.terminate()
        process.wait(timeout=5)
        self.assert_stopped()

    @unittest.skipUnless(sys.platform == "win32", "Windows kill-on-close Job Object gate")
    def test_windows_abrupt_owner_exit_kills_its_job_tree(self):
        process = self.start()
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in self.pids.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assert_stopped()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", type=Path)
    arguments, remaining = parser.parse_known_args()
    ENGINE = arguments.engine.resolve() if arguments.engine else None
    started = time.monotonic()
    program = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    if arguments.report:
        arguments.report.write_text(json.dumps({"ok": program.result.wasSuccessful(), "tests": program.result.testsRun,
            "skipped": [str(test) + ": " + reason for test, reason in program.result.skipped],
            "seconds": round(time.monotonic() - started, 3), "engine": str(ENGINE) if ENGINE else "source",
            "failures": [str(test) + ": " + trace for test, trace in program.result.failures + program.result.errors]}, indent=2), encoding="utf-8")
    raise SystemExit(0 if program.result.wasSuccessful() else 1)
