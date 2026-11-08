"""Opt-in regressions for auditing Firefox's Flatpak PID namespace."""

from pathlib import Path
import tempfile
import unittest

from tests import firefox_flatpak_support as support


class FlatpakProcessMapping(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='vidgrab-flatpak-proc-')
        self.addCleanup(self.temporary.cleanup)
        self.proc = Path(self.temporary.name)
        self.process(500, 400, [500, 1])
        self.process(510, 500, [510, 3])
        self.process(530, 510, [530, 21, 1], seccomp='2', privileges='1')
        # A second Flatpak instance can report the same internal PID.
        self.process(700, 600, [700, 21], seccomp='0', privileges='0')

    def process(self, pid, parent, namespace, *, seccomp='2', privileges='1'):
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / 'status').write_text(
            f'Name:\tfirefox\nPid:\t{pid}\nPPid:\t{parent}\n'
            'NSpid:\t' + '\t'.join(map(str, namespace)) + '\n'
            f'Seccomp:\t{seccomp}\nNoNewPrivs:\t{privileges}\n')

    def mapping(self):
        factory = getattr(support, 'FlatpakProcesses', None)
        self.assertIsNotNone(factory, 'Flatpak namespace translation is missing')
        return factory(500, proc=self.proc)

    def test_maps_the_browser_pid_to_its_host_process(self):
        self.assertEqual(self.mapping().host_pid(3), 510)

    def test_nested_content_namespace_uses_the_browser_namespace_depth(self):
        self.assertEqual(self.mapping().host_pid(21), 530)

    def test_outer_flatpak_launcher_can_be_in_the_host_namespace(self):
        (self.proc / '500/status').write_text(
            'Name:\tbwrap\nPid:\t500\nPPid:\t400\nNSpid:\t500\nSeccomp:\t0\nNoNewPrivs:\t0\n')
        self.assertEqual(self.mapping().host_pid(3), 510)

    def test_cleanup_observation_does_not_require_a_private_namespace(self):
        observe = getattr(support, 'process_tree', None)
        self.assertIsNotNone(observe, 'Cleanup process observation is missing')
        self.process(800, 400, [800])
        self.assertEqual(set(observe(800, proc=self.proc)), {800})

    def test_content_audit_excludes_the_other_instance(self):
        self.assertEqual(self.mapping().content([{'pid': 21, 'type': 'extension'}]),
            [{'pid': 530, 'namespace_pid': 21, 'type': 'extension',
                'seccomp': '2', 'no_new_privileges': '1'}])

    def test_unknown_pid_is_an_explicit_error(self):
        with self.assertRaisesRegex(AssertionError, 'PID 99'):
            self.mapping().host_pid(99)

    def test_ambiguous_pid_is_an_explicit_error(self):
        self.process(540, 510, [540, 21])
        with self.assertRaisesRegex(AssertionError, 'ambiguous'):
            self.mapping().host_pid(21)

    def test_unconfined_content_fails_the_audit(self):
        self.process(540, 510, [540, 22], seccomp='0')
        with self.assertRaisesRegex(AssertionError, 'sandbox'):
            self.mapping().content([{'pid': 22, 'type': 'extension'}])


if __name__ == '__main__':
    unittest.main(verbosity=2)
