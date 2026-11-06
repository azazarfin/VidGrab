"""Opt-in Linux fixture/driver helpers; importing this module starts nothing."""

import base64
from contextlib import contextmanager
import ctypes
import io
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
import wave


def isolate():
    """Run the entire check with private home, network, PID and D-Bus state."""
    if os.environ.get("VIDGRAB_VALIDATION_ISOLATED") == "1":
        return
    with tempfile.TemporaryDirectory(prefix="vidgrab-linux-validation-") as scratch:
        root = Path(scratch)
        home = root / "home"
        for directory in (home, home / ".config", home / ".cache", root / "run"):
            directory.mkdir(mode=0o700)
        empty_hosts = root / "empty-native-hosts"
        empty_hosts.mkdir()
        bus = root / "dbus.conf"
        bus.write_text('''<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>
<auth>EXTERNAL</auth><policy context="default"><allow own="*"/>
<allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>''')
        command = [
            "bwrap", "--unshare-net", "--unshare-pid", "--die-with-parent",
            "--ro-bind", "/", "/", "--bind", "/tmp", "/tmp",
            "--bind", str(home), str(Path.home()), "--tmpfs", "/tmp/.X11-unix",
            "--dev", "/dev", "--proc", "/proc", "--unsetenv", "WAYLAND_DISPLAY",
            "--unsetenv", "DBUS_SESSION_BUS_ADDRESS",
        ]
        for directory in ("/etc/opt/chrome/native-messaging-hosts", "/etc/chromium/native-messaging-hosts",
                          "/usr/lib/mozilla/native-messaging-hosts", "/usr/lib64/mozilla/native-messaging-hosts"):
            if Path(directory).is_dir():
                command.extend(["--ro-bind", str(empty_hosts), directory])
        environment = {
            "VIDGRAB_VALIDATION_ISOLATED": "1", "VIDGRAB_VALIDATION_HOME": str(home),
            "XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "CHROME_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
            "GDK_BACKEND": "x11", "GIO_USE_VFS": "local", "NO_AT_BRIDGE": "1",
            "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe",
            "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/50_mesa.json",
            "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
        }
        for name, value in environment.items():
            command.extend(["--setenv", name, value])
        command.extend(["/usr/bin/dbus-run-session", f"--config-file={bus}", "--",
                        sys.executable, "-B", str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as error:
            for output in (error.stdout, error.stderr):
                if output:
                    print(output.decode(errors="replace") if isinstance(output, bytes) else output, flush=True)
            raise
        print(result.stdout, end="", flush=True)
        if result.returncode:
            print(result.stderr[-12000:], file=sys.stderr, end="", flush=True)
        raise SystemExit(result.returncode)


def wait_for(predicate, *, timeout=20, label="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (ConnectionError, FileNotFoundError, socket.timeout):
            pass
        time.sleep(0.1)
    raise AssertionError(f"Deadline exceeded waiting for {label}: {last}")


def local_call(config, operation="status"):
    request = {"spike_version": 1, "id": "native-validation", "op": operation, "params": {}}
    payload = json.dumps(request).encode()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(Path(config["data_dir"]) / "ipc" / "control.sock"))
        connection.sendall(struct.pack("=I", len(payload)) + payload)
        def read(size):
            data = bytearray()
            while len(data) < size:
                block = connection.recv(size - len(data))
                if not block:
                    raise ConnectionError("Truncated IPC response")
                data.extend(block)
            return bytes(data)
        size = struct.unpack("=I", read(4))[0]
        assert 0 < size <= 65536, "IPC response exceeds the spike limit"
        response = json.loads(read(size))
        assert response["ok"], response
        return response["data"]


def stop_owner(config):
    endpoint = Path(config["data_dir"]) / "ipc" / "control.sock"
    if not endpoint.exists():
        return
    snapshot = local_call(config, "quit")
    pids = [snapshot.get("supervisor_pid"), snapshot.get("engine_pid")]
    wait_for(lambda: not endpoint.exists() and not any(pid and Path(f"/proc/{pid}").exists() for pid in pids),
             timeout=8, label="owned desktop and engine exit")


@contextmanager
def virtual_display(binary):
    # Repeated keymap warnings can fill an unread pipe and stall Xvfb's clients.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(binary), "-displayfd", "1", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            assert select.select([process.stdout], [], [], 10)[0], "Xvfb readiness deadline"
            number = process.stdout.readline().decode().strip()
            if not number:
                errors.seek(0)
                raise AssertionError(errors.read().decode(errors="replace"))
            yield {**os.environ, "DISPLAY": ":" + number}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()


class WebDriver:
    def __init__(self, binary, environment, log, *, arguments=()):
        with socket.socket() as connection:
            connection.bind(("127.0.0.1", 0))
            self.port = connection.getsockname()[1]
        self.log = Path(log).open("wb")
        self.process = subprocess.Popen([str(binary), f"--port={self.port}", *arguments], env=environment,
                                        stdout=self.log, stderr=self.log)
        self.session = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            def ready():
                if self.process.poll() is not None:
                    self.log.flush()
                    raise RuntimeError(Path(log).read_text(errors="replace"))
                return self.request("/status")
            wait_for(ready, label="WebDriver readiness")
        except BaseException:
            self.close()
            raise

    def request(self, path, body=None, *, method=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         headers={"Content-Type": "application/json"}, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)["value"]
        except urllib.error.URLError as error:
            if isinstance(error, urllib.error.HTTPError):
                body = error.read().decode()
                detail = json.loads(body).get("value", {})
                if detail.get("error") == "no such window" and "before the unload event" in detail.get("message", ""):
                    raise ConnectionError(detail["message"]) from error
                raise RuntimeError(body) from error
            raise ConnectionError(str(error)) from error

    def start(self, capabilities):
        response = self.request("/session", {"capabilities": {"alwaysMatch": capabilities}})
        self.session = response["sessionId"]
        return response["capabilities"]

    def call(self, path, body=None, *, method=None):
        return self.request(f"/session/{self.session}{path}", body, method=method)

    def script(self, script, *arguments, asynchronous=False):
        return self.call("/execute/async" if asynchronous else "/execute/sync",
                         {"script": script, "args": list(arguments)})

    def element(self, selector):
        return self.call("/element", {"using": "css selector", "value": selector})["element-6066-11e4-a52e-4f735466cecf"]

    def click(self, selector):
        self.call(f"/element/{self.element(selector)}/click", {})

    def fill(self, selector, value):
        element = self.element(selector)
        self.call(f"/element/{element}/clear", {})
        self.call(f"/element/{element}/value", {"text": value})

    def screenshot(self, destination):
        Path(destination).write_bytes(base64.b64decode(self.call("/screenshot")))

    def close(self):
        if self.session:
            try:
                self.call("", method="DELETE")
            except (ConnectionError, RuntimeError):
                pass
            self.session = None
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()


@contextmanager
def fixture_server(transfer_gate=None):
    memory = io.BytesIO()
    with wave.open(memory, "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 131072)
    media = memory.getvalue()
    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/fixture.wav":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(media)))
            self.end_headers()
            for offset in range(0, len(media), 4096):
                try:
                    self.wfile.write(media[offset:offset + 4096])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if transfer_gate is not None:
                    if offset == 0 and not transfer_gate.wait(timeout=30):
                        return
                else:
                    time.sleep(0.08)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture.wav"
    finally:
        if transfer_gate is not None:
            transfer_gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def close_window(title):
    """Send the same X11 protocol event as a window manager's Close action."""
    library = ctypes.CDLL("libX11.so.6")
    display_type = ctypes.c_void_p
    window_type = ctypes.c_ulong
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XOpenDisplay.restype = display_type
    library.XDefaultRootWindow.argtypes = [display_type]
    library.XDefaultRootWindow.restype = window_type
    library.XQueryTree.argtypes = [display_type, window_type, ctypes.POINTER(window_type),
                                  ctypes.POINTER(window_type), ctypes.POINTER(ctypes.POINTER(window_type)), ctypes.POINTER(ctypes.c_uint)]
    library.XFetchName.argtypes = [display_type, window_type, ctypes.POINTER(ctypes.c_void_p)]
    library.XInternAtom.argtypes = [display_type, ctypes.c_char_p, ctypes.c_int]
    library.XInternAtom.restype = window_type
    library.XFree.argtypes = [ctypes.c_void_p]
    library.XFlush.argtypes = [display_type]
    library.XCloseDisplay.argtypes = [display_type]
    display = library.XOpenDisplay(None)
    assert display, "Cannot connect to isolated X11 display"
    try:
        def find(window):
            name = ctypes.c_void_p()
            if library.XFetchName(display, window, ctypes.byref(name)) and name.value:
                text = ctypes.string_at(name).decode(errors="replace")
                library.XFree(name)
                if text == title:
                    return window
            root, parent = window_type(), window_type()
            children = ctypes.POINTER(window_type)()
            count = ctypes.c_uint()
            if library.XQueryTree(display, window, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)):
                values = list(children[:count.value])
                if children:
                    library.XFree(children)
                for child in values:
                    result = find(child)
                    if result:
                        return result
            return None
        window = find(library.XDefaultRootWindow(display))
        assert window, f"No native window titled {title}"
        class Data(ctypes.Union):
            _fields_ = [("longs", ctypes.c_long * 5)]
        class ClientMessage(ctypes.Structure):
            _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                        ("display", display_type), ("window", window_type), ("message_type", window_type),
                        ("format", ctypes.c_int), ("data", Data)]
        class Event(ctypes.Union):
            _fields_ = [("client", ClientMessage), ("padding", ctypes.c_long * 24)]
        event = Event()
        event.client.type = 33
        event.client.display = display
        event.client.window = window
        event.client.message_type = library.XInternAtom(display, b"WM_PROTOCOLS", False)
        event.client.format = 32
        event.client.data.longs[0] = library.XInternAtom(display, b"WM_DELETE_WINDOW", False)
        library.XSendEvent.argtypes = [display_type, window_type, ctypes.c_int, ctypes.c_long, ctypes.POINTER(Event)]
        library.XSendEvent(display, window, False, 0, ctypes.byref(event))
        library.XFlush(display)
    finally:
        library.XCloseDisplay(display)
