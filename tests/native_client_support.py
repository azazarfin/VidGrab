"""Opt-in actual-window/browser helpers. Importing this module starts nothing."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time
from uuid import uuid4
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def wait(predicate, label, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f'{label}: deadline exceeded; last observation {last!r}')


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def chrome_extension_worker(context, identifier):
    expected = f'chrome-extension://{identifier}/background/service-worker.js'
    worker = next((worker for worker in context.service_workers if worker.url == expected), None)
    if worker is None:
        worker = context.wait_for_event('serviceworker', predicate=lambda worker: worker.url == expected,
            timeout=15000)
    assert worker.url == expected, worker.url
    return worker


def windows_wait_file(path, timeout=25):
    """Wait for an owned handoff file through directory notifications."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FindFirstChangeNotificationW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD)
    kernel.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.FindNextChangeNotification.argtypes = (wintypes.HANDLE,)
    kernel.FindCloseChangeNotification.argtypes = (wintypes.HANDLE,)
    notification = kernel.FindFirstChangeNotificationW(str(path.parent), False, 0x11)
    assert notification and notification != ctypes.c_void_p(-1).value
    deadline = time.monotonic() + timeout
    ready = path.with_suffix('.ready')
    try:
        while not ready.exists():
            milliseconds = max(0, int((deadline-time.monotonic())*1000))
            assert milliseconds and kernel.WaitForSingleObject(notification, milliseconds) == 0, str(path)
            assert kernel.FindNextChangeNotification(notification)
        assert not path.is_symlink() and path.stat().st_size <= 65536
        return json.loads(path.read_text(encoding='utf-8'))
    finally:
        kernel.FindCloseChangeNotification(notification)


class WindowsBrowserTask:
    """Start only the predeclared, hash-bound browser task in the selected VM."""
    def __init__(self, descriptor, root, program, profile, port):
        self.launched, self.closed = False, False
        self.nonce, self.audit = str(uuid4()), {}
        metadata = json.loads(Path(descriptor).read_text(encoding='utf-8'))
        name, expected_hash = metadata['task_name'], metadata['task_xml_sha256']
        assert re.fullmatch(r'VidGrab-P04-native-chrome-[a-z0-9]+-\d+', name)
        assert re.fullmatch(r'[0-9a-f]{64}', expected_hash)
        for field, leaf in [('request_path','launch-request.json'), ('started_path','launcher-started.json'),
                ('stop_path','launcher-stop.json'), ('finished_path','launcher-finished.json')]:
            assert Path(metadata[field]) == root / leaf
        assert os.path.normcase(str(Path(metadata['chrome_path']))) == os.path.normcase(str(program))
        self.metadata = metadata
        # The unelevated reader creates these files so inherited owner rights
        # continue to authorize it after the administrator helper writes them.
        for field in ('started_path','finished_path'):
            with Path(metadata[field]).open('x',encoding='utf-8') as stream:
                stream.write('{}')
        request = {'nonce':self.nonce,'program':str(program),'profile':str(profile),'port':port}
        with Path(metadata['request_path']).open('x',encoding='utf-8') as stream:
            json.dump(request,stream)
        script = "$ErrorActionPreference='Stop';$service=New-Object -ComObject Schedule.Service;" \
            "$service.Connect();$task=$service.GetFolder('\\').GetTask('"+name+"');" \
            "$sha=[Security.Cryptography.SHA256]::Create();" \
            "$actual=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$task.Xml))).Replace('-','').ToLower();" \
            "if($actual -ne '"+expected_hash+"'){throw 'Owned browser task definition changed'};" \
            "$task.Run($null)|Out-Null"
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,text=True,timeout=20)
        assert result.returncode == 0, result.stdout+result.stderr
        self.launched = True
        try:
            self.audit = windows_wait_file(Path(metadata['started_path']))
            assert self.audit['nonce'] == self.nonce and self.audit['elevated_admin'] is True
            assert self.audit['program'] == str(program) and self.audit['session'] == 1
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed or not self.launched:
            return
        self.closed = True
        with Path(self.metadata['stop_path']).open('x',encoding='utf-8') as stream:
            json.dump({'nonce':self.nonce},stream)
        result = windows_wait_file(Path(self.metadata['finished_path']))
        assert result['nonce'] == self.nonce and result['joined'] is True, result
        self.audit['cleanup'] = result


class Native:
    def __init__(self, host, config_path, environment):
        self.host, self.path = Path(host), Path(config_path)
        self.config = json.loads(self.path.read_text())
        self.environment = environment

    def outer(self, operation, params=None, allow_unavailable=False):
        identifier = params['request_id'] if operation=='v1.request' else str(uuid4())
        request = {'spike_version': 1, 'id': identifier, 'op': operation, 'params': params or {}}
        payload = json.dumps(request, ensure_ascii=False).encode()
        result = subprocess.run([str(self.host), '--config', str(self.path), self.config['chrome_origin']],
            input=struct.pack('=I', len(payload)) + payload, capture_output=True, timeout=25,
            env=self.environment)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        assert len(result.stdout) >= 4, result.stderr.decode(errors='replace')
        size = struct.unpack('=I', result.stdout[:4])[0]
        assert 0 < size <= 65536 and len(result.stdout) == size + 4
        response = json.loads(result.stdout[4:])
        assert response.get('id') == identifier and response.get('spike_version') == 1, response
        if allow_unavailable and operation == 'status' and response.get('ok') is False \
                and response.get('error', {}).get('code') == 'ENGINE_UNAVAILABLE':
            return None
        assert response.get('ok') is True, response
        return response['data']

    def request(self, operation, params=None, key=None):
        request = {'protocol_version': 1, 'request_id': str(uuid4()), 'operation': operation, 'params': params or {}}
        if key is not None:
            request['idempotency_key'] = key
        response = self.outer('v1.request', request)
        assert response.get('ok') is True, response
        return response['data']

    def settings(self):
        return self.request('settings.get')

    def jobs(self):
        return self.request('downloads.list', {'limit': 50})['jobs']

    def job(self, url, status=None):
        return next((job for job in self.jobs() if job['url'] == url
            and (status is None or job['status'] == status)), None)


@contextmanager
def media_fixture():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 44100 * 12)
    payload = output.getvalue()
    validator = '"' + hashlib.sha256(payload).hexdigest() + '"'
    state = {'requests': [], 'release': threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, body):
            path = self.path.split('?', 1)[0]
            authenticated = 'vidgrab_validation_session=fresh' in self.headers.get('Cookie', '')
            range_header = self.headers.get('Range')
            state['requests'].append({'method': self.command, 'path': path,
                'authenticated': authenticated, 'range': range_header})
            if path == '/login':
                data = b'<!doctype html><title>VidGrab local sign-in fixture</title><p>Signed in</p>'
                self.send_response(200)
                self.send_header('Set-Cookie', 'vidgrab_validation_session=fresh; Path=/; SameSite=Lax')
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)
                return
            if not path.endswith('.wav'):
                self.send_error(404)
                return
            if path.endswith('-protected.wav') and not authenticated:
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            start = 0
            partial = body and range_header and self.headers.get('If-Range') in (None, validator)
            if partial:
                match = re.fullmatch(r'bytes=(\d+)-', range_header)
                if not match or int(match[1]) >= len(payload):
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{len(payload)}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                start = int(match[1])
            data = payload[start:]
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('ETag', validator)
            self.send_header('Accept-Ranges', 'bytes')
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
            self.end_headers()
            if body:
                try:
                    self.wfile.write(data[:65536])
                    self.wfile.flush()
                    if path.endswith(('-held.wav', '-protected.wav')):
                        assert state['release'].wait(40), 'Local fixture release deadline'
                    self.wfile.write(data[65536:])
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        state['release'].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


class Page:
    def __init__(self, page=None, driver=None, handle=None):
        self.page, self.driver, self.handle = page, driver, handle

    def activate(self):
        if self.page:
            self.page.bring_to_front()
        elif self.handle and getattr(self.driver, 'selected_window', None) != self.handle:
            self.driver.call('/window', {'handle': self.handle})
            self.driver.selected_window = self.handle

    def script(self, body, *arguments):
        if self.page:
            return self.page.evaluate('args => (function () {' + body + '}).apply(window,args)', list(arguments))
        self.activate()
        return self.driver.script(body, *arguments)

    def asynchronous(self, body, *arguments):
        if self.page:
            return self.page.evaluate('args => new Promise(done => { (function () {' + body
                + '}).apply(window,[...args,done]); })', list(arguments))
        self.activate()
        return self.driver.script(body, *arguments, asynchronous=True)

    def goto(self, url):
        if self.page:
            self.activate()
            self.page.goto(url)
            self.page.wait_for_load_state('networkidle')
        else:
            self.activate()
            self.driver.call('/url', {'url': url})

    def fill(self, selector, value):
        if self.page:
            self.activate()
            self.page.locator(selector).fill(value)
        else:
            self.activate()
            self.driver.fill(selector, value)

    def click(self, selector):
        if self.page:
            self.activate()
            self.page.locator(selector).click()
        else:
            self.activate()
            self.driver.click(selector)

    def button(self, label):
        if self.page:
            self.activate()
            self.page.get_by_role('button', name=label, exact=True).click(timeout=25000)
            return
        literal = "'" + label.replace("'", "', \"'\", '") + "'"
        if "'" in label:
            literal = 'concat(' + literal + ')'
        def press():
            if not self.script("""return [...document.querySelectorAll('button')].some(e =>
                e.textContent.trim() === arguments[0] && e.getClientRects().length && !e.disabled)""", label):
                return False
            try:
                element = self.driver.call('/element', {'using':'xpath',
                    'value':'//button[normalize-space(.)='+literal+'][not(@disabled)]'})['element-6066-11e4-a52e-4f735466cecf']
                self.driver.call('/element/'+element+'/click', {})
                return True
            except RuntimeError as error:
                if json.loads(str(error)).get('value',{}).get('error') != 'stale element reference':
                    raise
                return False
        wait(press, f'Visible enabled button: {label}')

    def select(self, selector, value):
        self.activate()
        assert self.script("""const e=document.querySelector(arguments[0]);
            if (!e || ![...e.options].some(o => o.value === arguments[1])) return false;
            e.value=arguments[1];e.dispatchEvent(new Event('change',{bubbles:true}));return true;""", selector, value)

    def value(self, selector):
        return self.script('return document.querySelector(arguments[0])?.value', selector)

    def text(self, selector='body'):
        return self.script("return document.querySelector(arguments[0])?.textContent || ''", selector)

    def visible(self, selector):
        return self.script("return Boolean(document.querySelector(arguments[0])?.getClientRects().length)", selector)

    def has_text(self, selector, text):
        try:
            return wait(lambda: text in self.text(selector), f'{selector} contains {text}')
        except AssertionError as error:
            raise AssertionError({'message':str(error),'last_text':self.text(selector),
                'body':self.text()}) from error

    def screenshot(self, path):
        if self.page:
            self.page.screenshot(path=str(path), full_page=True)
        else:
            self.activate()
            self.driver.screenshot(path)


@contextmanager
def windows_registrations(manifest):
    import ctypes
    from ctypes import wintypes
    import winreg
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, 'Local\\VidGrabP04NativeClientAcceptance')
    assert handle and kernel.WaitForSingleObject(handle, 30000) in (0, 0x80)
    installed = []
    selected = []
    try:
        for browser in (r'Google\Chrome', 'Mozilla'):
            path = 'Software\\' + browser + r'\NativeMessagingHosts\com.vidgrab.companion'
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | view) as key:
                        try:
                            value = winreg.QueryValueEx(key, '')[0]
                        except FileNotFoundError:
                            value = None
                except FileNotFoundError:
                    value = None
                assert value is None, f'Foreign native registration: {path}'
                selected.append((path, view, str(manifest[browser])))
        for path, view, value in selected:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE | view) as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, value)
            installed.append((path, view, value))
        yield
    finally:
        for path, view, expected in reversed(installed):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE | view) as key:
                try:
                    value = winreg.QueryValueEx(key, '')[0]
                except FileNotFoundError:
                    value = None
                assert value in (None, expected), 'Foreign replacement; refuse cleanup'
                if value is not None:
                    winreg.DeleteValue(key, '')
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
