import importlib.util
from pathlib import Path

from tests.support import OfflineTestCase


class AppImagePreparationTests(OfflineTestCase):
    def preparer(self):
        path = Path(__file__).resolve().parents[1] / "packaging" / "prepare_appimage.py"
        self.assertTrue(path.is_file(), "AppImage deployment must preserve the verified frozen engine")
        spec = importlib.util.spec_from_file_location("vidgrab_appimage_preparation", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def inputs(self):
        appdir = self.root / "deployed.AppDir"
        deployed = appdir / "usr/lib/VidGrab/engine"
        staged = self.root / "bundle/engine"
        for engine in (deployed, staged):
            (engine / "_internal/tools").mkdir(parents=True)
            (engine / "vidgrab-engine").write_bytes(b"frozen engine")
            (engine / "_internal/tools/manifest.json").write_text("{}")
            for name in ("deno", "ffmpeg", "ffprobe"):
                (engine / "_internal/tools" / name).write_bytes(b"verified tool")
        desktop = appdir / "usr/bin/vidgrab-desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_bytes(b"deployed desktop")
        return appdir, staged.parent, deployed, staged

    def test_restores_original_engine_bytes_after_deployment(self):
        preparer = self.preparer()
        appdir, bundle, deployed, staged = self.inputs()
        (deployed / "_internal/tools/deno").write_bytes(b"tool modified by patchelf")
        (deployed / "_internal/injected.so").write_bytes(b"deployment leftover")
        (staged / "_internal/library.so").write_bytes(b"original library")
        (staged / "_internal/alias.so").symlink_to("library.so")
        output = self.root / "final.AppDir"
        engine = preparer.prepare(appdir, bundle, output)
        self.assertEqual(engine, output / "usr/lib/VidGrab/engine/vidgrab-engine")
        self.assertEqual((engine.parent / "_internal/tools/deno").read_bytes(), b"verified tool")
        self.assertFalse((engine.parent / "_internal/injected.so").exists())
        self.assertTrue((engine.parent / "_internal/alias.so").is_symlink())
        self.assertEqual((output / "usr/bin/vidgrab-desktop").read_bytes(), b"deployed desktop")
        self.assertEqual((deployed / "_internal/tools/deno").read_bytes(), b"tool modified by patchelf")

    def test_refuses_an_existing_output_without_modifying_it(self):
        preparer = self.preparer()
        appdir, bundle, _, _ = self.inputs()
        output = self.root / "existing.AppDir"
        output.mkdir()
        marker = output / "preserved"
        marker.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "exist"):
            preparer.prepare(appdir, bundle, output)
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_refuses_incomplete_tools_before_creating_output(self):
        preparer = self.preparer()
        appdir, bundle, _, staged = self.inputs()
        (staged / "_internal/tools/deno").unlink()
        output = self.root / "final.AppDir"
        with self.assertRaisesRegex(ValueError, "complete"):
            preparer.prepare(appdir, bundle, output)
        self.assertFalse(output.exists())

    def test_refuses_a_dangling_output_symlink(self):
        preparer = self.preparer()
        appdir, bundle, _, _ = self.inputs()
        output = self.root / "linked.AppDir"
        target = self.root / "unexpected.AppDir"
        output.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "exist"):
            preparer.prepare(appdir, bundle, output)
        self.assertFalse(target.exists())

    def test_refuses_output_nested_inside_inputs(self):
        preparer = self.preparer()
        appdir, bundle, _, _ = self.inputs()
        output = appdir / "nested.AppDir"
        with self.assertRaisesRegex(ValueError, "overlap"):
            preparer.prepare(appdir, bundle, output)
        self.assertFalse(output.exists())

    def test_refuses_ambiguous_deployed_engines(self):
        preparer = self.preparer()
        appdir, bundle, _, _ = self.inputs()
        (appdir / "another").mkdir()
        (appdir / "another/vidgrab-engine").write_bytes(b"second engine")
        output = self.root / "final.AppDir"
        with self.assertRaisesRegex(ValueError, "one"):
            preparer.prepare(appdir, bundle, output)
        self.assertFalse(output.exists())

    def test_refuses_a_deployed_engine_symlink_outside_the_appdir(self):
        preparer = self.preparer()
        appdir, bundle, deployed, staged = self.inputs()
        binary = deployed / "vidgrab-engine"
        binary.unlink()
        binary.symlink_to(staged / "vidgrab-engine")
        output = self.root / "final.AppDir"
        with self.assertRaisesRegex(ValueError, "outside"):
            preparer.prepare(appdir, bundle, output)
        self.assertFalse(output.exists())
