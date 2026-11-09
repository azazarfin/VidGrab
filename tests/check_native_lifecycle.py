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
        # Explicit --config is for this isolated executable test, not browser payloads.
        self.environment = {**os.environ, 'PATH': '', 'PYTHONPATH': '', 'PYTHONHOME': ''}
        self.addCleanup(self.stop_owner)

    def invoke(self, op='status', params=None, *, origin=None, raw=None):
        message = {'spike_version': 1, 'id': 'বাংলা-request', 'op': op, 'params': params or {}}
        result = subprocess.run(
            [str(self.host), '--config', str(self.config_path), origin or self.config['chrome_origin']],
            input=frame(message) if raw is None else raw,
            capture_output=True, env=self.environment, timeout=20,
        )
        response = unframe(result.stdout)
        self.assertNotIn('Bearer ', result.stderr.decode(errors='replace'))
        return response

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
                    if request['op'] != 'fixture.download':
                        connection.sendall(frame({'spike_version': 1, 'id': request['id'], 'ok': True, 'data': {'state': 'ready', 'jobs': []}}))
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
            with sqlite3.connect(self.root / 'state/vidgrab.db') as database:
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
