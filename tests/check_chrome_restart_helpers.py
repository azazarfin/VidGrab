"""Opt-in checks that a diagnostic restart cannot hide a different failure."""

import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
LABEL = 'actual granted response-header observation'


class RestartBoundaryTests(unittest.TestCase):
    def adapter(self, wait, failed, restart, passed):
        self.assertIsNotNone(importlib.util.find_spec('tests.native_chrome_restart'),
            'The separate restart validation boundary is missing')
        from tests.native_chrome_restart import restart_observation_wait
        return restart_observation_wait(wait, failed, restart, passed)

    def test_original_failure_is_saved_before_one_restart_and_same_wait(self):
        events = []
        predicate = object()

        def wait(condition, label, timeout=25):
            events.append(('wait', condition, label, timeout))
            if len(events) == 1:
                raise AssertionError(LABEL + ': deadline exceeded; last observation []')
            return ['observed']

        adapter = self.adapter(wait, lambda error: events.append(('failed', str(error))),
            lambda: events.append(('restart',)), lambda result: events.append(('passed', result)))
        self.assertEqual(adapter(predicate, LABEL), ['observed'])
        self.assertEqual([event[0] for event in events], ['wait', 'failed', 'restart', 'wait', 'passed'])
        self.assertEqual(events[0], events[3])
        with self.assertRaises(AssertionError):
            adapter(predicate, LABEL)

    def test_unrelated_assertion_propagates_without_restart(self):
        def wait(*args):
            raise AssertionError('Foreign document identity')

        def forbidden(*args):
            self.fail('An unrelated failure entered the restart lane')

        adapter = self.adapter(wait, forbidden, forbidden, forbidden)
        with self.assertRaisesRegex(AssertionError, 'Foreign document identity'):
            adapter(object(), LABEL)

    def test_unexpected_fresh_success_does_not_receive_a_restart(self):
        def forbidden(*args):
            self.fail('A fresh success entered the failure/restart lane')

        adapter = self.adapter(lambda *args: ['fresh event'], forbidden, forbidden, forbidden)
        with self.assertRaisesRegex(AssertionError, 'Fresh-grant observation unexpectedly passed'):
            adapter(object(), LABEL)

    def test_failed_restart_does_not_repeat_the_observation(self):
        events = []

        def wait(*args):
            events.append('wait')
            raise AssertionError(LABEL + ': deadline exceeded; last observation []')

        def restart():
            events.append('restart')
            raise AssertionError('Selected Chrome is still alive')

        adapter = self.adapter(wait, lambda error: events.append('saved'), restart, lambda result: None)
        with self.assertRaisesRegex(AssertionError, 'Selected Chrome is still alive'):
            adapter(object(), LABEL)
        self.assertEqual(events, ['wait', 'saved', 'restart'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
