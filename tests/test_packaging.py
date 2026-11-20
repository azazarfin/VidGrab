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
        notices = bundle / "engine" / "_internal" / "notices"
        tools.mkdir(parents=True)
        notices.mkdir(parents=True)
        (tools / "manifest.json").write_text("{}")
        (tools / "ffmpeg").write_bytes(b"bundled-ffmpeg")
        for notice in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt"):
            (notices / notice).write_text(f"notice:{notice}\n", encoding="utf-8")
        (bundle / "engine" / "vidgrab-engine").write_bytes(b"frozen-engine")
        (bundle / "vidgrab-native-host").write_bytes(b"native-host")
        (bundle / "vidgrab-native-host").chmod(0o755)
        tauri = self.root / "tauri"
        preparer.prepare(bundle, tauri)
        self.assertEqual((tauri / "resources" / "engine" / "_internal" / "tools" / "ffmpeg").read_bytes(), b"bundled-ffmpeg")
        for notice in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt"):
            self.assertEqual((tauri / "resources" / "notices" / notice).read_text(encoding="utf-8"), f"notice:{notice}\n")
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
        (tools / "GPLv3.ffmpeg.txt").write_text("GPL-3.0\n")
        (bundle / "engine" / "vidgrab-engine.exe").write_bytes(b"windows-engine")
        (bundle / "vidgrab-native-host.exe").write_bytes(b"windows-bridge")
        tauri = self.root / "tauri"
        try:
            preparer.prepare(bundle, tauri, target="x86_64-pc-windows-msvc")
        except TypeError as error:
            self.fail(f"Windows target preparation is missing: {error}")
        self.assertEqual((tauri / "binaries" / "vidgrab-native-host-x86_64-pc-windows-msvc.exe").read_bytes(), b"windows-bridge")
        self.assertEqual((tauri / "resources" / "engine" / "_internal" / "tools" / "ffmpeg.exe").read_bytes(), b"windows-ffmpeg")

    def test_desktop_preparation_requires_every_notice_before_writes(self):
        preparer = self.preparer()
        # Isolate repository fallback candidates so all four missing cases execute.
        preparer.ROOT = self.root / "empty-repository"
        bundle = self.root / "notice-bundle"
        tools = bundle / "engine" / "_internal" / "tools"
        notices = bundle / "engine" / "_internal" / "notices"
        tools.mkdir(parents=True)
        notices.mkdir()
        (tools / "manifest.json").write_text("{}")
        (bundle / "engine" / "vidgrab-engine").write_bytes(b"frozen-engine")
        (bundle / "vidgrab-native-host").write_bytes(b"native-host")
        names = ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt")
        for missing in names:
            with self.subTest(missing=missing):
                for name in names:
                    path = notices / name
                    if name == missing:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(f"notice:{name}\n")
                tauri = self.root / f"tauri-{missing}"
                with self.assertRaisesRegex(ValueError, missing):
                    preparer.prepare(bundle, tauri)
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

    def test_rpm_bundle_config_declares_capabilities_and_notices(self):
        import json
        import xml.etree.ElementTree as ET

        repo = Path(__file__).resolve().parents[1]
        tauri = json.loads((repo / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        ubuntu = json.loads((repo / "packaging" / "ubuntu24.bundle.json").read_text(encoding="utf-8"))
        self.assertIn("rpm", tauri["bundle"]["targets"])
        self.assertEqual(tauri["bundle"]["license"], "MIT")
        rpm_cfg = tauri["bundle"]["linux"]["rpm"]
        ubuntu_rpm = ubuntu["bundle"]["linux"]["rpm"]
        for cap in (
            "libwebkit2gtk-4.1.so.0()(64bit)",
            "libgtk-3.so.0()(64bit)",
            "libayatana-appindicator3.so.1()(64bit)",
            "libsoup-3.0.so.0()(64bit)",
            "libcrypto.so.3()(64bit)",
        ):
            self.assertIn(cap, rpm_cfg["depends"])
            self.assertIn(cap, ubuntu_rpm["depends"])
        self.assertIn("libc.so.6(GLIBC_2.39)(64bit)", ubuntu_rpm["depends"])
        for notice in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt"):
            self.assertEqual(rpm_cfg["files"][f"/usr/share/doc/vidgrab/{notice}"], f"resources/notices/{notice}")
        metainfo_rel = rpm_cfg["files"]["/usr/share/metainfo/com.vidgrab.desktop.metainfo.xml"]
        metainfo_path = (repo / "desktop" / "src-tauri" / metainfo_rel).resolve()
        self.assertTrue(metainfo_path.is_file())
        component = ET.parse(metainfo_path).getroot()
        self.assertEqual(component.tag, "component")
        self.assertEqual(component.get("type"), "desktop-application")
        self.assertEqual(component.findtext("id"), "com.vidgrab.desktop")
        self.assertEqual(component.findtext("project_license"), "MIT")


if __name__ == "__main__":
    import unittest
    unittest.main()
