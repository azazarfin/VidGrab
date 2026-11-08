"""Opt-in mouse/keyboard tray lifecycle through a disposable VM's private QMP."""

import argparse
import json
from pathlib import Path
import select
import shlex
import socket
import subprocess
import tempfile
import traceback


class QMP:
    def __init__(self, root):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(10)
        self.socket.connect(str(root / 'qmp.sock'))
        self.stream = self.socket.makefile('rwb', buffering=0)
        json.loads(self.stream.readline())
        self.identifier = 0
        self.call('qmp_capabilities')
        assert self.call('query-name')['name'] == root.name, 'QMP must select this disposable VM'

    def call(self, operation, arguments=None):
        self.identifier += 1
        payload = {'execute': operation, 'id': self.identifier}
        if arguments is not None:
            payload['arguments'] = arguments
        self.stream.write((json.dumps(payload) + '\n').encode())
        while True:
            response = json.loads(self.stream.readline())
            if response.get('id') == self.identifier:
                assert 'error' not in response, response
                return response['return']

    def key(self, *keys):
        self.call('send-key', {'keys': [{'type': 'qcode', 'data': key} for key in keys], 'hold-time': 50})

    def press_keys(self, *keys):
        if not keys or len(keys) != len(set(keys)) or any(key not in ('ctrl', 'l', 'a', 'q', 'ret') for key in keys):
            raise ValueError('Select a supported manual Firefox setup shortcut')
        sequence = [(key, True) for key in keys] + [(key, False) for key in reversed(keys)]
        self.call('input-send-event', {'events': [
            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': key}}}
            for key, down in sequence]})

    def type_text(self, text, observe=None):
        if not text or len(text) > 2048:
            raise ValueError('Select 1 to 2048 supported ASCII characters for virtual input')
        if (len(text) > 1 and not callable(observe)) or (observe is not None and not callable(observe)):
            raise ValueError('Observe each character before sending more virtual keyboard input')
        symbols = {'.': ('dot', False), '-': ('minus', False), '/': ('slash', False),
            ':': ('semicolon', True), '_': ('minus', True), ' ': ('spc', False)}
        keys = []
        for character in text:
            if character in 'abcdefghijklmnopqrstuvwxyz0123456789':
                keys.append((character, False))
            elif character in symbols:
                keys.append(symbols[character])
            else:
                raise ValueError('Unsupported virtual keyboard character')
        for index, (key, shift) in enumerate(keys):
            sequence = ([('shift', True)] if shift else []) + [(key, True), (key, False)]
            if shift:
                sequence.append(('shift', False))
            self.call('input-send-event', {'events': [
                {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': name}}}
                for name, down in sequence]})
            if observe is not None:
                observe(text[:index + 1])

    def click(self, rectangle, screen, guest, *, title=False):
        x = rectangle['x'] + rectangle['width'] / 2
        y = rectangle['y'] + (18 if title else rectangle['height'] / 2)
        assert 0 <= x < screen['width'] and 0 <= y < screen['height'], (rectangle, screen)
        self.call('input-send-event', {'events': [
            {'type': 'abs', 'data': {'axis': 'x', 'value': round(x * 32767 / (screen['width'] - 1))}},
            {'type': 'abs', 'data': {'axis': 'y', 'value': round(y * 32767 / (screen['height'] - 1))}},
        ]})
        guest.call('pointer-position', x=x, y=y)
        # Observe each HID report in the guest before sending the next state.
        self.call('input-send-event', {'events': [
            {'type': 'btn', 'data': {'button': 'left', 'down': True}},
        ]})
        guest.call('pointer-down')
        self.call('input-send-event', {'events': [
            {'type': 'btn', 'data': {'button': 'left', 'down': False}},
        ]})
        guest.call('pointer-up')

    def screenshot(self, path):
        self.call('screendump', {'filename': str(path.resolve()), 'format': 'png'})

    def close(self):
        self.stream.close()
        self.socket.close()


class Guest:
    def __init__(self, root, vm, args):
        self.diagnostics = None
        self.errors = tempfile.TemporaryFile()
        remote = ['/opt/vidgrab-tests/venv/bin/python', '-u', '-B',
            '/mnt/inputs/source/tests/gnome_pointer_guest.py', '--app', args.app, '--home', args.home]
        if args.fuse:
            remote.append('--fuse')
        command = ['ssh', '-i', str(root / 'ssh-key'), '-p', str(vm['ssh_port']), '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(root / 'known-hosts'),
            'vidgrabtest@127.0.0.1', 'PYTHONPATH=/usr/lib/python3/dist-packages ' + shlex.join(remote)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.errors)

    def call(self, operation, **parameters):
        self.process.stdin.write((json.dumps({'op': operation, **parameters}) + '\n').encode())
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 35)[0], 'Guest pointer response deadline'
        data = self.process.stdout.readline()
        if not data:
            self.errors.seek(0)
            raise AssertionError(self.errors.read().decode(errors='replace'))
        result = json.loads(data)
        if 'error' in result:
            self.diagnostics = result
            raise AssertionError(result['error'])
        return result

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise
        finally:
            self.process.stdout.close()
            self.errors.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', type=Path, required=True, help='Private vm.json receipt')
    parser.add_argument('--app', required=True, help='Absolute application path inside the guest')
    parser.add_argument('--home', required=True, help='Fresh private home inside the guest')
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--fuse', action='store_true')
    args = parser.parse_args()
    vm = json.loads(args.vm.read_text())
    root = Path(vm['root']).resolve(strict=True)
    if root != args.vm.resolve().parent or root.stat().st_mode & 0o077:
        parser.error('Select the mode-0700 control directory of the disposable VM')
    if args.report.exists():
        parser.error('Select a fresh report')
    args.screenshots.mkdir(parents=True, exist_ok=True)
    qmp = QMP(root)
    guest = Guest(root, vm, args)
    receipt = {'app': args.app, 'result': 'running', 'input': 'QMP USB tablet and keyboard',
        'limits': ['virtual input/display; no physical hardware', 'Ubuntu GNOME only']}
    try:
        first = guest.call('start')
        receipt['initial'] = first
        snapshot = first['snapshot']
        if snapshot['overview_visible']:
            qmp.key('esc')
            snapshot = guest.call('desktop')
        qmp.click(snapshot['window']['frame'], snapshot['screen'], guest, title=True)
        snapshot = guest.call('focused')
        qmp.screenshot(args.screenshots / 'pointer-window.png')
        qmp.key('alt', 'f4')
        snapshot = guest.call('hidden')
        indicator = next(item for item in snapshot['indicators'] if item['name'] == first['panel_name'])
        qmp.click(indicator, snapshot['screen'], guest)
        snapshot = guest.call('menu')
        qmp.screenshot(args.screenshots / 'pointer-menu.png')
        indicator = next(item for item in snapshot['indicators'] if item['name'] == first['panel_name'])
        menu_item = next(item for item in indicator['menu'] if item['label'] == 'Open VidGrab')
        qmp.click(menu_item, snapshot['screen'], guest)
        snapshot = guest.call('visible')
        snapshot = guest.call('focused')
        receipt['reopened_window'] = snapshot['window']
        qmp.screenshot(args.screenshots / 'pointer-reopened.png')
        indicator = next(item for item in snapshot['indicators'] if item['name'] == first['panel_name'])
        qmp.click(indicator, snapshot['screen'], guest)
        snapshot = guest.call('menu')
        indicator = next(item for item in snapshot['indicators'] if item['name'] == first['panel_name'])
        menu_item = next(item for item in indicator['menu'] if item['label'] == 'Quit VidGrab')
        qmp.click(menu_item, snapshot['screen'], guest)
        receipt['quit'] = guest.call('quit')
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        receipt['diagnostics'] = guest.diagnostics
        qmp.screenshot(args.screenshots / 'pointer-failure.png')
        raise
    finally:
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        try:
            guest.close()
        finally:
            qmp.close()
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    main()
