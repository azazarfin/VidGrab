"""Bounded native Load unpacked input for the standalone restart fixture."""

import json
import os
from pathlib import Path
import select
import sys
import time
import traceback
from uuid import uuid4


def load_unpacked_ui(browser, extension):
    from tests.check_wayland_desktop import Desktop
    from tests.firefox_portal_guest import pointer_reply
    from tests.linux_accessibility import Accessibility
    from tests.native_client_support import wait
    from tests.native_grant_input import validate_acknowledgement

    assert browser.kind == 'chrome' and browser.permission_events
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    assert Path('/etc/hostname').read_text().strip() == 'vidgrab-p02-ubuntu-validation'
    assert extension.is_absolute() and extension.is_dir() and not extension.is_symlink()
    desktop, access = Desktop(), Accessibility()
    cdp = browser.context.browser.new_browser_cdp_session()
    pid = next(int(item['id']) for item in cdp.send('SystemInfo.getProcessInfo')['processInfo']
        if item['type'] == 'browser')
    cdp.detach()
    bound = {'browser': 'chrome', 'choice': 'load-unpacked', 'nonce': str(uuid4())}
    deadline = time.monotonic() + 25
    entry_node = None
    registry = access.api.Registry.getDesktop(0)
    applications = {}
    for index in range(registry.childCount):
        app = registry.getChildAtIndex(index)
        if app is not None and (app.get_process_id() == pid or app.name in (
                'gnome-shell', 'xdg-desktop-portal-gtk', 'xdg-desktop-portal-gnome')):
            applications[index] = {'name': app.name, 'pid': app.get_process_id()}

    def entry_text():
        text = entry_node.queryText()
        value = text.getText(0, text.characterCount)
        selections = text.getNSelections()
        assert selections <= 1
        if selections:
            start, end = text.getSelection(0)
            if end == text.characterCount:
                return value[:start]
        return value

    def exchange(event, operation):
        print(json.dumps({**event, **bound, 'pid': pid}), flush=True)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([sys.stdin], [], [], remaining)[0], 'Native unpacked input deadline'
            line = sys.stdin.readline(65537)
            assert line and len(line.encode()) <= 65536
            request = json.loads(line)
            if request.get('op') in ('pointer-position', 'pointer-down', 'pointer-up', 'desktop'):
                pointer_reply(desktop, request)
                continue
            if request.get('op') == 'folder-prefix':
                prefix = request.get('text')
                assert isinstance(prefix, str) and str(extension).startswith(prefix) and entry_node is not None
                validate_acknowledgement(request, {**bound, 'text': prefix}, 'folder-prefix')
                observed = wait(lambda: (text if (text := entry_text()) == prefix else None),
                    'QMP typed native folder prefix', min(3, max(0, deadline - time.monotonic())))
                text = entry_node.queryText()
                print(json.dumps({'reply': 'folder-prefix', 'observed': observed,
                    'entry_text': text.getText(0, text.characterCount),
                    'selections': [text.getSelection(index) for index in range(text.getNSelections())]}), flush=True)
                continue
            validate_acknowledgement(request, bound, operation)
            return

    def nodes():
        registry = access.api.Registry.getDesktop(0)
        result = []
        for index in range(registry.childCount):
            app = registry.getChildAtIndex(index)
            if app is not None and (app.get_process_id() == pid or app.name in (
                    'gnome-shell', 'xdg-desktop-portal-gtk', 'xdg-desktop-portal-gnome')):
                assert Path('/proc', str(app.get_process_id())).stat().st_uid == os.getuid()
                applications[index] = {'name': app.name, 'pid': app.get_process_id()}
                result.extend(node for node in access.snapshot(app.name)
                    if node['path'][0] == index and node.get('showing'))
        return result

    def node_at(path):
        node = access.api.Registry.getDesktop(0)
        assert node.getChildAtIndex(path[0]).get_process_id() == applications[path[0]]['pid']
        for index in path:
            node = node.getChildAtIndex(index)
        return node

    manager = browser.context.new_page()
    try:
        manager.goto('chrome://extensions/')
        exchange({'event': 'permission-focus', 'snapshot': desktop.snapshot()}, 'permission-focus-complete')
        print(json.dumps({'reply': 'permission-focus-complete'}), flush=True)
        assert any(window['pid'] == pid and window['focused'] for window in desktop.snapshot()['windows'])
        assert not [node for node in nodes() if node.get('role') == 'dialog']
        manager.locator('extensions-toolbar #loadUnpacked').click()
        dialogs = wait(lambda: [node for node in nodes() if node.get('role') == 'dialog'],
            'owned native unpacked folder chooser',
            min(5, max(0, deadline - time.monotonic())))
        assert len(dialogs) == 1
        snapshot = desktop.snapshot()
        windows = [window for window in snapshot['windows'] if window['focused']
            and window['title'] == 'Select the extension directory.']
        assert len(windows) == 1 and windows[0]['pid'] in {item['pid'] for item in applications.values()}
        exchange({'event': 'folder-picker', 'snapshot': snapshot, 'extension': str(extension),
            'dialog_window_pid': windows[0]['pid'], 'dialog_application': applications[dialogs[0]['path'][0]]}, 'folder-location')
        entries = wait(lambda: [node for node in nodes() if node.get('role') in ('entry', 'text')
            and node['path'][:len(dialogs[0]['path'])] == dialogs[0]['path']], 'native folder location entry',
            min(5, max(0, deadline - time.monotonic())))
        assert len(entries) == 1
        node = node_at(entries[0]['path'])
        assert node.getState().contains(access.api.STATE_FOCUSED)
        entry_node = node
        print(json.dumps({'reply': 'folder-location', 'entry_text': entry_text()}), flush=True)
        exchange({'event': 'folder-ready', 'snapshot': desktop.snapshot(), 'extension': str(extension)}, 'folder-selected')
        buttons = [node for node in nodes() if node.get('role') == 'push button' and node.get('name') == 'Select'
            and node['path'][:len(dialogs[0]['path'])] == dialogs[0]['path']]
        if buttons:
            assert len(buttons) == 1
            node = node_at(buttons[0]['path'])
            assert node.getState().contains(access.api.STATE_ENABLED)
            assert node.queryAction().doAction(0)
        wait(lambda: not [node for node in nodes() if node.get('role') == 'dialog'
            and node['path'] == dialogs[0]['path']], 'native unpacked chooser closes',
            max(0, deadline - time.monotonic()))
        print(json.dumps({'reply': 'folder-selected'}), flush=True)
        def installed():
            return manager.evaluate('''() => new Promise(resolve => chrome.developerPrivate.getExtensionsInfo(
                {includeDisabled:true,includeTerminated:true}, resolve))''')
        items = wait(lambda: (items if (items := installed()) else None), 'native unpacked installation',
            max(0, deadline - time.monotonic()))
        assert len(items) == 1 and items[0]['name'] == 'VidGrab' and items[0]['state'] == 'ENABLED'
        manager.screenshot(path=str(browser.root / 'native-unpacked-install.png'))
        (browser.root / 'native-unpacked-install.json').write_text(json.dumps({
            **bound, 'pid': pid, 'extension': str(extension), 'inventory': items,
            'portal_application': applications[dialogs[0]['path'][0]], 'dialog_window_pid': windows[0]['pid'],
            'input': 'native folder chooser; QMP focus/Ctrl+L/typed path and AT-SPI Select',
            'devtools_load_unpacked': False}, indent=2) + '\n')
        return {'id': items[0]['id']}
    except BaseException:
        registry = access.api.Registry.getDesktop(0)
        (browser.root / 'native-unpacked-failure.json').write_text(json.dumps({
            'failure': traceback.format_exc(), 'pid': pid, 'snapshot': desktop.snapshot(),
            'owned_nodes': nodes(), 'applications': [
                {'index': index, 'name': registry.getChildAtIndex(index).name,
                    'pid': registry.getChildAtIndex(index).get_process_id()}
                for index in range(registry.childCount) if registry.getChildAtIndex(index) is not None]
            }, indent=2) + '\n')
        raise
    finally:
        manager.close()
