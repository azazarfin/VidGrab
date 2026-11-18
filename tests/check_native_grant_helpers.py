"""Opt-in regressions for bounded native permission input acknowledgements."""

import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PermissionInputTests(unittest.TestCase):
    def validator(self):
        self.assertIsNotNone(importlib.util.find_spec('tests.native_grant_input'),
            'The native permission input boundary is missing')
        from tests.native_grant_input import validate_acknowledgement
        return validate_acknowledgement

    def test_an_acknowledgement_is_bound_to_the_browser_choice_and_nonce(self):
        validate = self.validator()
        expected = {'browser': 'chrome', 'choice': 'deny', 'nonce': 'owned-request'}
        response = {'op': 'permission-input-complete', **expected}
        self.assertEqual(validate(response, expected), response)
        for field, foreign in [('browser', 'firefox'), ('choice', 'allow'), ('nonce', 'another-request')]:
            with self.subTest(field=field), self.assertRaises(AssertionError):
                validate({**response, field: foreign}, expected)

    def test_missing_or_unexpected_input_fields_are_refused(self):
        validate = self.validator()
        expected = {'browser': 'firefox', 'choice': 'allow', 'nonce': 'owned-request'}
        response = {'op': 'permission-input-complete', **expected}
        for invalid in [None, [], {}, {**response, 'op': 'grant'},
                {**response, 'programmatic_grant': True}, {**response, 'nonce': None}]:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                validate(invalid, expected)

    def test_two_accessibility_paths_for_the_same_native_button_are_one_choice(self):
        from tests import native_grant_input
        select_button = getattr(native_grant_input, 'permission_button', None)
        self.assertTrue(callable(select_button), 'Native button alias handling is missing')
        node = {'name': 'Deny', 'role': 'push button', 'showing': True, 'enabled': True,
            'screen': {'x': 676, 'y': 247, 'width': 74, 'height': 39}, 'path': [8, 0, 1]}
        alias = {**node, 'path': [8, 1, 0]}
        self.assertEqual(select_button([node, alias], 'deny')['screen'], node['screen'])
        with self.assertRaises(AssertionError):
            select_button([node, {**alias, 'screen': {**node['screen'], 'x': 900}}], 'deny')
        with self.assertRaises(AssertionError):
            select_button([{**node, 'enabled': False}], 'deny')


if __name__ == '__main__':
    unittest.main(verbosity=2)
