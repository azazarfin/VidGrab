"""Download results must identify the file actually written by yt-dlp."""

from pathlib import Path
from unittest.mock import patch

from tests.support import OfflineTestCase


class DownloaderPathTests(OfflineTestCase):
    def test_audio_result_preserves_unicode_parent_directory(self):
        from backend.downloader import run_download
        from backend.runtime import RuntimeConfig

        runtime = RuntimeConfig(self.root / "state", self.root / "media বাংলা")
        final_file = runtime.download_dir / "fixture.mp3"

        class FakeYDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, *args, **kwargs):
                final_file.write_bytes(b"ID3 fixture")
                return {"title": "fixture", "_filename": str(final_file.with_suffix(".wav"))}

        with patch("backend.downloader.yt_dlp.YoutubeDL", FakeYDL):
            result = run_download(
                "job", "http://127.0.0.1:8080/fixture.wav", "best",
                str(runtime.download_dir), "fixture.%(ext)s", lambda *_: None,
                runtime, audio_only=True,
            )

        self.assertTrue(final_file.is_file())
        self.assertEqual(result["filepath"], str(final_file))
        self.assertTrue(Path(result["filepath"]).is_file())
