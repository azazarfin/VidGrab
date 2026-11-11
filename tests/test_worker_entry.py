"""Worker mode must bypass server startup and host component discovery."""

import importlib.util
from tests.support import OfflineTestCase


class WorkerEntryTests(OfflineTestCase):
    def validate(self, message):
        from backend.engine import worker_owner
        validator = getattr(worker_owner, "validate_worker_message", None)
        self.assertTrue(callable(validator), "Worker replies need a shared shape/state validator")
        return validator(message)

    def test_result_requires_a_nonempty_literal_path(self):
        for data in (["invalid"], {}, {"filepath": ""}, {"filepath": 1}, {"filepath": "bad\x00path"}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.validate({"type": "result", "data": data})

    def test_progress_cannot_declare_a_terminal_or_cancelling_state(self):
        for status in ("complete", "failed", "cancelled", "cancelling", "queued"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.validate({"type": "progress", "data": {"status": status}})

    def test_progress_requires_a_text_state(self):
        for status in (None, True, 1, [], {}):
            with self.subTest(status=status):
                with self.assertRaises(Exception) as rejected:
                    self.validate({"type": "progress", "data": {"status": status}})
                self.assertIsInstance(rejected.exception, ValueError)

    def test_error_messages_are_bounded_text(self):
        for message in ({"unexpected": "object"}, None, "x" * 4097):
            with self.subTest(message_type=type(message).__name__):
                with self.assertRaises(ValueError):
                    self.validate({"type": "error", "message": message})

    def test_worker_eof_exits_without_loading_the_application(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.engine.worker"),
                             "The engine needs a worker entrypoint separate from ASGI startup")
        code = """
import io, sys
sys.stdin = io.TextIOWrapper(io.BytesIO(b''))
from backend.engine import worker
worker.main()
assert 'backend.app' not in sys.modules
assert 'uvicorn' not in sys.modules
"""
        result = self.run_import_check(code, cwd=__import__('pathlib').Path.cwd(), environment={})
        self.assertEqual(result.returncode, 0, result.stderr)
