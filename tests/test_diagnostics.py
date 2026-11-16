"""Media diagnostics discard signed URLs and request headers at their sinks."""
import asyncio
import importlib.util
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

from tests.support import OfflineTestCase

SIGNED = "https://fixture.invalid/private-path-token?sig=private-query-token&x=1"


class DiagnosticRedactionTests(OfflineTestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.diagnostics"),
                             "Media diagnostics require one shared redaction boundary")
        from backend.services import diagnostics
        return diagnostics

    def test_urls_and_encoded_urls_are_removed_without_modifying_the_input(self):
        module = self.service()
        message = "Request failed for " + SIGNED + " and https%3A%2F%2Ffixture.invalid%2Fencoded-token"
        result = module.redact_diagnostic(message)
        for secret in ("private-path-token", "private-query-token", "encoded-token"):
            self.assertNotIn(secret, result)
        self.assertIn(SIGNED, message)
        self.assertEqual(module.redact_diagnostic("Format is unavailable"), "Format is unavailable")

    def test_header_and_context_dumps_are_discarded_including_multiline_values(self):
        module = self.service()
        for name in ("Cookie", "Set-Cookie", "Authorization", "Proxy-Authorization", "Referer", "User-Agent", "auth_ref", "probe_id"):
            with self.subTest(header=name):
                result = module.redact_diagnostic("Request headers: {'" + name + "': 'private-header-value'}\ncontinued-private-value")
                self.assertNotIn("private-header-value", result)
                self.assertNotIn("continued-private-value", result)

    def test_ipv6_and_punctuation_in_signed_urls_do_not_leave_request_material(self):
        module = self.service()
        for url in ("https://[::1]:8443/private-path-token?sig=private-query-token",
                    "https://fixture.invalid/media/(private-path-token)?sig=private-query-token",
                    "https://fixture.invalid/media?sig=private'query-token"):
            with self.subTest(url=url):
                result = module.redact_diagnostic("Rejected " + url)
                self.assertNotIn("private", result)
                self.assertNotIn("query-token", result)

    def test_all_extractor_logger_levels_redact_request_material(self):
        from backend.downloader import YtDlpLogger
        logger = YtDlpLogger()
        with self.assertLogs("vidgrab.downloader", level="DEBUG") as captured:
            logger.debug("[generic] Extracting URL: " + SIGNED)
            logger.info("Request failed for " + SIGNED)
            logger.warning("Cookie: private-cookie-value")
            logger.error("Authorization: Bearer private-auth-value")
        output = "\n".join(captured.output)
        for secret in ("private-path-token", "private-query-token", "private-cookie-value", "private-auth-value"):
            self.assertNotIn(secret, output)

    def test_exception_trace_redaction_preserves_error_type(self):
        module = self.service()
        result = module.redact_exception(ValueError("Rejected " + SIGNED))
        self.assertIn("ValueError", result)
        self.assertNotIn("private-path-token", result)
        self.assertNotIn("private-query-token", result)

    def test_scheduler_loop_redacts_request_exceptions(self):
        from backend import queue_manager
        queue = self.app().state.queue
        queue.clock = Mock(side_effect=ValueError("Cannot schedule " + SIGNED))
        boundary = SimpleNamespace(CancelledError=asyncio.CancelledError,
                                   sleep=AsyncMock(side_effect=[None, asyncio.CancelledError]))
        with patch.object(queue_manager, "asyncio", boundary), \
             self.assertLogs("vidgrab.queue_manager", level="ERROR") as captured:
            asyncio.run(queue._scheduler_loop())
        output = "\n".join(captured.output)
        self.assertIn("scheduler loop", output)
        self.assertNotIn("private-path-token", output)
        self.assertNotIn("private-query-token", output)

    def test_worker_loop_redacts_request_exceptions(self):
        from backend import queue_manager
        queue = self.app().state.queue
        boundary = SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=AsyncMock())
        with patch.object(queue.settings, "get_all", AsyncMock(side_effect=[
                ValueError("Cannot execute " + SIGNED), asyncio.CancelledError])), \
             patch.object(queue_manager, "asyncio", boundary), \
             self.assertLogs("vidgrab.queue_manager", level="ERROR") as captured:
            asyncio.run(queue._worker_loop())
        output = "\n".join(captured.output)
        self.assertIn("queue worker loop", output)
        self.assertNotIn("private-path-token", output)
        self.assertNotIn("private-query-token", output)

    def test_failed_websocket_delivery_redacts_errors_and_removes_the_connection(self):
        from backend.queue_manager import ConnectionManager
        manager = ConnectionManager()
        connection = SimpleNamespace(send_json=AsyncMock(side_effect=ValueError("Cannot send " + SIGNED)))
        manager.active_connections.append(connection)
        message = {"event": "task_progress", "data": {"url": SIGNED}}
        with self.assertLogs("vidgrab.queue_manager", level="WARNING") as captured:
            asyncio.run(manager.broadcast(message))
        output = "\n".join(captured.output)
        self.assertNotIn("private-path-token", output)
        self.assertNotIn("private-query-token", output)
        self.assertEqual(manager.active_connections, [])
        self.assertEqual(message["data"]["url"], SIGNED)

    def test_worker_typed_rejection_redacts_its_message_before_ipc(self):
        from backend.engine import worker
        from backend.services.playlists import PlaylistError
        runtime = self.runtime()
        sent = []
        bootstrap = {"options": {}, "runtime": {"data_dir": str(runtime.data_dir), "download_dir": str(runtime.download_dir)}}
        with patch.object(worker, "read_message", side_effect=[bootstrap, {"type": "execute"}]), \
             patch.object(worker, "send_message", side_effect=sent.append), \
             patch.object(worker.sys, "stdin", io.TextIOWrapper(io.BytesIO(b""))), \
             patch.object(worker, "threading", SimpleNamespace(Event=threading.Event, Thread=Mock())), \
             patch.object(worker.logging, "basicConfig"), \
             patch("backend.downloader.run_download", side_effect=PlaylistError("Rejected source " + SIGNED)):
            worker.main()
        self.assertEqual(sent[-1]["code"], "PLAYLIST_REJECTED")
        self.assertNotIn("private-query-token", sent[-1]["message"])
        self.assertNotIn("private-path-token", sent[-1]["message"])
