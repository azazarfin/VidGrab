"""Bounded visible-dialog/QMP input peer, only in the selected Ubuntu VM."""

import json
import os
from pathlib import Path
import select
import sys
import time
from uuid import uuid4


def validate_acknowledgement(response, expected, operation='permission-input-complete'):
    assert isinstance(response, dict) and response == {
        'op': operation, **expected}, 'Foreign or malformed permission acknowledgement'
    return response


def permission_button(nodes, choice):
    assert choice in ('deny', 'allow')
    names = ('Deny', 'Cancel', "Don't Allow") if choice == 'deny' else ('Allow', 'Add permissions', 'Allow permissions')
    choices = {}
    for node in nodes:
        if node.get('name') in names and node.get('role') == 'push button' and node.get('showing') and node.get('enabled'):
            box = node['screen']
            assert box['width'] > 0 and box['height'] > 0
            identity = (node['name'], *(box[field] for field in ('x', 'y', 'width', 'height')))
            choices.setdefault(identity, node)
    assert len(choices) == 1, 'Native permission choice is absent or ambiguous'
    return next(iter(choices.values()))


def permission_input(browser, choice):
    assert choice in ('deny', 'allow')
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    assert Path('/etc/hostname').read_text().strip() == 'vidgrab-p02-ubuntu-validation'
    from tests.check_wayland_desktop import Desktop
    from tests.firefox_portal_guest import pointer_reply
    from tests.linux_accessibility import Accessibility
    from tests.native_client_support import wait
    desktop, access = Desktop(), Accessibility()
    expected = {'browser': browser.kind, 'choice': choice, 'nonce': str(uuid4())}
    deadline = time.monotonic() + 25
    native_actions = []

    def exchange(event, operation):
        wire = json.dumps({**event, **expected})
        assert len(wire.encode()) <= 65536, 'Permission observation exceeds its frame bound'
        print(wire, flush=True)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([sys.stdin], [], [], remaining)[0], 'Native permission input deadline'
            line = sys.stdin.readline(65537)
            assert line and len(line.encode()) <= 65536, 'Missing or oversized native permission input'
            request = json.loads(line)
            if isinstance(request, dict) and request.get('op') in ('pointer-position', 'pointer-down', 'pointer-up', 'desktop'):
                pointer_reply(desktop, request)
                continue
            if isinstance(request, dict) and request.get('op') == 'permission-choice-focused':
                validate_acknowledgement(request, expected, 'permission-choice-focused')
                wait(lambda: permission_button(native_nodes() or [], choice).get('focused'),
                    'requested native permission button has keyboard focus', min(5, max(0, deadline-time.monotonic())))
                print(json.dumps({'reply': 'permission-choice-focused', 'focused': choice}), flush=True)
                continue
            if isinstance(request, dict) and request.get('op') == 'permission-choice-activate':
                validate_acknowledgement(request, expected, 'permission-choice-activate')
                assert browser.kind == 'firefox' and not native_actions
                button = permission_button(native_nodes() or [], choice)
                node = access.api.Registry.getDesktop(0)
                assert node.getChildAtIndex(button['path'][0]).get_process_id() == pid
                for index in button['path']:
                    node = node.getChildAtIndex(index)
                assert node.name == button['name'] and node.getRoleName() == 'push button'
                assert node.getState().contains(access.api.STATE_SHOWING) and node.getState().contains(access.api.STATE_ENABLED)
                action = node.queryAction()
                action_name = action.getName(0)
                assert action_name in ('click', 'press') and action.doAction(0)
                native_actions.append({'name': button['name'], 'action': action_name, 'transport': 'AT-SPI'})
                print(json.dumps({'reply': 'permission-choice-activate'}), flush=True)
                continue
            validate_acknowledgement(request, expected, operation)
            print(json.dumps({'reply': operation}), flush=True)
            return
    if browser.kind == 'chrome':
        cdp = browser.context.browser.new_browser_cdp_session()
        try:
            processes = cdp.send('SystemInfo.getProcessInfo')['processInfo']
            pid = next(int(item['id']) for item in processes if item['type'] == 'browser')
        finally:
            cdp.detach()
    else:
        pid = browser.version['moz:processID']
    exchange({'event': 'permission-focus', 'pid': pid, 'snapshot': desktop.snapshot()}, 'permission-focus-complete')
    assert any(window['pid'] == pid and window['focused'] for window in desktop.snapshot()['windows']), \
        'The selected browser did not acquire native focus'
    registry = access.api.Registry.getDesktop(0)
    applications = []
    for index in range(registry.childCount):
        app = registry.getChildAtIndex(index)
        if app is not None and app.get_process_id() == pid:
            applications.append({'index': index, 'name': app.name, 'pid': pid})
    assert applications, 'The owned visible browser is absent from AT-SPI'
    def native_nodes():
        nodes = []
        for app in applications:
            nodes.extend({key: node[key] for key in ('path', 'name', 'role', 'showing', 'enabled', 'focused', 'screen') if key in node}
                for node in access.snapshot(app['name'])
                if node['path'][0] == app['index'] and node.get('showing') and node.get('name'))
        names = {node['name'] for node in nodes if node.get('role') == 'push button' and node.get('enabled')}
        return nodes if 'Allow' in names and names.intersection(('Deny', 'Cancel', "Don't Allow")) else None

    nodes = wait(native_nodes, 'visible enabled native permission choices', min(5, max(0, deadline-time.monotonic())))
    snapshot = desktop.snapshot()
    assert snapshot['wayland'] and any(window['pid'] == pid and not window['hidden']
        for window in snapshot['windows']), 'The selected browser has no visible native window'
    event = {'event': 'permission-input', **expected, 'pid': pid,
        'applications': applications, 'nodes': nodes, 'snapshot': snapshot}
    exchange(event, 'permission-input-complete')
    return {**expected, 'pid': pid, 'applications': applications, 'native_nodes': nodes,
        'after_input': desktop.snapshot(), 'native_actions': native_actions,
        'input': 'AT-SPI native button click' if native_actions else 'QMP keyboard after native button focus',
        'browser_focus': 'QMP tablet with joined pointer reports'}
