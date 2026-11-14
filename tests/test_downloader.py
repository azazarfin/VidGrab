"""Download results must identify the file actually written by yt-dlp."""

import inspect
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.support import OfflineTestCase


class DownloaderPathTests(OfflineTestCase):
    def fake_ydl(self, records, captured=None):
        class FakeYDL:
            def __init__(self, options):
                self.options = options
                self.processors = []
                if captured is not None:
                    captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def add_post_processor(self, processor, when="post_process"):
                self.processors.append((when, processor))

            def prepare_filename(self, info):
                return info.get("_filename", "legacy-guessed.mp4")

            def extract_info(self, *args, **kwargs):
                entries = records(self.options) if callable(records) else records
                for info in entries:
                    for when, processor in self.processors:
                        if when == "post_process":
                            _, info = processor.run(info)
                    moves = dict(info.get("__files_to_move", {}))
                    source = info["filepath"]
                    destination = str(Path(info.get("__finaldir", Path(source).parent)) / Path(source).name)
                    moves[source] = destination
                    for old, new in moves.items():
                        new = new or str(Path(destination).parent / Path(old).name)
                        if old != new and not Path(new).exists():
                            Path(new).parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(old, new)
                    info["filepath"] = destination
                    info.pop("__files_to_move", None)
                    for when, processor in self.processors:
                        if when == "after_move":
                            _, info = processor.run(info)
                return entries[0] if len(entries) == 1 else {"_type": "playlist", "entries": entries}
        return FakeYDL

    def run_attempt(self, records, *, runtime=None, captured=None, **options):
        from backend.downloader import run_download
        runtime = runtime or self.runtime()
        with patch("backend.downloader.yt_dlp.YoutubeDL", self.fake_ydl(records, captured)):
            return run_download("job", "http://127.0.0.1:8080/fixture.wav", "best", str(runtime.download_dir),
                                "fixture.%(ext)s", lambda *_: None, runtime, **options)

    def test_final_postprocessing_path_wins_over_stale_filenames_and_audio_extension(self):
        runtime = self.runtime()
        runtime.download_dir.mkdir()
        output = runtime.download_dir / "finished বাংলা.converted"
        output.write_bytes(b"converted")
        result = self.run_attempt([{"filepath": str(output), "_filename": str(output.with_suffix(".wav")), "title": "fixture"}],
                          runtime=runtime, audio_only=True, audio_format="mp3")
        self.assertEqual(result["filepath"], str(output))
        self.assertIn("artifacts", result, "Completion must report authoritative output records")

    def test_multiple_media_and_retained_subtitle_thumbnail_outputs_are_reported(self):
        runtime = self.runtime()
        runtime.download_dir.mkdir()
        first = runtime.download_dir / "first.mkv"
        second = runtime.download_dir / "second.mkv"
        subtitle = runtime.download_dir / "first.en.srt"
        thumbnail = runtime.download_dir / "first.jpg"
        for path in (first, second, subtitle, thumbnail):
            path.write_bytes(b"fixture")
        records = [{"filepath": str(first), "title": "first",
                    "requested_subtitles": {"en": {"filepath": str(subtitle)}},
                    "thumbnails": [{"filepath": str(thumbnail)}],
                    "__files_to_move": {str(subtitle): str(subtitle), str(thumbnail): str(thumbnail)}},
                   {"filepath": str(second), "title": "second"}]
        result = self.run_attempt(records, runtime=runtime)
        self.assertIn("artifacts", result, "All completed post-processing outputs must be reported")
        self.assertEqual({(item["path"], item["kind"]) for item in result["artifacts"]},
                         {(str(first), "media"), (str(second), "media"),
                          (str(subtitle), "subtitle"), (str(thumbnail), "thumbnail")})
        self.assertEqual(result["filepath"], str(first))

    def test_attempt_temp_template_is_isolated_from_the_literal_final_template(self):
        from backend.downloader import run_download
        self.assertIn("attempt_dir", inspect.signature(run_download).parameters,
                      "Downloads need an owned intermediate directory")
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        captured = {}

        def records(options):
            staged = workspace / "fixture.mp4"
            staged.write_bytes(b"media")
            return [{"filepath": str(staged), "__finaldir": str(runtime.download_dir), "title": "fixture"}]

        result = self.run_attempt(records, runtime=runtime, captured=captured, attempt_dir=str(workspace))
        self.assertEqual(captured["outtmpl"]["default"], str(runtime.download_dir / "fixture.%(ext)s"))
        self.assertEqual(Path(captured["outtmpl"]["temp"]).parent, workspace)
        self.assertIs(captured["overwrites"], False)
        self.assertEqual(result["filepath"], str(runtime.download_dir / "fixture.mp4"))

    def test_existing_destination_is_preserved_and_cannot_be_reported_as_this_attempt(self):
        from backend.downloader import run_download
        self.assertIn("attempt_dir", inspect.signature(run_download).parameters)
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        staged = workspace / "fixture.mp4"
        staged.write_bytes(b"owned media")
        final = runtime.download_dir / staged.name
        final.write_bytes(b"unrelated existing media")
        with self.assertRaisesRegex(Exception, "exist|publish|owned"):
            self.run_attempt([{"filepath": str(staged), "__finaldir": str(runtime.download_dir)}],
                     runtime=runtime, attempt_dir=str(workspace))
        self.assertEqual(final.read_bytes(), b"unrelated existing media")
        self.assertEqual(staged.read_bytes(), b"owned media")

    def test_publication_journal_is_flushed_before_moving_the_output(self):
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        staged = workspace / "fixture.mp4"
        staged.write_bytes(b"owned media")
        final = runtime.download_dir / staged.name
        with patch("backend.services.artifacts.os.fsync", side_effect=OSError("journal flush failed")):
            with self.assertRaisesRegex(OSError, "journal flush failed"):
                self.run_attempt([{"filepath": str(staged), "__finaldir": str(runtime.download_dir)}],
                                 runtime=runtime, attempt_dir=str(workspace))
        self.assertFalse(final.exists())
        self.assertEqual(staged.read_bytes(), b"owned media")

    def test_staged_thumbnail_move_targets_the_final_media_directory(self):
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        media = workspace / "fixture.mp4"
        thumbnail = workspace / "fixture.png"
        media.write_bytes(b"media")
        thumbnail.write_bytes(b"thumbnail")
        result = self.run_attempt([{"filepath": str(media), "__finaldir": str(runtime.download_dir),
            "thumbnails": [{"filepath": str(thumbnail)}], "__files_to_move": {str(thumbnail): str(thumbnail)}}],
            runtime=runtime, attempt_dir=str(workspace))
        self.assertIn({"path": str(runtime.download_dir / "fixture.png"), "kind": "thumbnail"}, result["artifacts"])
        # Its hard-link witness remains until the owning SQLite completion commits.
        self.assertTrue(thumbnail.exists())

    def test_worker_result_is_journaled_with_verifiable_publication_ownership(self):
        from backend.services.artifacts import read_journal
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        staged = workspace / "fixture.mp4"
        staged.write_bytes(b"owned media")
        result = self.run_attempt([{"filepath": str(staged), "__finaldir": str(runtime.download_dir), "title": "fixture"}],
                                  runtime=runtime, attempt_dir=str(workspace))
        records, durable = read_journal(runtime, workspace)
        self.assertIsNotNone(durable, "The result must survive loss of the worker reply or SQLite terminal write")
        self.assertEqual(durable["filepath"], result["filepath"])
        self.assertEqual(records[0].get("version"), 2)
        self.assertTrue(staged.samefile(Path(result["filepath"])), "A retained inode must prove ownership across a crash")

    def test_destination_race_cannot_replace_a_foreign_file(self):
        import os
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        staged = workspace / "fixture.mp4"
        staged.write_bytes(b"owned media")
        final = runtime.download_dir / staged.name
        original_link = os.link

        def collision(source, destination, **options):
            final.write_bytes(b"a concurrent foreign download")
            return original_link(source, destination, **options)

        with patch("backend.services.publication.os.link", side_effect=collision):
            with self.assertRaisesRegex(ValueError, "publish|exist|retained"):
                self.run_attempt([{"filepath": str(staged), "__finaldir": str(runtime.download_dir)}],
                                 runtime=runtime, attempt_dir=str(workspace))
        self.assertEqual(final.read_bytes(), b"a concurrent foreign download")
        self.assertEqual(staged.read_bytes(), b"owned media")

    def test_postprocessing_refuses_an_input_outside_the_owned_workspace(self):
        from backend.downloader import make_postprocessor_hook
        self.assertIn("workspace", inspect.signature(make_postprocessor_hook).parameters,
                      "Ownership must be checked before FFmpeg can modify an existing file")
        runtime = self.runtime()
        runtime.prepare()
        workspace = runtime.data_dir / "attempts" / str(uuid4())
        workspace.mkdir(parents=True)
        existing = runtime.download_dir / "unrelated.wav"
        existing.write_bytes(b"existing media")
        hook = make_postprocessor_hook("job", lambda *_: None, workspace=workspace)
        with self.assertRaises(ValueError):
            hook({"status": "started", "postprocessor": "FFmpegExtractAudio", "info_dict": {"filepath": str(existing)}})
        self.assertEqual(existing.read_bytes(), b"existing media")

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
                return {"title": "fixture", "_filename": str(final_file.with_suffix(".wav")), "filepath": str(final_file)}

            def add_post_processor(self, *args, **kwargs):
                pass

        with patch("backend.downloader.yt_dlp.YoutubeDL", FakeYDL):
            result = run_download(
                "job", "http://127.0.0.1:8080/fixture.wav", "best",
                str(runtime.download_dir), "fixture.%(ext)s", lambda *_: None,
                runtime, audio_only=True,
            )

        self.assertTrue(final_file.is_file())
        self.assertEqual(result["filepath"], str(final_file))
        self.assertTrue(Path(result["filepath"]).is_file())
