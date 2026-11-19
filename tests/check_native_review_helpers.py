"""Opt-in checks for the native review lane's real HTTP failure fixtures."""

from pathlib import Path
import sys
import subprocess
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_client_support import media_fixture


class ReviewFixtureTests(unittest.TestCase):
    def test_native_review_import_requires_only_the_standard_library(self):
        result = subprocess.run([sys.executable, '-B', '-S', '-c',
            'import tests.native_review_journeys'], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rate_limit_exposes_an_actual_retry_after_for_head_and_get(self):
        with media_fixture(review_failures=True) as (base, fixture):
            for method in ('HEAD', 'GET'):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(Request(base + '/review-throttle.wav?signature=private', method=method), timeout=5)
                try:
                    self.assertEqual(raised.exception.code, 429)
                    self.assertEqual(raised.exception.headers['Retry-After'], '60')
                    self.assertEqual(raised.exception.read(), b'')
                finally:
                    raised.exception.close()
            self.assertEqual([r['method'] for r in fixture['requests']], ['HEAD', 'GET'])
            self.assertNotIn('private', str(fixture['requests']))

    def test_unsupported_source_returns_empty_html_instead_of_wave_bytes(self):
        with media_fixture(review_failures=True) as (base, _):
            with urlopen(base + '/review-unsupported.wav', timeout=5) as response:
                self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
                self.assertEqual(response.read(), b'<!doctype html><title>No media here</title>')

    def test_failure_routes_are_opt_in(self):
        with media_fixture() as (base, _):
            with urlopen(base + '/review-throttle.wav', timeout=5) as response:
                self.assertEqual(response.read(4), b'RIFF')


if __name__ == '__main__':
    unittest.main()
