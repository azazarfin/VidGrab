import hashlib
import json
import shutil
import sqlite3
import textwrap
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import OfflineTestCase


class RuntimeTests(OfflineTestCase):
    def test_platform_defaults_and_environment_overrides_create_no_files(self):
        from backend.runtime import RuntimeConfig

        linux = RuntimeConfig.from_env({"XDG_DATA_HOME": str(self.root / "xdg")}, platform="linux", home=self.root)
        self.assertEqual(linux.data_dir, self.root / "xdg" / "vidgrab")
        windows = RuntimeConfig.from_env({"LOCALAPPDATA": str(self.root / "local")}, platform="win32", home=self.root)
        self.assertEqual(windows.data_dir, self.root / "local" / "VidGrab")
        chosen = RuntimeConfig.from_env({
            "VIDGRAB_DATA_DIR": str(self.root / "chosen"),
            "VIDGRAB_DOWNLOAD_DIR": str(self.root / "media"),
            "VIDGRAB_TEMP_DIR": str(self.root / "temporary"),
            "VIDGRAB_LOG_DIR": str(self.root / "logs"),
        }, home=self.root)
        self.assertEqual(chosen.db_path, self.root / "chosen" / "vidgrab.db")
        self.assertEqual(chosen.temp_dir, self.root / "temporary")
        self.assertFalse(chosen.data_dir.exists())

    def test_python_and_source_directory_compatibility_fail_before_writes(self):
        from backend.runtime import RuntimeConfig, RuntimeCompatibilityError, check_python_compatibility

        with self.assertRaises(RuntimeCompatibilityError):
            check_python_compatibility((3, 10))
        with self.assertRaises(RuntimeCompatibilityError):
            self.app(RuntimeConfig(
                data_dir=Path(__file__).resolve().parents[1] / "forbidden-runtime",
                download_dir=self.root / "media",
            ))

    def test_import_from_read_only_install_has_no_application_side_effects(self):
        source = Path(__file__).resolve().parents[1] / "backend"
        install = self.root / "install"
        shutil.copytree(source, install / "backend", ignore=shutil.ignore_patterns("*.db", "*.db-*", "__pycache__"))
        paths = list(install.rglob("*")) + [install]
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.addCleanup(lambda: [path.chmod(0o755 if path.is_dir() else 0o644) for path in paths])
        code = textwrap.dedent("""
            import logging, sys
            attempts = []
            def audit(event, args):
                writing = event == "open" and (
                    isinstance(args[1], str) and any(flag in args[1] for flag in "wax+")
                    or isinstance(args[2], int) and args[2] & 3
                )
                if writing or event in {
                    "sqlite3.connect", "os.mkdir", "os.remove", "os.rename", "os.link",
                    "subprocess.Popen", "socket.connect", "socket.getaddrinfo",
                }:
                    attempts.append(event)
                    raise AssertionError("Import attempted application I/O: " + event)
            sys.addaudithook(audit)
            handlers = list(logging.getLogger().handlers)
            from backend.main import app, create_app
            other = create_app()
            assert app.state.queue.worker_task is None
            assert other.state.queue.scheduler_task is None
            assert not app.state.runtime.data_dir.exists()
            assert handlers == logging.getLogger().handlers
            assert not attempts, attempts
        """)
        result = self.run_import_check(code, install, {
            "VIDGRAB_DATA_DIR": str(self.root / "state"),
            "VIDGRAB_DOWNLOAD_DIR": str(self.root / "media"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "state").exists())

    def test_app_creation_does_not_create_runtime_directories(self):
        runtime = self.runtime()
        self.app(runtime)
        self.assertFalse(runtime.data_dir.exists())
        self.assertFalse(runtime.download_dir.exists())

    def test_lifespan_initializes_only_the_selected_runtime(self):
        repository_db = Path(__file__).resolve().parents[1] / "backend" / "vidgrab.db"
        digest = hashlib.sha256(repository_db.read_bytes()).digest() if repository_db.exists() else None
        runtime = self.runtime()
        app = self.app(runtime)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertTrue(runtime.db_path.is_file())
            self.assertTrue(runtime.logs_dir.is_dir())
            self.assertTrue(runtime.temp_dir.is_dir())
            self.assertTrue(runtime.download_dir.is_dir())
            self.assertIsNone(app.state.queue.worker_task)
        if digest is not None:
            self.assertEqual(hashlib.sha256(repository_db.read_bytes()).digest(), digest)

    def test_two_apps_have_separate_settings_and_downloads(self):
        first, second = self.app(self.runtime("first")), self.app(self.runtime("second"))
        with TestClient(first) as one, TestClient(second) as two:
            self.assertEqual(one.patch("/config", json={"max_concurrent": 7}).status_code, 200)
            self.assertEqual(two.get("/config").json()["max_concurrent"], 3)
            result = one.post("/downloads", json={"url": "https://fixture.invalid/media", "format_id": "best"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(one.get("/downloads").json()), 1)
            self.assertEqual(two.get("/downloads").json(), [])
            self.assertFalse(second.state.queue.in_memory_progress)

    def test_health_before_startup_reports_not_ready_without_writes(self):
        runtime = self.runtime()
        with TestClient(self.app(runtime)) as client:
            self.assertEqual(client.get("/health").json()["status"], "ready")
        client = TestClient(self.app(self.runtime("unstarted")))
        self.addCleanup(client.close)
        self.assertEqual(client.get("/health").status_code, 503)
        self.assertFalse((self.root / "unstarted").exists())


class SettingsTests(OfflineTestCase):
    def test_download_overrides_and_templates_cannot_target_the_install_tree(self):
        install = Path(__file__).resolve().parents[1]
        app = self.app()
        with TestClient(app) as client:
            initial = client.get("/config").json()
            for endpoint, payload in (
                ("/downloads", {"url": "https://fixture.invalid/media", "format_id": "best",
                                "download_dir": str(install / "backend")}),
                ("/downloads", {"url": "https://fixture.invalid/media", "format_id": "best",
                                "output_template": str(install / "backend" / "vidgrab.db")}),
                ("/config", {"download_dir": str(install / "backend")}),
                ("/config", {"filename_template": str(install / "backend" / "vidgrab.db")}),
            ):
                with self.subTest(endpoint=endpoint):
                    response = client.post(endpoint, json=payload) if endpoint == "/downloads" else client.patch(endpoint, json=payload)
                    self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(client.get("/config").json(), initial)
            self.assertEqual(client.get("/downloads").json(), [])
            self.assertEqual(app.state.queue.task_cookies, {})

    def test_output_paths_cannot_overwrite_application_storage(self):
        runtime = self.runtime()
        with TestClient(self.app(runtime)) as client:
            for directory in (runtime.data_dir, runtime.logs_dir, runtime.temp_dir):
                for endpoint, field, value in (
                    ("/config", "download_dir", str(directory)),
                    ("/config", "filename_template", str(directory / "vidgrab.db")),
                    ("/downloads", "download_dir", str(directory)),
                    ("/downloads", "output_template", str(directory / "vidgrab.db")),
                ):
                    with self.subTest(endpoint=endpoint, field=field, directory=directory):
                        payload = {field: value}
                        if endpoint == "/downloads":
                            payload.update(url="https://fixture.invalid/media", format_id="best")
                            response = client.post(endpoint, json=payload)
                        else:
                            response = client.patch(endpoint, json=payload)
                        self.assertEqual(response.status_code, 400, response.text)

    def test_output_validation_resolves_symlinks_and_relative_paths(self):
        runtime = self.runtime()
        alias = self.root / "state-alias"
        alias.symlink_to(runtime.data_dir, target_is_directory=True)
        with TestClient(self.app(runtime)) as client:
            for value in (str(alias / "vidgrab.db"), "../state/vidgrab.db"):
                with self.subTest(template=value):
                    self.assertEqual(client.patch("/config", json={"filename_template": value}).status_code, 400)
                    self.assertEqual(client.post("/downloads", json={
                        "url": "https://fixture.invalid/media", "format_id": "best", "output_template": value,
                    }).status_code, 400)

    def test_invalid_path_patch_is_atomic_and_does_not_create_media_directories(self):
        runtime = self.runtime()
        candidate = self.root / "not-created"
        with TestClient(self.app(runtime)) as client:
            initial = client.get("/config").json()
            response = client.patch("/config", json={
                "download_dir": str(candidate), "filename_template": str(runtime.db_path), "max_concurrent": 8,
            })
            self.assertEqual(response.status_code, 400)
            self.assertFalse(candidate.exists())
            self.assertEqual(client.get("/config").json(), initial)

    def test_safe_nested_and_absolute_filename_templates_are_preserved(self):
        with TestClient(self.app()) as client:
            for template in ("series/%(title)s/%(id)s.%(ext)s", str(self.root / "other-media" / "%(title)s.%(ext)s")):
                with self.subTest(template=template):
                    response = client.patch("/config", json={"filename_template": template})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["filename_template"], template)
                    self.assertEqual(client.post("/downloads", json={
                        "url": "https://fixture.invalid/media", "format_id": "best", "output_template": template,
                    }).status_code, 200)

    def test_initialization_rejects_unsafe_saved_outputs(self):
        runtime = self.runtime()
        with TestClient(self.app(runtime)):
            pass
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("UPDATE config SET value=? WHERE key='filename_template'", (json.dumps(str(runtime.db_path)),))
        with self.assertRaises(ValueError):
            with TestClient(self.app(runtime)):
                self.fail("Unsafe stored output settings must fail before the app is ready")

    def test_downloader_rechecks_paths_before_mkdir_or_extraction(self):
        from backend.downloader import run_download

        runtime = self.runtime()
        candidate = self.root / "not-created"

        class FixtureExtractor:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, *args, **kwargs):
                return {"_filename": str(candidate / "fixture.mp4"), "title": "Fixture"}

        with patch("backend.downloader.yt_dlp.YoutubeDL", FixtureExtractor):
            with self.assertRaises(ValueError):
                run_download(
                    task_id="fixture", url="https://fixture.invalid/media", format_id="best",
                    download_dir=str(candidate), filename_template=str(runtime.db_path),
                    progress_callback=lambda *args: None, runtime=runtime,
                )
        self.assertFalse(candidate.exists())

    def test_partial_patch_preserves_existing_fields(self):
        with TestClient(self.app()) as client:
            initial = {"filename_template": "chosen-%(id)s.%(ext)s", "download_threads": 2, "max_retries": 1}
            self.assertEqual(client.patch("/config", json=initial).status_code, 200)
            updated = client.patch("/config", json={"max_concurrent": 6})
            self.assertEqual(updated.status_code, 200)
            for key, value in initial.items():
                self.assertEqual(updated.json()[key], value)

    def test_numeric_settings_reject_null_and_out_of_range_values(self):
        with TestClient(self.app()) as client:
            for key, values in {
                "max_concurrent": [None, 0, 11, 1.5],
                "download_threads": [None, 0, 33, 1.5],
                "max_retries": [None, -1, 11, 1.5],
                "speed_limit": [None, -1, 2**63, 1.5],
            }.items():
                for value in values:
                    with self.subTest(key=key, value=value):
                        response = client.patch("/config", json={key: value})
                        self.assertEqual(response.status_code, 422, response.text)

    def test_empty_patch_does_not_recreate_the_media_directory(self):
        runtime = self.runtime()
        with TestClient(self.app(runtime)) as client:
            runtime.download_dir.rmdir()
            self.assertEqual(client.patch("/config", json={}).status_code, 200)
            self.assertFalse(runtime.download_dir.exists())

    def test_status_and_update_route_do_not_contact_pypi_or_run_pip(self):
        with TestClient(self.app()) as client:
            status = client.get("/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["status"], "online")
            self.assertIsNone(status.json()["yt_dlp_latest"])
            self.assertFalse(status.json()["update_available"])
            self.assertEqual(client.post("/update-ytdlp").status_code, 409)
