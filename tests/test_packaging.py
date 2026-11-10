import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile

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


if __name__ == "__main__":
    import unittest
    unittest.main()
