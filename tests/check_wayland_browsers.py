"""Opt-in native Chrome/Firefox package handoff with content sandboxes in a test VM."""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import check_installed_browsers as browsers
from tests.check_packaged_desktop import HOST, configure, launch_diagnostics
from tests.check_wayland_desktop import Desktop
from tests.native_support import fixture_server


def remove_browser_registration(app, environment, fixture_root):
    result = subprocess.run([str(app), '--unregister-browser-host'], env=environment,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    registration = fixture_root / 'chrome-check/profile/NativeMessagingHosts' / HOST
    profile_removed = False
    if registration.exists() or registration.is_symlink():
        assert not registration.is_symlink(), 'Refuse a symlinked diagnostic registration'
        bridge = Path(environment['XDG_DATA_HOME']) / 'vidgrab-p02/native'
        descriptor = json.loads((bridge / 'host-config.json').read_text())
        manifest = json.loads(registration.read_text())
        assert manifest.get('name') == HOST.removesuffix('.json') \
            and manifest.get('type') == 'stdio' \
            and manifest.get('path') == str(bridge / 'vidgrab-native-host') \
            and manifest.get('allowed_origins') == [descriptor['chrome_origin']], manifest
        registration.unlink()
        profile_removed = True
    return {'canonical': json.loads(result.stdout), 'diagnostic_profile_removed': profile_removed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--chrome', type=Path, default=Path('/opt/google/chrome/chrome'))
    parser.add_argument('--firefox', type=Path, default=Path('/mnt/inputs/firefox/firefox'))
    parser.add_argument('--geckodriver', type=Path, default=Path('/mnt/inputs/geckodriver'))
    parser.add_argument('--browser', choices=('chrome', 'firefox', 'both'), default='both')
    args = parser.parse_args()
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
            Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation':
        parser.error('Select the explicitly isolated Ubuntu VM')
    if args.home.exists() or args.report.exists():
        parser.error('Select fresh home and report paths')
    os.environ.update(XDG_RUNTIME_DIR='/run/user/1000', DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
    values = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line)
    assert values['XDG_SESSION_TYPE'] == 'wayland' and Desktop().snapshot()['wayland']
    app = args.app.resolve(strict=True)
    args.home.mkdir(mode=0o700, parents=True)
    environment = {**os.environ, **{key: values[key] for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE')},
        'HOME': str(args.home), 'XDG_DATA_HOME': str(args.home / 'data'),
        'XDG_CONFIG_HOME': str(args.home / 'config'), 'XDG_CACHE_HOME': str(args.home / 'cache'),
        'VIDGRAB_VALIDATION_HOME': str(args.home), 'GDK_BACKEND': 'wayland',
        'LIBGL_ALWAYS_SOFTWARE': '1', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1'}
    for key in ('VIDGRAB_DISABLE_TRAY', 'APPIMAGE_EXTRACT_AND_RUN', 'MOZ_DISABLE_CONTENT_SANDBOX',
            'MOZ_DISABLE_GMP_SANDBOX', 'MOZ_DISABLE_RDD_SANDBOX'):
        environment.pop(key, None)
        os.environ.pop(key, None)
    os.environ.update(environment)
    receipt = {'app': str(app), 'kernel': platform.release(), 'result': 'running', 'browsers': [],
        'limits': ['native packages/headless browser drivers; no Snap/Flatpak',
            'development extensions and isolated browser debugging', 'no Windows claim']}

    def checkpoint():
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')

    def failure(config, root):
        receipt['before_cleanup'] = {'config': config, 'launch': launch_diagnostics(args.home),
            'desktop': Desktop().snapshot()}
        checkpoint()

    original = browsers.configure
    browsers.configure = lambda bundle, root, browser: configure(app, root, environment, browser)
    try:
        if args.browser in ('chrome', 'both'):
            gate = threading.Event()
            with fixture_server(gate) as url:
                receipt['browsers'].append(browsers.check_chrome(app.parent, environment, url,
                    args.chrome, chromium_sandbox=True, transfer_gate=gate))
                checkpoint()
        if args.browser in ('firefox', 'both'):
            gate = threading.Event()
            with fixture_server(gate) as url:
                receipt['browsers'].append(browsers.check_firefox(app.parent, environment, url,
                    args.firefox, args.geckodriver, on_failure=failure, require_content_sandbox=True,
                    transfer_gate=gate))
                checkpoint()
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc(), launch=launch_diagnostics(args.home))
        raise
    finally:
        browsers.configure = original
        try:
            receipt['cleanup'] = {'result': 'passed',
                **remove_browser_registration(app, environment, args.home)}
        except Exception:
            receipt['cleanup'] = {'result': 'failed', 'failure': traceback.format_exc()}
            receipt['result'] = 'failed'
        checkpoint()
        print(json.dumps(receipt, indent=2), flush=True)
    assert receipt['result'] == 'passed', receipt['cleanup']


if __name__ == '__main__':
    main()
