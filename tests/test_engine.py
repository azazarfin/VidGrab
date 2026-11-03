"""The packaged entrypoint must not expose the legacy development API."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import OfflineTestCase


class PackagedEngineTests(OfflineTestCase):
    def engine_module(self):
        self.assertIsNotNone(
            importlib.util.find_spec("backend.engine"),
            "P02 needs an import-safe packaged engine boundary",
        )
        from backend.engine import create_engine_app
        return create_engine_app

    def engine(self):
        factory = self.engine_module()
        return factory(
            self.runtime(), token="a" * 64, port=43210,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            start_queue=False,
        )

    def client(self, app):
        return TestClient(app, base_url="http://127.0.0.1:43210", headers={"Authorization": "Bearer " + "a" * 64})

    def test_factory_has_no_runtime_side_effects(self):
        self.engine()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_frozen_runtime_protects_the_complete_installation(self):
        installation = self.root / "installation"
        code = f'''
import sys
from pathlib import Path
sys.frozen = True
sys.executable = {str(installation / 'engine' / 'vidgrab-engine')!r}
from backend.runtime import RuntimeConfig, RuntimeCompatibilityError, OutputPathError
runtime = RuntimeConfig(Path({str(self.root / 'state')!r}), Path({str(self.root / 'media')!r}))
try:
    runtime.validate_output(Path({str(installation)!r}), "outer-executable.%(ext)s")
except OutputPathError:
    pass
else:
    raise AssertionError("Frozen output validation must protect sibling shell/host files")
'''
        result = self.run_import_check(code, Path(__file__).resolve().parents[1], {})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_and_wrong_credentials_are_refused(self):
        with self.client(self.engine()) as client:
            for credential in ("", "Bearer " + "b" * 64):
                with self.subTest(credential=bool(credential)):
                    response = client.get("/health", headers={"Authorization": credential})
                    self.assertEqual(response.status_code, 401)

    def test_host_and_origin_are_checked_before_routes(self):
        with self.client(self.engine()) as client:
            for headers in ({"Host": "attacker.example"}, {"Host": "127.0.0.1:43211"}, {"Origin": "https://attacker.example"}):
                with self.subTest(headers=headers):
                    self.assertEqual(client.get("/health", headers=headers).status_code, 403)

    def test_authenticated_health_and_bounded_snapshot(self):
        with self.client(self.engine()) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            result = client.get("/spike/status")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["jobs"], [])
            self.assertEqual(result.json()["spike_version"], 1)

    def test_legacy_endpoints_and_websockets_are_unavailable(self):
        with self.client(self.engine()) as client:
            for method, path in (("GET", "/config"), ("POST", "/downloads"), ("POST", "/update-ytdlp"), ("GET", "/docs")):
                with self.subTest(path=path):
                    self.assertEqual(client.request(method, path, json={}).status_code, 404)
            from starlette.websockets import WebSocketDisconnect
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws/progress"):
                    pass

    def test_only_loopback_fixture_requests_can_be_enqueued(self):
        with self.client(self.engine()) as client:
            for request in ({"url": "https://example.com/fixture.wav"}, {"url": "http://localhost:8080/fixture.wav"}, {"url": "http://127.0.0.1:65536/fixture.wav"}, {"url": "http://127.0.0.1:8080/fixture.wav", "extra_args": {"exec": "unsafe"}}):
                with self.subTest(request=request):
                    self.assertEqual(client.post("/spike/fixture", json=request).status_code, 422)
            accepted = client.post("/spike/fixture", json={"url": "http://127.0.0.1:8080/fixture.wav"})
            self.assertEqual(accepted.status_code, 200)
            snapshot = client.get("/spike/status").json()
            self.assertEqual(len(snapshot["jobs"]), 1)
            self.assertEqual(snapshot["jobs"][0]["id"], accepted.json()["task_id"])
            self.assertEqual(snapshot["jobs"][0]["status"], "queued")

    def test_packaged_tools_override_legacy_options(self):
        from backend.downloader import run_download
        runtime = self.runtime()
        runtime.prepare()
        captured = {}

        class FakeYDL:
            def __init__(self, options):
                captured.update(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, *args, **kwargs):
                return {"title": "fixture", "_filename": str(runtime.download_dir / "fixture.wav")}

        # The assertion makes a missing interface fail as a behavior check.
        import inspect
        self.assertIn("components", inspect.signature(run_download).parameters, "The frozen runner must accept app-owned media tools")
        with patch("backend.downloader.yt_dlp.YoutubeDL", FakeYDL):
            run_download(
                "job", "http://127.0.0.1:8080/fixture.wav", "best", str(runtime.download_dir),
                "fixture.%(ext)s", lambda *_: None, runtime,
                components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
                extra_args={"js_runtimes": {"node": {"path": "/host/node"}}, "remote_components": ["ejs:github"]},
            )
        self.assertEqual(captured["ffmpeg_location"], "/bundled")
        self.assertEqual(captured["js_runtimes"], {"deno": {"path": "/bundled/deno"}})
        self.assertEqual(captured["remote_components"], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
