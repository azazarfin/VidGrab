"""Opt-in execution-tree checks. Only local fixture processes are launched."""

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

    def start(self, *, complete=False):
        # The child creates a stubborn descendant resembling a stuck media tool.
        code = """
import json, os, signal, subprocess, sys, time
bootstrap = json.loads(sys.stdin.readline())
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN) if sys.platform != "win32" else None; time.sleep(60)'], close_fds=True)
with open(bootstrap['receipt'], 'w', encoding='utf-8') as stream:
    json.dump({'worker': os.getpid(), 'descendant': child.pid}, stream)
if bootstrap['complete']:
    print(json.dumps({'type': 'result', 'data': {'filepath': 'literal বাংলা/fixture.mp3'}}), flush=True)
else:
    time.sleep(60)
"""
        self.receipt = self.root / "processes.json"
        command = [sys.executable, "-B", "-m", "backend.engine.worker_owner", "--", sys.executable, "-B", "-c", code]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        self.addCleanup(self.cleanup, process)
        process.stdin.write((json.dumps({"receipt": str(self.receipt), "complete": complete}) + "\n").encode())
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
        process.wait(timeout=5)
        self.assert_stopped()

    def test_result_is_forwarded_only_after_descendant_exit(self):
        process = self.start(complete=True)
        messages = [json.loads(line) for line in process.stdout]
        process.wait(timeout=5)
        self.assertEqual(messages[-1], {"type": "result", "data": {"filepath": "literal বাংলা/fixture.mp3"}})
        self.assert_stopped()

    @unittest.skipIf(sys.platform == "win32", "Windows owner exit closes its Job Object")
    def test_owner_sigterm_stops_a_separate_worker_process_group(self):
        process = self.start()
        process.terminate()
        process.wait(timeout=5)
        self.assert_stopped()


if __name__ == "__main__":
    unittest.main(verbosity=2)
