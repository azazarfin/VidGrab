"""Offline redaction, identity-preservation and stage checks for live worker diagnostics."""

import errno
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LiveWorkerDiagnosticHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_worker_diagnostics.py')
        self.assertTrue(path.is_file(), 'Live worker diagnostic harness is missing')
        spec = importlib.util.spec_from_file_location('live_worker_diagnostics', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_wire_observer_records_methods_statuses_and_preserves_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            obs = self.runner.WorkerObserver(Path(temporary) / 'worker.json')
            req_head = type('Req', (), {'method': 'HEAD', 'get_method': lambda self: 'HEAD',
                'full_url': 'https://example.invalid/?token=private-secret'})()
            req_get = type('Req', (), {'method': 'GET', 'get_method': lambda self: 'GET',
                'full_url': 'https://example.invalid/media?sig=private-secret'})()
            resp_200 = type('Resp', (), {'status': 200})()
            resp_403 = type('Resp', (), {'status': 403})()
            self.assertIs(obs.send(lambda r: resp_200, req_head), resp_200)
            self.assertIs(obs.send(lambda r: resp_403, req_get), resp_403)
            snap = obs.snapshot()
            self.assertEqual(snap['http_method_status_counts'], {'GET:403': 1, 'HEAD:200': 1})
            self.assertEqual(snap['first_non_2xx'], {'method': 'GET', 'status': 403, 'request_index': 2})
            self.assertEqual(snap['total_requests'], 2)
            self.assertNotIn('private-secret', json.dumps(snap))

    def test_wire_observer_records_transport_exception_and_errno_without_leaking(self):
        with tempfile.TemporaryDirectory() as temporary:
            obs = self.runner.WorkerObserver(Path(temporary) / 'worker.json')
            obs.stage('head_probe_start')
            req = type('Req', (), {'get_method': lambda self: 'HEAD',
                'full_url': 'https://example.invalid/frag?secret=private-secret'})()
            err = ConnectionResetError(errno.ECONNRESET, 'private-secret reset')
            def fail(_):
                raise err
            with self.assertRaises(ConnectionResetError) as raised:
                obs.send(fail, req)
            self.assertIs(raised.exception, err)
            snap = obs.snapshot()
            self.assertEqual(snap['http_method_status_counts'], {'HEAD:EXCEPTION': 1})
            self.assertIn('ConnectionResetError', snap['http_exception_types'])
            expected_errno = errno.errorcode.get(errno.ECONNRESET, 'ECONNRESET')
            self.assertIn(expected_errno, snap['os_errnos'])
            self.assertEqual(snap['first_transport_error'], {
                'method': 'HEAD', 'exception_type': 'ConnectionResetError',
                'errno': expected_errno, 'request_index': 1, 'stage': 'head_probe_start'})
            self.assertNotIn('private-secret', json.dumps(snap))

    def test_ffprobe_observer_redacts_stderr_and_preserves_return_tuple(self):
        with tempfile.TemporaryDirectory() as temporary:
            obs = self.runner.WorkerObserver(Path(temporary) / 'worker.json')
            result = ('', '/private-secret/file.mp4: No such file or directory\n', 1)
            actual = obs.observe_popen(lambda *a, **kw: result,
                ['/tools/ffprobe', '-hide_banner', 'file:/private-secret/file.mp4'])
            self.assertEqual(actual, result)
            snap = obs.snapshot()
            self.assertEqual(len(snap['ffprobe_runs']), 1)
            entry = snap['ffprobe_runs'][0]
            self.assertEqual(entry['exit_code'], 1)
            self.assertTrue(entry['stdout_empty'])
            self.assertEqual(entry['stdout_len'], 0)
            self.assertFalse(entry['stderr_empty'])
            self.assertIn('NO_SUCH_FILE', entry['stderr_signals'])
            self.assertNotIn('private-secret', json.dumps(snap))

    def test_error_frames_and_signals_keep_only_allowlisted_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            obs = self.runner.WorkerObserver(Path(temporary) / 'worker.json')
            obs.message('WARNING: [youtube] private-id: Some web client https formats have been skipped as they are missing a url. PO Token required. https://example.invalid/?x=private-secret')
            try:
                json.loads('')
            except json.JSONDecodeError as error:
                obs.record_exception(error)
            snap = obs.snapshot()
            self.assertIn('FORMATS_MISSING_URL', snap['signals'])
            self.assertIn('PO_TOKEN_NOTICE', snap['signals'])
            self.assertIn('JSONDecodeError', snap['exception_types'])
            self.assertTrue(any(f['module'].startswith('json') for f in snap['error_frames']))
            dumped = json.dumps(snap)
            self.assertNotIn('private-secret', dumped)
            self.assertNotIn('private-id', dumped)
            self.assertNotIn('/', json.dumps(snap['error_frames']))

    def test_probe_format_summary_redacts_urls_and_reports_quality_presence(self):
        summary = self.runner.summarize_probe_format({
            'format_id': 'mp4', 'ext': 'mp4', 'url': 'https://example.invalid/?secret=private-secret',
            'vcodec': None, 'acodec': None, 'height': None, 'width': None, 'resolution': None,
        })
        self.assertEqual(summary, {
            'has_ext': True, 'has_height': False, 'has_width': False,
            'has_resolution': False, 'has_vcodec': False, 'has_acodec': False,
        })
        self.assertNotIn('private-secret', json.dumps(summary))

    def test_worker_entry_command_patch_wraps_only_worker_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            diag_file = Path(temporary) / 'worker.json'
            owner_cmd = self.runner.observed_entry_command(
                lambda m, f: ['/bin/python', '-B', '-m', m], diag_file,
                'backend.engine.worker_owner', '--worker-owner')
            worker_cmd = self.runner.observed_entry_command(
                lambda m, f: ['/bin/python', '-B', '-m', m], diag_file,
                'backend.engine.worker', '--worker')
            self.assertEqual(owner_cmd, ['/bin/python', '-B', '-m', 'backend.engine.worker_owner'])
            self.assertEqual(worker_cmd[0], sys.executable)
            self.assertEqual(worker_cmd[-3:], ['tests.check_live_worker_diagnostics', '--observe-worker', str(diag_file)])

    def test_snapshot_flush_persists_valid_redacted_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            diag_file = Path(temporary) / 'worker.json'
            obs = self.runner.WorkerObserver(diag_file)
            obs.stage('worker_start')
            obs.update_fragments(count=12, head_attempts=3, get_started=1)
            obs.flush()
            loaded = json.loads(diag_file.read_text(encoding='utf-8'))
            self.assertEqual(loaded['stages'], ['worker_start'])
            self.assertEqual(loaded['fragment_stats']['count'], 12)
            self.assertEqual(loaded['fragment_stats']['head_attempts'], 3)
            self.assertEqual(loaded['fragment_stats']['get_started'], 1)

    def test_cli_requires_public_opt_in_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'existing'
            out.mkdir()
            p1 = subprocess.run([sys.executable, '-B', '-m', 'tests.check_live_worker_diagnostics',
                '--tools', temporary, '--output', str(Path(temporary) / 'fresh')],
                capture_output=True, text=True)
            self.assertNotEqual(p1.returncode, 0)
            p2 = subprocess.run([sys.executable, '-B', '-m', 'tests.check_live_worker_diagnostics',
                '--allow-public-network', '--tools', temporary, '--output', str(out)],
                capture_output=True, text=True)
            self.assertNotEqual(p2.returncode, 0)

    def test_install_patches_and_restores_cleanly(self):
        from contextlib import ExitStack
        from yt_dlp.utils import Popen as YtPopen
        original_run = YtPopen.run
        with tempfile.TemporaryDirectory() as temporary:
            diag_file = Path(temporary) / 'worker.json'
            obs = self.runner.WorkerObserver(diag_file)
            with ExitStack() as stack:
                obs.install(stack)
                self.assertIsNot(YtPopen.run, original_run)
                # Non-ffmpeg command passes through untouched without recording ffprobe_runs
                out, err, rc = YtPopen.run([sys.executable, '-B', '-c', 'print("ok")'],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(rc, 0)
                self.assertEqual(out.strip(), 'ok')
                self.assertEqual(obs.snapshot()['ffprobe_runs'], [])
            self.assertEqual(YtPopen.run, original_run)


if __name__ == '__main__':
    unittest.main()
