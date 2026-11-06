"""Opt-in tray-protocol regressions for the GNOME desktop check."""

import unittest

from tests.check_wayland_desktop import Desktop


class TrayAddresses(unittest.TestCase):
    def check_address(self, registration, expected_name, expected_path, title='VidGrab'):
        desktop = Desktop.__new__(Desktop)
        calls = []

        def call(name, path, interface, method, signature, arguments):
            calls.append((name, path))
            if method == 'Get':
                return ([registration],)
            self.assertEqual((name, path), (expected_name, expected_path))
            return ({'Title': title, 'Menu': '/Menu'},)

        desktop.call = call
        tray = desktop.tray()
        self.assertIsNotNone(tray)
        self.assertEqual(tray['name'], expected_name)
        self.assertEqual(tray['path'], expected_path)
        self.assertEqual(len(calls), 2)

    def test_gnome_appindicator_registration(self):
        self.check_address(':1.42@/org/ayatana/NotificationItem/vidgrab',
            ':1.42', '/org/ayatana/NotificationItem/vidgrab')

    def test_service_and_path_registration(self):
        self.check_address(':1.42/StatusNotifierItem', ':1.42', '/StatusNotifierItem')

    def test_service_registration_uses_default_path(self):
        self.check_address('org.example.Tray', 'org.example.Tray', '/StatusNotifierItem')

    def test_linux_tray_title_uses_executable_name(self):
        self.check_address(':1.57@/org/ayatana/NotificationItem/tray_icon_tray_app_12351_1',
            ':1.57', '/org/ayatana/NotificationItem/tray_icon_tray_app_12351_1', 'vidgrab-desktop')


if __name__ == '__main__':
    unittest.main(verbosity=2)
