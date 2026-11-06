"""Private JSON-line peer for check_gnome_pointer.py, only inside its test VM."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_packaged_desktop import configure
from tests.check_wayland_desktop import Desktop, mounted_image
from tests.native_support import local_call, stop_owner, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    assert Path('/etc/hostname').read_text().strip() == 'vidgrab-p02-ubuntu-validation'
    assert not args.home.exists(), 'Select a fresh VM test home'
    os.environ.update(XDG_RUNTIME_DIR='/run/user/1000', DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
    values = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line)
    environment = {**os.environ, **{key: values[key] for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE')},
        'HOME': str(args.home), 'XDG_DATA_HOME': str(args.home / 'data'),
        'XDG_CONFIG_HOME': str(args.home / 'config'), 'XDG_CACHE_HOME': str(args.home / 'cache'),
        'GDK_BACKEND': 'wayland', 'LIBGL_ALWAYS_SOFTWARE': '1', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1'}
    environment.pop('VIDGRAB_DISABLE_TRAY', None)
    environment.pop('APPIMAGE_EXTRACT_AND_RUN', None)
    assert environment['XDG_SESSION_TYPE'] == 'wayland'
    os.environ.update(environment)
    desktop = Desktop()
    assert desktop.snapshot()['wayland']
    args.home.mkdir(mode=0o700, parents=True)
    config = configure(args.app, args.home / 'app', environment)
    process = None
    pid = None
    mount = None

    def snapshot():
        value = desktop.snapshot()
        value['window'] = next((window for window in value['windows'] if window['pid'] == pid), None)
        return value

    try:
        for line in sys.stdin:
            request = json.loads(line)
            operation = request['op']
            if operation == 'start':
                assert process is None
                with (args.home / 'app.log').open('wb') as log:
                    process = subprocess.Popen([str(args.app)], env=environment,
                        start_new_session=True, stdout=log, stderr=log)
                # Reap the owned child as soon as it exits; do not poll it.
                threading.Thread(target=process.wait, daemon=True).start()
                status = wait_for(lambda: value if (value := local_call(config)).get('state') == 'ready'
                    else None, label='pointer owner ready')
                pid = status['supervisor_pid']
                wait_for(lambda: snapshot()['window'], label='pointer native window')
                tray = wait_for(desktop.tray, label='pointer tray item')
                panel_name = f"appindicator-{tray['name']}@{tray['path']}"
                wait_for(lambda: any(item['name'] == panel_name and item['mapped']
                    for item in snapshot()['indicators']), label='pointer panel indicator')
                mount = mounted_image(pid) if args.fuse else None
                result = {'status': status, 'snapshot': snapshot(), 'panel_name': panel_name, 'mount': mount}
            elif operation == 'desktop':
                result = wait_for(lambda: value if not (value := snapshot())['overview_visible']
                    else None, label='pointer leaves overview')
            elif operation == 'pointer-position':
                result = wait_for(lambda: value if abs((value := snapshot())['pointer']['x'] - request['x']) < 3
                    and abs(value['pointer']['y'] - request['y']) < 3 else None, label='USB tablet position')
            elif operation in ('pointer-down', 'pointer-up'):
                result = wait_for(lambda: value if (value := snapshot())['pointer']['primary_down']
                    == (operation == 'pointer-down') else None, label=operation)
            elif operation == 'focused':
                result = wait_for(lambda: value if (value := snapshot())['window']['focused']
                    else None, label='pointer focuses VidGrab')
            elif operation == 'hidden':
                result = wait_for(lambda: value if not (value := snapshot())['window']
                    or value['window']['hidden'] else None, label='keyboard Close hides VidGrab')
                assert local_call(config)['supervisor_pid'] == pid
            elif operation == 'visible':
                result = wait_for(lambda: value if (value := snapshot())['window']
                    and not value['window']['hidden'] else None, label='pointer tray Open')
                assert local_call(config)['supervisor_pid'] == pid
            elif operation == 'menu':
                def open_menu():
                    value = snapshot()
                    return value if any(item['name'] == panel_name and item['menu_open']
                        and item['menu'] for item in value['indicators']) else None
                result = wait_for(open_menu, label='pointer opens GNOME tray menu')
            elif operation == 'quit':
                process.wait(timeout=8)
                wait_for(lambda: not (Path(config['data_dir']) / 'ipc/control.sock').exists()
                    and not Path(f'/proc/{pid}').exists()
                    and not Path(f'/proc/{status["engine_pid"]}').exists()
                    and not any(item['name'] == panel_name for item in snapshot()['indicators']),
                    timeout=8, label='pointer tray Quit')
                if mount:
                    wait_for(lambda: not any(line.split()[4] == mount['mountpoint']
                        for line in Path('/proc/self/mountinfo').read_text().splitlines()),
                        timeout=8, label='pointer FUSE unmount')
                result = {'quit': True, 'mount_removed': bool(mount)}
            else:
                raise ValueError('Unsupported pointer-check operation')
            print(json.dumps(result), flush=True)
    except BaseException:
        diagnostic = {'error': traceback.format_exc()}
        for name, probe in [('snapshot', desktop.snapshot), ('owner', lambda: local_call(config))]:
            try:
                diagnostic[name] = probe()
            except Exception as error:
                diagnostic[name] = str(error)
        print(json.dumps(diagnostic), flush=True)
        # Keep the launch alive until the host captures the screen and closes stdin.
        sys.stdin.readline()
        raise
    finally:
        stop_owner(config)
        if process:
            process.wait(timeout=8)


if __name__ == '__main__':
    main()
