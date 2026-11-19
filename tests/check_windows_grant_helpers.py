"""Opt-in regressions for the Windows native permission input boundary."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class WindowsPermissionInputTests(unittest.TestCase):
    def test_reply_reservation_preserves_an_existing_foreign_file(self):
        from tests import native_windows_grant_input
        reserve = getattr(native_windows_grant_input, 'reserve_reply', None)
        self.assertTrue(callable(reserve), 'The unelevated reply reservation is missing')
        with tempfile.TemporaryDirectory() as directory:
            reply = Path(directory) / 'reply.json'
            reserve(reply)
            self.assertEqual(reply.read_text(), '{}')
            reply.write_text('foreign bytes')
            with self.assertRaises(FileExistsError):
                reserve(reply)
            self.assertEqual(reply.read_text(), 'foreign bytes')

    def test_an_elevated_browser_hash_requires_the_exact_owned_creation_identity(self):
        from tests import native_windows_grant_input
        select_hash = getattr(native_windows_grant_input, 'owned_browser_hash', None)
        self.assertTrue(callable(select_hash), 'The elevated browser hash boundary is missing')
        audit = {'pid': 42, 'creation_filetime': 123456, 'program': 'C:/Chrome/chrome.exe',
            'program_sha256': 'a' * 64}
        self.assertEqual(select_hash(42, 123456, audit['program'], audit), 'a' * 64)
        for pid, created, program in [(43, 123456, audit['program']),
                (42, 123457, audit['program']), (42, 123456, 'C:/Other/chrome.exe')]:
            with self.subTest(pid=pid, created=created, program=program), self.assertRaises(AssertionError):
                select_hash(pid, created, program, audit)

    def test_native_reply_requires_the_exact_process_creation_and_requested_choice(self):
        from tests.native_windows_grant_input import validate_receipt
        expected = {'browser': 'firefox', 'choice': 'deny', 'nonce': 'owned-request',
            'pid': 42, 'creation_filetime': 123456, 'program': 'C:/Firefox/firefox.exe',
            'program_sha256': 'a' * 64, 'session': 1}
        reply = {'result': 'passed', 'acknowledgement': expected,
            'input': 'UIAutomation InvokePattern', 'target': {'name': "Don't Allow", 'pid': 42}}
        self.assertEqual(validate_receipt(reply, expected), reply)
        for field, foreign in [('browser', 'chrome'), ('choice', 'allow'),
                ('nonce', 'foreign'), ('pid', 43), ('creation_filetime', 123457),
                ('program', 'C:/Other/firefox.exe'), ('program_sha256', 'b' * 64), ('session', 2)]:
            with self.subTest(field=field), self.assertRaises(AssertionError):
                validate_receipt({**reply, 'acknowledgement': {**expected, field: foreign}}, expected)

    def test_failed_or_programmatic_permission_reply_is_refused(self):
        from tests.native_windows_grant_input import validate_receipt
        expected = {'nonce': 'owned-request', 'choice': 'allow'}
        reply = {'result': 'passed', 'acknowledgement': expected,
            'input': 'UIAutomation InvokePattern', 'target': {'name': 'Allow'}}
        for invalid in [None, [], {}, {**reply, 'result': 'failed'},
                {**reply, 'input': 'permissions.request'}, {**reply, 'target': None},
                {**reply, 'acknowledgement': {**expected, 'extra': True}}]:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                validate_receipt(invalid, expected)

    def test_an_owned_acknowledgement_cannot_hide_a_different_native_button(self):
        from tests.native_windows_grant_input import validate_receipt
        expected = {'nonce': 'owned-request', 'choice': 'deny', 'pid': 42}
        reply = {'result': 'passed', 'acknowledgement': expected,
            'input': 'UIAutomation InvokePattern', 'target': {'name': 'Deny', 'pid': 42}}
        self.assertEqual(validate_receipt(reply, expected), reply)
        for target in [{'name': 'Allow', 'pid': 42}, {'name': 'Deny', 'pid': 43}]:
            with self.subTest(target=target), self.assertRaises(AssertionError):
                validate_receipt({**reply, 'target': target}, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
