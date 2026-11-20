"""Opt-in actual-package lifecycle check in a disposable Ubuntu GNOME Wayland VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_packaged_desktop import configure, launch_diagnostics
from tests.native_support import WebDriver, fixture_server, local_call, stop_owner, wait_for


class Desktop:
    def __init__(self):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.Gio = Gio
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(self, name, path, interface, method, signature=None, arguments=()):
        parameters = self.GLib.Variant(signature, arguments) if signature else None
        return self.bus.call_sync(name, path, interface, method, parameters, None,
            self.Gio.DBusCallFlags.NONE, 5000, None).unpack()

    def snapshot(self):
        return json.loads(self.call('com.vidgrab.Validation', '/com/vidgrab/Validation',
            'com.vidgrab.Validation', 'Snapshot')[0])

    def window(self, pid):
        return next((window for window in self.snapshot()['windows']
            if window['pid'] == pid and window['title'] == 'VidGrab'), None)

    def close_window(self, pid):
        self.call('com.vidgrab.Validation', '/com/vidgrab/Validation',
            'com.vidgrab.Validation', 'CloseWindow', '(i)', (pid,))

    def tray(self):
        items = self.call('org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher',
            'org.freedesktop.DBus.Properties', 'Get', '(ss)',
            ('org.kde.StatusNotifierWatcher', 'RegisteredStatusNotifierItems'))[0]
        self.observed_trays = []
        for item in items:
            name, separator, path = item.partition('/')
            # GNOME AppIndicator identifies an item as bus-name@/object/path.
            name = name.removesuffix('@')
            path = '/' + path if separator else '/StatusNotifierItem'
            properties = self.call(name, path, 'org.freedesktop.DBus.Properties',
                'GetAll', '(s)', ('org.kde.StatusNotifierItem',))[0]
            self.observed_trays.append({'name': name, 'path': path,
                'properties': {key: properties.get(key) for key in ('Title', 'Id', 'Status', 'Menu')}})
            if 'vidgrab' in properties.get('Title', '').lower() or 'vidgrab' in properties.get('Id', '').lower():
                return {'name': name, 'path': path, 'properties': properties}
        return None

    def menu(self, tray):
        _, layout = self.call(tray['name'], tray['properties']['Menu'],
            'com.canonical.dbusmenu', 'GetLayout', '(iias)', (0, -1, []))
        entries = []
        def visit(node):
            identifier, properties, children = node
            if 'label' in properties:
                entries.append({'id': identifier, 'label': properties['label']})
            for child in children:
                visit(child)
        visit(layout)
        return entries

    def select(self, tray, label):
        item = next(item for item in self.menu(tray) if item['label'] == label)
        # A menu event is a mutation: send it once, never replay a lost reply.
        self.call(tray['name'], tray['properties']['Menu'], 'com.canonical.dbusmenu',
            'Event', '(isvu)', (item['id'], 'clicked', self.GLib.Variant('i', 0), 0))


def mounted_image(pid):
    executable = Path(f'/proc/{pid}/exe').resolve(strict=True)
    entries = [line for line in Path('/proc/self/mountinfo').read_text().splitlines()
        if line.split(' - ')[1].split()[0].startswith('fuse.')]
    matches = [line for line in entries if executable.is_relative_to(Path(line.split()[4]))]
    assert len(matches) == 1, {'executable': str(executable), 'mounts': entries}
    return {'executable': str(executable), 'mountpoint': matches[0].split()[4], 'mountinfo': matches[0]}


def check(app, environment, screenshots, *, fuse):
    desktop = Desktop()
    initial = desktop.snapshot()
    assert initial['wayland'], initial
    config = configure(app, Path(environment['HOME']) / 'Wayland desktop বাংলা', environment)
    driver = WebDriver('/usr/bin/WebKitWebDriver',
        {**environment, 'TAURI_WEBVIEW_AUTOMATION': 'true'}, Path(environment['HOME']) / 'driver.log')
    try:
        started = time.monotonic()
        webview = driver.start({'webkitgtk:browserOptions': {'binary': str(app), 'args': []}})
        wait_for(lambda: driver.script("return document.querySelector('.connection')?.textContent === 'Connected'"),
            label='Wayland package connected UI')
        startup = round(time.monotonic() - started, 3)
        assert driver.script("return document.body.innerText.includes('running in the tray')")
        first = local_call(config)
        pid = first['supervisor_pid']
        window = wait_for(lambda: desktop.window(pid), label='native Wayland window')
        assert window['wayland'] and not window['hidden'], window
        tray = wait_for(desktop.tray, label='registered VidGrab tray item')
        panel_name = f"appindicator-{tray['name']}@{tray['path']}"
        panel = wait_for(lambda: [item for item in desktop.snapshot()['indicators']
            if item['name'] == panel_name and item['visible'] and item['mapped'] and item['width'] > 0],
            label='visible GNOME VidGrab tray indicator')
        assert {entry['label'] for entry in desktop.menu(tray)} >= {'Open VidGrab', 'Quit VidGrab'}
        mount = mounted_image(pid) if fuse else None
        driver.screenshot(screenshots / 'wayland-empty.png')
        with fixture_server() as url:
            driver.click('#toggle-media-options')
            driver.click('#option-audio-only')
            driver.fill('#media-url', url)
            driver.click('.download-form button.primary')
            desktop.close_window(pid)
            wait_for(lambda: (window := desktop.window(pid)) is None or window['hidden'],
                label='compositor Close hides window')
            assert local_call(config)['supervisor_pid'] == pid
            completed = wait_for(lambda: snapshot if (snapshot := local_call(config)).get('jobs')
                and any(job['status'] == 'complete' for job in snapshot['jobs']) else None,
                timeout=30, label='hidden-window frozen MP3 completion')
        media = Path(completed['jobs'][0]['file_path'])
        assert media.is_file() and media.suffix == '.mp3' and 'বাংলা' in str(media), completed
        checksum = hashlib.sha256(media.read_bytes()).hexdigest()
        desktop.select(tray, 'Open VidGrab')
        wait_for(lambda: (window := desktop.window(pid)) and not window['hidden'], label='tray Open reopens window')
        assert local_call(config)['engine_pid'] == first['engine_pid']
        driver.click('[data-desktop-tab="history"]')
        wait_for(lambda: driver.script("return document.querySelector('.history-view .job-status')?.textContent === 'Finished'"),
            label='reopened Wayland history')
        driver.screenshot(screenshots / 'wayland-complete.png')
        desktop.close_window(pid)
        wait_for(lambda: (window := desktop.window(pid)) is None or window['hidden'], label='second compositor Close')
        repeated = subprocess.run([str(app)], env=environment, capture_output=True, timeout=30)
        assert repeated.returncode == 0, repeated.stderr.decode(errors='replace')
        wait_for(lambda: (window := desktop.window(pid)) and not window['hidden'], label='package activation reopens window')
        assert local_call(config)['supervisor_pid'] == pid
        os.kill(first['engine_pid'], signal.SIGKILL)
        recovered = wait_for(lambda: snapshot if (snapshot := local_call(config)).get('engine_generation') == 2
            and snapshot.get('state') == 'ready' else None, label='Wayland engine recovery')
        assert recovered['supervisor_pid'] == pid and recovered['jobs'][0]['status'] == 'complete'
        assert hashlib.sha256(media.read_bytes()).hexdigest() == checksum
        desktop.select(tray, 'Quit VidGrab')
        wait_for(lambda: not (Path(config['data_dir']) / 'ipc/control.sock').exists()
            and not Path(f'/proc/{pid}').exists() and not Path(f'/proc/{recovered["engine_pid"]}').exists(),
            timeout=8, label='tray Quit reaps owner and engine')
        wait_for(lambda: not any(item['name'] == panel_name for item in desktop.snapshot()['indicators']),
            label='tray removal after Quit')
        if mount:
            wait_for(lambda: not any(line.split()[4] == mount['mountpoint']
                for line in Path('/proc/self/mountinfo').read_text().splitlines()), timeout=8,
                label='normal OS FUSE unmount after tray Quit')
        return {'result': 'passed', 'webview': webview, 'startup_seconds': startup,
            'window': window, 'panel': panel, 'media_sha256': checksum, 'fuse_mount': mount,
            'checks': ['native Wayland window', 'visible GNOME tray', 'Close hides window',
                'hidden-window MP3 completion', 'tray Open', 'repeated package activation',
                'engine recovery', 'tray Quit and removal']}
    except BaseException:
        diagnostics = {'launch': launch_diagnostics(Path(environment['HOME'])),
            'trays': getattr(desktop, 'observed_trays', [])}
        for name, probe in [('desktop', desktop.snapshot), ('owner', lambda: local_call(config)),
                ('page', lambda: driver.script('return document.body.innerText'))]:
            try:
                diagnostics[name] = probe()
            except Exception as error:
                diagnostics[name] = str(error)
        (screenshots / 'wayland-diagnostics.json').write_text(json.dumps(diagnostics, indent=2) + '\n')
        raise
    finally:
        stop_owner(config)
        driver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1':
        parser.error('Use an explicitly isolated disposable VM, never the real user session')
    if os.environ.get('XDG_SESSION_TYPE') != 'wayland' or not os.environ.get('WAYLAND_DISPLAY'):
        parser.error('Select the disposable GNOME Wayland session')
    if args.home.exists() or args.report.exists():
        parser.error('Select fresh home and report paths')
    if args.fuse and args.app.suffix != '.AppImage':
        parser.error('--fuse requires an AppImage')
    app = args.app.resolve(strict=True)
    args.home.mkdir(mode=0o700, parents=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'HOME': str(args.home.resolve()), 'GDK_BACKEND': 'wayland',
        'XDG_DATA_HOME': str(args.home.resolve() / 'data'), 'XDG_CONFIG_HOME': str(args.home.resolve() / 'config'),
        'XDG_CACHE_HOME': str(args.home.resolve() / 'cache'), 'PATH': '/usr/bin:/bin'}
    environment.pop('VIDGRAB_DISABLE_TRAY', None)
    environment.pop('APPIMAGE_EXTRACT_AND_RUN', None)
    os.environ.update(environment)
    receipt = {'app': str(app), 'os_release': platform.freedesktop_os_release(), 'kernel': platform.release(),
        'result': 'running', 'limits': ['KVM virtual display/software GPU',
            'compositor probe and real D-Bus tray menu events; no physical pointer',
            'isolated native Ubuntu session; no Windows or sandboxed-browser claim']}
    try:
        receipt['desktop'] = check(app, environment, args.screenshots, fuse=args.fuse)
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        raise
    finally:
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    main()
