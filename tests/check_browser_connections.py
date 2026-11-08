"""Observe package connection controls through the disposable Ubuntu VM's QMP."""

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_firefox_portal import Guest as PortalGuest
from tests.check_gnome_pointer import QMP


class Guest(PortalGuest):
    def __init__(self, root, vm, args):
        self.errors = (args.screenshots / 'guest-stderr.log').open('wb')
        remote = ['/opt/vidgrab-tests/venv/bin/python', '-u', '-B',
            '/mnt/inputs/source/tests/browser_connections_guest.py', '--app', args.app,
            '--home', args.home, '--report', args.guest_report]
        if args.fuse:
            remote.append('--fuse')
        self.command = ['ssh', '-i', str(root / 'ssh-key'), '-p', str(vm['ssh_port']),
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(root / 'known-hosts'), 'vidgrabtest@127.0.0.1',
            'VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages ' + shlex.join(remote)]
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', type=Path, required=True)
    parser.add_argument('--app', required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--guest-report', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    vm = json.loads(args.vm.read_text())
    root = Path(vm['root']).resolve(strict=True)
    if root != args.vm.resolve().parent or root.stat().st_mode & 0o077 or vm['state'] != 'running':
        parser.error('Select the running disposable VM and private mode-0700 controls')
    if args.report.exists() or args.screenshots.exists():
        parser.error('Select fresh report/screenshots paths')
    args.screenshots.mkdir(mode=0o700, parents=True)
    qmp = QMP(root)
    guest = Guest(root, vm, args)
    receipt = {'result': 'running', 'command': guest.command, 'events': []}
    try:
        while True:
            event = guest.receive()
            receipt['events'].append(event)
            if event['event'] == 'browser-focus':
                snapshot = event['snapshot']
                window = next(window for window in snapshot['windows'] if window['pid'] == event['pid'])
                qmp.click(window['frame'], snapshot['screen'], guest, title=True)
                guest.call('browser-focused')
            elif event['event'] == 'setup-input':
                assert 'rectangle' in event and 'text' not in event and 'keys' not in event, event
                qmp.click(event['rectangle'], event['snapshot']['screen'], guest)
                guest.call('setup-input-complete')
            elif event['event'] == 'checkpoint':
                print('Browser controls: ' + event['stage'], flush=True)
                qmp.screenshot(args.screenshots / (event['stage'] + '.png'))
            elif event['event'] == 'failure':
                raise AssertionError(event['failure'])
            elif event['event'] == 'complete':
                assert event['result'] == 'passed', event
                receipt['result'] = 'passed'
                break
            else:
                raise ValueError('Unsupported connection-controls event')
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        qmp.screenshot(args.screenshots / 'failure-before-cleanup.png')
        raise
    finally:
        try:
            receipt['ssh_returncode'] = guest.close()
            if receipt['result'] == 'passed' and receipt['ssh_returncode'] != 0:
                receipt['result'] = 'failed'
                raise AssertionError('Guest failed after its connection checks')
        finally:
            qmp.close()
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps({'result': receipt['result'], 'report': str(args.report)}), flush=True)


if __name__ == '__main__':
    main()
