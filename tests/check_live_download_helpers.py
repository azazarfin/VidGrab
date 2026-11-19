"""Offline bounds, output ownership and redaction checks for public downloads."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class LiveDownloadHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_downloads.py')
        self.assertTrue(path.is_file(), 'Bounded public download harness is missing')
        spec = importlib.util.spec_from_file_location('live_downloads', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_selection_prefers_small_muxed_format_over_video_only(self):
        selected = self.runner.select_format({'is_playlist': False, 'formats': [
            {'format_id': 'video', 'vcodec': 'avc1', 'acodec': 'none', 'tbr': 100},
            {'format_id': 'large', 'vcodec': 'avc1', 'acodec': 'aac', 'tbr': 900},
            {'format_id': 'small', 'vcodec': 'avc1', 'acodec': 'aac', 'tbr': 250}]})
        self.assertEqual(selected, 'small')
        self.assertIsNone(self.runner.select_format({'is_playlist': True, 'formats': []}))

    def test_selection_falls_back_to_audio_and_rejects_empty_formats(self):
        self.assertEqual(self.runner.select_format({'formats': [
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'aac', 'tbr': 64},
            {'format_id': 'video', 'vcodec': 'avc1', 'acodec': 'none', 'tbr': 200}]}), 'audio')
        self.assertIsNone(self.runner.select_format({'formats': []}))

    def test_guard_stops_at_time_bytes_and_first_retry_or_auth_failure(self):
        guard = self.runner.stop_reason
        self.assertEqual(guard({'status': 'downloading'}, 121, 0), 'TIME_LIMIT')
        self.assertEqual(guard({'status': 'downloading'}, 1, 65 * 1024**2), 'BYTE_LIMIT')
        self.assertEqual(guard({'status': 'retry_wait', 'error_code': 'TRANSPORT_ERROR'}, 1, 0), 'TRANSPORT_ERROR')
        self.assertEqual(guard({'status': 'waiting_for_auth', 'error_code': 'AUTH_REQUIRED'}, 1, 0), 'AUTH_REQUIRED')
        self.assertIsNone(guard({'status': 'complete'}, 121, 0))

    def test_report_omits_job_ids_urls_paths_titles_and_unknown_error_text(self):
        row = self.runner.summarize_job({'status': 'failed', 'error_code': 'private-secret',
            'url': 'https://example.invalid/?token=private-secret', 'title': 'private-secret',
            'file_path': '/private-secret/output', 'id': 'private-secret'})
        self.assertEqual(row['error_category'], 'UNCLASSIFIED_FAILURE')
        self.assertNotIn('private-secret', json.dumps(row))

    def test_output_verification_rejects_foreign_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);media = root / 'media';media.mkdir()
            foreign = root / 'foreign.mp4';foreign.write_bytes(b'private-secret')
            for path in [foreign, media / 'missing.mp4']:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.runner.verify_output(path, media, {})
            try:
                (media / 'linked.mp4').symlink_to(foreign)
            except OSError:
                return
            with self.assertRaises(ValueError):
                self.runner.verify_output(media / 'linked.mp4', media, {})

    def test_guard_sends_one_cancel_and_preserves_first_failure(self):
        calls = []
        async def send(operation, params, key=None):
            calls.append((operation, params, key))
            return {'ok': True, 'data': {'accepted_status': 'cancelling'}}
        job = {'id': 'owned', 'revision': 3, 'status': 'retry_wait', 'error_code': 'TRANSPORT_ERROR'}
        result = asyncio.run(self.runner.cancel_guarded(send, job, 'TRANSPORT_ERROR'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'downloads.action')
        self.assertEqual(calls[0][1], {'job_id': 'owned', 'expected_revision': 3, 'action': 'cancel'})
        self.assertEqual(result['error_category'], 'TRANSPORT_ERROR')

    def test_cleanup_audit_reads_the_actual_runtime_schema(self):
        self.assertTrue(hasattr(self.runner, 'audit_database'), 'Final runtime audit is missing')
        from backend.engine import create_engine_app
        from backend.runtime import RuntimeConfig
        async def check(root):
            runtime = RuntimeConfig(root / 'state', root / 'media')
            app = create_engine_app(runtime, token='a' * 64, port=43210, components={},
                                    media_mode='ordinary', start_queue=False)
            async with app.app.router.lifespan_context(app.app):
                pass
            return self.runner.audit_database(runtime.db_path)
        with tempfile.TemporaryDirectory() as temporary:
            audit = asyncio.run(check(Path(temporary)))
        self.assertEqual(audit['attempt_states'], {})
        self.assertEqual(audit['unreleased_owners'], 0)
        self.assertEqual(audit['unfinished_jobs'], 0)

    def test_reply_preserves_typed_format_rejection(self):
        self.assertTrue(hasattr(self.runner, 'summarize_reply'), 'Typed reply summarizer is missing')
        row = self.runner.summarize_reply({'expected': 'media', 'id': 'direct',
            'media_type': 'direct', 'authentication': 'public'},
            {'ok': False, 'error': {'code': 'FORMAT_UNAVAILABLE', 'message': 'private-secret'}}, 1)
        self.assertEqual(row['error_category'], 'FORMAT_UNAVAILABLE')
        self.assertNotIn('private-secret', json.dumps(row))

    def test_explicit_defaults_do_not_replay_a_rejected_format_selection(self):
        self.assertTrue(hasattr(self.runner, 'enqueue_parameters'), 'Explicit defaults selector is missing')
        case = {'url': 'https://example.invalid/media'}
        probe = {'probe_id': 'private-probe'}
        params = self.runner.enqueue_parameters(case, probe, 'mp4', use_defaults=True)
        self.assertEqual(params, {'url': case['url'], 'format_id': 'best'})
        selected = self.runner.enqueue_parameters(case, probe, 'mp4')
        self.assertEqual(selected['probe_id'], 'private-probe')

    def test_audit_closes_its_sqlite_connection(self):
        from backend.engine import create_engine_app
        from backend.runtime import RuntimeConfig
        async def initialize(root):
            runtime = RuntimeConfig(root / 'state', root / 'media')
            app = create_engine_app(runtime, token='a' * 64, port=43210, components={},
                                    media_mode='ordinary', start_queue=False)
            async with app.app.router.lifespan_context(app.app):
                pass
            return runtime.db_path
        with tempfile.TemporaryDirectory() as temporary:
            database = asyncio.run(initialize(Path(temporary)))
            original = sqlite3.connect
            connections = []
            def connect(*args, **kwargs):
                connection = original(*args, **kwargs)
                connections.append(connection)
                return connection
            try:
                with patch('sqlite3.connect', connect):
                    self.runner.audit_database(database)
                self.assertEqual(len(connections), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connections[0].execute('SELECT 1')
            finally:
                for connection in connections:
                    connection.close()


if __name__ == '__main__':
    unittest.main()
