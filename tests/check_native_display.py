"""Opt-in local display-helper regressions; no X server or browser is launched."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_support import virtual_display, wait_for


class VirtualDisplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vidgrab-display-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def display_fixture(self, body):
        executable = self.root / "fake-xvfb"
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        return executable

    def test_compiler_warnings_cannot_block_display_clients(self):
        ready = self.root / "compiler-finished"
        executable = self.display_fixture(f"""import os, signal, sys
from pathlib import Path
print('41', flush=True)
sys.stderr.write('keyboard compiler warning\\n' * 16384)
sys.stderr.flush()
Path({str(ready)!r}).write_text(str(os.getpid()))
signal.pause()
""")
        with virtual_display(executable) as environment:
            self.assertEqual(environment["DISPLAY"], ":41")
            wait_for(ready.exists, timeout=2, label="keyboard compiler stderr completion")
            pid = int(ready.read_text())
        self.assertFalse(Path(f"/proc/{pid}").exists(), "The display fixture must be reaped")

    def test_startup_failure_retains_display_errors(self):
        executable = self.display_fixture("import sys\nsys.stderr.write('isolated display could not start\\n')\nsys.exit(1)\n")
        with self.assertRaisesRegex(AssertionError, "isolated display could not start"):
            with virtual_display(executable):
                self.fail("A failed display must not be yielded")


if __name__ == "__main__":
    unittest.main()
