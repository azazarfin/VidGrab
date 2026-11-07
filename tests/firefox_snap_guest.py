"""Private event peer for the Firefox Snap check, only inside its Ubuntu VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_installed_browsers import FIREFOX_UUID, completed_job, firefox_content_sandbox, firefox_fixture
from tests.check_packaged_desktop import configure, launch_diagnostics
from tests.check_wayland_desktop import Desktop, mounted_image
from tests.native_support import WebDriver, fixture_server, local_call, stop_owner, wait_for


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def reset_test_permission():
    """Request a fresh real prompt for this fixture, without granting access."""
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    arguments = GLib.Variant('(sss)', ('webextensions', 'com.vidgrab.companion', 'snap.firefox'))

    def call(operation, result_type):
        return bus.call_sync('org.freedesktop.impl.portal.PermissionStore',
            '/org/freedesktop/impl/portal/PermissionStore', 'org.freedesktop.impl.portal.PermissionStore',
            operation, arguments, GLib.VariantType(result_type), Gio.DBusCallFlags.NONE, 10000, None).unpack()

    try:
        permissions, = call('GetPermission', '(as)')
    except GLib.Error as error:
        if Gio.DBusError.get_remote_error(error) == 'org.freedesktop.portal.Error.NotFound':
            return []
        raise
    if permissions:
        call('DeletePermission', '()')
    return permissions


def pointer_reply(desktop, request):
    operation = request['op']
    if operation == 'desktop':
        snapshot = wait_for(lambda: value if not (value := desktop.snapshot())['overview_visible']
            else None, label='Snap browser leaves overview')
    elif operation == 'pointer-position':
        snapshot = wait_for(lambda: value if abs((value := desktop.snapshot())['pointer']['x'] - request['x']) < 3
            and abs(value['pointer']['y'] - request['y']) < 3 else None, label='Snap permission pointer position')
    elif operation in ('pointer-down', 'pointer-up'):
        snapshot = wait_for(lambda: value if (value := desktop.snapshot())['pointer']['primary_down']
            == (operation == 'pointer-down') else None, label='Snap permission ' + operation)
    else:
        raise ValueError('Unsupported permission input observation')
    print(json.dumps({'reply': operation, 'snapshot': snapshot}), flush=True)


def focus_browser(desktop, pid):
    def visible_window():
        snapshot = desktop.snapshot()
        return snapshot if any(window['pid'] == pid for window in snapshot['windows']) else None

    snapshot = wait_for(visible_window, label='visible Snap browser window')
    emit('browser-focus', pid=pid, snapshot=snapshot)
    while True:
        line = sys.stdin.readline()
        assert line, 'Host must observe browser focus'
        request = json.loads(line)
        if request['op'] == 'browser-focused':
            snapshot = wait_for(lambda: value if not (value := desktop.snapshot())['overview_visible']
                and any(window['pid'] == pid and window['focused'] for window in value['windows'])
                else None, label='Snap browser has focus for portal access')
            print(json.dumps({'reply': request['op'], 'snapshot': snapshot}), flush=True)
            return snapshot
        pointer_reply(desktop, request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--geckodriver', type=Path, default=Path('/mnt/inputs/geckodriver'))
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    guest_home = Path('/home/vidgrabtest')
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
            Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation' or \
            os.getuid() != 1000 or Path.home() != guest_home:
        parser.error('Select the explicitly isolated Ubuntu validation guest')
    if not args.home.is_absolute() or not args.home.is_relative_to(guest_home / 'validation') or \
            args.home.exists() or args.report.exists():
        parser.error('Select fresh home/report paths in the disposable guest')
    metadata = Path('/snap/firefox/current/meta/snap.yaml').read_text()
    assert re.search(r'^confinement:\s*strict\s*$', metadata, re.MULTILINE), 'Firefox must be a strict Snap'
    args.home.mkdir(mode=0o700, parents=True)
    profile_root = guest_home / 'snap/firefox/common' / args.home.name
    assert not profile_root.exists(), 'Select a fresh Snap-visible profile'
    profile_root.mkdir(mode=0o700, parents=True)
    os.environ.update(XDG_RUNTIME_DIR='/run/user/1000', DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
    session = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line)
    assert session['XDG_SESSION_TYPE'] == 'wayland'
    environment = {**os.environ, **{key: session[key] for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE')},
        'HOME': str(guest_home), 'XDG_DATA_HOME': str(guest_home / '.local/share'),
        'XDG_CONFIG_HOME': str(guest_home / '.config'), 'XDG_CACHE_HOME': str(guest_home / '.cache'),
        'GDK_BACKEND': 'wayland', 'LIBGL_ALWAYS_SOFTWARE': '1', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1',
        'MOZ_LOG': 'NativeMessagingPortal:5'}
    for key in ('VIDGRAB_DISABLE_TRAY', 'APPIMAGE_EXTRACT_AND_RUN', 'MOZ_DISABLE_CONTENT_SANDBOX',
            'MOZ_DISABLE_GMP_SANDBOX', 'MOZ_DISABLE_RDD_SANDBOX'):
        environment.pop(key, None)
        os.environ.pop(key, None)
    os.environ.update(environment)
    desktop = Desktop()
    assert desktop.snapshot()['wayland']
    firewall = json.loads(subprocess.check_output(
        ['sudo', 'nft', '-j', 'list', 'table', 'inet', 'vidgrab_validation'], text=True))
    chains = [entry['chain'] for entry in firewall['nftables'] if 'chain' in entry]
    assert {chain['name'] for chain in chains if chain['policy'] == 'drop'} == {'input', 'output'}
    receipt = {'result': 'running', 'app': str(args.app), 'kernel': platform.release(),
        'app_sha256': hashlib.sha256(args.app.read_bytes()).hexdigest(),
        'browser_launcher': '/snap/bin/firefox', 'snap_revision': Path('/snap/firefox/current').resolve().name,
        'firewall': firewall, 'checks': [], 'limits': ['disposable Ubuntu GNOME VM and virtual input',
            'temporary development extension; loopback Marionette with isolated system-access flag',
            'strict Snap launched through its canonical launcher; no confinement disabling',
            'no Flatpak, physical-device or Windows claim']}
    config = driver = browser = monitor = None
    image_mounts = []
    gate = threading.Event()
    descriptor = guest_home / '.local/share/vidgrab-p02/native/host-config.json'
    saved_descriptor = None
    log = None

    def checkpoint(stage, **values):
        receipt['checks'].append({'stage': stage, **values})
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        emit('checkpoint', stage=stage)

    def connect():
        nonlocal browser, driver, log
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            marionette_port = reserved.getsockname()[1]
        preferences = {'extensions.webextensions.uuids': json.dumps({config['firefox_id']: FIREFOX_UUID}),
            'marionette.port': marionette_port, 'marionette.log.level': 'Info',
            'app.update.auto': False, 'browser.startup.homepage': 'about:blank',
            'browser.shell.checkDefaultBrowser': False, 'datareporting.healthreport.uploadEnabled': False,
            'toolkit.telemetry.enabled': False, 'network.trr.mode': 5}
        (profile / 'user.js').write_text('\n'.join(f'user_pref({json.dumps(key)}, {json.dumps(value)});'
            for key, value in preferences.items()) + '\n')
        log = (args.home / 'firefox.log').open('ab')
        browser = subprocess.Popen(['/snap/bin/firefox', '--no-remote', '--marionette',
            '--profile', str(profile)], env={**environment, 'MOZ_REMOTE_ALLOW_SYSTEM_ACCESS': '1'},
            stdout=log, stderr=log, start_new_session=True)
        driver = WebDriver(args.geckodriver, environment, args.home / 'driver.log',
            arguments=('--allow-system-access', '--connect-existing', f'--marionette-port={marionette_port}'))
        capabilities = driver.start({'browserName': 'firefox'})
        identifier = driver.call('/moz/addon/install', {'path': str(archive), 'temporary': True})
        assert identifier == config['firefox_id'], identifier
        driver.call('/url', {'url': f'moz-extension://{FIREFOX_UUID}/native-check.html'})
        content = firefox_content_sandbox(driver)
        pid = capabilities.get('moz:processID')
        if pid is None:
            driver.call('/moz/context', {'context': 'chrome'})
            try:
                pid = driver.script('return Services.appinfo.processID')
            finally:
                driver.call('/moz/context', {'context': 'content'})
        label = Path(f'/proc/{pid}/attr/current').read_text().strip()
        assert label.startswith('snap.firefox.firefox ') and '(enforce)' in label, label
        assert str(Path(f'/proc/{pid}/exe').resolve()).startswith('/snap/firefox/'), pid
        sandbox = {'browser_pid': pid, 'apparmor': label, 'content': content}
        receipt.setdefault('sandboxes', []).append(sandbox)
        receipt['browser_version'] = capabilities['browserVersion']
        receipt.setdefault('browser_windows', []).append(focus_browser(desktop, pid))
        return pid

    def close_browser():
        nonlocal driver, browser, log
        owned = []
        if browser:
            for directory in Path('/proc').iterdir():
                if not directory.name.isdecimal():
                    continue
                try:
                    if (directory / 'attr/current').read_text().startswith('snap.firefox.firefox'):
                        owned.append(int(directory.name))
                except (OSError, UnicodeError):
                    continue
        if driver:
            driver.close()
            driver = None
        if browser:
            try:
                browser.terminate()
            except ProcessLookupError:
                pass
            browser.wait(timeout=10)
            browser = None
        if owned:
            wait_for(lambda: not any(Path(f'/proc/{pid}').exists() for pid in owned),
                timeout=8, label='complete Snap browser process-tree exit')
            receipt.setdefault('browser_exits', []).append({'pids': owned, 'all_exited': True})
        if log:
            log.close()
            log = None

    def request(operation, params=None):
        driver.script('''
const output = document.getElementById('result'); output.textContent = '';
browser.runtime.getBackgroundPage().then(background => background.vidGrabNative.send(arguments[0], arguments[1]))
  .then(data => { output.textContent = JSON.stringify({ok: true, data}); },
    error => { output.textContent = JSON.stringify({ok: false, code: error.code || null, message: error.message}); });
''', operation, params or {})

    def response():
        return json.loads(wait_for(lambda: driver.script("return document.getElementById('result').textContent"),
            label='Snap native response'))

    try:
        receipt['previous_test_permission'] = reset_test_permission()
        config, registration, manifest = configure(args.app, args.home / 'app বাংলা', environment, 'firefox')
        archive = firefox_fixture(profile_root)
        profile = profile_root / 'profile'
        profile.mkdir(mode=0o700)
        with (args.home / 'portal.log').open('wb') as portal_log:
            monitor = subprocess.Popen(['dbus-monitor', '--session',
                "interface='org.freedesktop.portal.WebExtensions'"], env=environment,
                stdout=portal_log, stderr=portal_log)
        connect()
        request('status')
        missing = response()
        assert not missing['ok'] and not Path(config['data_dir']).exists(), missing
        checkpoint('missing-registration', response=missing)
        registration.write_text(json.dumps(manifest) + '\n')
        request('status')

        def permission():
            snapshot = desktop.snapshot()
            for dialog in snapshot['dialogs']:
                text = ' '.join(dialog['labels'])
                if 'VidGrab' in text or 'com.vidgrab.companion' in text:
                    return {'dialog': dialog, 'snapshot': snapshot}
            if driver.script("return document.getElementById('result').textContent"):
                raise AssertionError('Registered native request ended without a permission prompt: ' +
                    driver.script("return document.getElementById('result').textContent"))
            return None

        prompt = wait_for(permission, label='Snap native-host permission prompt')
        emit('permission', **prompt)
        while True:
            line = sys.stdin.readline()
            assert line, 'Host must acknowledge the observed permission input'
            command = json.loads(line)
            if command['op'] == 'permission-clicked':
                print(json.dumps({'reply': command['op'], 'clicked': True}), flush=True)
                break
            pointer_reply(desktop, command)
        started = response()
        assert started['ok'] and started['data']['state'] == 'ready', started
        initial = started['data']
        window = wait_for(lambda: desktop.window(initial['supervisor_pid']), label='Snap-activated native window')
        assert window['wayland'], window
        mount = mounted_image(initial['supervisor_pid']) if args.fuse else None
        if mount:
            image_mounts.append(mount['mountpoint'])
        checkpoint('permission-and-activation', response=started, prompt=prompt['dialog'], window=window, mount=mount)
        request('activate')
        activated = response()
        assert activated['ok'] and activated['data']['supervisor_pid'] == initial['supervisor_pid'], activated
        checkpoint('repeat-activation', response=activated)
        with fixture_server(gate) as url:
            request('fixture.download', {'url': url})
            admitted = response()
            assert admitted['ok'], admitted
            task = admitted['data']['task_id']
            wait_for(lambda: any(job['id'] == task and job['status'] == 'downloading'
                for job in local_call(config).get('jobs', [])), label='Snap fixture active')
            close_browser()
            alive = local_call(config)
            assert alive['supervisor_pid'] == initial['supervisor_pid'] and alive['engine_pid'] == initial['engine_pid']
            assert len(alive['jobs']) == 1 and alive['jobs'][0]['id'] == task \
                and alive['jobs'][0]['status'] == 'downloading', alive
            checkpoint('browser-exit-keeps-download', task_id=task, status=alive)
            gate.set()
            complete, checksum = completed_job(config, task)
        stop_owner(config)
        connect()
        request('status')
        restored = response()
        assert restored['ok'] and restored['data']['supervisor_pid'] != initial['supervisor_pid'], restored
        assert any(job['id'] == task and job['status'] == 'complete' for job in restored['data']['jobs']), restored
        restored_mount = mounted_image(restored['data']['supervisor_pid']) if args.fuse else None
        if restored_mount:
            image_mounts.append(restored_mount['mountpoint'])
        checkpoint('restart-restores-history', response=restored, media_sha256=checksum, mount=restored_mount)
        stop_owner(config)
        saved_descriptor = descriptor.read_bytes()
        missing_config = {**config, 'app': str(args.home / 'missing-app')}
        descriptor.write_text(json.dumps(missing_config))
        request('status')
        missing_app = response()
        assert not missing_app['ok'] and missing_app['code'] == 'APP_MISSING', missing_app
        descriptor.write_bytes(saved_descriptor)
        saved_descriptor = None
        checkpoint('missing-app', response=missing_app)
        removal = subprocess.run([str(args.app), '--unregister-browser-host'], env=environment,
            capture_output=True, text=True, check=True, timeout=30)
        request('status')
        disconnected = response()
        assert not disconnected['ok'] and not registration.exists(), disconnected
        checkpoint('disconnection', command_result=json.loads(removal.stdout), response=disconnected)
        assert 'GetManifest' in (args.home / 'portal.log').read_text() and 'GetPipes' in (args.home / 'portal.log').read_text()
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc(), launch=launch_diagnostics(args.home),
            desktop=desktop.snapshot())
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        emit('failure', failure=receipt['failure'])
        # Capture the actual failed launch/permission before browser cleanup.
        sys.stdin.readline()
        raise
    finally:
        gate.set()
        cleanup_errors = []

        def cleanup(name, operation):
            try:
                return operation()
            except BaseException:
                cleanup_errors.append({'step': name, 'failure': traceback.format_exc()})

        if saved_descriptor:
            cleanup('restore-descriptor', lambda: descriptor.write_bytes(saved_descriptor))
        cleanup('browser-exit', close_browser)
        if config:
            cleanup('owner-exit', lambda: stop_owner(config))
            cleanup('registration-removal', lambda: subprocess.run(
                [str(args.app), '--unregister-browser-host'], env=environment,
                capture_output=True, text=True, timeout=30, check=True))
        receipt['removed_test_permission'] = cleanup('permission-removal', reset_test_permission)
        if monitor:
            cleanup('portal-monitor-exit', monitor.terminate)
            cleanup('portal-monitor-reap', lambda: monitor.wait(timeout=5))
        for mountpoint in image_mounts:
            cleanup('image-unmount', lambda path=mountpoint: wait_for(lambda: not Path(path).exists(),
                label='Snap-activated AppImage unmount'))
        receipt['cleanup'] = {'result': 'failed' if cleanup_errors else 'passed',
            'errors': cleanup_errors, 'unmounted_images': image_mounts}
        if cleanup_errors:
            receipt['result'] = 'failed'
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
    emit('complete', result=receipt['result'], report=str(args.report))


if __name__ == '__main__':
    main()
