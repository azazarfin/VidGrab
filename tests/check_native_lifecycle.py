"""Opt-in native-process checks. Uses only a generated loopback WAV fixture.

Build/stage first, then pass --bundle /absolute/path/to/the/staged/spike.
This is intentionally outside unittest discovery and never selects user state.
"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode()
    return struct.pack('=I', len(payload)) + payload


def unframe(data):
    if len(data) < 4:
        raise AssertionError('Native host returned no framed response')
    size = struct.unpack('=I', data[:4])[0]
    if len(data) != size + 4:
        raise AssertionError('Stdout must contain exactly one complete native frame')
    return json.loads(data[4:])


class NativeLifecycleChecks(unittest.TestCase):
    bundle = None

    def setUp(self):
        self.assertIsNotNone(self.bundle, 'Pass a staged P02 bundle')
        self.assertTrue((self.bundle / 'vidgrab-native-host').is_file(), 'P02 needs a built native bridge')
        self.temporary = tempfile.TemporaryDirectory(prefix='vidgrab-native-')
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.config = {
            'app': str(self.bundle / 'vidgrab-supervisor'),
            'engine': str(self.bundle / 'engine' / 'vidgrab-engine'),
            'data_dir': str(self.root / 'state'),
            'download_dir': str(self.root / 'media'),
            'chrome_origin': json.loads((self.bundle / 'host-config.json').read_text())['chrome_origin'],
            'firefox_id': 'vidgrab@azaz.local',
        }
        self.config_path = self.root / 'host-config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.host = self.root / 'vidgrab-native-host'
        self.host.symlink_to(self.bundle / 'vidgrab-native-host')
        self.firefox_manifest = self.root / 'firefox-native-manifest.json'
        self.firefox_manifest.write_text(json.dumps({
            'name': 'com.vidgrab.companion', 'path': str(self.host), 'type': 'stdio',
            'allowed_extensions': [self.config['firefox_id']],
        }))
        # Explicit --config is for this isolated executable test, not browser payloads.
        self.environment = {**os.environ, 'PATH': '', 'PYTHONPATH': '', 'PYTHONHOME': ''}
        self.addCleanup(self.stop_owner)

    def invoke(self, op='status', params=None, *, origin=None, raw=None):
        message = {'spike_version': 1, 'id': 'বাংলা-request', 'op': op, 'params': params or {}}
        caller = origin or self.config['chrome_origin']
        browser_arguments = [str(self.firefox_manifest), caller] if caller == self.config['firefox_id'] else [caller]
        result = subprocess.run(
            [str(self.host), '--config', str(self.config_path), *browser_arguments],
            input=frame(message) if raw is None else raw,
            capture_output=True, env=self.environment, timeout=20,
        )
        response = unframe(result.stdout)
        self.assertNotIn('Bearer ', result.stderr.decode(errors='replace'))
        return response

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

    def local(self, op):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            connection.connect(str(self.root / 'state' / 'ipc' / 'control.sock'))
            connection.sendall(frame({'spike_version': 1, 'id': 'local', 'op': op, 'params': {}}))
            data = b''
            while len(data) < 4:
                data += connection.recv(4 - len(data))
            size = struct.unpack('=I', data)[0]
            while len(data) < size + 4:
                piece = connection.recv(size + 4 - len(data))
                if not piece:
                    raise AssertionError('IPC response was truncated')
                data += piece
            return unframe(data)

    def wait_for(self, predicate, *, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.invoke()
            if predicate(last):
                return last
            time.sleep(0.1)
        self.fail(f'Lifecycle deadline exceeded: {last}')

    def ready(self):
        # The host may validly report starting/recovering before accepting work.
        return self.wait_for(lambda reply: reply.get('data', {}).get('state') == 'ready')

    def stop_owner(self):
        if not (self.root / 'state' / 'ipc' / 'control.sock').exists():
            return
        try:
            response = self.local('quit')
            pid = response.get('data', {}).get('supervisor_pid')
            deadline = time.monotonic() + 8
            while ((self.root / 'state' / 'ipc' / 'control.sock').exists() or (pid and Path(f'/proc/{pid}').exists())) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse((self.root / 'state' / 'ipc' / 'control.sock').exists(), 'Quit must remove the owned endpoint')
            if pid:
                self.assertFalse(Path(f'/proc/{pid}').exists(), 'Quit must reap the supervisor')
        except OSError:
            pass

    def test_wrong_browser_origin_cannot_activate_app(self):
        reply = self.invoke(origin='chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/')
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error']['code'], 'ORIGIN_REJECTED')
        self.assertFalse((self.root / 'state').exists())

    def test_invalid_frames_and_operations_do_not_activate_app(self):
        for raw in (struct.pack('=I', 65537), struct.pack('=I', 3) + b'\xffxx', struct.pack('=I', 20) + b'{}'):
            with self.subTest(raw=raw[:4]):
                self.assertFalse(self.invoke(raw=raw)['ok'])
        self.assertEqual(self.invoke('exec')['error']['code'], 'UNSUPPORTED_OPERATION')
        self.assertFalse((self.root / 'state').exists())

    def test_live_packaged_host_observes_connection_changes(self):
        packaged = {key: value for key, value in self.config.items() if key != 'engine'}
        packaged.update(package_version=1, app=str(self.root / 'missing-app'))
        self.config_path.write_text(json.dumps(packaged))
        preference = self.root / 'browser-connection.json'
        preference.write_text(json.dumps({'connection_version': 1, 'enabled': False}))
        native = subprocess.Popen(
            [str(self.host), '--config', str(self.config_path), self.config['chrome_origin']],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment, bufsize=0,
        )
        try:
            for index, (enabled, operation, error) in enumerate([
                (False, 'status', 'BROWSER_DISCONNECTED'),
                (True, 'status', 'APP_MISSING'),
                (False, 'fixture.download', 'BROWSER_DISCONNECTED'),
            ]):
                with self.subTest(enabled=enabled, operation=operation):
                    preference.write_text(json.dumps({'connection_version': 1, 'enabled': enabled}))
                    request = {'spike_version': 1, 'id': f'live-{index}', 'op': operation,
                        'params': {'url': 'http://127.0.0.1:8080/fixture.wav'} if operation == 'fixture.download' else {}}
                    native.stdin.write(frame(request))
                    data = b''
                    expected = 4
                    deadline = time.monotonic() + 5
                    while len(data) < expected:
                        self.assertTrue(select.select([native.stdout], [], [],
                            max(0, deadline - time.monotonic()))[0], 'A live native reply must be bounded')
                        part = os.read(native.stdout.fileno(), expected - len(data))
                        self.assertTrue(part, 'The host must retain its framed input session')
                        data += part
                        if len(data) == 4 and expected == 4:
                            size = struct.unpack('=I', data)[0]
                            self.assertTrue(0 < size <= 65536)
                            expected += size
                    response = unframe(data)
                    self.assertEqual(response['id'], request['id'])
                    self.assertEqual(response['error']['code'], error)
                    self.assertIsNone(native.poll(), 'Connection changes must use the same host process')
            self.assertFalse((self.root / 'state').exists())
            self.assertFalse((self.root / 'media').exists())
            native.stdin.close()
            self.assertEqual(native.wait(timeout=5), 0)
            self.assertNotIn(b'Bearer ', native.stderr.read())
        finally:
            if native.poll() is None:
                native.terminate()
                native.wait(timeout=5)
            for stream in (native.stdin, native.stdout, native.stderr):
                stream.close()

    def test_lost_mutation_response_is_not_replayed_or_relaunched(self):
        marker = self.root / 'unexpected-launch'
        dummy = self.root / 'must-not-launch'
        dummy.write_text('#!/usr/bin/python3\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("started")\n')
        dummy.chmod(0o755)
        self.config['app'] = str(dummy)
        self.config_path.write_text(json.dumps(self.config))
        ipc = self.root / 'state' / 'ipc'
        ipc.mkdir(parents=True, mode=0o700)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(ipc / 'control.sock'))
        server.listen()
        server.settimeout(0.2)
        stopped = threading.Event()
        commands = []
        def serve():
            while not stopped.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    connection.settimeout(2)
                    data = connection.recv(4)
                    size = struct.unpack('=I', data)[0]
                    while len(data) < size + 4:
                        data += connection.recv(size + 4 - len(data))
                    request = unframe(data)
                    commands.append(request['op'])
                    if request['op'] not in ('fixture.download', 'v1.request'):
                        connection.sendall(frame({'spike_version': 1, 'id': request['id'], 'ok': True,
                            'data': {'state': 'ready', 'jobs': [], 'protocol_min': 1, 'protocol_max': 1}}))
                    # For a mutation, simulate acceptance followed by connection loss.
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        def cleanup():
            stopped.set()
            server.close()
            thread.join(timeout=3)
            (ipc / 'control.sock').unlink(missing_ok=True)
        self.addCleanup(cleanup)
        reply = self.invoke('fixture.download', {'url': 'http://127.0.0.1:8080/fixture.wav'})
        self.assertEqual(reply['error']['code'], 'REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(commands.count('fixture.download'), 1, commands)
        mutations = [
            ('auth.create', {'instance_id': 'engine', 'url': 'https://fixture.invalid/media',
                'cookies': [{'domain': '.fixture.invalid', 'name': 'session', 'value': 'secret'}]}),
            ('downloads.enqueue', {'url': 'https://fixture.invalid/media', 'auth_ref': 'reference'}),
            ('downloads.action', {'job_id': 'job', 'expected_revision': 2, 'action': 'retry', 'auth_ref': 'reference'}),
            ('downloads.authenticate', {'job_id': 'job', 'expected_revision': 2, 'auth_ref': 'reference'}),
        ]
        for operation, params in mutations:
            sent = commands.count('v1.request')
            reply = self.invoke_control(operation, params, request_id='lost-' + operation, key='lost-' + operation)
            self.assertEqual(reply['error']['code'], 'REQUEST_OUTCOME_UNKNOWN', reply)
            self.assertEqual(commands.count('v1.request'), sent + 1, commands)
        self.assertFalse(marker.exists(), 'A dropped response must not launch another app')

    def test_engine_error_is_preserved_for_the_browser(self):
        ipc = self.root / 'state' / 'ipc'
        ipc.mkdir(parents=True, mode=0o700)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(ipc / 'control.sock'))
        server.listen()
        def serve():
            with server.accept()[0] as connection:
                data = connection.recv(4)
                size = struct.unpack('=I', data)[0]
                while len(data) < size + 4:
                    data += connection.recv(size + 4 - len(data))
                request = unframe(data)
                connection.sendall(frame({'spike_version': 1, 'id': request['id'], 'ok': False,
                    'error': {'code': 'ENGINE_UNAVAILABLE', 'message': 'Engine unavailable'}}))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        def cleanup():
            server.close()
            thread.join(timeout=3)
            (ipc / 'control.sock').unlink(missing_ok=True)
        self.addCleanup(cleanup)
        reply = self.invoke()
        self.assertFalse(reply['ok'], 'The bridge must preserve an engine failure')
        self.assertEqual(reply['error']['code'], 'ENGINE_UNAVAILABLE')
        self.assertEqual(reply['id'], 'বাংলা-request')

    def test_concurrent_browser_launches_have_one_owner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: self.invoke(), range(4)))
        self.assertTrue(all(reply['ok'] for reply in replies), replies)
        self.assertEqual(len({reply['data']['supervisor_pid'] for reply in replies}), 1)
        self.assertEqual(len({reply['data']['engine_pid'] for reply in replies}), 1)
        for filename in ('owner.lock', 'control.sock'):
            self.assertEqual((self.root / 'state' / 'ipc' / filename).stat().st_mode & 0o777, 0o600)

    def test_fixture_completes_after_native_host_exits_and_survives_reopen(self):
        memory = io.BytesIO()
        with wave.open(memory, 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x00\x00' * 32000)
        media = memory.getvalue()

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/fixture.wav':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(media)))
                self.end_headers()
                for offset in range(0, len(media), 4096):
                    try:
                        self.wfile.write(media[offset:offset + 4096])
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return  # The extractor may close its metadata-only request.
                    time.sleep(0.04)
            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.ready()
        reply = self.invoke('fixture.download', {'url': f'http://127.0.0.1:{server.server_port}/fixture.wav'})
        self.assertTrue(reply['ok'], reply)
        task_id = reply['data']['task_id']
        complete = self.wait_for(lambda r: any(job['id'] == task_id and job['status'] == 'complete' for job in r.get('data', {}).get('jobs', [])))
        job = next(job for job in complete['data']['jobs'] if job['id'] == task_id)
        output = Path(job['file_path'])
        self.assertTrue(output.is_file())
        self.assertEqual(output.suffix, '.mp3', 'Bundled FFmpeg must actually postprocess the fixture')
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        self.stop_owner()
        reopened = self.ready()
        self.assertTrue(reopened['ok'], reopened)
        self.assertEqual(next(job for job in reopened['data']['jobs'] if job['id'] == task_id)['status'], 'complete')
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), checksum)

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
            self.assertEqual(self.control('downloads.list')['data']['counts'], {'total': 1, 'unfinished': 1})
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
        self.assertEqual(snapshot['snapshot']['counts'], {'total': 1, 'unfinished': 0})
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

    def test_removal_shutdown_waits_for_download_completion(self):
        from tests.native_support import fixture_server
        gate = threading.Event()
        self.addCleanup(gate.set)
        with fixture_server(gate) as url:
            self.ready()
            reply = self.invoke('fixture.download', {'url': url})
            self.assertTrue(reply['ok'], reply)
            active = self.wait_for(lambda response: any(job['status'] == 'downloading'
                for job in response.get('data', {}).get('jobs', [])))
            with closing(sqlite3.connect(self.root / 'state/vidgrab.db')) as database, database:
                database.executemany(
                    "INSERT INTO downloads (id, url, status, created_at) VALUES (?, ?, 'complete', ?)",
                    [(f'recent-{index}', url, f'2099-01-01T00:00:{index:02d}') for index in range(50)],
                )
            self.assertTrue(all(job['status'] == 'complete' for job in self.invoke()['data']['jobs']),
                            'The active job must be outside the recent history view')
            refused = self.local('quit_if_idle')
            self.assertFalse(refused['ok'], refused)
            self.assertEqual(refused['error']['code'], 'DOWNLOADS_ACTIVE')
            self.assertEqual(self.invoke()['data']['supervisor_pid'], active['data']['supervisor_pid'])
            self.assertTrue((self.root / 'state/ipc/control.sock').exists())
            self.assertFalse(self.invoke('quit_if_idle')['ok'], 'Browser shutdown remains unavailable')
            gate.set()
            self.wait_for(lambda response: response.get('data', {}).get('downloads_active') is False)
        removed = self.local('quit_if_idle')
        self.assertTrue(removed['ok'], removed)
        deadline = time.monotonic() + 8
        while (self.root / 'state/ipc/control.sock').exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertFalse((self.root / 'state/ipc/control.sock').exists())

    def test_engine_crash_restarts_once_and_preserves_owner(self):
        first = self.ready()
        self.assertTrue(first['ok'], first)
        os.kill(first['data']['engine_pid'], signal.SIGKILL)
        recovered = self.wait_for(lambda r: r.get('data', {}).get('engine_generation', 0) == 2 and r['data'].get('state') == 'ready')
        self.assertEqual(first['data']['supervisor_pid'], recovered['data']['supervisor_pid'])
        self.assertNotEqual(first['data']['engine_pid'], recovered['data']['engine_pid'])

    def test_quit_kills_and_reaps_an_owned_descendant_ignoring_term(self):
        engine = self.root / 'owned-tree-engine'
        engine.write_text('''#!/usr/bin/python3
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
json.loads(sys.stdin.buffer.readline())
descendant = os.fork()
if descendant == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        body = b'{"jobs":[]}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
print(json.dumps({'spike_version':1, 'port':server.server_port, 'pid':os.getpid(), 'components':{'descendant_pid':descendant}}), flush=True)
server.serve_forever()
''')
        engine.chmod(0o755)
        self.config['engine'] = str(engine)
        self.config_path.write_text(json.dumps(self.config))
        started = self.invoke()
        self.assertTrue(started['ok'], started)
        descendant = started['data']['components']['descendant_pid']
        def cleanup_descendant():
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup_descendant)
        self.assertTrue(Path(f'/proc/{descendant}').exists())
        self.stop_owner()
        self.assertFalse(Path(f'/proc/{descendant}').exists(), 'Quit must reap the stubborn descendant before exiting')

    def test_repeated_engine_crashes_stop_at_restart_budget(self):
        for generation in range(1, 5):
            reply = self.wait_for(lambda r: r.get('data', {}).get('engine_generation') == generation and r['data'].get('state') == 'ready')
            os.kill(reply['data']['engine_pid'], signal.SIGKILL)
        failed = self.wait_for(lambda r: r.get('data', {}).get('state') == 'failed')
        self.assertIsNone(failed['data']['engine_pid'])
        self.assertEqual(failed['data']['engine_generation'], 4)
        time.sleep(1)
        self.assertEqual(self.invoke()['data']['state'], 'failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--case', help='Optional unittest case to run')
    arguments = parser.parse_args()
    NativeLifecycleChecks.bundle = arguments.bundle.resolve()
    unittest.main(argv=[__file__] + ([arguments.case] if arguments.case else []), verbosity=2)
