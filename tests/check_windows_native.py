"""Opt-in source/frozen Windows lifecycle checks in the selected disposable VM.

Run with --bundle and --report. Only temporary state and generated loopback WAV
media are selected. No browser registration or real runtime is installed.
"""
import argparse
import concurrent.futures
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.native_control_checks import NativeControlChecks


def frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(payload)) + payload


def unframe(data):
    if len(data) < 4:
        raise AssertionError("No complete native response")
    size = struct.unpack("=I", data[:4])[0]
    if not 0 < size <= 65536 or len(data) != size + 4:
        raise AssertionError("Stdout must contain exactly one bounded native frame")
    return json.loads(data[4:])


def pipe_available(stream):
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    kernel.PeekNamedPipe.restype = wintypes.BOOL
    available = wintypes.DWORD()
    if not kernel.PeekNamedPipe(msvcrt.get_osfhandle(stream.fileno()), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == 109:  # ERROR_BROKEN_PIPE: every writer closed its handle.
            return None
        raise ctypes.WinError(error)
    return available.value


class ProcessHandle:
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = self.kernel.OpenProcess(0x00100001, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, seconds):
        return self.kernel.WaitForSingleObject(self.handle, int(seconds * 1000)) == 0

    def kill(self):
        if not self.wait(0) and not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsNativeChecks(NativeControlChecks, unittest.TestCase):
    bundle = None
    native_override = None
    evidence = None

    def test_native_helpers_do_not_require_a_visual_cpp_installation(self):
        import pefile  # Locked Windows PyInstaller development dependency.
        imports = {}
        for name in ("vidgrab-supervisor.exe", "vidgrab-native-host.exe"):
            image = pefile.PE(str(self.bundle / name), fast_load=True)
            try:
                image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
                dependencies = sorted(entry.dll.decode("ascii").lower() for entry in image.DIRECTORY_ENTRY_IMPORT)
                forbidden = [dll for dll in dependencies if dll.startswith(("vcruntime", "msvcp", "concrt"))]
                self.assertEqual(forbidden, [], f"{name} must carry its C++ runtime through static linkage")
                imports[name] = dependencies
            finally:
                image.close()
        self.evidence.append({"check": self.id(), "imports": imports})

    def setUp(self):
        self.assertEqual(os.name, "nt", "Run on the Windows kernel")
        self.assertEqual(os.environ.get("COMPUTERNAME", "").upper(), "DESKTOP-OSOIBNA", "Only the selected disposable VM is authorized")
        self.temp = tempfile.TemporaryDirectory(prefix="vidgrab native বাংলা ")
        self.root = Path(self.temp.name)
        self.owner = None
        self.handles = {}
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop_owner)
        self.host = self.root / "vidgrab-native-host.exe"
        shutil.copy2(self.native_override or self.bundle / "vidgrab-native-host.exe", self.host)
        self.config = {
            "app": str(self.bundle / "vidgrab-supervisor.exe"),
            "engine": str(self.bundle / "engine" / "vidgrab-engine.exe"),
            "data_dir": str(self.root / "state"), "download_dir": str(self.root / "media"),
            "chrome_origin": json.loads((self.bundle / "host-config.json").read_text())["chrome_origin"],
            "firefox_id": "vidgrab@azaz.local",
        }
        self.config_path = self.root / "host-config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.firefox_manifest = self.root / "firefox-native-manifest.json"
        self.firefox_manifest.write_text(json.dumps({
            "name": "com.vidgrab.companion", "path": str(self.host), "type": "stdio",
            "allowed_extensions": [self.config["firefox_id"]],
        }), encoding="utf-8")
        self.environment = {**os.environ, "PATH": "", "PYTHONPATH": "", "PYTHONHOME": "", "PYTHONUTF8": "1"}

    def invoke(self, op="status", params=None, origin=None, raw=None):
        request = {"spike_version": 1, "id": "বাংলা-request", "op": op, "params": params or {}}
        caller = origin or self.config["chrome_origin"]
        browser_arguments = [str(self.firefox_manifest), caller] if caller == self.config["firefox_id"] else [caller]
        native = subprocess.Popen([str(self.host), "--config", str(self.config_path), *browser_arguments],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self.environment, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            native.stdin.write(frame(request) if raw is None else raw)
            native.stdin.close()
            data = bytearray()
            expected = 4
            deadline = time.monotonic() + 20
            while len(data) < expected:
                available = pipe_available(native.stdout)
                self.assertIsNotNone(available, "Native stdout ended before the response")
                if available:
                    data.extend(os.read(native.stdout.fileno(), min(available, expected - len(data))))
                    if len(data) == 4 and expected == 4:
                        length = struct.unpack("=I", data)[0]
                        self.assertTrue(0 < length <= 65536)
                        expected += length
                else:
                    self.assertLess(time.monotonic(), deadline, "Native reply exceeded its deadline")
                    time.sleep(0.005)
            reply = unframe(data)
            if reply.get("data", {}).get("supervisor_pid"):
                pid = reply["data"]["supervisor_pid"]
                if pid not in self.handles:
                    self.handles[pid] = ProcessHandle(pid)
            self.assertEqual(native.wait(timeout=2), 0)
            self.assertIsNone(pipe_available(native.stdout),
                              "The app inherited the exited native host's output pipe")
            stderr = bytearray()
            while (available := pipe_available(native.stderr)):
                stderr.extend(os.read(native.stderr.fileno(), available))
            self.assertIsNone(available, "The app inherited the exited native host's error pipe")
            self.assertNotIn(b"Bearer ", stderr)
        finally:
            if native.poll() is None:
                native.kill()
                native.wait(timeout=3)
            for stream in (native.stdin, native.stdout, native.stderr):
                stream.close()
        return reply

    def local(self, op):
        result = subprocess.run([self.config["app"], "--config", str(self.config_path), "--request", op],
                                env=self.environment, capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def ready(self):
        deadline = time.monotonic() + 25
        last = None
        while time.monotonic() < deadline:
            last = self.invoke()
            if last.get("data", {}).get("state") == "ready":
                return last
            if not last.get("ok"):
                self.fail(f"Native activation failed: {last}")
            time.sleep(0.1)
        self.fail(f"Readiness deadline: {last}")

    def stop_owner(self):
        error = None
        if self.handles:
            try:
                self.local("quit")
                for handle in self.handles.values():
                    self.assertTrue(handle.wait(8), "Quit must stop the selected owner")
            except Exception as exc:
                error = exc
        try:
            if self.owner is not None and self.owner.poll() is None:
                self.owner.terminate()
                self.owner.wait(timeout=8)
            for handle in self.handles.values():
                if not handle.wait(0):
                    handle.kill()
                    self.assertTrue(handle.wait(8), "Owned process cleanup failed")
                handle.close()
        finally:
            self.handles.clear()
        if error:
            raise error

    def test_local_diagnostic_status_and_quit_do_not_start_another_owner(self):
        self.owner = subprocess.Popen([self.config["app"], "--config", str(self.config_path)], env=self.environment,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        # Ownership exists before engine readiness; this is only an observation.
        deadline = time.monotonic() + 15
        while not (self.root / "state" / "ipc" / "owner.lock").exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        reply = self.local("status")
        self.assertEqual(reply["data"]["supervisor_pid"], self.owner.pid)
        self.local("quit")
        self.assertEqual(self.owner.wait(timeout=8), 0)

    def test_rejected_origins_frames_and_operations_never_activate(self):
        self.assertEqual(self.invoke(origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/")["error"]["code"], "ORIGIN_REJECTED")
        for raw in (struct.pack("=I", 65537), struct.pack("=I", 3) + b"\xffxx", struct.pack("=I", 20) + b"{}"):
            self.assertFalse(self.invoke(raw=raw)["ok"])
        self.assertEqual(self.invoke("exec")["error"]["code"], "UNSUPPORTED_OPERATION")
        self.assertFalse((self.root / "state").exists())

    def test_activation_leaves_no_native_stdio_handle_in_the_running_app(self):
        reply = self.invoke()
        self.assertTrue(reply["ok"], reply)
        owner = self.handles[reply["data"]["supervisor_pid"]]
        self.assertFalse(owner.wait(0), "Native-host exit must retain the app")

    def test_engine_failure_recovers_under_the_same_owner_and_stops_at_four_generations(self):
        ready = self.ready()["data"]
        owner = ready["supervisor_pid"]
        for generation in range(1, 5):
            self.assertEqual(ready["engine_generation"], generation)
            engine = ProcessHandle(ready["engine_pid"])
            try:
                engine.kill()
                self.assertTrue(engine.wait(5))
            finally:
                engine.close()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = self.invoke()["data"]
                self.assertEqual(status["supervisor_pid"], owner)
                if generation == 4 and status["state"] == "failed":
                    self.assertEqual(status["engine_generation"], 4)
                    return
                if status["state"] == "ready" and status["engine_generation"] > generation:
                    ready = status
                    break
                time.sleep(0.1)
            else:
                self.fail(f"Bounded engine recovery failed at generation {generation}")
        self.fail("The owner exceeded its four-generation recovery limit")

    def test_fixture_finishes_after_native_hosts_exit_and_history_survives_owner_reopen(self):
        ready = self.ready()
        first_pid = ready["data"]["supervisor_pid"]
        self.assertEqual(self.invoke("activate")["data"]["supervisor_pid"], first_pid)
        wav = io.BytesIO()
        with wave.open(wav, "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(22050)
            audio.writeframes(b"\x00\x00" * 44100)
        payload = wav.getvalue()
        gate = threading.Event()
        requested = threading.Event()
        class Fixture(BaseHTTPRequestHandler):
            def do_GET(inner):
                if inner.path != "/fixture.wav":
                    inner.send_error(404); return
                requested.set()
                if not gate.wait(15):
                    inner.send_error(408); return
                inner.send_response(200); inner.send_header("Content-Type", "audio/wav")
                inner.send_header("Content-Length", str(len(payload))); inner.end_headers(); inner.wfile.write(payload)
            def log_message(inner, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            accepted = self.invoke("fixture.download", {"url": f"http://127.0.0.1:{server.server_port}/fixture.wav"})
            self.assertTrue(accepted["ok"], accepted)
            task = accepted["data"]["task_id"]
            self.assertTrue(requested.wait(5), "The frozen engine must reach the loopback fixture")
            self.assertFalse(self.handles[first_pid].wait(0), "Native-host exit must retain the owner")
            gate.set(); deadline = time.monotonic() + 20; completed = None
            while time.monotonic() < deadline:
                status = self.invoke()
                completed = next((job for job in status.get("data", {}).get("jobs", []) if job["id"] == task), None)
                if completed and completed["status"] in ("complete", "error"):
                    break
                time.sleep(0.1)
            self.assertIsNotNone(completed); self.assertEqual(completed["status"], "complete", completed)
            media = list((self.root / "media").rglob("*.mp3")); self.assertEqual(len(media), 1)
            digest = hashlib.sha256(media[0].read_bytes()).hexdigest()
            self.local("quit"); self.assertTrue(self.handles[first_pid].wait(8))
            reopened = self.ready(); self.assertNotEqual(reopened["data"]["supervisor_pid"], first_pid)
            self.assertEqual([job["id"] for job in reopened["data"]["jobs"]], [task])
            self.assertEqual(hashlib.sha256(media[0].read_bytes()).hexdigest(), digest)
            self.evidence.append({"check": self.id(), "media_sha256": digest, "path": str(media[0]), "first_owner": first_pid,
                                  "second_owner": reopened["data"]["supervisor_pid"], "components": reopened["data"]["components"]})
        finally:
            gate.set(); server.shutdown(); server.server_close(); thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--test", help="Optional exact test method")
    parser.add_argument("--native-host", type=Path, help="Optional earlier bridge for the bounded inheritance regression")
    args = parser.parse_args()
    WindowsNativeChecks.bundle = args.bundle.resolve()
    WindowsNativeChecks.native_override = args.native_host.resolve() if args.native_host else None
    WindowsNativeChecks.evidence = []
    suite = unittest.TestSuite([WindowsNativeChecks(args.test)]) if args.test else unittest.defaultTestLoader.loadTestsFromTestCase(WindowsNativeChecks)
    start = time.monotonic(); result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"result": "passed" if result.wasSuccessful() else "failed", "tests": result.testsRun,
              "seconds": round(time.monotonic() - start, 3), "evidence": WindowsNativeChecks.evidence,
              "failures": [str(error) for _, error in result.failures + result.errors]}
    args.report.parent.mkdir(parents=True, exist_ok=True); args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
