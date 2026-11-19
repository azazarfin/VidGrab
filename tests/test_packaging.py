import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import zipfile

from tests.support import OfflineTestCase


class ComponentPackagingTests(OfflineTestCase):
    def preparer(self):
        path = Path(__file__).resolve().parents[1] / "packaging" / "prepare_desktop.py"
        self.assertTrue(path.is_file(), "P02 needs repeatable Tauri resource preparation")
        spec = importlib.util.spec_from_file_location("vidgrab_desktop_preparer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_desktop_preparation_copies_the_complete_engine_support_tree(self):
        preparer = self.preparer()
        bundle = self.root / "bundle"
        tools = bundle / "engine" / "_internal" / "tools"
        tools.mkdir(parents=True)
        (tools / "manifest.json").write_text("{}")
        (tools / "ffmpeg").write_bytes(b"bundled-ffmpeg")
        (bundle / "engine" / "vidgrab-engine").write_bytes(b"frozen-engine")
        (bundle / "vidgrab-native-host").write_bytes(b"native-host")
        (bundle / "vidgrab-native-host").chmod(0o755)
        tauri = self.root / "tauri"
        preparer.prepare(bundle, tauri)
        self.assertEqual((tauri / "resources" / "engine" / "_internal" / "tools" / "ffmpeg").read_bytes(), b"bundled-ffmpeg")
        native = tauri / "binaries" / "vidgrab-native-host-x86_64-unknown-linux-gnu"
        self.assertEqual(native.read_bytes(), b"native-host")
        if os.name != "nt":
            self.assertTrue(native.stat().st_mode & 0o111)

    def test_desktop_preparation_refuses_missing_inputs_before_writes(self):
        preparer = self.preparer()
        tauri = self.root / "tauri"
        with self.assertRaisesRegex(ValueError, "bundle"):
            preparer.prepare(self.root / "missing", tauri)
        self.assertFalse(tauri.exists())

    def test_windows_preparation_preserves_support_files_and_target_executable_suffix(self):
        preparer = self.preparer()
        bundle = self.root / "windows-bundle"
        tools = bundle / "engine" / "_internal" / "tools"
        tools.mkdir(parents=True)
        (tools / "manifest.json").write_text("{}")
        (tools / "ffmpeg.exe").write_bytes(b"windows-ffmpeg")
        (bundle / "engine" / "vidgrab-engine.exe").write_bytes(b"windows-engine")
        (bundle / "vidgrab-native-host.exe").write_bytes(b"windows-bridge")
        tauri = self.root / "tauri"
        try:
            preparer.prepare(bundle, tauri, target="x86_64-pc-windows-msvc")
        except TypeError as error:
            self.fail(f"Windows target preparation is missing: {error}")
        self.assertEqual((tauri / "binaries" / "vidgrab-native-host-x86_64-pc-windows-msvc.exe").read_bytes(), b"windows-bridge")
        self.assertEqual((tauri / "resources" / "engine" / "_internal" / "tools" / "ffmpeg.exe").read_bytes(), b"windows-ffmpeg")

    def test_desktop_preparation_refuses_overlapping_source_and_destination(self):
        preparer = self.preparer()
        bundle = self.root / "bundle"
        with self.assertRaisesRegex(ValueError, "overlap"):
            preparer.prepare(bundle, bundle / "tauri")
        self.assertFalse(bundle.exists())

    def fetcher(self):
        path = Path(__file__).resolve().parents[1] / "packaging" / "fetch_components.py"
        self.assertTrue(path.is_file(), "P02 needs a checksum-enforcing component staging command")
        spec = importlib.util.spec_from_file_location("vidgrab_component_fetcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_corrupted_cache_is_rejected_without_network(self):
        fetcher = self.fetcher()
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "archive").write_bytes(b"changed")
        item = {"file": "archive", "sha256": hashlib.sha256(b"original").hexdigest(), "url": "https://example.com"}
        with self.assertRaisesRegex(ValueError, "checksum"):
            fetcher.cached_asset(item, cache, offline=True)

    def test_only_selected_regular_files_are_extracted(self):
        fetcher = self.fetcher()
        archive = self.root / "fixture.tar.xz"
        with tarfile.open(archive, "w:xz") as output:
            for name, contents in (("bundle/ffmpeg", b"fake-ffmpeg"), ("bundle/ffprobe", b"fake-ffprobe"), ("bundle/GPLv3.txt", b"notice"), ("../../escaped", b"unwanted")):
                entry = tarfile.TarInfo(name)
                entry.size = len(contents)
                output.addfile(entry, io.BytesIO(contents))
        directory = self.root / "tools"
        directory.mkdir()
        fetcher.extract_ffmpeg(archive, directory)
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["GPLv3.ffmpeg.txt", "ffmpeg", "ffprobe"])
        self.assertFalse((self.root.parent / "escaped").exists())

    def test_btbn_tar_extracts_binaries_and_license_txt(self):
        fetcher = self.fetcher()
        archive = self.root / "btbn.tar.xz"
        with tarfile.open(archive, "w:xz") as output:
            for name, contents in (
                ("ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1/bin/ffmpeg", b"btbn-ffmpeg"),
                ("ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1/bin/ffprobe", b"btbn-ffprobe"),
                ("ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1/LICENSE.txt", b"GPLv3-btbn"),
                ("ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1/doc/ffplay.html", b"ignored"),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(contents)
                output.addfile(entry, io.BytesIO(contents))
        directory = self.root / "tools"
        directory.mkdir()
        fetcher.extract_ffmpeg(archive, directory)
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["GPLv3.ffmpeg.txt", "ffmpeg", "ffprobe"])
        self.assertEqual((directory / "GPLv3.ffmpeg.txt").read_bytes(), b"GPLv3-btbn")
        self.assertEqual((directory / "ffmpeg").read_bytes(), b"btbn-ffmpeg")
        self.assertEqual((directory / "ffprobe").read_bytes(), b"btbn-ffprobe")

    def test_linux_tar_rejects_ambiguous_or_missing_license(self):
        fetcher = self.fetcher()
        for label, entries in (
            ("missing", (("bin/ffmpeg", b"f"), ("bin/ffprobe", b"p"))),
            ("ambiguous", (("bin/ffmpeg", b"f"), ("bin/ffprobe", b"p"), ("LICENSE.txt", b"a"), ("GPLv3.txt", b"b"))),
        ):
            archive = self.root / f"{label}.tar.xz"
            with tarfile.open(archive, "w:xz") as output:
                for name, contents in entries:
                    entry = tarfile.TarInfo(name)
                    entry.size = len(contents)
                    output.addfile(entry, io.BytesIO(contents))
            directory = self.root / f"tools-{label}"
            directory.mkdir()
            with self.assertRaisesRegex(ValueError, "Expected one regular"):
                fetcher.extract_ffmpeg(archive, directory)

    def test_windows_zip_extracts_only_selected_tools_and_license(self):
        fetcher = self.fetcher()
        archive = self.root / "windows.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name, contents in (("build/bin/ffmpeg.exe", b"ffmpeg"), ("build/bin/ffprobe.exe", b"ffprobe"),
                                   ("build/LICENSE", b"GPLv3"), ("../../escaped", b"ignored")):
                output.writestr(name, contents)
        directory = self.root / "tools"
        directory.mkdir()
        try:
            fetcher.extract_ffmpeg(archive, directory, target="x86_64-pc-windows-msvc")
        except TypeError as error:
            self.fail(f"Windows archive support is missing: {error}")
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["GPLv3.ffmpeg.txt", "ffmpeg.exe", "ffprobe.exe"])
        self.assertFalse((self.root.parent / "escaped").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
