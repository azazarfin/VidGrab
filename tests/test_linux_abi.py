import importlib.util
from pathlib import Path

from tests.support import OfflineTestCase


class LinuxAbiTests(OfflineTestCase):
    def auditor(self):
        path = Path(__file__).resolve().parents[1] / "packaging" / "check_abi.py"
        self.assertTrue(path.is_file(), "The clean Linux build needs a glibc ceiling check before bundling")
        spec = importlib.util.spec_from_file_location("vidgrab_abi_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def elf(self, name):
        path = self.root / "bundle" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELFfixture")
        return path

    def test_rejects_a_bundled_library_above_the_selected_glibc_ceiling(self):
        auditor = self.auditor()
        self.elf("engine/vidgrab-engine")
        newer = self.elf("engine/_internal/libtinfo.so.6")
        def inspect(path):
            return "Name: GLIBC_2.42" if path == newer else "Name: GLIBC_2.39"
        with self.assertRaisesRegex(ValueError, r"libtinfo\.so\.6.*2\.42.*2\.39"):
            auditor.audit_tree(self.root / "bundle", max_glibc=(2, 39), inspect=inspect)

    def test_compares_versions_numerically_and_counts_a_symlinked_elf_once(self):
        auditor = self.auditor()
        older = self.elf("old.so")
        self.elf("engine")
        (older.parent / "alias.so").symlink_to(older.name)
        def inspect(path):
            return "Name: GLIBC_2.9" if path == older else "Name: GLIBC_2.39"
        result = auditor.audit_tree(older.parent, max_glibc=(2, 39), inspect=inspect)
        self.assertEqual(result["elf_files"], 2)
        self.assertEqual(result["required_glibc"], "2.39")

    def test_refuses_a_symlink_outside_the_bundle_without_inspecting_it(self):
        auditor = self.auditor()
        bundle = self.root / "bundle"
        bundle.mkdir()
        outside = self.root / "unshipped.so"
        outside.write_bytes(b"\x7fELFfixture")
        (bundle / "library.so").symlink_to(outside)
        inspected = []
        with self.assertRaisesRegex(ValueError, "outside"):
            auditor.audit_tree(bundle, inspect=lambda path: inspected.append(path))
        self.assertEqual(inspected, [])

    def test_refuses_an_empty_tree_instead_of_certifying_a_missing_bundle(self):
        auditor = self.auditor()
        bundle = self.root / "empty"
        bundle.mkdir()
        with self.assertRaisesRegex(ValueError, "ELF"):
            auditor.audit_tree(bundle, max_glibc=(2, 39), inspect=lambda path: "")
