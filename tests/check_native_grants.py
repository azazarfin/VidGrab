"""Visible optional-grant input through a hash-bound source and owned Ubuntu QMP.

Guest main retains the original native assertion and its process deadlines.
This driver never grants a permission through the permissions API.
"""

import argparse
import hashlib
import json
from pathlib import Path
import select
import shlex
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_gnome_pointer import QMP
from tests.native_grant_input import permission_button


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('vm', 'manifest', 'report', 'screenshots'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('source', 'guest-manifest', 'bundle', 'app', 'home', 'guest-report',
            'guest-screenshots', 'chrome', 'firefox', 'geckodriver', 'node'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--browser', choices=('chrome', 'firefox', 'both'), default='both')
    args = parser.parse_args()
    vm = json.loads(args.vm.read_text())
    control = Path(vm['control_dir'])
    if args.vm.resolve().parent != control or control.stat().st_mode & 0o077 \
            or not vm['state'].startswith('running') or args.report.exists() or args.screenshots.exists():
        parser.error('Select owned running VM controls and fresh report/screenshots')
    assert json.loads((control / 'vm.json').read_text()) == vm
    process_path = Path('/proc') / str(vm['pid'])
    stat = (process_path / 'stat').read_text()
    assert stat[stat.rfind(')') + 2:].split()[19] == vm['proc_starttime']
    launch = vm['launch_command']
    assert (process_path / 'cmdline').read_bytes().split(b'\0')[:-1] == [arg.encode() for arg in launch]
    assert launch[launch.index('-name') + 1] == control.name
    assert launch[launch.index('-drive') + 1].startswith('file=' + vm['disk'] + ',format=qcow2,')
    assert 'restrict=on' in launch[launch.index('-netdev') + 1]
    assert launch[launch.index('-fsdev') + 1] == 'local,id=inputs,path=' + vm['inputs'] + ',security_model=none,readonly=on'
    manifest = json.loads(args.manifest.read_text())
    repo = Path(__file__).resolve().parents[1]
    for name in ('tests/check_native_grants.py', 'tests/native_grant_input.py'):
        assert hashlib.sha256((repo / name).read_bytes()).hexdigest() == manifest['files'][name]
    assert Path(args.source).is_relative_to('/mnt/inputs') and Path(args.guest_manifest).is_relative_to('/mnt/inputs')
    assert Path(args.home).is_relative_to('/tmp') or Path(args.home).is_relative_to('/home/vidgrabtest/validation')
    args.screenshots.mkdir(parents=True, mode=0o700)
    command = ['--discovery', '--discovery-grants', '--permission-events', '--browser', args.browser]
    for name in ('bundle', 'app', 'home', 'chrome', 'firefox', 'geckodriver'):
        command.extend(['--' + name, getattr(args, name)])
    command.extend(['--report', args.guest_report, '--screenshots', args.guest_screenshots])
    bootstrap = '''import hashlib,json,os,runpy,sys
from pathlib import Path
source,manifest=Path(sys.argv[1]),json.loads(Path(sys.argv[2]).read_text())
for name,expected in manifest['files'].items():
    assert hashlib.sha256((source/name).read_bytes()).hexdigest()==expected,name
os.chdir(source)
sys.argv=['tests/check_native_clients.py',*sys.argv[3:]]
runpy.run_path(sys.argv[0],run_name='__main__')'''
    remote = 'VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages VIDGRAB_NATIVE_HELPER_NODE=' \
        + shlex.quote(args.node) + ' ' + shlex.join(['/opt/vidgrab-tests/venv/bin/python', '-u', '-B', '-c',
            bootstrap, args.source, args.guest_manifest, *command])
    ssh = ['ssh', '-i', vm['ssh_key'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + vm['host_key_alias'],
        '-o', 'UserKnownHostsFile=' + vm['known_hosts'], '-o', 'ConnectTimeout=10',
        '-p', str(vm['ssh_port']), vm['ssh_user'] + '@127.0.0.1', remote]
    receipt = {'result': 'running', 'browser': args.browser, 'vm_name': vm['vm_name'],
        'vm_pid': vm['pid'], 'vm_starttime': vm['proc_starttime'], 'events': [],
        'source_manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'limits': ['owned native Ubuntu VM; temporary profiles and development extension',
            'Chrome QMP keyboard and Firefox native AT-SPI button actions',
            'QMP tablet browser focus; actual grant removal uses the browser API',
            'no physical-input, browser-manager revocation, Windows or live-site claim']}
    qmp = QMP(control)
    errors = (args.screenshots / 'guest-stderr.log').open('xb')
    try:
        process = subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, bufsize=0)
    except BaseException:
        qmp.close()
        errors.close()
        raise
    deadline = time.monotonic() + 360

    class Peer:
        def receive(self):
            remaining = min(60, deadline - time.monotonic())
            assert remaining > 0 and select.select([process.stdout], [], [], remaining)[0], 'Native grant event deadline'
            line = process.stdout.readline(65537)
            if not line:
                return None
            assert len(line) <= 65536
            event = json.loads(line)
            receipt['events'].append(event)
            return event

        def call(self, operation, **values):
            process.stdin.write((json.dumps({'op': operation, **values}) + '\n').encode())
            response = self.receive()
            assert response and response.get('reply') == operation, response
            return response.get('snapshot', response)

    peer = Peer()
    try:
        while event := peer.receive():
            if event.get('event') == 'permission-focus':
                snapshot = event['snapshot']
                if snapshot['overview_visible']:
                    qmp.key('esc')
                    snapshot = peer.call('desktop')
                windows = [window for window in snapshot['windows'] if window['pid'] == event['pid'] and not window['hidden']]
                assert len(windows) == 1
                qmp.click(windows[0]['frame'], snapshot['screen'], peer, title=True)
                peer.call('permission-focus-complete', **{name: event[name] for name in ('browser', 'choice', 'nonce')})
            elif event.get('event') == 'permission-input':
                bound = {name: event[name] for name in ('browser', 'choice', 'nonce')}
                nodes = event['nodes']
                assert any('VidGrab' in node.get('name', '') for node in nodes)
                assert any(window['pid'] == event['pid'] and window['focused'] for window in event['snapshot']['windows'])
                button = permission_button(nodes, event['choice'])
                qmp.screenshot(args.screenshots / (event['browser'] + '-' + event['choice'] + '-native.png'))
                if event['browser'] == 'firefox':
                    peer.call('permission-choice-activate', **bound)
                else:
                    assert event['browser'] == 'chrome'
                    if not button.get('focused'):
                        qmp.key('tab')
                    peer.call('permission-choice-focused', **bound)
                    qmp.key('ret')
                peer.call('permission-input-complete', **bound)
                print(json.dumps({'browser': event['browser'], 'choice': event['choice'], 'native_input': True}), flush=True)
            else:
                receipt['guest_result'] = event
        process.stdin.close()
        receipt['exit'] = process.wait(timeout=10)
        assert receipt['exit'] == 0 and receipt['guest_result']['result'] == 'passed', 'Guest assertions failed'
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        if not process.stdin.closed:
            process.stdin.close()
        try:
            receipt['exit'] = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            receipt['cleanup_failure'] = 'Owned SSH peer did not join; guest processes require an identity audit'
        raise
    finally:
        qmp.close()
        errors.close()
        process.stdout.close()
        receipt['screenshots'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in args.screenshots.iterdir() if path.is_file()}
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
