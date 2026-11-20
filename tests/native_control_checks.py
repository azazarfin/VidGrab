"""Shared opt-in v1 native journeys; importing this module starts nothing."""

import concurrent.futures
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(payload)) + payload


class NativeControlChecks:
    def invoke_control(self, operation, params=None, *, request_id='control', key=None, origin=None, version=1):
        request = {'protocol_version': version, 'request_id': request_id, 'operation': operation, 'params': params or {}}
        if key is not None:
            request['idempotency_key'] = key
        return self.invoke(origin=origin, raw=frame({
            'spike_version': 1, 'id': request_id, 'op': 'v1.request', 'params': request,
        }))

    def control(self, operation, params=None, **options):
        response = self.invoke_control(operation, params, **options)
        self.assertTrue(response['ok'], response)
        result = response['data']
        self.assertEqual(result['protocol_version'], 1)
        self.assertEqual(result['request_id'], options.get('request_id', 'control'))
        return result

    def wait_for(self, predicate, *, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.invoke()
            if predicate(last):
                return last
            time.sleep(0.1)
        self.fail(f'Lifecycle deadline exceeded: {last}')

    def test_versioned_control_replays_settings_and_downloads_after_owner_restart(self):
        from tests.native_support import fixture_server
        ready = self.ready()
        self.assertEqual((ready['data']['protocol_min'], ready['data']['protocol_max']), (1, 1))
        self.assertIs(self.control('capabilities.get')['data']['fixture_only'], False)
        chrome = self.control('settings.get', request_id='chrome-settings')['data']
        firefox = self.control('settings.get', request_id='firefox-settings', origin=self.config['firefox_id'])['data']
        self.assertEqual(chrome, firefox)
        updates = [
            ({'expected_revision': chrome['revision'], 'patch': {'max_concurrent': 4}}, 'chrome-update', None),
            ({'expected_revision': firefox['revision'], 'patch': {'max_concurrent': 5}}, 'firefox-update', self.config['firefox_id']),
        ]
        def update(selected):
            params, key, origin = selected
            return self.control('settings.patch', params, request_id=key, key=key, origin=origin)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as clients:
            results = list(clients.map(update, updates))
        self.assertEqual(sum(result['ok'] for result in results), 1, results)
        winner = next(index for index, result in enumerate(results) if result['ok'])
        conflict = results[1 - winner]
        self.assertEqual(conflict['error']['code'], 'REVISION_CONFLICT')
        original = results[winner]['data']
        later = self.control('settings.patch', {'expected_revision': original['revision'], 'patch': {'max_concurrent': 7}},
                             request_id='later-update', key='later-update')['data']
        self.assertEqual(update(updates[winner])['data'], original)
        self.assertEqual(self.control('settings.get')['data'], later)
        before = self.control('events.read')['data']
        gate = threading.Event()
        self.addCleanup(gate.set)
        with fixture_server(gate) as url:
            params = {'url': url, 'format_id': 'bestaudio/best', 'audio_only': True, 'title': 'বাংলা contract fixture'}
            submitted = self.control('downloads.enqueue', params, request_id='chrome-enqueue', key='stable-download')['data']
            replay = self.control('downloads.enqueue', params, request_id='firefox-enqueue', key='stable-download',
                                  origin=self.config['firefox_id'])['data']
            self.assertEqual(replay, submitted)
            job_id = submitted['job_id']
            self.wait_for(lambda response: any(job['id'] == job_id and job['status'] == 'downloading'
                for job in response.get('data', {}).get('jobs', [])))
            self.assertEqual(self.control('downloads.list')['data']['counts'], {'total': 1, 'unfinished': 1, 'paused': 0, 'scheduled': 0, 'history': 0})
            gate.set()
            complete = self.wait_for(lambda response: any(job['id'] == job_id and job['status'] == 'complete'
                for job in response.get('data', {}).get('jobs', [])))
        job = next(job for job in complete['data']['jobs'] if job['id'] == job_id)
        output = Path(job['file_path'])
        self.assertTrue(output.is_file())
        self.assertEqual(output.suffix, '.mp3')
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        changes = self.control('events.read', {'instance_id': before['instance_id'], 'cursor': before['cursor']})['data']
        self.assertFalse(changes['resnapshot'])
        self.assertTrue(any(event['entity_id'] == job_id for event in changes['events']))
        self.assertNotIn(url, json.dumps(changes['events']))
        self.stop_owner()
        reopened = self.ready()
        snapshot = self.control('events.read', {'instance_id': changes['instance_id'], 'cursor': changes['cursor']})['data']
        self.assertTrue(snapshot['resnapshot'])
        self.assertNotEqual(snapshot['instance_id'], changes['instance_id'])
        self.assertEqual(snapshot['snapshot']['settings'], later)
        self.assertEqual(snapshot['snapshot']['counts'], {'total': 1, 'unfinished': 0, 'paused': 0, 'scheduled': 0, 'history': 1})
        self.assertEqual(self.control('downloads.enqueue', params, request_id='reopened-enqueue', key='stable-download')['data'], submitted)
        self.assertEqual(update(updates[winner])['data'], original)
        self.assertEqual(self.control('settings.get')['data'], later)
        refused = self.invoke_control('settings.patch', {'expected_revision': later['revision'], 'patch': {'max_concurrent': 8}},
                                      request_id='incompatible-update', key='incompatible-update', version=2)
        self.assertFalse(refused['ok'])
        self.assertEqual(refused['error']['code'], 'APP_INCOMPATIBLE')
        self.assertEqual(self.control('settings.get')['data'], later)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), checksum)
        print(json.dumps({'case': 'versioned-control', 'settings_revision': later['revision'], 'jobs': 1,
                          'output_sha256': checksum, 'first_owner': ready['data']['supervisor_pid'],
                          'reopened_owner': reopened['data']['supervisor_pid'], 'resnapshot': True}), flush=True)

    def test_ordinary_probe_cookie_actions_and_auth_replay_after_owner_restart(self):
        memory = io.BytesIO()
        with wave.open(memory, 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x00\x00' * 32000)
        media = memory.getvalue()
        requests = []
        release = threading.Event()
        hold = threading.Event()
        self.addCleanup(release.set)
        secret = 'native-context-secret'
        class Media(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                if self.path not in ('/ordinary.wav', '/protected.wav'):
                    self.send_error(404)
                    return
                if self.path == '/protected.wav' and self.headers.get('Cookie') != 'session=' + secret:
                    self.send_response(403)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/protected.wav' and hold.is_set() and not release.wait(15):
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(media)))
                self.send_header('ETag', '"ordinary-media-v1"')
                self.end_headers()
                try:
                    self.wfile.write(media)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Media)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ordinary = f'http://127.0.0.1:{server.server_port}/ordinary.wav'
        protected = f'http://127.0.0.1:{server.server_port}/protected.wav'
        ready = self.ready()
        capabilities = self.control('capabilities.get')['data']
        probe = self.control('probes.create', {'url': ordinary}, request_id='ordinary-probe')
        self.assertTrue(probe['ok'], probe)
        self.assertIs(capabilities['fixture_only'], False)
        self.assertGreater(probe['data']['format_count'], 0)
        params = {'url': ordinary, 'audio_only': True, 'title': 'বাংলা ordinary media'}
        ordinary_job = self.control('downloads.enqueue', params, key='ordinary-job')['data']['job_id']
        self.wait_for(lambda response: any(job['id'] == ordinary_job and job['status'] == 'complete'
            for job in response.get('data', {}).get('jobs', [])))
        missing = self.control('probes.create', {'url': protected}, request_id='protected-probe')
        self.assertFalse(missing['ok'], missing)
        self.assertEqual(missing['error']['code'], 'AUTH_REQUIRED')
        protected_job = self.control('downloads.enqueue', {'url': protected, 'audio_only': True,
            'title': 'Protected ordinary media'}, key='protected-job')['data']['job_id']
        self.wait_for(lambda response: any(job['id'] == protected_job and job['status'] == 'waiting_for_auth'
            for job in response.get('data', {}).get('jobs', [])))
        def current_job():
            return next(job for job in self.control('downloads.list')['data']['jobs'] if job['id'] == protected_job)
        context_params = {'instance_id': capabilities['instance_id'], 'url': protected, 'job_id': protected_job,
            'cookies': [{'domain': '127.0.0.1', 'path': '/', 'name': 'session', 'value': secret,
                         'secure': False, 'expirationDate': time.time() + 600}],
            'user_agent': 'VidGrab native context', 'referer': f'http://127.0.0.1:{server.server_port}/page'}
        reference = self.control('auth.create', context_params, key='protected-context',
            origin=self.config['firefox_id'])['data']
        authenticated_probe = self.control('probes.create', {'url': protected, 'job_id': protected_job,
            'auth_ref': reference['reference']})
        self.assertTrue(authenticated_probe['ok'], authenticated_probe)
        hold.set()
        authentication = {'job_id': protected_job, 'expected_revision': current_job()['revision'],
                          'auth_ref': reference['reference']}
        accepted = self.control('downloads.authenticate', authentication, key='protected-authentication')['data']
        self.wait_for(lambda response: any(job['id'] == protected_job and job['status'] == 'downloading'
            for job in response.get('data', {}).get('jobs', [])))
        cancellation = {'job_id': protected_job, 'expected_revision': current_job()['revision'], 'action': 'cancel'}
        cancelled = self.control('downloads.action', cancellation, key='protected-cancel')['data']
        self.assertEqual(cancelled['accepted_status'], 'cancelling')
        release.set()
        self.assertEqual(current_job()['status'], 'cancelled')
        self.assertEqual(self.control('downloads.authenticate', authentication, key='protected-authentication')['data'], accepted)
        self.stop_owner()
        reopened = self.ready()
        current_capabilities = self.control('capabilities.get')['data']
        self.assertNotEqual(current_capabilities['instance_id'], capabilities['instance_id'])
        self.assertEqual(self.control('downloads.authenticate', authentication, key='protected-authentication')['data'], accepted)
        self.assertEqual(self.control('downloads.action', cancellation, key='protected-cancel')['data'], cancelled)
        self.assertEqual(current_job()['status'], 'cancelled')
        expired = self.control('auth.create', context_params, key='old-instance-context')
        self.assertFalse(expired['ok'], expired)
        self.assertEqual(expired['error']['code'], 'AUTH_CONTEXT_EXPIRED')
        retry = {'job_id': protected_job, 'expected_revision': current_job()['revision'], 'action': 'retry',
                 'auth_ref': reference['reference']}
        expired = self.control('downloads.action', retry, key='expired-retry')
        self.assertFalse(expired['ok'], expired)
        self.assertEqual(expired['error']['code'], 'AUTH_CONTEXT_EXPIRED')
        context_params['instance_id'] = current_capabilities['instance_id']
        fresh = self.control('auth.create', context_params, key='fresh-context')['data']
        retry['auth_ref'] = fresh['reference']
        retried = self.control('downloads.action', retry, key='fresh-retry')['data']
        complete = self.wait_for(lambda response: any(job['id'] == protected_job and job['status'] == 'complete'
            for job in response.get('data', {}).get('jobs', [])))
        request_count = len(requests)
        self.assertEqual(self.control('downloads.action', retry, key='fresh-retry')['data'], retried)
        self.assertEqual(len(requests), request_count, 'Action replay must not perform another media request')
        checksums = {}
        for job in complete['data']['jobs']:
            self.assertEqual(job['status'], 'complete')
            output = Path(job['file_path'])
            self.assertEqual(output.suffix, '.mp3')
            checksums[job['id']] = hashlib.sha256(output.read_bytes()).hexdigest()
        authorized = [headers for path, headers in requests if path == '/protected.wav' and headers.get('Cookie')]
        self.assertGreaterEqual(len(authorized), 3, 'Both probe and owned worker must receive the context')
        for headers in authorized:
            self.assertEqual(headers['Cookie'], 'session=' + secret)
            self.assertEqual(headers['User-Agent'], context_params['user_agent'])
            self.assertEqual(headers['Referer'], context_params['referer'])
        with closing(sqlite3.connect(self.root / 'state/vidgrab.db')) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM attempt_cookie_files').fetchone()[0], 0)
            durable = database.execute('SELECT download_options FROM downloads').fetchall()
            self.assertNotIn(secret, json.dumps(durable))
            self.assertNotIn(context_params['referer'], json.dumps(durable))
        self.assertNotIn(secret.encode(), (self.root / 'state/vidgrab.db').read_bytes())
        self.assertEqual(len(checksums), 2)
        print(json.dumps({'case': 'ordinary-native-media', 'jobs': 2, 'probe_formats': probe['data']['format_count'],
            'first_owner': ready['data']['supervisor_pid'], 'reopened_owner': reopened['data']['supervisor_pid'],
            'output_sha256': checksums, 'cookie_records': 0, 'action_replay': True}), flush=True)
