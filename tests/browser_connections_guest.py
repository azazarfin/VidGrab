"""Actual package connection controls, only in the disposable Ubuntu GNOME VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_packaged_desktop import configure, launch_diagnostics
from tests.check_wayland_desktop import Desktop, mounted_image
from tests.firefox_manual_setup import VirtualSetup
from tests.firefox_portal_guest import focus_browser, pointer_reply
from tests.native_support import WebDriver, fixture_server, local_call, stop_owner, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
            Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation':
        parser.error('Select only the named disposable Ubuntu VM')
    if not args.home.is_absolute() or not args.home.is_relative_to('/home/vidgrabtest/validation') \
            or args.home.exists() or args.report.exists():
        parser.error('Select fresh fixture home/report paths in the disposable guest')
    session = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line)
    assert session.get('XDG_SESSION_TYPE') == 'wayland' and session.get('WAYLAND_DISPLAY'), session
    firewall = subprocess.check_output(['sudo', '-n', 'nft', 'list', 'table', 'inet', 'vidgrab_validation'], text=True)
    assert firewall.count('policy drop') == 2 and firewall.count(' accept') == 6, firewall
    args.home.mkdir(mode=0o700, parents=True)
    screenshots = args.home / 'screenshots'
    screenshots.mkdir(mode=0o700)
    environment = {**os.environ, **session, 'HOME': str(args.home),
        'XDG_DATA_HOME': str(args.home / 'data'), 'XDG_CONFIG_HOME': str(args.home / 'config'),
        'XDG_CACHE_HOME': str(args.home / 'cache'), 'PATH': '/usr/bin:/bin',
        'LIBGL_ALWAYS_SOFTWARE': '1', 'GDK_BACKEND': 'wayland'}
    config = configure(args.app.resolve(strict=True), args.home / 'fixture বাংলা', environment)
    desktop = Desktop()
    virtual = VirtualSetup(desktop, pointer_reply)
    driver = None
    mounts = []
    receipt = {'result': 'running', 'app': str(args.app), 'kernel': platform.release(),
        'app_sha256': hashlib.sha256(args.app.read_bytes()).hexdigest(), 'firewall': firewall,
        'checks': [], 'limits': ['disposable Ubuntu GNOME VM; virtual pointer input',
            'compiled UI and copied native host; no installed-browser portal decision',
            'connection removal preserves files; no package uninstaller or Windows evidence']}

    def emit(event, **values):
        print(json.dumps({'event': event, **values}), flush=True)

    def checkpoint(stage, **values):
        receipt['checks'].append({'stage': stage, **values})
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        driver.screenshot(screenshots / (stage + '.png'))
        emit('checkpoint', stage=stage)

    def cli_status():
        result = subprocess.run([str(args.app), '--browser-connection-status'], env=environment,
            capture_output=True, text=True, check=True, timeout=20)
        return json.loads(result.stdout)

    def native_status():
        payload = json.dumps({'spike_version': 1, 'id': 'connection-controls', 'op': 'status', 'params': {}}).encode()
        native = args.home / 'data/vidgrab-p02/native/vidgrab-native-host'
        result = subprocess.run([str(native), config['chrome_origin']], env=environment,
            input=struct.pack('=I', len(payload)) + payload, capture_output=True, check=True, timeout=20)
        size = struct.unpack('=I', result.stdout[:4])[0]
        assert 0 < size <= 65536 and len(result.stdout) == size + 4, result.stderr
        response = json.loads(result.stdout[4:])
        assert response['id'] == 'connection-controls', response
        return response

    def open_app():
        nonlocal driver
        driver = WebDriver('/usr/bin/WebKitWebDriver',
            {**environment, 'TAURI_WEBVIEW_AUTOMATION': 'true'}, args.home / f'driver-{len(mounts)}.log')
        driver.start({'webkitgtk:browserOptions': {'binary': str(args.app), 'args': []}})
        wait_for(lambda: driver.script('return document.querySelector(".connection")?.textContent === "Connected"'),
            label='packaged connection controls ready')
        owner = local_call(config)
        focus_browser(desktop, owner['supervisor_pid'])
        window = desktop.window(owner['supervisor_pid'])
        assert window['wayland'] and window['visible'], window
        if args.fuse:
            mounts.append(mounted_image(owner['supervisor_pid']))
        return owner

    def summary(text):
        driver.script('document.querySelector(\'[data-desktop-tab="browser"]\')?.click()')
        return driver.script('return document.querySelector(".browser-connection-summary")?.textContent === arguments[0]', text)

    def click_connection(action, expected):
        driver.script('document.querySelector(\'[data-desktop-tab="browser"]\')?.click()')
        selector = f'[data-connection-action="{action}"]'
        driver.script('document.querySelector(arguments[0]).scrollIntoView({block:"center"})', selector)
        virtual.input('Click ' + action + ' browser connection', expected,
            rectangle=virtual.rectangle(driver, selector))

    def close_app():
        nonlocal driver
        stop_owner(config)
        if driver:
            driver.close()
            driver = None
        for mount in mounts:
            wait_for(lambda: not any(line.split()[4] == mount['mountpoint']
                for line in Path('/proc/self/mountinfo').read_text().splitlines()), timeout=8,
                label='connection controls AppImage unmount')

    try:
        first = open_app()
        wait_for(lambda: summary('Registered for Chrome and Firefox'), label='initial browser registration')
        checkpoint('registered', status=cli_status(), owner_pid=first['supervisor_pid'])
        gate = threading.Event()
        with fixture_server(gate) as url:
            driver.script('document.querySelector(\'[data-desktop-tab="queue"]\')?.click()')
            driver.click('#toggle-media-options')
            driver.click('#option-audio-only')
            driver.fill('#media-url', url)
            driver.click('.download-form button.primary')
            active = wait_for(lambda: status if (status := local_call(config)).get('jobs')
                and status['jobs'][0]['status'] == 'downloading' else None, label='gated active fixture')
            click_connection('disconnect', lambda: summary('Disconnected'))
            status = cli_status()
            assert not status['enabled'] and status['chrome'] == status['firefox'] == 'missing', status
            rejected = native_status()
            assert rejected['error']['code'] == 'BROWSER_DISCONNECTED', rejected
            current = local_call(config)
            assert current['supervisor_pid'] == first['supervisor_pid'] and len(current['jobs']) == 1
            assert current['jobs'][0]['status'] == 'downloading', current
            checkpoint('disconnect-keeps-download', status=status, native=rejected, owner_pid=current['supervisor_pid'])
            gate.set()
            finished = wait_for(lambda: status if (status := local_call(config))['jobs'][0]['status'] == 'complete'
                else None, timeout=30, label='disconnected fixture completion')
        job = finished['jobs'][0]
        media = Path(job['file_path'])
        checksum = hashlib.sha256(media.read_bytes()).hexdigest()
        assert checksum == '958bcfd31005dab8833844f7c058a3514e02baa209df62ab110228245a50f9a5', checksum
        close_app()
        reopened = open_app()
        wait_for(lambda: summary('Disconnected'), label='reopened browser disconnection')
        assert reopened['supervisor_pid'] != first['supervisor_pid'] and reopened['jobs'] == [job], reopened
        assert hashlib.sha256(media.read_bytes()).hexdigest() == checksum
        assert not cli_status()['enabled'] and native_status()['error']['code'] == 'BROWSER_DISCONNECTED'
        checkpoint('reopen-retains-disconnection', job=job, media_sha256=checksum, status=cli_status())
        foreign = args.home / '.mozilla/native-messaging-hosts/com.vidgrab.companion.json'
        foreign_bytes = b'{"name":"com.vidgrab.companion","path":"/another/test/host"}'
        foreign.write_bytes(foreign_bytes)
        click_connection('connect', lambda: driver.script('return document.querySelector("[role=alert]")?.textContent.includes("another installation")'))
        status = cli_status()
        assert not status['enabled'] and status['firefox'] == 'conflict' and status['chrome'] == 'missing', status
        assert foreign.read_bytes() == foreign_bytes and summary('Disconnected')
        checkpoint('foreign-registration-preserved', status=status)
        foreign.unlink()
        click_connection('connect', lambda: summary('Registered for Chrome and Firefox'))
        status = cli_status()
        response = native_status()
        assert status['enabled'] and status['chrome'] == status['firefox'] == 'registered'
        assert response['ok'] and response['data']['supervisor_pid'] == reopened['supervisor_pid'], response
        assert local_call(config)['jobs'] == [job] and hashlib.sha256(media.read_bytes()).hexdigest() == checksum
        checkpoint('reconnect-preserves-history', status=status, native=response, media_sha256=checksum)
        click_connection('disconnect', lambda: summary('Disconnected'))
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc(), diagnostics=launch_diagnostics(args.home))
        if driver:
            try:
                receipt['page'] = driver.script('return document.body.innerText')
                driver.screenshot(screenshots / 'failure.png')
            except Exception as error:
                receipt['page_error'] = str(error)
        emit('failure', failure=receipt['failure'])
        # Hold cleanup until the host captures the current display or closes stdin.
        sys.stdin.readline()
        raise
    finally:
        try:
            close_app()
            receipt['cleanup'] = {'result': 'passed', 'unmounted_images': [mount['mountpoint'] for mount in mounts]}
        except Exception:
            receipt['cleanup'] = {'result': 'failed', 'failure': traceback.format_exc()}
            receipt['result'] = 'failed'
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        emit('complete', result=receipt['result'])


if __name__ == '__main__':
    main()
