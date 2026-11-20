import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
import zipfile

from tests.support import OfflineTestCase

ROOT = Path(__file__).resolve().parents[1]


def load_packager():
    path = ROOT / "packaging" / "package_extension.py"
    spec = importlib.util.spec_from_file_location("vidgrab_package_extension", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtensionPackagingTests(OfflineTestCase):
    def test_chrome_and_firefox_archives_strip_foreign_manifests_and_junk(self):
        pkg = load_packager()
        stage_src = self.root / "ext-src"
        # Mirror real extension sources and add junk that must be excluded.
        import shutil
        shutil.copytree(ROOT / "extension", stage_src)
        (stage_src / ".DS_Store").write_bytes(b"junk")
        (stage_src / "popup" / "popup.js.bak").write_text("backup")
        (stage_src / "__pycache__").mkdir()
        (stage_src / "__pycache__" / "junk.pyc").write_bytes(b"pyc")
        (stage_src / "tests").mkdir()
        (stage_src / "tests" / "spec.js").write_text("test")

        out = self.root / "pkg-out"
        receipt = pkg.package_extensions(stage_src, out)

        self.assertEqual(receipt["chrome_origin"], "chrome-extension://ikjpekhbbaehdhigakbahlhafaojbjpm/")
        self.assertEqual(receipt["firefox_id"], "vidgrab@azaz.local")

        chrome_zip = out / "vidgrab-chrome-extension.zip"
        firefox_zip = out / "vidgrab-firefox-extension.zip"
        firefox_xpi = out / "vidgrab-firefox-extension.xpi"
        sums_file = out / "SHA256SUMS.txt"

        self.assertTrue(chrome_zip.is_file())
        self.assertTrue(firefox_zip.is_file())
        self.assertTrue(firefox_xpi.is_file())
        self.assertTrue(sums_file.is_file())
        self.assertEqual(firefox_zip.read_bytes(), firefox_xpi.read_bytes())

        with zipfile.ZipFile(chrome_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("fonts/LICENSE.txt", names)
            self.assertIn("background/service-worker.js", names)
            self.assertNotIn("manifest.firefox.json", names)
            self.assertFalse(any(name.endswith(".bak") or name.endswith(".pyc") or ".DS_Store" in name or name.startswith("tests/") for name in names))
            chrome_manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(chrome_manifest["manifest_version"], 3)
            self.assertIn("nativeMessaging", chrome_manifest["permissions"])
            self.assertIn("webRequest", chrome_manifest["optional_permissions"])
            self.assertNotIn("browser_specific_settings", chrome_manifest)
            self.assertIn("service_worker", chrome_manifest["background"])

        with zipfile.ZipFile(firefox_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("fonts/LICENSE.txt", names)
            self.assertNotIn("manifest.firefox.json", names)
            firefox_manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(firefox_manifest["manifest_version"], 3)
            self.assertEqual(firefox_manifest["browser_specific_settings"]["gecko"]["id"], "vidgrab@azaz.local")
            self.assertIn("scripts", firefox_manifest["background"])
            self.assertNotIn("key", firefox_manifest)

        # Verify SHA256SUMS matches actual archive bytes.
        parsed_sums = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            digest, filename = line.split("  ", 1)
            parsed_sums[filename] = digest
        self.assertEqual(parsed_sums["vidgrab-chrome-extension.zip"], hashlib.sha256(chrome_zip.read_bytes()).hexdigest())
        self.assertEqual(parsed_sums["vidgrab-firefox-extension.zip"], hashlib.sha256(firefox_zip.read_bytes()).hexdigest())
        self.assertEqual(parsed_sums["vidgrab-firefox-extension.xpi"], hashlib.sha256(firefox_xpi.read_bytes()).hexdigest())

    def test_packaging_is_deterministic_across_independent_runs(self):
        pkg = load_packager()
        out1 = self.root / "run-1"
        out2 = self.root / "run-2"
        r1 = pkg.package_extensions(ROOT / "extension", out1)
        r2 = pkg.package_extensions(ROOT / "extension", out2)
        self.assertEqual(r1, r2)
        for name in ("vidgrab-chrome-extension.zip", "vidgrab-firefox-extension.zip", "vidgrab-firefox-extension.xpi", "SHA256SUMS.txt"):
            self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes())

    def test_refuses_existing_output_directory_and_tracked_dist(self):
        pkg = load_packager()
        existing = self.root / "already-here"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("preserved")
        with self.assertRaisesRegex(ValueError, "fresh output directory"):
            pkg.package_extensions(ROOT / "extension", existing)
        self.assertEqual(marker.read_text(), "preserved")

        if (ROOT / "dist").exists():
            with self.assertRaisesRegex(ValueError, "fresh output directory"):
                pkg.package_extensions(ROOT / "extension", ROOT / "dist")

    def test_refuses_symlink_in_extension_sources(self):
        pkg = load_packager()
        stage_src = self.root / "ext-symlink"
        import shutil
        shutil.copytree(ROOT / "extension", stage_src)
        target = self.root / "outside.txt"
        target.write_text("secret")
        try:
            (stage_src / "leak.txt").symlink_to(target)
        except OSError:
            self.skipTest("Symlinks require privilege on this filesystem")
        with self.assertRaisesRegex(ValueError, "symlink"):
            pkg.package_extensions(stage_src, self.root / "out")

    def test_store_id_and_key_overrides(self):
        pkg = load_packager()
        out_store = self.root / "store-out"
        receipt = pkg.package_extensions(
            ROOT / "extension",
            out_store,
            omit_chrome_key=True,
            chrome_origin_override="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
            firefox_id="vidgrab-store@addons.example.org",
        )
        self.assertEqual(receipt["chrome_origin"], "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/")
        self.assertEqual(receipt["firefox_id"], "vidgrab-store@addons.example.org")
        with zipfile.ZipFile(out_store / "vidgrab-chrome-extension.zip") as zf:
            cm = json.loads(zf.read("manifest.json"))
            self.assertNotIn("key", cm)
        with zipfile.ZipFile(out_store / "vidgrab-firefox-extension.xpi") as zf:
            fm = json.loads(zf.read("manifest.json"))
            self.assertEqual(fm["browser_specific_settings"]["gecko"]["id"], "vidgrab-store@addons.example.org")

    def test_build_sh_delegates_without_modifying_tracked_dist(self):
        if os.name == "nt":
            self.skipTest("POSIX bash check")
        dist_files = {
            path.relative_to(ROOT / "dist"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "dist").rglob("*") if path.is_file()
        }
        out = self.root / "build-sh-out"
        from unittest.mock import patch
        with patch("subprocess.Popen.__init__", self.original_popen_init):
            subprocess.run(["bash", str(ROOT / "build.sh"), str(out)], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertIn("SHA256SUMS", json.dumps(list(p.name for p in out.iterdir())))
        self.assertTrue((out / "vidgrab-chrome-extension.zip").is_file())
        self.assertTrue((out / "vidgrab-firefox-extension.xpi").is_file())
        after_dist = {
            path.relative_to(ROOT / "dist"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "dist").rglob("*") if path.is_file()
        }
        self.assertEqual(dist_files, after_dist)


if __name__ == "__main__":
    unittest.main()
