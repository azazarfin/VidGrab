"""Offline redaction, config, guard, and case-validation checks for frozen/native/browser live lanes."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class LiveNativeHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_native.py')
        self.assertTrue(path.is_file(), 'Live native/frozen/browser harness is missing')
        spec = importlib.util.spec_from_file_location('live_native', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_public_native_cases_are_anonymous_and_bounded(self):
        cases = list(self.runner.PUBLIC_NATIVE_CASES)
        self.assertEqual([c['id'] for c in cases], [
            'mdn-flower-control',
            'mux-bbb',
            'youtube-zoo-control',
            'iana-example',
        ])
        for case in cases:
            self.assertEqual(case['authentication'], 'public')
            self.runner.validate_case(case)

    def test_validate_case_rejects_signed_in_or_credentials(self):
        bad_cases = [
            {'id': 'x', 'media_type': 'direct', 'authentication': 'signed-in',
             'url': 'https://example.invalid/a.mp4', 'expected': 'media'},
            {'id': 'x', 'media_type': 'direct', 'authentication': 'public',
             'url': 'https://user:pass@example.invalid/a.mp4', 'expected': 'media'},
            {'id': 'x', 'media_type': 'direct', 'authentication': 'public',
             'url': 'file:///tmp/local.mp4', 'expected': 'media'},
        ]
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.runner.validate_case(bad)

    def test_build_host_config_uses_explicit_app_and_frozen_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'vidgrab-desktop'
            engine = root / 'engine' / 'vidgrab-engine'
            engine.parent.mkdir(parents=True)
            app.write_text('app')
            engine.write_text('engine')
            cfg = self.runner.build_host_config(
                app=app,
                engine=engine,
                data_dir=root / 'state',
                download_dir=root / 'media',
            )
            self.assertEqual(cfg['app'], str(app.resolve()))
            self.assertEqual(cfg['engine'], str(engine.resolve()))
            self.assertEqual(cfg['chrome_origin'], f'chrome-extension://{self.runner.CHROME_ID}/')
            self.assertEqual(cfg['firefox_id'], self.runner.FIREFOX_ID)

    def test_clean_environment_strips_dev_overrides_without_breaking_os_vars(self):
        env = self.runner.clean_environment({
            'PATH': '/usr/bin',
            'HOME': '/home/test',
            'PYTHONPATH': '/leak/src',
            'PYTHONHOME': '/leak/py',
            'VIDGRAB_DATA_DIR': '/leak/data',
            'VIDGRAB_DOWNLOAD_DIR': '/leak/dl',
        })
        self.assertEqual(env['PATH'], '/usr/bin')
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('VIDGRAB_DATA_DIR', env)
        self.assertNotIn('VIDGRAB_DOWNLOAD_DIR', env)

    def test_assert_redacted_rejects_urls_paths_and_ids(self):
        good = {
            'id': 'mdn-flower-control',
            'media_type': 'direct',
            'authentication': 'public',
            'transport': 'frozen-native',
            'phase': 'download',
            'result': 'complete',
            'assessment': 'matched',
            'output': {'size': 1128375, 'sha256': 'a' * 64, 'duration': 5.055,
                       'video_streams': 1, 'audio_streams': 1, 'full_decode_passed': True},
            'seconds': 1.23,
        }
        self.runner.assert_redacted(good)
        for bad in (
            {**good, 'url': 'https://secret.example.com/v.mp4'},
            {**good, 'job_id': 'job-123'},
            {**good, 'probe_id': 'probe-123'},
            {**good, 'file_path': '/tmp/foo.mp4'},
            {**good, 'note': 'http://127.0.0.1:9999/secret'},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.runner.assert_redacted(bad)

    def test_summarize_native_run_handles_auth_guard_cancel_without_leaking(self):
        class FakeNative:
            def __init__(self):
                self.actions = []
                self.status_calls = 0

            def outer(self, op, params=None, allow_unavailable=False):
                if op == 'v1.request':
                    operation = params['operation']
                    if operation == 'probes.create':
                        return {'ok': True, 'data': {
                            'probe_id': 'secret-probe-id',
                            'is_playlist': False,
                            'duration': None,
                            'formats': [{'format_id': '18', 'vcodec': 'avc1', 'acodec': 'aac', 'tbr': 200}],
                        }}
                    if operation == 'downloads.enqueue':
                        return {'ok': True, 'data': {'job_id': 'secret-job-id', 'revision': 1}}
                    if operation == 'downloads.list':
                        self.status_calls += 1
                        if self.status_calls == 1:
                            return {'ok': True, 'data': {'jobs': [{
                                'id': 'secret-job-id',
                                'revision': 1,
                                'status': 'waiting_for_auth',
                                'error_code': 'AUTH_REQUIRED',
                                'url': 'https://www.youtube.com/watch?v=secret-id',
                                'title': 'Secret Title',
                            }]}}
                        return {'ok': True, 'data': {'jobs': [{
                            'id': 'secret-job-id',
                            'revision': 2,
                            'status': 'cancelled',
                            'error_code': 'AUTH_REQUIRED',
                        }]}}
                    if operation == 'downloads.action':
                        self.actions.append(params['params'])
                        return {'ok': True, 'data': {'accepted_status': 'cancelling'}}
                raise AssertionError(op)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'media').mkdir()
            (root / 'state' / 'attempts').mkdir(parents=True)
            fake = FakeNative()
            row = self.runner.run_native_case(
                fake,
                {'id': 'youtube-zoo-control', 'media_type': 'extractor', 'authentication': 'public',
                 'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'expected': 'media'},
                root=root,
                download_dir=root / 'media',
                components={},
                use_defaults=False,
            )
            self.assertEqual(len(fake.actions), 1)
            self.assertEqual(fake.actions[0]['action'], 'cancel')
            self.assertEqual(row['result'], 'cancelled')
            self.assertEqual(row['error_category'], 'AUTH_REQUIRED')
            self.assertTrue(row['guard_cancelled'])
            self.runner.assert_redacted(row)
            serialized = json.dumps(row)
            for secret in ('secret-probe-id', 'secret-job-id', 'Secret Title', 'jNQXAC9IVRw', 'youtube.com'):
                self.assertNotIn(secret, serialized)

    def test_runtime_root_keeps_posix_socket_within_sun_path_limit(self):
        root = self.runner.make_runtime_root('vgln-')
        try:
            sock_path = str(root / 'state' / 'ipc' / 'control.sock')
            self.assertLess(len(sock_path.encode('utf-8')), 107)
        finally:
            root.rmdir()

    def test_verify_cleanup_audit_respects_platform_owner_release_semantics(self):
        clean_posix = {'request_probes': 0, 'request_leases': 0, 'unfinished_jobs': 0, 'unreleased_owners': 0}
        self.runner.verify_cleanup_audit(clean_posix, platform_name='posix')
        nt_job_exit = {'request_probes': 0, 'request_leases': 0, 'unfinished_jobs': 0, 'unreleased_owners': 1}
        self.runner.verify_cleanup_audit(nt_job_exit, platform_name='nt')
        with self.assertRaises(RuntimeError):
            self.runner.verify_cleanup_audit(nt_job_exit, platform_name='posix')
        with self.assertRaises(RuntimeError):
            self.runner.verify_cleanup_audit({**clean_posix, 'unfinished_jobs': 1}, platform_name='nt')


if __name__ == '__main__':
    unittest.main()
