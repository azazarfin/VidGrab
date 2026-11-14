"""Wire admission counts redirects and holds slots until body exit."""

import importlib.util
import io
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from types import SimpleNamespace
from urllib.request import Request

from tests.support import OfflineTestCase


class RequestNetworkTests(OfflineTestCase):
    @contextmanager
    def server(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_HEAD(self):
                self.do_GET()
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/media")
                    body = b"redirect"
                elif self.path == "/limited":
                    self.send_response(429)
                    self.send_header("Retry-After", "120")
                    body = b"limited"
                elif self.path == "/index.m3u8":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    body = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nmedia\n#EXT-X-ENDLIST\n"
                else:
                    self.send_response(200)
                    body = b"fixture"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_pinned_transport_counts_actual_redirects_and_releases_bodies(self):
        import yt_dlp
        from backend.services.request_network import install_request_budget
        coordinator = self.gate().coordinator
        with self.server() as url, yt_dlp.YoutubeDL({"quiet": True, "proxy": ""}) as ydl, \
                patch("backend.services.request_network.RequestCoordinator", return_value=coordinator):
            gate = install_request_budget(ydl, self.runtime(), {}, lambda: False, clock=lambda: 1000)
            try:
                response = ydl.urlopen(url + "/redirect")
                self.assertEqual(self.requests, [url + "/redirect", url + "/media"])
                self.assertEqual(len(self.active), 1)
                self.assertEqual(response.read(), b"fixture")
                response.close()
                self.assertEqual(self.active, set())
            finally:
                gate.close()

    def test_pinned_transport_preserves_http_deadline_through_wrapped_errors(self):
        import yt_dlp
        from backend.services.request_network import install_request_budget
        from backend.services.retry_policy import decide
        coordinator = self.gate().coordinator
        with self.server() as url, yt_dlp.YoutubeDL({"quiet": True, "proxy": ""}) as ydl, \
                patch("backend.services.request_network.RequestCoordinator", return_value=coordinator):
            gate = install_request_budget(ydl, self.runtime(), {}, lambda: False, clock=lambda: 1000)
            try:
                with self.assertRaises(Exception) as caught:
                    ydl.urlopen(url + "/limited")
                decision = decide(caught.exception, 0, 3, 1000)
                self.assertEqual((decision.code, decision.retry_at), ("UPSTREAM_RATE_LIMITED", 1120))
                self.assertEqual(self.active, set())
            finally:
                gate.close()

    def test_response_body_failure_blocks_hidden_followup_requests(self):
        gate = self.gate()
        response = self.response()
        def read(*_):
            raise TimeoutError("fixture body timeout")
        response.read = read
        response = gate.open(lambda _: response, Request("https://fixture.invalid/media"))
        with self.assertRaises(Exception):
            response.read()
        with self.assertRaisesRegex(Exception, "TRANSPORT_ERROR"):
            gate.open(lambda _: self.response(), Request("https://fixture.invalid/retry"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.active, set())

    def test_proxy_handler_does_not_acquire_a_second_wire_slot(self):
        import yt_dlp
        from yt_dlp.networking import Request as NetworkRequest
        from backend.services.request_network import install_request_budget
        coordinator = self.gate().coordinator
        with self.server() as proxy, yt_dlp.YoutubeDL({"quiet": True}) as ydl, \
                patch("backend.services.request_network.RequestCoordinator", return_value=coordinator):
            gate = install_request_budget(ydl, self.runtime(), {}, lambda: False, clock=lambda: 1000)
            try:
                response = ydl.urlopen(NetworkRequest("http://fixture.invalid/media", proxies={"http": proxy}))
                self.assertEqual(response.read(), b"fixture")
                response.close()
                self.assertEqual(self.requests, ["http://fixture.invalid/media"])
                self.assertEqual(self.active, set())
            finally:
                gate.close()

    def test_generic_hls_header_response_closes_before_nested_manifest_request(self):
        import yt_dlp
        from backend.services.request_network import install_request_budget
        from backend.services.retry_policy import RequestFailure
        coordinator = self.gate().coordinator
        acquire = coordinator.acquire
        def one_slot(url, now):
            if self.active:
                raise RequestFailure("ADMISSION_LIMITED", retry_at=1001, consumes_retry=False)
            return acquire(url, now)
        coordinator.acquire = one_slot
        with self.server() as url, yt_dlp.YoutubeDL({"quiet": True, "proxy": "", "format": "best", "remote_components": []}) as ydl, \
                patch("backend.services.request_network.RequestCoordinator", return_value=coordinator):
            gate = install_request_budget(ydl, self.runtime(), {}, lambda: False, clock=lambda: 1000)
            try:
                info = ydl.extract_info(url + "/index.m3u8", download=False)
                gate.check()
                self.assertTrue(info["formats"])
                self.assertEqual(self.active, set())
            finally:
                gate.close()

    def test_worker_request_failure_envelope_rejects_unbounded_deadlines(self):
        from backend.engine.worker_owner import validate_worker_message
        detail = {"code": "ADMISSION_LIMITED", "retry_at": 1120, "consumes_retry": False}
        validate_worker_message({"type": "error", "message": "fixture", "request_failure": detail})
        for deadline in (float("nan"), float("inf"), -1, 253402300800):
            with self.assertRaises(ValueError):
                validate_worker_message({"type": "error", "message": "fixture", "request_failure": {**detail, "retry_at": deadline}})

    def gate(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.request_network"), "Wire requests need one budgeted transport")
        from backend.services.request_network import BudgetedRequests
        self.active = set()
        self.requests = []
        def acquire(url, now):
            lease = str(len(self.requests))
            self.requests.append(url)
            self.active.add(lease)
            return lease
        coordinator = SimpleNamespace(acquire=acquire, release=self.active.discard, cooldown=lambda *args: None)
        return BudgetedRequests(coordinator, lambda: False, clock=lambda: 1000)

    def response(self, body=b"fixture", status=200, headers=None):
        response = io.BytesIO(body)
        response.status, response.headers = status, headers or {}
        response.isclosed = lambda: response.closed or response.tell() == len(body)
        return response

    def test_body_read_and_close_release_exactly_one_lease(self):
        gate = self.gate()
        response = gate.open(lambda _: self.response(), Request("https://fixture.invalid/media"))
        self.assertEqual(len(self.active), 1)
        self.assertEqual(response.read(), b"fixture")
        self.assertEqual(self.active, set())
        response.close()
        self.assertEqual(self.requests, ["https://fixture.invalid/media"])

    def test_redirect_body_exits_before_next_wire_request(self):
        gate = self.gate()
        redirect = gate.open(lambda _: self.response(status=302), Request("https://fixture.invalid/original"))
        redirect.read()
        redirected = gate.open(lambda _: self.response(), Request("https://other.invalid/final"))
        self.assertEqual(self.active, {"1"})
        redirected.close()
        self.assertEqual(len(self.requests), 2)

    def test_upstream_cooldown_is_sticky_and_prevents_inner_request_retries(self):
        gate = self.gate()
        with self.assertRaisesRegex(Exception, "UPSTREAM_RATE_LIMITED"):
            gate.open(lambda _: self.response(status=429, headers={"Retry-After": "120"}), Request("https://fixture.invalid/media"))
        with self.assertRaisesRegex(Exception, "UPSTREAM_RATE_LIMITED"):
            gate.open(lambda _: self.response(), Request("https://fixture.invalid/media"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.active, set())
