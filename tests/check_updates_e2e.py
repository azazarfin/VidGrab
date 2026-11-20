#!/usr/bin/env python3
"""End-to-end loopback signed-update, backup retention, idle-gating, and rollback verification.

Exercises real compiled `vidgrab-supervisor` + real `backend.engine` (or explicit built binaries)
against a local loopback HTTP update server with ephemeral Ed25519 keys.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util

def _load_release_manifest():
    spec = importlib.util.spec_from_file_location("vidgrab_release_manifest", ROOT / "packaging" / "release_manifest.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_rm = _load_release_manifest()
REQUIRED_NOTICES = _rm.REQUIRED_NOTICES
build_engine_tar_bundle = _rm.build_engine_tar_bundle
create_signed_manifest = _rm.create_signed_manifest
generate_keypair = _rm.generate_keypair


def frame(value: dict) -> bytes:
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(payload)) + payload


def unframe(data: bytes) -> dict:
    if len(data) < 4:
        raise AssertionError("No complete IPC response frame")
    size = struct.unpack("=I", data[:4])[0]
    if not 0 < size <= 65536 or len(data) != size + 4:
        raise AssertionError(f"Expected single bounded frame, got {len(data)} bytes (header={size})")
    return json.loads(data[4:].decode("utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_notices(notices_dir: Path) -> None:
    notices_dir.mkdir(parents=True, exist_ok=True)
    candidates = {
        "LICENSE": ROOT / "LICENSE",
        "THIRD_PARTY.md": ROOT / "packaging" / "THIRD_PARTY.md",
        "FONTS_LICENSE.txt": ROOT / "extension" / "fonts" / "LICENSE.txt",
        "GPLv3.ffmpeg.txt": ROOT / "desktop" / "src-tauri" / "resources" / "notices" / "GPLv3.ffmpeg.txt",
    }
    for name in REQUIRED_NOTICES:
        src = candidates[name]
        if src.is_file():
            shutil.copy2(src, notices_dir / name)
        elif name == "GPLv3.ffmpeg.txt":
            (notices_dir / name).write_text("GNU GENERAL PUBLIC LICENSE Version 3 (test fixture notice)\n", encoding="utf-8")
        else:
            raise FileNotFoundError(f"Missing source notice file: {src}")


def _write_linux_engine_wrapper(path: Path, version_tag: str, log_file: Path, *, mode: str = "normal") -> None:
    py = sys.executable
    path.write_text(
        f"""#!{py}
import asyncio, json, logging, os, socket, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from backend.engine import __main__ as em, create_engine_app
from backend.runtime import RuntimeConfig

with open({str(log_file)!r}, "a", encoding="utf-8") as lf:
    lf.write(f"{version_tag}:{mode}:{{' '.join(sys.argv[1:])}}\\n")

mode = {mode!r}
args = sys.argv[1:]
if mode == "fail_self_check":
    sys.exit(2)
if "--fetch-update" in args or "--unpack-update" in args:
    em.main()
    sys.exit(0)
if "--self-check" in args:
    print(json.dumps({{"spike_version": 1, "components": {{"bundle": {version_tag!r}}}}}, sort_keys=True), flush=True)
    sys.exit(0)
if mode == "fail_startup":
    print("Authorization: Bearer supersecret_engine_token", file=sys.stderr, flush=True)
    sys.exit(1)

bootstrap = em.Bootstrap.model_validate_json(sys.stdin.buffer.readline(16385))
runtime = RuntimeConfig(Path(bootstrap.data_dir), Path(bootstrap.download_dir))
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    versions = {{"python": sys.version.split()[0], "yt_dlp": "test", "ejs": "0.8.0", "bundle": {version_tag!r}}}
    app = create_engine_app(runtime, token=bootstrap.token, port=listener.getsockname()[1],
                            components={{"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "deno": "deno"}},
                            component_versions=versions, media_mode="ordinary")
    server = em.ReadyServer(em.uvicorn.Config(app, host="127.0.0.1", log_config=None, access_log=False,
                                              loop="asyncio", lifespan="on", timeout_graceful_shutdown=1))
    server.component_versions = versions
    asyncio.run(server.serve(sockets=[listener]))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _windows_pipe_name(data_dir: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buf, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        user = TOKEN_USER.from_buffer(buf)
        sid_ptr = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(sid_ptr)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = sid_ptr.value
        finally:
            kernel32.LocalFree(sid_ptr)
    finally:
        kernel32.CloseHandle(token)

    resolved = str(data_dir.resolve()).lstrip("\\\\?\\").replace("/", "\\").lower()
    digest = hashlib.sha256(f"{sid}\0{resolved}".encode("utf-8")).hexdigest()
    return rf"\\.\pipe\vidgrab-p02-{digest}"


class UpdateServer:
    def __init__(self):
        self.routes: dict[str, tuple[int, str, bytes]] = {}
        self.requests: list[str] = []
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server_ref.requests.append(self.path)
                entry = server_ref.routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "9")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                code, ctype, body = entry
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class SignedUpdatesE2ETests(unittest.TestCase):
    supervisor_path: Path

    @classmethod
    def setUpClass(cls):
        candidate = getattr(cls, "_cli_supervisor", None)
        if candidate is None:
            exe = "vidgrab-supervisor.exe" if os.name == "nt" else "vidgrab-supervisor"
            candidate = ROOT / "target" / "release" / exe
            if not candidate.is_file():
                subprocess.run(
                    ["cargo", "build", "--offline", "--locked", "--release", "-p", "vidgrab-supervisor"],
                    cwd=ROOT,
                    check=True,
                )
        if not candidate.is_file():
            raise FileNotFoundError(f"Missing compiled vidgrab-supervisor binary: {candidate}")
        cls.supervisor_path = candidate.resolve()

    def setUp(self):
        if os.name == "nt":
            self.tmp_obj = tempfile.TemporaryDirectory(prefix="vg_upd_")
        else:
            self.tmp_obj = tempfile.TemporaryDirectory(dir="/tmp", prefix="vg_upd_")
        self.root = Path(self.tmp_obj.name)
        self.data_dir = self.root / "state"
        self.download_dir = self.root / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.invocations_log = self.root / "engine-invocations.log"
        self.seed_hex, self.pub_hex = generate_keypair()
        self.platform = "windows-x64" if os.name == "nt" else "linux-x86_64"

        self.builtin_dir = self.root / "builtin_engine"
        self.builtin_dir.mkdir(parents=True, exist_ok=True)
        _copy_notices(self.builtin_dir / "_internal" / "notices")
        self.builtin_engine = self.builtin_dir / ("vidgrab-engine.exe" if os.name == "nt" else "vidgrab-engine")
        if os.name == "nt":
            raise unittest.SkipTest("Windows signed-update E2E requires explicit --supervisor + --engine binary")
        _write_linux_engine_wrapper(self.builtin_engine, "0.1.0", self.invocations_log)

        self.config_path = self.root / "runtime.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "app": str(self.root / "missing-app"),
                    "engine": str(self.builtin_engine),
                    "data_dir": str(self.data_dir),
                    "download_dir": str(self.download_dir),
                    "chrome_origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
                    "firefox_id": "vidgrab@example.invalid",
                }
            ),
            encoding="utf-8",
        )

        self.addCleanup(self.tmp_obj.cleanup)
        self.server = UpdateServer()
        self.addCleanup(self.server.close)
        self.addCleanup(self._stop_supervisor)

        updates_dir = self.data_dir / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        (updates_dir / "config.json").write_text(
            json.dumps(
                {
                    "manifest_url": self.server.url("/manifest.json"),
                    "channel": "stable",
                    "test_public_key_hex": self.pub_hex,
                }
            ),
            encoding="utf-8",
        )

        env = {**os.environ, "VIDGRAB_ALLOW_TEST_UPDATE_KEY": "1"}
        self.proc = subprocess.Popen(
            [str(self.supervisor_path), "--config", str(self.config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._wait_ready()

    def _ipc_call(self, op: str, params: dict | None = None, req_id: str | None = None) -> dict:
        rid = req_id or f"req-{uuid4().hex[:8]}"
        req = {"spike_version": 1, "id": rid, "op": op, "params": params or {}}
        if os.name == "nt":
            pipe = _windows_pipe_name(self.data_dir)
            with open(pipe, "r+b", buffering=0) as f:
                f.write(frame(req))
                hdr = f.read(4)
                size = struct.unpack("=I", hdr)[0]
                return unframe(hdr + f.read(size))
        sock_path = self.data_dir / "ipc" / "control.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(45)
            sock.connect(str(sock_path))
            sock.sendall(frame(req))
            raw = b""
            while len(raw) < 4:
                chunk = sock.recv(4 - len(raw))
                if not chunk:
                    raise AssertionError("Truncated IPC header")
                raw += chunk
            size = struct.unpack("=I", raw)[0]
            while len(raw) < 4 + size:
                chunk = sock.recv(4 + size - len(raw))
                if not chunk:
                    raise AssertionError("Truncated IPC payload")
                raw += chunk
            return unframe(raw)

    def _v1(self, operation: str, params: dict | None = None, *, mutation: bool = False) -> dict:
        rid = f"v1-{uuid4().hex[:8]}"
        body = {
            "protocol_version": 1,
            "request_id": rid,
            "operation": operation,
            "params": params or {},
        }
        if mutation:
            body["idempotency_key"] = f"key-{uuid4().hex[:12]}"
        res = self._ipc_call("v1.request", body, req_id=rid)
        self.assertTrue(res["ok"], f"v1 IPC call {operation} failed: {res}")
        return res["data"]

    def _wait_ready(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        sock_path = self.data_dir / "ipc" / "control.sock"
        while time.monotonic() < deadline:
            if os.name == "nt" or sock_path.exists():
                try:
                    res = self._ipc_call("status")
                    if res.get("ok") and res.get("data", {}).get("state") == "ready":
                        return res["data"]
                except OSError:
                    pass
            time.sleep(0.1)
        raise AssertionError("Supervisor did not reach ready state before deadline")

    def _stop_supervisor(self):
        if getattr(self, "proc", None) and self.proc.poll() is None:
            try:
                self._ipc_call("quit")
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def _build_bundle_and_manifest(
        self,
        version: str,
        *,
        compress: bool = False,
        mode: str = "normal",
        protocol_min: int = 1,
        protocol_max: int = 1,
        notes_override: str | None = None,
        drop_notice: bool = False,
    ) -> tuple[str, bytes]:
        uid = uuid4().hex[:6]
        stage_dir = self.root / f"stage_{version}_{uid}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        notices_dir = stage_dir / "_internal" / "notices"
        _copy_notices(notices_dir)
        if drop_notice:
            (notices_dir / "FONTS_LICENSE.txt").unlink()
        exe = stage_dir / "vidgrab-engine"
        _write_linux_engine_wrapper(exe, version, self.invocations_log, mode=mode)

        ext = ".tar.gz" if compress else ".tar"
        archive_path = self.root / f"vidgrab-{version}-{uid}{ext}"
        if drop_notice:
            # Build tar manually without notices_dir check to test supervisor staging rejection
            import tarfile
            with tarfile.open(archive_path, "w:gz" if compress else "w") as tf:
                for p in sorted(stage_dir.rglob("*")):
                    if p.is_file():
                        tf.add(p, arcname=(Path("engine") / p.relative_to(stage_dir)).as_posix())
            notices_sha = "0" * 64
        else:
            _, _, notices_sha = build_engine_tar_bundle(stage_dir, archive_path, compress=compress)

        bundle_path = f"/bundles/vidgrab-{version}-{uid}{ext}"
        self.server.routes[bundle_path] = (200, "application/octet-stream", archive_path.read_bytes())
        manifest = create_signed_manifest(
            version=version,
            channel="stable",
            platform=self.platform,
            url=self.server.url(bundle_path),
            archive_path=archive_path,
            notices_sha256=notices_sha,
            private_seed_hex=self.seed_hex,
            protocol_min=protocol_min,
            protocol_max=protocol_max,
            notes=notes_override,
        )
        manifest_path = f"/manifest-{version}-{uid}.json"
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        self.server.routes[manifest_path] = (200, "application/json", manifest_bytes)
        return self.server.url(manifest_path), manifest_bytes

    def _assert_no_failed_staging_trees(self):
        staging = self.data_dir / "updates" / "staging"
        bundles = self.data_dir / "updates" / "bundles"
        self.assertEqual(list(staging.iterdir()) if staging.exists() else [], [])
        self.assertEqual(list(bundles.iterdir()) if bundles.exists() else [], [])

    def test_failed_staging_removes_unpack_and_bundle_trees(self):
        for options in ({"drop_notice": True}, {"mode": "fail_self_check"}):
            with self.subTest(options=options):
                url, _ = self._build_bundle_and_manifest("0.2.0", **options)
                result = self._ipc_call("update.apply", {"url": url})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "UPDATE_INVALID")
                self._assert_no_failed_staging_trees()

        # Refuse a partial extraction after creating its destination tree.
        url, raw = self._build_bundle_and_manifest("0.2.0")
        manifest = json.loads(raw)
        from urllib.parse import urlsplit
        bundle_path = urlsplit(manifest["url"]).path
        archive = self.server.routes[bundle_path][2][:600]
        manifest.update(size=len(archive), sha256=hashlib.sha256(archive).hexdigest())
        manifest["signature"] = _rm.sign_ed25519(
            bytes.fromhex(self.seed_hex), _rm.canonical_signing_bytes(manifest)
        ).hex()
        self.server.routes[bundle_path] = (200, "application/octet-stream", archive)
        self.server.routes[urlsplit(url).path] = (200, "application/json", json.dumps(manifest).encode())
        result = self._ipc_call("update.apply", {"url": url})
        self.assertFalse(result["ok"])
        self._assert_no_failed_staging_trees()

        # Make the atomic pending-state publication fail after a valid self-check.
        url, _ = self._build_bundle_and_manifest("0.2.0")
        state_file = self.data_dir / "updates" / "state.json"
        state_file.mkdir()
        try:
            result = self._ipc_call("update.apply", {"url": url})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "UPDATE_INVALID")
            self._assert_no_failed_staging_trees()
        finally:
            state_file.rmdir()
        self.assertEqual(self._ipc_call("status")["data"]["state"], "ready")

    def test_failed_activation_restores_ready_before_apply_returns(self):
        url, _ = self._build_bundle_and_manifest("0.2.0")
        self.assertTrue(self._ipc_call("update.apply", {"url": url})["ok"])
        bad_url, _ = self._build_bundle_and_manifest("0.2.1", mode="fail_startup")
        failed = self._ipc_call("update.apply", {"url": bad_url})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "UPDATE_FAILED")
        restored = self._ipc_call("status")["data"]
        self.assertEqual(restored["state"], "ready", restored)
        self.assertEqual(restored["engine_bundle_version"], "0.2.0")
        self.assertEqual(restored["engine_generation"], 1)
        status = self._ipc_call("update.status")["data"]
        self.assertIn("v0.2.1", status["rollback_notice"])
        self.assertIn("v0.2.0", status["rollback_notice"])

    def test_full_signed_update_backup_idle_gating_and_auto_rollback_lifecycle(self):
        # 1. Offline update check does not fail local controls
        closed_url = f"http://127.0.0.1:{_free_port()}/manifest.json"
        offline = self._ipc_call("update.check", {"url": closed_url})
        self.assertTrue(offline["ok"], offline)
        self.assertEqual(offline["data"]["last_check"]["status"], "offline")

        # Mutate user setting and prove settings persist across updates & rollback
        settings_snap = self._v1("settings.get")["data"]
        rev = settings_snap["revision"]
        patched = self._v1(
            "settings.patch",
            {"expected_revision": rev, "patch": {"max_concurrent": 6}},
            mutation=True,
        )["data"]
        self.assertEqual(patched["values"]["max_concurrent"], 6)

        # 2. Invalid signature rejected
        valid_v020_url, valid_v020_bytes = self._build_bundle_and_manifest("0.2.0", compress=False, notes_override="v0.2.0 test")
        tampered = json.loads(valid_v020_bytes)
        sig = list(tampered["signature"])
        sig[0] = "f" if sig[0] != "f" else "0"
        tampered["signature"] = "".join(sig)
        self.server.routes["/manifest-bad-sig.json"] = (200, "application/json", json.dumps(tampered).encode("utf-8"))
        bad_sig_check = self._ipc_call("update.check", {"url": self.server.url("/manifest-bad-sig.json")})
        self.assertTrue(bad_sig_check["ok"])
        self.assertEqual(bad_sig_check["data"]["last_check"]["status"], "error")
        self.assertIn("signature verification failed", bad_sig_check["data"]["last_check"]["message"].lower())
        bad_sig_apply = self._ipc_call("update.apply", {"url": self.server.url("/manifest-bad-sig.json")})
        self.assertFalse(bad_sig_apply["ok"])
        self.assertEqual(bad_sig_apply["error"]["code"], "UPDATE_INVALID")

        # 3. Protocol mismatch rejected
        proto_bad_url, _ = self._build_bundle_and_manifest("0.2.0", protocol_min=2, protocol_max=3)
        proto_bad = self._ipc_call("update.apply", {"url": proto_bad_url})
        self.assertFalse(proto_bad["ok"])
        self.assertEqual(proto_bad["error"]["code"], "UPDATE_INVALID")
        self.assertIn("protocol", proto_bad["error"]["message"].lower())

        # 4. Missing required notice rejected
        missing_notice_url, _ = self._build_bundle_and_manifest("0.2.0", drop_notice=True)
        notice_bad = self._ipc_call("update.apply", {"url": missing_notice_url})
        self.assertFalse(notice_bad["ok"])
        self.assertEqual(notice_bad["error"]["code"], "UPDATE_INVALID")

        # 5. Bad --self-check binary rejected at staging time without stopping v0.1.0
        bad_selfcheck_url, _ = self._build_bundle_and_manifest("0.2.0", mode="fail_self_check")
        bad_sc = self._ipc_call("update.apply", {"url": bad_selfcheck_url})
        self.assertFalse(bad_sc["ok"])
        self.assertEqual(bad_sc["error"]["code"], "UPDATE_INVALID")
        self.assertIn("--self-check", bad_sc["error"]["message"])

        # 6. Idle-gating: active/scheduled download blocks update.apply until cancelled
        future_iso = "2099-01-01T00:00:00Z"
        enq = self._v1(
            "downloads.enqueue",
            {
                "url": "https://example.com/scheduled-update-gate.mp4",
                "title": "Scheduled gate",
                "scheduled_at": future_iso,
            },
            mutation=True,
        )
        job_id = enq["data"]["job_id"]
        job_rev = enq["data"].get("revision", 1)
        gated = self._ipc_call("update.apply", {"url": valid_v020_url})
        self.assertFalse(gated["ok"])
        self.assertEqual(gated["error"]["code"], "DOWNLOADS_ACTIVE")
        self._v1("downloads.action", {"job_id": job_id, "expected_revision": job_rev, "action": "cancel"}, mutation=True)

        # 7. Valid check + signed upgrade 0.1.0 -> 0.2.0 (.tar)
        chk_v020 = self._ipc_call("update.check", {"url": valid_v020_url})
        self.assertTrue(chk_v020["ok"])
        self.assertEqual(chk_v020["data"]["last_check"]["status"], "update_available")
        self.assertEqual(chk_v020["data"]["last_check"]["available_version"], "0.2.0")

        up_020 = self._ipc_call("update.apply", {"url": valid_v020_url})
        self.assertTrue(up_020["ok"], up_020)
        self.assertEqual(up_020["data"]["engine_bundle_version"], "0.2.0")
        self.assertEqual(up_020["data"]["previous_version"], "0.1.0")
        self.assertTrue(up_020["data"]["rollback_available"])
        self.assertEqual(up_020["data"]["backups_count"], 1)
        applied_status = self._ipc_call("update.status")["data"]
        self.assertEqual(applied_status["last_check"]["status"], "up_to_date")
        self.assertEqual(applied_status["last_check"]["available_version"], "0.2.0")
        self.assertIsNone(applied_status["last_check"]["message"])
        self.assertEqual(self._v1("settings.get")["data"]["values"]["max_concurrent"], 6)
        self.assertIn("0.2.0:", self.invocations_log.read_text(encoding="utf-8"))

        # 8. Valid signed upgrades 0.2.0 -> 0.3.0 (.tar.gz via engine --unpack-update) & 0.3.0 -> 0.4.0 (.tar) -> 0.5.0 (.tar)
        url_030, _ = self._build_bundle_and_manifest("0.3.0", compress=True)
        up_030 = self._ipc_call("update.apply", {"url": url_030})
        self.assertTrue(up_030["ok"], up_030)
        self.assertEqual(up_030["data"]["engine_bundle_version"], "0.3.0")
        self.assertEqual(up_030["data"]["backups_count"], 2)

        url_040, _ = self._build_bundle_and_manifest("0.4.0", compress=False)
        up_040 = self._ipc_call("update.apply", {"url": url_040})
        self.assertTrue(up_040["ok"], up_040)
        self.assertEqual(up_040["data"]["engine_bundle_version"], "0.4.0")
        self.assertEqual(up_040["data"]["backups_count"], 3)

        url_050, _ = self._build_bundle_and_manifest("0.5.0", compress=False)
        up_050 = self._ipc_call("update.apply", {"url": url_050})
        self.assertTrue(up_050["ok"], up_050)
        self.assertEqual(up_050["data"]["engine_bundle_version"], "0.5.0")
        # Bounded backup retention capped at 3
        self.assertEqual(up_050["data"]["backups_count"], 3)
        backup_dirs = [p for p in (self.data_dir / "backups").iterdir() if p.is_dir()]
        self.assertEqual(len(backup_dirs), 3)
        for bdir in backup_dirs:
            manifest = json.loads((bdir / "backup.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(manifest["files"]), 1)
            for entry in manifest["files"]:
                actual = hashlib.sha256((bdir / entry["name"]).read_bytes()).hexdigest()
                self.assertEqual(actual, entry["sha256"])

        # 9. Manual rollback 0.5.0 -> 0.4.0
        rb = self._ipc_call("update.rollback")
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["data"]["engine_bundle_version"], "0.4.0")
        self.assertIn("0.5.0", rb["data"]["rollback_notice"])
        self.assertIn("0.4.0", rb["data"]["rollback_notice"])

        # 10. Broken startup 0.4.1 passes --self-check but exits 1 on startup -> auto-rollback to 0.4.0
        bad_startup_url, _ = self._build_bundle_and_manifest("0.4.1", mode="fail_startup")
        failed = self._ipc_call("update.apply", {"url": bad_startup_url})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "UPDATE_FAILED")

        # The restored process must be ready when apply returns, before any backoff tick.
        restored = self._ipc_call("status")["data"]
        self.assertEqual(restored["state"], "ready", restored)
        self.assertEqual(restored["engine_bundle_version"], "0.4.0")
        status = self._ipc_call("update.status")["data"]
        self.assertEqual(status["engine_bundle_version"], "0.4.0")
        self.assertIn("0.4.1", status["rollback_notice"])
        self.assertIn("0.4.0", status["rollback_notice"])
        self.assertNotIn("supersecret_engine_token", status["rollback_notice"])
        self.assertNotIn("Bearer ", status["rollback_notice"])

        # Settings & DB survived all upgrades, manual rollback, and auto-rollback
        self.assertEqual(self._v1("settings.get")["data"]["values"]["max_concurrent"], 6)
        with closing(sqlite3.connect(self.data_dir / "vidgrab.db")) as db:
            rows = db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            self.assertEqual(rows, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervisor", type=Path, help="Path to compiled vidgrab-supervisor binary")
    args, remaining = parser.parse_known_args()
    if args.supervisor:
        SignedUpdatesE2ETests._cli_supervisor = args.supervisor
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SignedUpdatesE2ETests)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
