"""Offline redaction, group-guard and enqueue checks for bounded live playlist downloads."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class LivePlaylistHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_playlists.py')
        self.assertTrue(path.is_file(), 'Live playlist download harness is missing')
        spec = importlib.util.spec_from_file_location('live_playlists', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_default_public_playlist_cases_are_bounded_and_anonymous(self):
        cases = self.runner.load_playlist_cases()
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case['id'], 'youtube-playlist')
        self.assertEqual(case['media_type'], 'playlist')
        self.assertEqual(case['authentication'], 'public')
        self.assertEqual(case['expected'], 'playlist')
        self.assertEqual(case['playlist_items'], '1-2')

    def test_load_playlist_cases_rejects_signed_in_credentials_and_bad_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            for bad in (
                [{'id': 'p1', 'media_type': 'playlist', 'authentication': 'signed-in',
                  'url': 'https://example.invalid/list', 'expected': 'playlist', 'playlist_items': '1-2'}],
                [{'id': 'p1', 'media_type': 'playlist', 'authentication': 'public',
                  'url': 'https://user:pass@example.invalid/list', 'expected': 'playlist', 'playlist_items': '1-2'}],
                [{'id': 'p1', 'media_type': 'playlist', 'authentication': 'public',
                  'url': 'https://example.invalid/list', 'expected': 'playlist', 'playlist_items': '0-2'}],
                [{'id': 'p1', 'media_type': 'playlist', 'authentication': 'public',
                  'url': 'https://example.invalid/list', 'expected': 'playlist', 'playlist_items': '2-1'}],
                [{'id': 'p1', 'media_type': 'playlist', 'authentication': 'public',
                  'url': 'https://example.invalid/list', 'expected': 'media', 'playlist_items': '1'}],
            ):
                spec_path = Path(temporary) / 'bad.json'
                spec_path.write_text(json.dumps(bad), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.runner.load_playlist_cases(spec_path)

    def test_playlist_enqueue_parameters_bind_playlist_options_without_probe_id(self):
        case = {'id': 'youtube-playlist', 'url': 'https://example.invalid/playlist?list=PL1',
                'playlist_items': '1-2'}
        params = self.runner.playlist_enqueue_parameters(case)
        self.assertEqual(params, {
            'url': 'https://example.invalid/playlist?list=PL1',
            'format_id': 'best',
            'options': {'is_playlist': True, 'playlist_items': '1-2'},
        })
        self.assertNotIn('probe_id', params)

    def test_playlist_stop_reason_waits_for_active_sibling_before_group_guard(self):
        parent = {'id': 'p1', 'status': 'waiting_for_children', 'error_code': None}
        # One child waiting for auth while sibling still downloading -> keep waiting
        children_mixed = [
            {'id': 'c1', 'status': 'waiting_for_auth', 'error_code': 'AUTH_REQUIRED'},
            {'id': 'c2', 'status': 'downloading', 'error_code': None},
        ]
        self.assertIsNone(self.runner.playlist_stop_reason(parent, children_mixed, 10.0, 0))
        # Once all non-terminal children are waiting_for_auth / retry_wait -> trigger group guard
        children_stuck = [
            {'id': 'c1', 'status': 'waiting_for_auth', 'error_code': 'AUTH_REQUIRED'},
            {'id': 'c2', 'status': 'waiting_for_auth', 'error_code': 'AUTH_REQUIRED'},
        ]
        self.assertEqual(self.runner.playlist_stop_reason(parent, children_stuck, 10.0, 0), 'AUTH_REQUIRED')
        # Mixed complete + waiting -> stop with waiting child's code
        children_partial = [
            {'id': 'c1', 'status': 'complete', 'error_code': None},
            {'id': 'c2', 'status': 'retry_wait', 'error_code': 'TRANSPORT_ERROR'},
        ]
        self.assertEqual(self.runner.playlist_stop_reason(parent, children_partial, 10.0, 0), 'TRANSPORT_ERROR')
        # Parent waiting directly before expansion
        parent_wait = {'id': 'p1', 'status': 'retry_wait', 'error_code': 'UPSTREAM_RATE_LIMITED'}
        self.assertEqual(self.runner.playlist_stop_reason(parent_wait, [], 5.0, 0), 'UPSTREAM_RATE_LIMITED')
        # Time and byte bounds
        self.assertEqual(self.runner.playlist_stop_reason(parent, children_mixed, 120.0, 0), 'TIME_LIMIT')
        self.assertEqual(self.runner.playlist_stop_reason(parent, children_mixed, 10.0, 64 * 1024**2), 'BYTE_LIMIT')
        # All terminal -> done
        done_parent = {'id': 'p1', 'status': 'complete', 'error_code': None}
        done_children = [
            {'id': 'c1', 'status': 'complete', 'error_code': None},
            {'id': 'c2', 'status': 'complete', 'error_code': None},
        ]
        self.assertIsNone(self.runner.playlist_stop_reason(done_parent, done_children, 10.0, 0))

    def test_summarize_playlist_group_redacts_ids_urls_titles_and_paths(self):
        parent = {
            'id': 'secret-parent-id', 'status': 'cancelled', 'error_code': None,
            'url': 'https://example.invalid/?secret=parent', 'title': 'Private Playlist Title',
            'file_path': '/private/path/none',
        }
        children = [
            {'id': 'secret-child-1', 'status': 'cancelled', 'error_code': 'AUTH_REQUIRED',
             'url': 'https://example.invalid/v1?secret=1', 'title': 'Private Video 1',
             'file_path': '/private/path/1.mp4'},
            {'id': 'secret-child-2', 'status': 'complete', 'error_code': None,
             'url': 'https://example.invalid/v2?secret=2', 'title': 'Private Video 2',
             'file_path': '/private/path/2.mp4'},
        ]
        verified = {1: {'size': 1234, 'sha256': 'a' * 64, 'duration': 5.0,
                        'video_streams': 1, 'audio_streams': 1, 'full_decode_passed': True}}
        summary = self.runner.summarize_playlist_group(parent, children, verified)
        self.assertEqual(summary['result'], 'cancelled')
        self.assertEqual(summary['assessment'], 'mismatch')
        self.assertEqual(summary['child_count'], 2)
        self.assertEqual(summary['child_status_counts'], {'cancelled': 1, 'complete': 1})
        self.assertEqual(summary['children'][0]['error_category'], 'AUTH_REQUIRED')
        self.assertEqual(summary['children'][1]['output']['size'], 1234)
        dumped = json.dumps(summary)
        for secret in ('secret-parent-id', 'secret-child-1', 'secret-child-2',
                       'Private Playlist Title', 'Private Video 1', 'Private Video 2',
                       'example.invalid', '/private/path'):
            self.assertNotIn(secret, dumped)

    def test_multi_worker_wrapper_allocates_distinct_files_for_worker_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            out_dir = Path(temporary)
            allocated = []
            wrapper = self.runner.make_multi_worker_entry_command(
                lambda m, f: ['/bin/python', '-B', '-m', m], out_dir, 'youtube-playlist', allocated)
            owner_cmd = wrapper('backend.engine.worker_owner', '--worker-owner')
            w1 = wrapper('backend.engine.worker', '--worker')
            w2 = wrapper('backend.engine.worker', '--worker')
            self.assertEqual(owner_cmd, ['/bin/python', '-B', '-m', 'backend.engine.worker_owner'])
            self.assertEqual(len(allocated), 2)
            self.assertNotEqual(allocated[0], allocated[1])
            self.assertEqual(w1[-2:], ['--observe-worker', str(allocated[0])])
            self.assertEqual(w2[-2:], ['--observe-worker', str(allocated[1])])

    def test_audit_database_counts_playlist_parents_and_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / 'state.db'
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript('''
                    CREATE TABLE downloads (id TEXT, status TEXT, job_kind TEXT, parent_id TEXT);
                    CREATE TABLE download_attempts (id TEXT, status TEXT);
                    CREATE TABLE request_probes (id TEXT);
                    CREATE TABLE request_leases (id TEXT);
                    CREATE TABLE runtime_owners (id TEXT, released_at REAL);
                    INSERT INTO downloads VALUES ('p1', 'cancelled', 'playlist', NULL);
                    INSERT INTO downloads VALUES ('c1', 'cancelled', 'media', 'p1');
                    INSERT INTO downloads VALUES ('c2', 'cancelled', 'media', 'p1');
                    INSERT INTO download_attempts VALUES ('a1', 'complete');
                    INSERT INTO download_attempts VALUES ('a2', 'cancelled');
                    INSERT INTO download_attempts VALUES ('a3', 'cancelled');
                    INSERT INTO runtime_owners VALUES ('o1', 123.0);
                ''')
                db.commit()
            audit = self.runner.audit_playlist_database(db_path)
            self.assertEqual(audit['downloads'], 3)
            self.assertEqual(audit['playlist_parents'], 1)
            self.assertEqual(audit['playlist_children'], 2)
            self.assertEqual(audit['download_attempts'], 3)
            self.assertEqual(audit['unfinished_jobs'], 0)
            self.assertEqual(audit['unreleased_owners'], 0)
            self.assertEqual(audit['attempt_states'], {'cancelled': 2, 'complete': 1})

    def test_cli_requires_public_opt_in_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / 'existing'
            existing.mkdir()
            p1 = subprocess.run([sys.executable, '-B', '-m', 'tests.check_live_playlists',
                '--tools', temporary, '--output', str(Path(temporary) / 'fresh')],
                capture_output=True, text=True)
            self.assertNotEqual(p1.returncode, 0)
            p2 = subprocess.run([sys.executable, '-B', '-m', 'tests.check_live_playlists',
                '--allow-public-network', '--tools', temporary, '--output', str(existing)],
                capture_output=True, text=True)
            self.assertNotEqual(p2.returncode, 0)


if __name__ == '__main__':
    unittest.main()
