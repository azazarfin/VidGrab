"""Offline, opt-in checks for redacted live compatibility evidence."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class LiveSiteHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_sites.py')
        self.assertTrue(path.is_file(), 'The opt-in live compatibility runner is missing')
        spec = importlib.util.spec_from_file_location('live_sites', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def case(self, **changes):
        return {'id': 'sample', 'media_type': 'direct', 'authentication': 'public',
                'url': 'https://example.invalid/media?token=private-secret',
                'expected': 'media', **changes}

    def test_reports_omit_urls_titles_entries_and_exception_messages(self):
        row = self.runner.summarize_result(self.case(), {'ok': True, 'data': {
            'url': self.case()['url'], 'title': 'private-secret', 'is_playlist': False,
            'formats': [{'url': self.case()['url']}], 'format_count': 1,
            'entries': [{'title': 'private-secret'}], 'entry_count': 0}}, 1.0)
        self.assertEqual(row['assessment'], 'matched')
        self.assertEqual(row['format_count'], 1)
        self.assertNotIn('private-secret', json.dumps(row))
        failed = self.runner.summarize_result(self.case(), {'ok': False, 'error': {
            'code': 'NETWORK_ERROR', 'message': self.case()['url'],
            'details': {'cookies': 'private-secret'}}}, 2.0)
        self.assertEqual(failed['error_category'], 'NETWORK_ERROR')
        self.assertNotIn('private-secret', json.dumps(failed))

    def test_empty_formats_do_not_count_as_successful_media_compatibility(self):
        row = self.runner.summarize_result(self.case(), {'ok': True, 'data': {
            'is_playlist': False, 'format_count': 0, 'entry_count': 0}}, 1.0)
        self.assertEqual(row['assessment'], 'mismatch')
        self.assertEqual(row['error_category'], 'EMPTY_METADATA')

    def test_unsupported_expectation_requires_the_specific_error_category(self):
        for code, assessment in [('UNSUPPORTED_MEDIA', 'matched'), ('NETWORK_ERROR', 'mismatch')]:
            with self.subTest(code=code):
                row = self.runner.summarize_result(self.case(expected='unsupported'),
                    {'ok': False, 'error': {'code': code}}, 1.0)
                self.assertEqual(row['assessment'], assessment)

    def test_cases_reject_credentials_duplicate_ids_and_signed_in_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'cases.json'
            for cases in [[self.case(url='https://user:secret@example.invalid/media')],
                          [self.case(), self.case()], [self.case(authentication='signed-in')],
                          [self.case(cookies=[{'value': 'private-secret'}])],
                          [self.case(url='file:///tmp/media')]]:
                with self.subTest(cases=cases):
                    path.write_text(json.dumps(cases), encoding='utf-8')
                    with self.assertRaises(ValueError) as raised:
                        self.runner.load_cases(path)
                    self.assertNotIn('private-secret', str(raised.exception))
                    self.assertNotIn('https://', str(raised.exception))

    def test_runner_preserves_each_failure_without_retry_or_stopping_later_rows(self):
        calls = []
        async def dispatch(case):
            calls.append(case['id'])
            if case['id'] == 'first':
                raise RuntimeError('https://example.invalid/?token=private-secret')
            return {'ok': True, 'data': {'is_playlist': True, 'entry_count': 2, 'format_count': 0}}
        rows = asyncio.run(self.runner.run_cases([
            self.case(id='first'), self.case(id='second', media_type='playlist', expected='playlist')], dispatch))
        self.assertEqual(calls, ['first', 'second'])
        self.assertEqual(rows[0]['error_category'], 'HARNESS_ERROR')
        self.assertEqual(rows[1]['assessment'], 'matched')
        self.assertNotIn('private-secret', json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
