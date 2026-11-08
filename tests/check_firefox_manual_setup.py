"""Opt-in manual Firefox geometry checks; no VM or browser is launched."""

import unittest
import os
from pathlib import Path
import select
import subprocess
import sys
from types import SimpleNamespace

from tests import firefox_manual_setup
from tests.firefox_manual_setup import VirtualSetup


class ManualSetupGeometry(unittest.TestCase):
    def setup(self, windows):
        desktop = SimpleNamespace(snapshot=lambda: {'windows': windows})
        return VirtualSetup(desktop, None)

    def driver(self):
        geometry = {'x': 300, 'y': 405, 'width': 296, 'height': 32,
            'innerX': 0, 'innerY': 84, 'viewportWidth': 1280, 'viewportHeight': 684, 'scale': 1}
        return SimpleNamespace(script=lambda *arguments: geometry)

    def test_click_includes_the_gnome_panel_offset(self):
        setup = self.setup([{'focused': True, 'visible': True,
            'frame': {'x': 0, 'y': 32, 'width': 1280, 'height': 768}}])
        self.assertTrue(callable(getattr(setup, 'rectangle', None)),
            'Map Firefox content through the compositor window frame')
        self.assertEqual(setup.rectangle(self.driver(), '#warningButton'),
            {'x': 300, 'y': 521, 'width': 296, 'height': 32})

    def test_hidden_windows_do_not_supply_click_geometry(self):
        setup = self.setup([
            {'focused': True, 'visible': False, 'frame': {'x': 50, 'y': 50, 'width': 1280, 'height': 768}},
            {'focused': True, 'visible': True, 'frame': {'x': 0, 'y': 32, 'width': 1280, 'height': 768}}])
        self.assertTrue(callable(getattr(setup, 'rectangle', None)))
        self.assertEqual(setup.rectangle(self.driver(), '#warningButton')['y'], 521)

    def test_ambiguous_focus_is_rejected_before_reading_geometry(self):
        setup = self.setup([{'focused': True, 'visible': True}, {'focused': True, 'visible': True}])
        self.assertTrue(callable(getattr(setup, 'rectangle', None)))
        driver = SimpleNamespace(script=lambda *arguments: self.fail('Focus must be unambiguous first'))
        with self.assertRaises(AssertionError):
            setup.rectangle(driver, '#warningButton')


class ManualBrowserExit(unittest.TestCase):
    def observer(self):
        self.assertTrue(callable(getattr(firefox_manual_setup, 'browser_exited', None)),
            'Browser-exit observation must reap the owned launcher')
        return firefox_manual_setup.browser_exited

    def test_exited_launcher_is_reaped_before_proc_verification(self):
        observe = self.observer()
        process = subprocess.Popen([sys.executable, '-c', 'pass'])
        descriptor = os.pidfd_open(process.pid)
        try:
            self.assertTrue(select.select([descriptor], [], [], 5)[0])
            self.assertTrue(Path(f'/proc/{process.pid}').exists(), 'Child remains until its parent reaps it')
            self.assertTrue(observe(process, [process.pid]))
            self.assertEqual(process.returncode, 0)
            self.assertFalse(Path(f'/proc/{process.pid}').exists())
        finally:
            os.close(descriptor)
            process.wait(timeout=5)

    def test_running_launcher_has_not_exited(self):
        observe = self.observer()
        process = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])
        try:
            self.assertFalse(observe(process, [process.pid]))
            self.assertIsNone(process.returncode)
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
