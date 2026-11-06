"""Opt-in regression for retaining package-command failure diagnostics."""

import subprocess
import sys
import unittest

from tests.check_package_installation import run


class PackageCommandDiagnostics(unittest.TestCase):
    def test_failed_command_keeps_both_output_streams(self):
        with self.assertRaises(RuntimeError) as failure:
            run([sys.executable, '-c',
                "import sys; print('package stdout'); print('package stderr', file=sys.stderr); sys.exit(7)"])
        self.assertIn('package stdout', str(failure.exception))
        self.assertIn('package stderr', str(failure.exception))
        self.assertIsInstance(failure.exception.__cause__, subprocess.CalledProcessError)
        self.assertEqual(failure.exception.__cause__.returncode, 7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
