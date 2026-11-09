"""Virtual input and bounded observations for the disposable release GUI check."""

import json
from pathlib import Path
import select
import shlex
import subprocess

from tests.check_gnome_pointer import QMP
from tests.native_support import wait_for


class Guest:
    def __init__(self, root, vm, args):
        self.errors = (args.screenshots / 'guest-stderr.log').open('xb')
        remote = ['/opt/vidgrab-tests/venv/bin/python', '-u', '-B',
            '/mnt/inputs/source/tests/release_installation_guest.py',
            '--fixture-root', args.fixture_root, '--package', args.package]
        self.command = ['ssh', '-i', str(root / 'ssh-key'), '-p', str(vm['ssh_port']),
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(root / 'known-hosts'),
            'vidgrabtest@127.0.0.1',
            'PYTHONPATH=/usr/lib/python3/dist-packages ' + shlex.join(remote)]
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.errors)

    def call(self, operation, **values):
        self.process.stdin.write((json.dumps({'op': operation, **values}) + '\n').encode())
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 45)[0], 'Release GUI observation deadline'
        line = self.process.stdout.readline(1024 * 1024 + 1)
        assert line and len(line) <= 1024 * 1024, 'Release GUI response exceeds its bound or peer exited'
        value = json.loads(line)
        assert value.get('reply') == operation and 'error' not in value, value
        return value.get('snapshot', value)

    def close(self):
        self.process.stdin.close()
        try:
            result = self.process.wait(timeout=20)
            assert result == 0, 'Release GUI peer exited unsuccessfully'
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.errors.close()


class Controls:
    def __init__(self, root, vm, args):
        self.root, self.args = root, args
        self.guest = Guest(root, vm, args)
        self.qmp = QMP(root)
        self.records = []
        self.sequence = 0
        self.capture_dir = root / 'inputs/release-ux-screen-captures'
        self.capture_dir.mkdir(mode=0o700, exist_ok=True)

    def screenshot(self, name, labels=(), *, region=None, segmentation=11):
        self.sequence += 1
        path = self.capture_dir / f'{self.args.report.stem}-{self.sequence:03d}-{name}.png'
        assert not path.exists(), 'Capture paths must be fresh'
        self.qmp.screenshot(path)
        guest_path = '/mnt/inputs/release-ux-screen-captures/' + path.name
        value = self.guest.call('screen',
            path=guest_path,
            labels=list(labels), region=region, segmentation=segmentation)
        value['capture_path'] = guest_path
        self.records.append({'name': name, 'path': str(path), 'observation': value})
        return value

    def key(self, *keys):
        allowed = {'alt', 'esc', 'tab', 'ctrl', 'l', 'a', 'q', 'ret', 'delete',
            'shift', 'f4', 'meta_l', 'caps_lock'}
        assert keys and len(keys) == len(set(keys)) and set(keys) <= allowed
        sequence = [(key, True) for key in keys] + [(key, False) for key in reversed(keys)]
        self.qmp.call('input-send-event', {'events': [
            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': key}}}
            for key, down in sequence]})

    def click(self, rectangle, snapshot):
        self.qmp.click(rectangle, snapshot['screen'], self.guest)

    def focus(self, title):
        snapshot = wait_for(lambda: (value if any(window['title'] == title
            for window in value['windows']) else None) if (value := self.guest.call('snapshot')) else None,
            timeout=20, label='selected application window appears')
        if snapshot['overview_visible']:
            # Escape first clears a GNOME search; Super closes the overview.
            self.key('meta_l')
            snapshot = self.guest.call('overview-state', visible=False)
        if not any(window['title'] == title and window['visible'] for window in snapshot['windows']):
            # Release checks create their windows on the guest's first workspace.
            self.qmp.key('meta_l', 'home')
            snapshot = wait_for(lambda: (value if any(window['title'] == title and window['visible']
                for window in value['windows']) else None) if (value := self.guest.call('snapshot')) else None,
                timeout=5, label='selected test workspace')
        for _ in range(len(snapshot['windows']) + 1):
            if any(window['title'] == title and window['focused'] for window in snapshot['windows']):
                return snapshot
            previous = next((window['pid'] for window in snapshot['windows'] if window['focused']), None)
            # The application switcher includes other workspaces; Alt-Escape
            # only cycles windows in the current workspace on this guest.
            self.key('alt', 'tab')
            snapshot = wait_for(lambda: (value if any(window['focused'] and window['pid'] != previous
                for window in value['windows']) else None) if (value := self.guest.call('snapshot')) else None,
                timeout=5, label='virtual keyboard window switch')
        raise AssertionError('Unable to focus the observed window: ' + title)

    def node(self, application, name, role=None, *, include_offscreen=False):
        value = self.guest.call('a11y', application=application)
        nodes = [node for node in value['nodes'] if node.get('name') == name
            and (include_offscreen or node.get('showing')) and (not role or node.get('role') == role)]
        # GTK4 Files exposes nested cells for the same item. Select its outer cell.
        nodes.sort(key=lambda node: len(node['path']))
        assert nodes and (len(nodes) == 1 or all(node['path'][:len(nodes[0]['path'])] == nodes[0]['path']
            for node in nodes[1:])), (application, name, nodes)
        return nodes[0], value['snapshot']

    def rectangle(self, node, snapshot, *, web=False):
        frame = next(window['frame'] for window in snapshot['windows'] if window['focused'])
        # This named GNOME 46 guest uses the observed 37px GTK title bar for Wry.
        return {**node['window'], 'x': node['window']['x'] + frame['x'],
            'y': node['window']['y'] + frame['y'] + (37 if web else 0)}

    def click_node(self, application, name, role=None, *, web=False):
        node, snapshot = self.node(application, name, role)
        self.click(self.rectangle(node, snapshot, web=web), snapshot)
        return node

    def reveal(self, name, role=None):
        for _ in range(35):
            node, snapshot = self.node('vidgrab-desktop', name, role, include_offscreen=True)
            rectangle = self.rectangle(node, snapshot, web=True)
            frame = next(window['frame'] for window in snapshot['windows'] if window['focused'])
            if frame['y'] + 47 <= rectangle['y'] and rectangle['y'] + rectangle['height'] <= frame['y'] + frame['height'] - 5:
                return node, rectangle, snapshot
            x, y = frame['x'] + frame['width'] - 8, frame['y'] + frame['height'] / 2
            self.qmp.call('input-send-event', {'events': [
                {'type': 'abs', 'data': {'axis': 'x', 'value': round(x * 32767 / (snapshot['screen']['width'] - 1))}},
                {'type': 'abs', 'data': {'axis': 'y', 'value': round(y * 32767 / (snapshot['screen']['height'] - 1))}}]})
            self.guest.call('pointer-position', x=x, y=y)
            direction = 'wheel-down' if rectangle['y'] > frame['y'] + 47 else 'wheel-up'
            self.qmp.call('input-send-event', {'events': [
                {'type': 'btn', 'data': {'button': direction, 'down': down}} for down in (True, False)]})
            wait_for(lambda: self.node('vidgrab-desktop', name, role, include_offscreen=True)[0]['window']['y'] != node['window']['y'],
                timeout=4, label='observed document scroll')
        raise AssertionError('Unable to reveal the application control: ' + name)

    def app_click(self, name):
        self.focus('VidGrab')
        node, rectangle, snapshot = self.reveal(name)
        self.click(rectangle, snapshot)
        return node

    def application_section(self):
        self.focus('VidGrab')
        node, rectangle, snapshot = self.reveal('Install and remove VidGrab')
        if not node['expanded']:
            self.click(rectangle, snapshot)

    def launch_file(self):
        snapshot = self.guest.call('snapshot')
        if any(window['title'] == 'Properties' for window in snapshot['windows']):
            self.focus('Properties')
            self.key('alt', 'f4')
            self.guest.call('window-gone', title='Properties')
            snapshot = self.guest.call('snapshot')
        windows = [window for window in snapshot['windows'] if window['title'] == 'release-ux']
        assert len(windows) == 1, 'Open the selected release-ux package folder in Files first'
        self.focus('release-ux')
        self.click_node('org.gnome.Nautilus', Path(self.args.package).name, 'table cell')
        self.key('ret')

    def launch_applications(self, label):
        if not self.guest.call('snapshot')['overview_visible']:
            self.key('meta_l')
            self.guest.call('overview-state', visible=True)
        for attempt in range(3):
            self.key('ctrl', 'a')
            self.qmp.type_text('vidgrab', observe=lambda prefix: self.guest.call('overview-text', prefix=prefix))
            try:
                snapshot = wait_for(lambda: (value if any(actor['kind'] == 'label' and actor['text'] == label
                    for actor in value['overview_actors']) else None)
                    if (value := self.guest.call('snapshot')) else None,
                    timeout=5, label='Applications entry after catalog refresh')
                break
            except AssertionError:
                if attempt == 2:
                    raise
                # GNOME can cache a negative search before its installed-app
                # refresh completes. Re-enter only this reversible search.
        matches = [actor for actor in snapshot['overview_actors'] if actor['kind'] == 'label' and actor['text'] == label]
        assert len(matches) == 1
        self.click(matches[0], snapshot)
        self.guest.call('window', title='VidGrab')

    def authenticate(self, outcome):
        if 'authentication' not in outcome:
            return
        assert self.args.password_file, 'Select the disposable guest authentication file'
        password = self.args.password_file.read_text().strip()
        self.qmp.type_text(password, observe=lambda prefix: self.guest.call('password', characters=len(prefix)))
        snapshot = self.guest.call('password', characters=len(password))
        buttons = [button for dialog in snapshot['dialogs'] for button in dialog['buttons']
            if button['label'] == 'Authenticate' and button['reactive']]
        assert len(buttons) == 1
        self.click(buttons[0], snapshot)

    def close(self):
        try:
            self.guest.close()
        finally:
            self.qmp.close()
