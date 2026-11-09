"""Guarded observations and loopback fixtures for the Ubuntu release GUI check."""
import argparse
import json, os, sys, subprocess, threading, traceback, hashlib
from pathlib import Path

def main():
    assert Path('/etc/hostname').read_text().strip() == 'vidgrab-p02-ubuntu-validation'
    os.environ.update(VIDGRAB_VALIDATION_VM_ISOLATED='1', XDG_RUNTIME_DIR='/run/user/1000', DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
    session = dict((line.split('=', 1) for line in subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line))
    os.environ.update(session)
    sys.path.insert(0, '/mnt/inputs/source')
    from tests.check_wayland_desktop import Desktop
    from tests.linux_accessibility import Accessibility
    from tests.linux_screen import Screen
    from tests.firefox_portal_guest import pointer_reply
    from tests.native_support import wait_for, fixture_server, local_call
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-root', type=Path, required=True)
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    root = args.fixture_root.resolve(strict=True)
    assert root.parent == Path('/home/vidgrabtest/validation') and root.name.startswith('release-ux-')
    firewall = subprocess.check_output(['sudo', '-n', 'nft', 'list', 'table', 'inet', 'vidgrab_validation'], text=True)
    assert firewall.count('policy drop') == 2 and firewall.count(' accept') == 6
    assert session.get('XDG_SESSION_TYPE') == 'wayland' and session.get('WAYLAND_DISPLAY')
    descriptor = Path.home() / '.local/share/vidgrab-p02/native/host-config.json'
    config = json.loads(descriptor.read_text())
    assert config['data_dir'] == str(root / 'state') and config['download_dir'] == str(root / 'media')
    desktop = Desktop()
    access = Accessibility()
    gate = threading.Event()
    server = None
    for line in sys.stdin:
        request = json.loads(line)
        op = request['op']
        try:
            if op in ('pointer-position', 'pointer-down', 'pointer-up', 'desktop'):
                pointer_reply(desktop, request)
                continue
            if op == 'snapshot':
                result = desktop.snapshot()
            elif op == 'overview-label':
                result = wait_for(lambda: (s if any((a['kind'] == 'label' and a['text'] == request['name'] for a in s['overview_actors'])) else None) if (s := desktop.snapshot()) else None, label='Applications search result')
            elif op == 'app-control':

                def control():
                    nodes = access.snapshot('vidgrab-desktop')
                    matches = [n for n in nodes if n.get('showing') and (n.get('name') == request['name'] or n.get('text') == request['name']) and (not request.get('role') or n.get('role') == request['role'])]
                    return {'node': matches[0], 'document': next((n for n in nodes if n.get('role') == 'document web'))['screen'], 'snapshot': desktop.snapshot()} if len(matches) == 1 else None
                result = wait_for(control, label='rendered application control')
            elif op == 'app-state':

                def state():
                    value = access.snapshot('vidgrab-desktop')
                    matched = any((n.get('text') == request['text'] or n.get('name') == request['text'] or (request.get('contains') and request['text'] in n.get('text', '')) for n in value))
                    return value if matched else None
                result = wait_for(state, label='rendered application state')
            elif op == 'fixture-start':
                server = fixture_server(gate)
                result = {'url': server.__enter__()}
            elif op == 'fixture-release':
                gate.set()
                result = {'released': True}
            elif op == 'owner':
                result = local_call(config)
            elif op == 'owner-ready':
                result = wait_for(lambda: (s if s.get('state') == 'ready' else None) if (s := local_call(config)) else None, label='selected fixture readiness')
            elif op == 'owner-active':
                result = wait_for(lambda: (s if any((j['status'] == 'downloading' for j in s.get('jobs', []))) else None) if (s := local_call(config)) else None, label='gated fixture starts')
            elif op == 'owner-complete':
                result = wait_for(lambda: (s if any((j['status'] == 'complete' for j in s.get('jobs', []))) else None) if (s := local_call(config)) else None, timeout=30, label='fixture completion')
            elif op == 'owner-down':
                result = wait_for(lambda: not (root / 'state/ipc/control.sock').exists(), timeout=10, label='selected owner shutdown')
            elif op == 'saved':
                assert not (root / 'state/ipc/control.sock').exists()
                files = [root / 'state/vidgrab.db', *list((root / 'media').rglob('*.mp3'))]
                assert len(files) >= 2 and all((p.is_file() for p in files))
                result = {'files': {str(p): hashlib.file_digest(p.open('rb'), 'sha256').hexdigest() for p in files}, 'descriptor': json.loads(descriptor.read_text()), 'connection': json.loads((descriptor.parent / 'browser-connection.json').read_text())}
            elif op == 'application-files':
                folder = descriptor.parent.parent / 'application'
                shortcut = descriptor.parent.parent.parent / 'applications/com.vidgrab.P02.AppImage.desktop'
                paths = [folder / 'VidGrab.AppImage', folder / 'icon.png', folder / 'installation.json', shortcut]
                result = {'paths': {str(path): path.is_file() for path in paths},
                    'descriptor': json.loads(descriptor.read_text()),
                    'connection': json.loads((descriptor.parent / 'browser-connection.json').read_text())}
            elif op == 'package-file':
                assert args.package and args.package.is_relative_to('/home/vidgrabtest/packages')
                assert args.package.resolve(strict=True) == args.package and args.package.is_file()
                with args.package.open('rb') as stream:
                    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                result = {'path': str(args.package), 'mode': args.package.stat().st_mode & 0o777,
                    'sha256': checksum, 'bytes': args.package.stat().st_size}
            elif op == 'other-packages':
                value = subprocess.check_output(['dpkg-query', '-W',
                    '-f=${Package}\t${Architecture}\t${Version}\t${Status}\n'], text=True)
                result = {line.split('\t', 1)[0]: line for line in value.splitlines()
                    if line.split('\t', 1)[0] != 'vid-grab'}
            elif op == 'package-or-auth':

                def outcome():
                    state = desktop.snapshot()
                    dialogs = [d for d in state['dialogs'] if any((e['focused'] for e in d['entries']))]
                    if dialogs:
                        return {'authentication': state}
                    value = subprocess.run(['dpkg-query', '-W', '-f=${Status}', 'vid-grab'], text=True, capture_output=True)
                    return {'installed': request['expected']} if (value.returncode == 0 and value.stdout == 'install ok installed') == request['expected'] else None
                result = wait_for(outcome, timeout=40, label='package operation or OS authentication')
            elif op == 'installed':

                def installed():
                    value = subprocess.run(['dpkg-query', '-W', '-f=${Status}', 'vid-grab'], text=True, capture_output=True)
                    return {'installed': request['expected']} if (value.returncode == 0 and value.stdout == 'install ok installed') == request['expected'] else None
                result = wait_for(installed, timeout=40, label='OS package state')
            elif op == 'overview-text':
                result = wait_for(lambda: (s if any((a['kind'] == 'entry' and a['text'] == request['prefix'] for a in s['overview_actors'])) else None) if (s := desktop.snapshot()) else None, label='overview text')
            elif op == 'screen':
                screen = Screen(request['path'], segmentation=request.get('segmentation', 11), crop=request.get('region'))
                result = {'snapshot': desktop.snapshot(), 'words': screen.words, 'labels': {text: screen.find(text, region=request.get('region')) for text in request.get('labels', [])}}
            elif op == 'a11y':
                nodes = [node for node in access.snapshot(request['application']) if node.get('name') or node.get('characters') or node.get('role') == 'document web']
                result = {'nodes': nodes, 'snapshot': desktop.snapshot()}
            elif op == 'text':

                def entered():
                    v = access.focused_text(request['application'])
                    return v if v and (v.get('text') == request['prefix'] or (v.get('text', '').startswith(request['prefix']) and any((start == len(request['prefix']) and end == v['characters'] for start, end in v.get('selections', []))))) else None
                result = wait_for(entered, label='OS text entry')
            elif op == 'password':
                result = wait_for(lambda: (s if any((e['focused'] and e['characters'] == request['characters'] for d in s['dialogs'] for e in d['entries'])) else None) if (s := desktop.snapshot()) else None, label='OS authentication entry')
            elif op in ('focused', 'window', 'window-gone', 'overview-state'):

                def observed():
                    s = desktop.snapshot()
                    windows = [w for w in s['windows'] if w['title'] == request.get('title')]
                    ok = not windows if op == 'window-gone' else any((w['focused'] for w in windows)) if op in ('focused', 'window') else s['overview_visible'] == request['visible']
                    return s if ok else None
                result = wait_for(observed, label='OS window state')
            else:
                raise ValueError('Unsupported guarded operation')
            print(json.dumps({'reply': op, 'snapshot': result}), flush=True)
        except BaseException:
            print(json.dumps({'reply': op, 'error': traceback.format_exc()}), flush=True)
    gate.set()
    if server:
        server.__exit__(None, None, None)
if __name__ == '__main__':
    main()
