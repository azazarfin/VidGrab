"""Opt-in guards for the separate Windows Chrome restart fixture."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class WindowsRestartTests(unittest.TestCase):
    def test_manager_withholding_keeps_api_grants_and_preference_separate(self):
        from tests import native_windows_chrome_restart as helper
        validate = getattr(helper, 'validate_manager_receipt', None)
        self.assertTrue(callable(validate), 'The actual manager withholding receipt is missing')
        before = {'request_observation_enabled': True, 'request_observation_granted': True,
            'navigation_granted': True, 'site_granted': True}
        record = {'before': before, 'withheld': {**before, 'site_granted': False}, 'restored': before.copy(),
            'counts': {'before': 1, 'withheld': 0, 'restored': 1},
            'host_access': ['ON_ALL_SITES', 'ON_CLICK', 'ON_ALL_SITES']}
        self.assertEqual(validate(record), record)
        for changed in ({**record, 'withheld': {**record['withheld'], 'request_observation_enabled': False}},
                {**record, 'counts': {**record['counts'], 'withheld': 1}},
                {**record, 'restored': {**before, 'site_granted': False}}):
            with self.assertRaises(AssertionError):
                validate(changed)

    def test_closed_profile_witness_cannot_substitute_another_launch(self):
        from tests import native_windows_chrome_restart as helper
        validate = getattr(helper, 'validate_closed_profile', None)
        self.assertTrue(callable(validate), 'The launcher-owned closed-profile witness is missing')
        profile = Path('E:/owned/profile')
        witness = {'nonce': 'selected', 'profile': str(profile), 'preferences_sha256': 'a' * 64,
            'extension_entry': {'state': 1}, 'selected_file': 'Secure Preferences',
            'records': {'Preferences': {'sha256': 'b' * 64, 'extension_entry': None},
                'Secure Preferences': {'sha256': 'a' * 64, 'extension_entry': {'state': 1}}}}
        self.assertEqual(validate(witness, 'selected', profile), witness)
        for changed in ({**witness, 'nonce': 'foreign'}, {**witness, 'profile': 'E:/foreign'},
                {**witness, 'preferences_sha256': 'invalid'}, {**witness, 'extension_entry': None},
                {**witness, 'selected_file': 'Preferences'}, {**witness, 'records': {}}):
            with self.assertRaises(AssertionError):
                validate(changed, 'selected', profile)

    def test_chooser_is_armed_before_a_new_dialog_can_be_selected(self):
        from tests import native_windows_chrome_restart as helper
        validate = getattr(helper, 'validate_install_armed', None)
        self.assertTrue(callable(validate), 'The pre-dialog readiness boundary is missing')
        expected = {'pid': 42, 'nonce': 'selected'}
        receipt = {'stage': 'armed-before-dialog', 'acknowledgement': expected.copy(), 'initial_hwnds': [1, 2]}
        self.assertEqual(validate(receipt, expected), receipt)
        for changed in ({**receipt, 'stage': 'dialog-already-open'},
                {**receipt, 'acknowledgement': {**expected, 'nonce': 'foreign'}},
                {**receipt, 'initial_hwnds': ['foreign']}):
            with self.assertRaises(AssertionError):
                validate(changed, expected)

    def test_native_folder_reply_binds_identity_path_and_choice(self):
        from tests import native_windows_chrome_restart as helper
        validate = getattr(helper, 'validate_install_reply', None)
        self.assertTrue(callable(validate), 'The bounded native folder acknowledgement is missing')
        expected = {'browser': 'chrome', 'choice': 'load-unpacked', 'nonce': 'selected',
            'pid': 42, 'creation_filetime': 123, 'program': 'chrome.exe',
            'program_sha256': 'a' * 64, 'session': 1, 'extension': 'E:/owned/extension',
            'profile': 'E:/owned/profile'}
        reply = {'result': 'passed', 'acknowledgement': expected.copy(),
            'input': 'UIAutomation Load unpacked; Win32 WM_SETTEXT and BM_CLICK',
            'target': {'pid': 50, 'browser_pid': 42, 'name': 'Select Folder'}, 'folder': expected['extension'],
            'provider': {'pid': 50, 'parent_pid': 42, 'creation_filetime': 124,
                'program': 'chrome.exe', 'program_sha256': 'a' * 64, 'session': 1,
                'profile': expected['profile']}}
        self.assertEqual(validate(reply, expected), reply)
        for field, value in [('pid', 43), ('creation_filetime', 124), ('nonce', 'foreign'),
                ('extension', 'E:/foreign/extension'), ('choice', 'allow')]:
            changed = {**reply, 'acknowledgement': {**expected, field: value}}
            with self.subTest(field=field), self.assertRaises(AssertionError):
                validate(changed, expected)
        for changed in ({**reply, 'folder': 'E:/foreign/extension'},
                {**reply, 'target': {'pid': 43, 'name': 'Select Folder'}},
                {**reply, 'result': 'failed'}, {**reply, 'input': 'API install'}):
            with self.assertRaises(AssertionError):
                validate(changed, expected)
        for field, value in [('parent_pid', 43), ('program_sha256', 'b' * 64),
                ('profile', 'E:/foreign/profile'), ('session', 0), ('creation_filetime', 122)]:
            with self.subTest(provider_field=field), self.assertRaises(AssertionError):
                validate({**reply, 'provider': {**reply['provider'], field: value}}, expected)

    def test_restart_descriptor_cannot_select_another_profile_or_executable(self):
        from tests import native_windows_chrome_restart as helper
        validate = getattr(helper, 'validate_relaunch', None)
        self.assertTrue(callable(validate), 'The same-profile relaunch guard is missing')
        original = {'nonce': 'first', 'pid': 42, 'creation_filetime': 123,
            'program': 'chrome.exe', 'program_sha256': 'a' * 64, 'session': 1}
        profile = Path('E:/owned/profile')
        restarted = {**original, 'nonce': 'second', 'pid': 43, 'creation_filetime': 124,
            'command': ['chrome.exe', '--user-data-dir=' + str(profile)]}
        validate(original, restarted, profile)
        for changed in ({**restarted, 'pid': 42, 'creation_filetime': 123},
                {**restarted, 'program_sha256': 'b' * 64},
                {**restarted, 'session': 0}, {**restarted, 'command': ['chrome.exe']}):
            with self.assertRaises(AssertionError):
                validate(original, changed, profile)


if __name__ == '__main__':
    unittest.main(verbosity=2)
