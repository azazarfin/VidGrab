"""Private Snap/Flatpak WebExtensions-portal peer, only inside its Ubuntu VM."""

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
from tests.firefox_flatpak_support import FlatpakProcesses, process_tree
from tests.native_support import WebDriver, fixture_server, local_call, stop_owner, wait_for


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def reset_test_permission(application='snap.firefox'):
    """Request a fresh real prompt for this fixture, without granting access."""
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    arguments = GLib.Variant('(sss)', ('webextensions', 'com.vidgrab.companion', application))

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
            else None, label='Firefox browser leaves overview')
    elif operation == 'pointer-position':
        snapshot = wait_for(lambda: value if abs((value := desktop.snapshot())['pointer']['x'] - request['x']) < 3
            and abs(value['pointer']['y'] - request['y']) < 3 else None, label='Firefox permission pointer position')
    elif operation in ('pointer-down', 'pointer-up'):
        snapshot = wait_for(lambda: value if (value := desktop.snapshot())['pointer']['primary_down']
            == (operation == 'pointer-down') else None, label='Firefox permission ' + operation)
    else:
        raise ValueError('Unsupported permission input observation')
    print(json.dumps({'reply': operation, 'snapshot': snapshot}), flush=True)


def focus_browser(desktop, pid):
    def visible_window():
        snapshot = desktop.snapshot()
        return snapshot if any(window['pid'] == pid for window in snapshot['windows']) else None

    snapshot = wait_for(visible_window, label='visible Firefox browser window')
    emit('browser-focus', pid=pid, snapshot=snapshot)
    while True:
        line = sys.stdin.readline()
        assert line, 'Host must observe browser focus'
        request = json.loads(line)
        if request['op'] == 'browser-focused':
            snapshot = wait_for(lambda: value if not (value := desktop.snapshot())['overview_visible']
                and any(window['pid'] == pid and window['focused'] for window in value['windows'])
                else None, label='Firefox browser has focus for portal access')
            print(json.dumps({'reply': request['op'], 'snapshot': snapshot}), flush=True)
            return snapshot
        pointer_reply(desktop, request)


def main(default_distribution='snap'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--geckodriver', type=Path, default=Path('/mnt/inputs/geckodriver'))
    parser.add_argument('--fuse', action='store_true')
    parser.add_argument('--distribution', choices=('snap', 'flatpak'), default=default_distribution)
    parser.add_argument('--enable-native-portal', action='store_true')
    args = parser.parse_args()
    guest_home = Path('/home/vidgrabtest')
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
            Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation' or \
            os.getuid() != 1000 or Path.home() != guest_home:
        parser.error('Select the explicitly isolated Ubuntu validation guest')
    if not args.home.is_absolute() or not args.home.is_relative_to(guest_home / 'validation') or \
            args.home.exists() or args.report.exists():
        parser.error('Select fresh home/report paths in the disposable guest')
    distribution = args.distribution
    if args.enable_native_portal and distribution != 'flatpak':
        parser.error('The explicit portal preference check is for Firefox Flatpak')
    application = 'snap.firefox' if distribution == 'snap' else 'org.mozilla.firefox'
    if distribution == 'snap':
        metadata = Path('/snap/firefox/current/meta/snap.yaml').read_text()
        assert re.search(r'^confinement:\s*strict\s*$', metadata, re.MULTILINE), 'Firefox must be a strict Snap'
    args.home.mkdir(mode=0o700, parents=True)
    browser_home = guest_home / ('snap/firefox/common' if distribution == 'snap'
        else '.var/app/org.mozilla.firefox')
    profile_root = browser_home / args.home.name
    assert not profile_root.exists(), 'Select a fresh sandbox-visible profile'
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
        'distribution': distribution,
        'native_portal_opt_in': args.enable_native_portal,
        'browser_launcher': '/snap/bin/firefox' if distribution == 'snap' else 'flatpak run --user org.mozilla.firefox',
        'firewall': firewall, 'checks': [], 'limits': ['disposable Ubuntu GNOME VM and virtual input',
            'temporary development extension; loopback Marionette with isolated system-access flag',
            'canonical browser launcher; no permission overrides or confinement disabling',
            'no other browser distribution, physical-device or Windows claim']}
    if distribution == 'snap':
        receipt['snap_revision'] = Path('/snap/firefox/current').resolve().name
    else:
        receipt['flatpak_version'] = subprocess.check_output(['flatpak', '--version'], text=True).strip()
        receipt['flatpak_info'] = subprocess.check_output(['flatpak', 'info', '--user', application], text=True)
        receipt['flatpak_permissions'] = subprocess.check_output(
            ['flatpak', 'info', '--user', '--show-permissions', application], text=True)
        for target in ([], [application]):
            overrides = subprocess.check_output(['flatpak', 'override', '--user', '--show', *target], text=True)
            assert not overrides.strip(), 'Select stock Flatpak permissions, without user overrides'
    config = driver = browser = monitor = None
    image_mounts = []
    gate = threading.Event()
    descriptor = guest_home / '.local/share/vidgrab-p02/native/host-config.json'
    saved_descriptor = None
    log = None
    flatpak_root_pid = None
    flatpak_instance = None

    def checkpoint(stage, **values):
        receipt['checks'].append({'stage': stage, **values})
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        emit('checkpoint', stage=stage)

    def connect():
        nonlocal browser, driver, log, flatpak_root_pid, flatpak_instance
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            marionette_port = reserved.getsockname()[1]
        preferences = {'extensions.webextensions.uuids': json.dumps({config['firefox_id']: FIREFOX_UUID}),
            'marionette.port': marionette_port, 'marionette.log.level': 'Info',
            'app.update.auto': False, 'browser.startup.homepage': 'about:blank',
            'browser.shell.checkDefaultBrowser': False, 'datareporting.healthreport.uploadEnabled': False,
            'toolkit.telemetry.enabled': False, 'network.trr.mode': 5}
        if args.enable_native_portal:
            preferences['widget.use-xdg-desktop-portal.native-messaging'] = 1
        (profile / 'user.js').write_text('\n'.join(f'user_pref({json.dumps(key)}, {json.dumps(value)});'
            for key, value in preferences.items()) + '\n')
        log = (args.home / 'firefox.log').open('ab')
        launcher = ['/snap/bin/firefox'] if distribution == 'snap' else ['flatpak', 'run', '--user',
            '--env=MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1', '--env=MOZ_LOG=NativeMessagingPortal:5', application]
        browser = subprocess.Popen([*launcher, '--no-remote', '--marionette',
            '--profile', str(profile)], env={**environment, 'MOZ_REMOTE_ALLOW_SYSTEM_ACCESS': '1'},
            stdout=log, stderr=log, start_new_session=True)
        driver = WebDriver(args.geckodriver, environment, args.home / 'driver.log',
            arguments=('--allow-system-access', '--connect-existing', f'--marionette-port={marionette_port}'))
        capabilities = driver.start({'browserName': 'firefox'})
        identifier = driver.call('/moz/addon/install', {'path': str(archive), 'temporary': True})
        assert identifier == config['firefox_id'], identifier
        driver.call('/url', {'url': f'moz-extension://{FIREFOX_UUID}/native-check.html'})
        if distribution == 'snap':
            content = firefox_content_sandbox(driver)
            pid = capabilities.get('moz:processID')
        else:
            pid = None
        if pid is None:
            driver.call('/moz/context', {'context': 'chrome'})
            try:
                pid = driver.script('return Services.appinfo.processID')
            finally:
                driver.call('/moz/context', {'context': 'content'})
        if distribution == 'flatpak':
            rows = [line.split() for line in subprocess.check_output(
                ['flatpak', 'ps', '--columns=instance,application,pid'], text=True).splitlines()]
            instances = [row for row in rows if len(row) == 3 and row[1] == application
                and row[2] == str(browser.pid)]
            assert len(instances) == 1, {'launcher_pid': browser.pid, 'instances': rows}
            flatpak_root_pid = int(instances[0][2])
            flatpak_instance = instances[0][0]
            receipt.setdefault('flatpak_process_observations', []).append({
                'instance': instances[0], 'processes': process_tree(flatpak_root_pid)})
            processes = FlatpakProcesses(flatpak_root_pid)
            namespace_pid = pid
            pid = processes.host_pid(namespace_pid)
            driver.call('/moz/context', {'context': 'chrome'})
            try:
                information = driver.script('''const done = arguments[arguments.length - 1];
                    ChromeUtils.requestProcInfo().then(info => done(info.children.map(
                        child => ({pid: child.pid, type: child.type}))));''', asynchronous=True)
                receipt['portal_preference'] = driver.script('''const key = 'widget.use-xdg-desktop-portal.native-messaging';
                    return {type: Services.prefs.getPrefType(key), user_set: Services.prefs.prefHasUserValue(key),
                        value: Services.prefs.getIntPref(key, -1)};''')
            finally:
                driver.call('/moz/context', {'context': 'content'})
            processes = FlatpakProcesses(flatpak_root_pid)
            content = processes.content(information)
            assert processes.entries[pid]['seccomp'] == '2' and processes.entries[pid]['no_new_privileges'] == '1'
            sandbox = {'browser_pid': pid, 'namespace_pid': namespace_pid, 'instance': instances[0][0],
                'instance_pid': flatpak_root_pid, 'namespace': processes.entries[pid]['namespace'],
                'content': content, 'flatpak_info': Path(f'/proc/{pid}/root/.flatpak-info').read_text()}
        else:
            label = Path(f'/proc/{pid}/attr/current').read_text().strip()
            assert label.startswith('snap.firefox.firefox ') and '(enforce)' in label, label
            assert str(Path(f'/proc/{pid}/exe').resolve()).startswith('/snap/firefox/'), pid
            sandbox = {'browser_pid': pid, 'apparmor': label, 'content': content}
        receipt.setdefault('sandboxes', []).append(sandbox)
        receipt['browser_version'] = capabilities['browserVersion']
        receipt.setdefault('browser_windows', []).append(focus_browser(desktop, pid))
        return pid

    def close_browser():
        nonlocal driver, browser, log, flatpak_root_pid, flatpak_instance
        owned = []
        if browser and distribution == 'flatpak':
            owned = list(process_tree(browser.pid))
            if flatpak_instance is None:
                rows = [line.split() for line in subprocess.check_output(
                    ['flatpak', 'ps', '--columns=instance,application,pid'], text=True).splitlines()]
                matches = [row for row in rows if len(row) == 3 and row[1] == application
                    and row[2] == str(browser.pid)]
                if matches:
                    assert len(matches) == 1, matches
                    flatpak_instance = matches[0][0]
        elif browser and distribution == 'snap':
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
            if distribution == 'flatpak' and flatpak_instance:
                # Gecko's connect-existing DELETE detaches. Killing only outer bwrap leaves Firefox alive.
                command = ['flatpak', 'kill', flatpak_instance]
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                receipt.setdefault('browser_stop_commands', []).append({'command': command,
                    'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                assert result.returncode == 0 or not any(Path(f'/proc/{pid}').exists() for pid in owned), result.stderr
            else:
                try:
                    browser.terminate()
                except ProcessLookupError:
                    pass
            browser.wait(timeout=10)
            browser = None
        if owned:
            wait_for(lambda: not any(Path(f'/proc/{pid}').exists() for pid in owned),
                timeout=8, label='complete Firefox browser process-tree exit')
            receipt.setdefault('browser_exits', []).append({'pids': owned, 'all_exited': True})
        flatpak_root_pid = None
        flatpak_instance = None
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
            label='Firefox native response'))

    try:
        receipt['previous_test_permission'] = reset_test_permission(application)
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

        prompt = wait_for(permission, label='Firefox native-host permission prompt')
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
        window = wait_for(lambda: desktop.window(initial['supervisor_pid']), label='Firefox-activated native window')
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
                for job in local_call(config).get('jobs', [])), label='Firefox fixture active')
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
        receipt['removed_test_permission'] = cleanup('permission-removal', lambda: reset_test_permission(application))
        if monitor:
            cleanup('portal-monitor-exit', monitor.terminate)
            cleanup('portal-monitor-reap', lambda: monitor.wait(timeout=5))
        for mountpoint in image_mounts:
            cleanup('image-unmount', lambda path=mountpoint: wait_for(lambda: not Path(path).exists(),
                label='Firefox-activated AppImage unmount'))
        receipt['cleanup'] = {'result': 'failed' if cleanup_errors else 'passed',
            'errors': cleanup_errors, 'unmounted_images': image_mounts}
        if cleanup_errors:
            receipt['result'] = 'failed'
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
    emit('complete', result=receipt['result'], report=str(args.report))


if __name__ == '__main__':
    main()
