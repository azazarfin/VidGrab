"""Shared Firefox WebExtensions-portal journey through a private Ubuntu VM."""

import argparse
import json
from pathlib import Path
import select
import shlex
import subprocess
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_gnome_pointer import QMP


class Guest:
    def __init__(self, root, vm, args, distribution):
        self.errors = (args.screenshots / 'guest-stderr.log').open('wb')
        remote = ['/opt/vidgrab-tests/venv/bin/python', '-u', '-B',
            '/mnt/inputs/source/tests/firefox_portal_guest.py', '--distribution', distribution,
            '--app', args.app, '--home', args.home,
            '--report', args.guest_report]
        if args.fuse:
            remote.append('--fuse')
        if args.enable_native_portal:
            remote.append('--enable-native-portal')
        command = ['ssh', '-i', str(root / 'ssh-key'), '-p', str(vm['ssh_port']), '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(root / 'known-hosts'),
            'vidgrabtest@127.0.0.1', 'VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages ' + shlex.join(remote)]
        self.command = command
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)

    def receive(self):
        assert select.select([self.process.stdout], [], [], 45)[0], 'Firefox portal guest event deadline'
        line = self.process.stdout.readline(65537)
        if not line:
            self.errors.flush()
            detail = Path(self.errors.name).read_text(errors='replace')[-6000:]
            raise AssertionError('Firefox portal peer ended: ' + detail)
        assert len(line) <= 65536, 'Firefox portal peer exceeded its event bound'
        return json.loads(line)

    def call(self, operation, **values):
        self.process.stdin.write((json.dumps({'op': operation, **values}) + '\n').encode())
        self.process.stdin.flush()
        response = self.receive()
        assert response.get('reply') == operation, response
        return response.get('snapshot', response)

    def close(self):
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise
        finally:
            self.process.stdout.close()
            self.errors.close()


def main(distribution='snap'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', type=Path, required=True)
    parser.add_argument('--app', required=True, help='Guest package executable, never a host executable')
    parser.add_argument('--home', required=True, help='Fresh guest /home/vidgrabtest/validation directory')
    parser.add_argument('--guest-report', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    parser.add_argument('--enable-native-portal', action='store_true',
        help='Explicit Firefox Flatpak profile opt-in; never changes sandbox permissions')
    args = parser.parse_args()
    if args.enable_native_portal and distribution != 'flatpak':
        parser.error('The explicit portal preference check is for Firefox Flatpak')
    vm = json.loads(args.vm.read_text())
    root = Path(vm['root']).resolve(strict=True)
    if root != args.vm.resolve().parent or root.stat().st_mode & 0o077 or vm['state'] != 'running':
        parser.error('Select the running disposable VM and its private mode-0700 controls')
    if args.report.exists() or args.screenshots.exists():
        parser.error('Select fresh report/screenshots paths')
    args.screenshots.mkdir(mode=0o700, parents=True)
    qmp = QMP(root)
    guest = Guest(root, vm, args, distribution)
    receipt = {'result': 'running', 'distribution': distribution, 'command': guest.command,
        'app': args.app, 'events': []}
    try:
        while True:
            event = guest.receive()
            receipt['events'].append(event)
            if event['event'] == 'browser-focus':
                snapshot = event['snapshot']
                if snapshot['overview_visible']:
                    qmp.key('esc')
                    snapshot = guest.call('desktop')
                window = next(window for window in snapshot['windows'] if window['pid'] == event['pid'])
                qmp.click(window['frame'], snapshot['screen'], guest, title=True)
                guest.call('browser-focused')
            elif event['event'] == 'permission':
                text = ' '.join(event['dialog']['labels'])
                assert 'VidGrab' in text or 'com.vidgrab.companion' in text, event
                choices = [button for button in event['dialog']['buttons']
                    if button['label'] == 'Allow' and button['reactive']]
                assert len(choices) == 1, event['dialog']
                qmp.screenshot(args.screenshots / 'permission.png')
                qmp.click(choices[0], event['snapshot']['screen'], guest)
                guest.call('permission-clicked')
            elif event['event'] == 'checkpoint':
                print('Firefox ' + distribution.title() + ': ' + event['stage'], flush=True)
                if event['stage'] in ('permission-and-activation', 'browser-exit-keeps-download', 'restart-restores-history'):
                    qmp.screenshot(args.screenshots / (event['stage'] + '.png'))
            elif event['event'] == 'failure':
                qmp.screenshot(args.screenshots / 'failure-before-cleanup.png')
                raise AssertionError(event['failure'])
            elif event['event'] == 'complete':
                assert event['result'] == 'passed', event
                receipt['result'] = 'passed'
                break
            else:
                raise ValueError('Unsupported Firefox portal guest event')
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        if not (args.screenshots / 'failure-before-cleanup.png').exists():
            qmp.screenshot(args.screenshots / 'failure-before-cleanup.png')
        raise
    finally:
        try:
            receipt['ssh_returncode'] = guest.close()
            if receipt['result'] == 'passed' and receipt['ssh_returncode'] != 0:
                receipt.update(result='failed', failure='Guest exited unsuccessfully after its checks')
                raise AssertionError(receipt['failure'])
        finally:
            qmp.close()
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps({'result': receipt['result'], 'report': str(args.report)}), flush=True)


if __name__ == '__main__':
    main()
