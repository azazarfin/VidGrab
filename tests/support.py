import importlib.util
import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.external_attempts = []
        self.original_popen_init = subprocess.Popen.__init__

        def deny_external_socket(sock, address):
            if sock.family == socket.AF_UNIX:
                return original_connect(sock, address)
            if isinstance(address, tuple) and is_loopback(address[0]):
                return original_connect(sock, address)
            self.external_attempts.append("network")
            raise AssertionError("External network access is forbidden in the default suite")

        def is_loopback(host):
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return host == "localhost"

        original_lookup = socket.getaddrinfo

        def deny_external_lookup(host, *args, **kwargs):
            if host is None or is_loopback(host):
                return original_lookup(host, *args, **kwargs)
            self.external_attempts.append("DNS")
            raise AssertionError("External DNS lookup is forbidden in the default suite")

        def deny_process(*args, **kwargs):
            self.external_attempts.append("process")
            raise AssertionError("Use an injected process adapter in the default suite")

        original_connect = socket.socket.connect
        self.addCleanup(self.assertEqual, self.external_attempts, [])
        for target, replacement in (
            ("socket.socket.connect", deny_external_socket),
            ("socket.socket.connect_ex", deny_external_socket),
            ("socket.getaddrinfo", deny_external_lookup),
            ("subprocess.Popen.__init__", deny_process),
        ):
            guard = patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)

    def runtime(self, name="state"):
        self.assertIsNotNone(
            importlib.util.find_spec("backend.runtime"),
            "Runtime paths must be explicitly injectable before app startup",
        )
        from backend.runtime import RuntimeConfig

        return RuntimeConfig(
            data_dir=self.root / name,
            download_dir=self.root / (name + "-media"),
        )

    def app(self, runtime=None, **kwargs):
        from backend import main

        factory = getattr(main, "create_app", None)
        self.assertTrue(callable(factory), "App creation needs a side-effect-free factory")
        kwargs.setdefault("start_queue", False)
        return factory(runtime or self.runtime(), **kwargs)

    def run_import_check(self, code, cwd, environment):
        # This controlled child checks a copied install, with its own audit guard.
        with patch("subprocess.Popen.__init__", self.original_popen_init):
            return subprocess.run(
                [sys.executable, "-B", "-c", code], cwd=cwd,
                env={**os.environ, **environment}, timeout=10,
                capture_output=True, text=True,
            )
