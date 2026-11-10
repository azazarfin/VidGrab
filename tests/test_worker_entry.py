"""Worker mode must bypass server startup and host component discovery."""

import importlib.util
from tests.support import OfflineTestCase


class WorkerEntryTests(OfflineTestCase):
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
