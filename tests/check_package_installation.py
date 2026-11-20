"""Opt-in Ubuntu VM package removal/repair check using completed fixture homes."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_support import wait_for


def run(arguments, environment=None):
    try:
        return subprocess.run([str(arg) for arg in arguments], env=environment, capture_output=True,
            text=True, check=True, timeout=60).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'{error}\nstdout:\n{error.stdout or ""}\nstderr:\n{error.stderr or ""}') from error


def environment(home):
    return {**os.environ, 'HOME': str(home), 'XDG_DATA_HOME': str(home / 'data'),
        'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
        'PATH': '/usr/bin:/bin'}


def saved(home):
    return json.loads((home / 'data/vidgrab-p02/native/host-config.json').read_text())


def snapshot(config):
    assert not (Path(config['data_dir']) / 'ipc/control.sock').exists(), 'Quit the selected fixture owner first'
    media = list(Path(config['download_dir']).rglob('*.mp3'))
    assert media and (Path(config['data_dir']) / 'vidgrab.db').is_file(), 'Use a completed fixture home'
    result = {}
    for directory in (Path(config['data_dir']), Path(config['download_dir'])):
        for path in directory.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    result[str(path)] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def register(app, home):
    before = saved(home)
    value = json.loads(run([app, '--register-browser-host'], environment(home)))
    assert value['package_version'] == 1 and 'engine' not in value
    assert value['data_dir'] == before['data_dir'] and value['download_dir'] == before['download_dir']
    manifest = json.loads((home / 'config/google-chrome/NativeMessagingHosts/com.vidgrab.companion.json').read_text())
    return value, manifest


def unregister(app, home):
    value = json.loads(run([app, '--unregister-browser-host'], environment(home)))
    assert not (home / 'config/google-chrome/NativeMessagingHosts/com.vidgrab.companion.json').exists()
    assert not (home / '.mozilla/native-messaging-hosts/com.vidgrab.companion.json').exists()
    assert (home / 'data/vidgrab-p02/native/host-config.json').is_file()
    return value


def missing_app(manifest, home, expected_code='APP_MISSING'):
    payload = json.dumps({'spike_version': 1, 'id': 'package-removal', 'op': 'status', 'params': {}}).encode()
    result = subprocess.run([manifest['path'], manifest['allowed_origins'][0]],
        input=struct.pack('=I', len(payload)) + payload, env=environment(home),
        capture_output=True, check=True, timeout=15)
    assert len(result.stdout) >= 4, result.stderr.decode(errors='replace')
    size = struct.unpack('=I', result.stdout[:4])[0]
    assert 0 < size <= 65536 and len(result.stdout) == size + 4
    response = json.loads(result.stdout[4:])
    assert response['id'] == 'package-removal' and not response['ok'], response
    assert response['error']['code'] == expected_code, response
    return response


@contextmanager
def mounted(app):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(app), '--appimage-mount'], stdout=subprocess.PIPE, stderr=errors)
        try:
            assert select.select([process.stdout], [], [], 10)[0], 'Image mount readiness'
            mount = Path(process.stdout.readline().decode().strip())
            assert mount.is_dir() and (mount / 'usr/bin/vidgrab-desktop').is_file(), str(mount)
            yield mount
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            process.stdout.close()
            if 'mount' in locals():
                wait_for(lambda: not any(line.split()[4] == str(mount)
                    for line in Path('/proc/self/mountinfo').read_text().splitlines()),
                    timeout=8, label='headless image unmount')


def check_deb(deb, home):
    app = Path('/usr/bin/vidgrab-desktop')
    config = saved(home)
    files = snapshot(config)
    layout = json.loads(run([app, '--check-package'], environment(home)))
    components = json.loads(run([layout['engine'], '--self-check'], {**environment(home), 'PATH': ''}))
    _, manifest = register(app, home)
    disconnected = unregister(app, home)
    disconnected_native = missing_app(manifest, home, expected_code='BROWSER_DISCONNECTED')
    _, manifest = register(app, home)
    assert snapshot(config) == files
    package = run(['dpkg-deb', '-f', deb, 'Package']).strip()
    try:
        removal = run(['sudo', 'apt-get', 'remove', '-y', package])
        assert not app.exists() and not Path(layout['engine']).exists()
        assert snapshot(config) == files and Path(manifest['path']).is_file()
        absent = missing_app(manifest, home, expected_code='APP_MISSING')
        assert snapshot(config) == files
    finally:
        if not app.exists():
            pending = sys.exception()
            try:
                # Guest networking is blocked; APT still needs its local-file acquire step.
                run(['sudo', 'apt-get', 'install', '-y', deb])
            except Exception as repair_error:
                if pending is None:
                    raise
                pending.add_note(f'Package restoration also failed: {repair_error}')
    repaired, _ = register(app, home)
    assert repaired['app'] == str(app) and snapshot(config) == files
    unregister(app, home)
    return {'result': 'passed', 'package': package, 'components_empty_path': components,
        'disconnected': disconnected, 'native_when_disconnected': disconnected_native,
        'native_after_removal': absent, 'preserved_files': files,
        'checks': ['apt removal', 'stable copied bridge', 'BROWSER_DISCONNECTED', 'APP_MISSING',
            'apt reinstall and registration repair', 'state/media preservation']}


def check_image(app, home):
    config = saved(home)
    files = snapshot(config)
    with app.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    with mounted(app) as mount:
        selected = {**environment(home), 'APPIMAGE': str(app), 'APPDIR': str(mount)}
        layout = json.loads(run([mount / 'usr/bin/vidgrab-desktop', '--check-package'], selected))
        components = json.loads(run([layout['engine'], '--self-check'], {**selected, 'PATH': ''}))
    _, manifest = register(app, home)
    directory = Path(tempfile.mkdtemp(prefix='relocated package বাংলা-', dir=app.parent))
    destination = directory / app.name
    removed = directory / 'removed-image'
    app.rename(destination)
    try:
        moved, _ = register(destination, home)
        assert moved['app'] == str(destination) and snapshot(config) == files
        unregister(destination, home)
        disconnected_native = missing_app(manifest, home, expected_code='BROWSER_DISCONNECTED')
        register(destination, home)
        # Remove the registered launch path while retaining the original bytes for cleanup.
        destination.rename(removed)
        absent = missing_app(manifest, home, expected_code='APP_MISSING')
        assert snapshot(config) == files and Path(manifest['path']).is_file()
    finally:
        original = removed if removed.exists() else destination
        if original.exists():
            original.rename(app)
        directory.rmdir()
    restored, _ = register(app, home)
    assert restored['app'] == str(app) and snapshot(config) == files
    unregister(app, home)
    with app.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == checksum
    return {'result': 'passed', 'image_sha256': checksum, 'components_empty_path': components,
        'native_when_disconnected': disconnected_native, 'native_after_removal': absent,
        'preserved_files': files,
        'checks': ['normal FUSE helper', 'actual Unicode relocation', 'registration repair',
            'image removal and restoration', 'BROWSER_DISCONNECTED', 'APP_MISSING',
            'state/media preservation']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deb', type=Path, required=True)
    parser.add_argument('--appimage', type=Path, required=True)
    parser.add_argument('--deb-home', type=Path, required=True)
    parser.add_argument('--image-home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
            Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation':
        parser.error('Use the explicitly isolated Ubuntu VM')
    if args.report.exists():
        parser.error('Select a fresh report')
    receipt = {'result': 'running', 'limits': ['CLI preview installation/disconnection, no release uninstaller',
        'disposable native Ubuntu VM; no real user state']}
    try:
        receipt['deb'] = check_deb(args.deb.resolve(strict=True), args.deb_home.resolve(strict=True))
        receipt['appimage'] = check_image(args.appimage.resolve(strict=True), args.image_home.resolve(strict=True))
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        raise
    finally:
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    main()
