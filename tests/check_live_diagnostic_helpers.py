"""Offline privacy and request-behavior checks for public probe diagnostics."""

import importlib.util
import json
from pathlib import Path
import unittest


class LiveDiagnosticHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_diagnostics.py')
        self.assertTrue(path.is_file(), 'Public diagnostic observer is missing')
        spec = importlib.util.spec_from_file_location('live_diagnostics', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.observer = self.runner.Observer()

    def test_messages_keep_only_allowlisted_signals(self):
        self.observer.message('ERROR: [youtube] private-id: Video unavailable. '
                              'https://example.invalid/?token=private-secret')
        self.observer.message('Authorization: Bearer private-secret')
        self.observer.message('Requested format is not available. private-secret')
        report = self.observer.take()
        self.assertIn('VIDEO_UNAVAILABLE', report['signals'])
        self.assertIn('FORMAT_UNAVAILABLE', report['signals'])
        self.assertNotIn('private', json.dumps(report))
        self.assertNotIn('https://', json.dumps(report))

    def test_wire_observer_calls_once_and_preserves_response(self):
        calls = []
        response = type('Response', (), {'status': 403})()
        request = object()
        def send(value):
            calls.append(value)
            return response
        self.assertIs(self.observer.send(send, request), response)
        self.assertEqual(calls, [request])
        self.assertEqual(self.observer.take()['http_status_counts'], {'403': 1})

    def test_wire_observer_preserves_exception_identity(self):
        error = RuntimeError('private-secret')
        calls = []
        def send(request):
            calls.append(request)
            raise error
        with self.assertRaises(RuntimeError) as raised:
            self.observer.send(send, None)
        self.assertIs(raised.exception, error)
        self.assertEqual(calls, [None])
        self.assertNotIn('private-secret', json.dumps(self.observer.take()))

    def test_nested_failure_keeps_typed_code_without_secret_message(self):
        from backend.services.retry_policy import RequestFailure
        error = RuntimeError('private-secret')
        error.__cause__ = RequestFailure('AUTH_REQUIRED')
        error.__context__ = error
        self.observer.error(error)
        report = self.observer.take()
        self.assertIn('AUTH_REQUIRED', report['request_failure_codes'])
        self.assertIn('RequestFailure', report['exception_types'])
        self.assertNotIn('private-secret', json.dumps(report))

    def test_rows_do_not_share_signals_or_accept_untrusted_status(self):
        self.observer.message('Video unavailable')
        response = type('Response', (), {'status': 'private-secret'})()
        self.observer.send(lambda request: response, None)
        self.assertIn('VIDEO_UNAVAILABLE', self.observer.take()['signals'])
        clean = self.observer.take()
        self.assertEqual(clean['signals'], [])
        self.assertEqual(clean['http_status_counts'], {})
        self.assertNotIn('private-secret', json.dumps(clean))


if __name__ == '__main__':
    unittest.main()
