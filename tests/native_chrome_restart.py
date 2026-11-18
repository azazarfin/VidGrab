"""Separate Ubuntu validation lane; importing this module starts nothing.

The original journey and its assertions stay intact. Only its observation wait
is adapted, after saving the expected fresh-grant failure as a separate gate.
This is an explicit fixture browser restart, never a production repair/retry.
"""

import ast
import inspect
import json
import os
from pathlib import Path
import select
import time
import traceback

LABEL = 'actual granted response-header observation'


def restart_observation_wait(original_wait, failed, restart, passed):
    used = False

    def wait(predicate, label, timeout=25):
        nonlocal used
        if label != LABEL:
            return original_wait(predicate, label, timeout)
        assert not used, 'The explicit restart boundary was entered twice'
        used = True
        try:
            original_wait(predicate, label, timeout)
        except AssertionError as error:
            if not str(error).startswith(LABEL + ': deadline exceeded; last observation '):
                raise
            failed(error)
        else:
            raise AssertionError('Fresh-grant observation unexpectedly passed; record a new browser result')
        restart()
        result = original_wait(predicate, label, timeout)
        passed(result)
        return result

    return wait


def extension_inventory(browser, wait_identifier=None):
    manager = browser.context.new_page()
    try:
        manager.goto('chrome://extensions/')
        if wait_identifier:
            manager.wait_for_function('''identifier => new Promise(resolve =>
                chrome.developerPrivate.getExtensionsInfo({includeDisabled:true,includeTerminated:true},
                    items => resolve(items.some(item => item.id === identifier))))''',
                arg=wait_identifier, timeout=25000)
        items = manager.evaluate('''() => new Promise(resolve => chrome.developerPrivate.getExtensionsInfo(
            {includeDisabled:true,includeTerminated:true}, resolve))''')
        return items
    finally:
        manager.close()


def standalone_inventory(browser, identifier):
    items = extension_inventory(browser, wait_identifier=identifier)
    assert len(items) == 1 and items[0]['id'] == identifier and items[0]['state'] == 'ENABLED', items
    return items


def restart_browser(browser, source, args, environment, playwright, identifier, record):
    from tests.check_native_clients import INSTRUMENT
    from tests.native_client_support import chrome_extension_worker, wait
    from tests.check_installed_browsers import chrome_renderer_sandbox

    assert browser.kind == 'chrome' and os.name != 'nt' and browser.permission_events
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    assert Path('/etc/hostname').read_text().strip() == 'vidgrab-p02-ubuntu-validation'
    profile = browser.root / 'profile'
    assert profile.is_dir() and not profile.is_symlink()
    profile_identity = (profile.stat().st_dev, profile.stat().st_ino)
    before = browser.permissions(source.page.url)
    assert before['request_observation_granted'] and before['navigation_granted'] and before['request_observation_enabled']
    inventory = standalone_inventory(browser, identifier)
    cdp = browser.context.browser.new_browser_cdp_session()
    processes = cdp.send('SystemInfo.getProcessInfo')['processInfo']
    main = next(int(item['id']) for item in processes if item['type'] == 'browser')
    snapshot, descriptors = [], []
    try:
        for item in processes:
            pid = int(item['id'])
            proc = Path('/proc') / str(pid)
            try:
                raw = (proc / 'stat').read_text()
                start = raw[raw.rfind(')') + 2:].split()[19]
                assert (proc / 'exe').resolve() == args.chrome.resolve(), pid
                descriptor = os.pidfd_open(pid)
                after = (proc / 'stat').read_text()
                assert after[after.rfind(')') + 2:].split()[19] == start
            except FileNotFoundError:
                assert pid != main
                continue
            descriptors.append(descriptor)
            command = (proc / 'cmdline').read_bytes().split(b'\0')[:-1]
            if pid == main:
                assert ('--user-data-dir=' + str(profile)).encode() in command
            snapshot.append({'pid': pid, 'starttime': start, 'type': item['type'],
                'command': [arg.decode() for arg in command]})
        assert any(item['pid'] == main for item in snapshot)
        pages = [(page, page.page.url) for page in browser.pages]
        version = browser.version
        record({'phase': 'before-explicit-restart', 'permissions': before, 'inventory': inventory,
            'profile': str(profile), 'profile_identity': profile_identity, 'processes': snapshot})
        cdp.detach()
        browser.context.close()
        deadline = time.monotonic() + 25
        pending = set(descriptors)
        while pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'Selected Chrome is still alive'
            exited, _, _ = select.select(list(pending), [], [], remaining)
            assert exited, 'Selected Chrome is still alive'
            pending.difference_update(exited)
        exit_states = []
        for item in snapshot:
            proc = Path('/proc') / str(item['pid'])
            try:
                raw = (proc / 'stat').read_text()
                fields = raw[raw.rfind(')') + 2:].split()
                assert fields[19] != item['starttime'] or fields[0] == 'Z', item
                state = 'zombie' if fields[19] == item['starttime'] else 'identity-replaced'
            except FileNotFoundError:
                state = 'absent'
            exit_states.append({**item, 'exit_state': state})
        record({'phase': 'full-browser-exit-joined', 'processes': exit_states})
        preferences = json.loads((profile / 'Default/Preferences').read_text())
        entry = preferences.get('extensions', {}).get('settings', {}).get(identifier)
        record({'phase': 'closed-profile-installation', 'extension_entry': entry})
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    assert (profile.stat().st_dev, profile.stat().st_ino) == profile_identity
    browser.context = playwright.chromium.launch_persistent_context(str(profile),
        executable_path=str(args.chrome), headless=False, env=environment, chromium_sandbox=True,
        ignore_default_args=['--disable-extensions'],
        args=['--enable-unsafe-extension-debugging', '--disable-background-networking',
            '--disable-component-update', '--no-first-run', '--no-default-browser-check',
            '--force-renderer-accessibility'])
    browser.pages, browser.network_guards = [], []
    record({'phase': 'reopened-browser-inventory', 'inventory': extension_inventory(browser)})
    standalone_inventory(browser, identifier)
    for page, url in pages:
        fresh = browser.open('popup/popup.html')
        if fresh.page.url != url:
            fresh.page.goto(url, wait_until='domcontentloaded')
        page.page = fresh.page
        browser.pages[-1] = page
    browser.worker = chrome_extension_worker(browser.context, identifier)
    wait(lambda: browser.background('return Boolean(this.vidGrabAPI && this.vidGrabNative && this.vidGrabCompanion)'),
        'restarted actual extension bootstrap')
    browser.background(INSTRUMENT, 'vidGrabNative', 'request', False)
    browser.popup.has_text('#connection-status', 'Connected')
    browser.options.has_text('#status-message', 'Settings loaded')
    browser.options.click('[data-tab="tab-advanced"]')
    cdp = browser.context.browser.new_browser_cdp_session()
    try:
        browser.version = cdp.send('Browser.getVersion')
        assert browser.version == version
        restarted_pid = next(int(item['id']) for item in cdp.send('SystemInfo.getProcessInfo')['processInfo']
            if item['type'] == 'browser')
        assert restarted_pid != main
        browser.sandbox = chrome_renderer_sandbox(cdp)
        browser.background_diagnostics = {'selected_url': browser.worker.url,
            'workers': [worker.url for worker in browser.context.service_workers],
            'targets': cdp.send('Target.getTargets')['targetInfos']}
    finally:
        cdp.detach()
    after = browser.permissions(source.page.url)
    assert after == before
    inventory = standalone_inventory(browser, identifier)
    record({'phase': 'same-granted-profile-reopened', 'pid': restarted_pid, 'version': browser.version,
        'permissions': after, 'inventory': inventory, 'sandbox': browser.sandbox,
        'profile_identity': profile_identity, 'extension_loaded_again': False})


def restart_grants_journey(browser, native, base, screenshots, permission_input, args, environment, playwright):
    from tests import native_discovery_journeys as journeys
    from tests.check_native_clients import CHROME_ID
    from tests.native_client_support import wait

    source = browser.open('popup/popup.html')
    source.goto(base + '/discovery/next')
    receipt = {'result': 'running', 'fresh_grant_gate': 'pending', 'restart_gate': 'pending', 'phases': [],
        'scope': 'explicit standalone Chrome restart; fresh-grant failure remains a separate failed gate'}
    report = screenshots / 'chrome-explicit-restart.json'

    def save():
        report.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')

    def tab_key():
        tab = wait(lambda: browser.background('''return new Promise(resolve => this.chrome.tabs.query({},
            tabs => resolve(tabs.find(tab => tab.url === arguments[0]))));''', source.page.url),
            'standalone source tab identity')
        return 'detected_videos_' + str(tab['id'])

    key = tab_key()

    def candidates():
        return browser.background('''return new Promise(resolve => this.chrome.storage.local.get(arguments[0],
            value => resolve(value[arguments[0]] || [])));''', key)

    def failed(error):
        receipt.update(fresh_grant_gate='failed-original-observation-assertion', fresh_grant_failure=str(error),
            fresh_permissions=browser.permissions(base),
            fresh_diagnostics=browser.background('return this.__grantDiagnostics'),
            fresh_inventory=standalone_inventory(browser, CHROME_ID))
        save()
        source.screenshot(screenshots / 'chrome-fresh-grant-failure.png')
        browser.options.screenshot(screenshots / 'chrome-fresh-grant-options.png')

    def record(phase):
        receipt['phases'].append(phase)
        save()

    def restart():
        nonlocal key
        try:
            restart_browser(browser, source, args, environment, playwright, CHROME_ID, record)
        except BaseException:
            receipt['restart_failure'] = traceback.format_exc()
            save()
            raise
        key = tab_key()
        # Reinstall only the original journey's volatile diagnostic witnesses.
        tree = ast.parse(inspect.getsource(journeys.optional_grants_journey))
        scripts = [node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == 'background' and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
            and 'scope.__grantDiagnostics={events:[],before:[],admissions:[]}' in node.args[0].value]
        assert len(scripts) == 1
        browser.background(scripts[0], base)
        source.goto(base + '/discovery/watch?browser=' + browser.kind)
        key = tab_key()
        wait(lambda: len(candidates()) == 3, 'content detection after the explicit browser restart')
        source.asynchronous('''fetch(arguments[0], {cache:'no-store'}).then(response => response.text())
            .then(() => arguments[arguments.length-1](true));''', base + '/discovery/manifest.m3u8?cycle=enabled')

    def passed(observed):
        receipt.update(restart_gate='passed-original-observation-wait', observed_count=len(observed))
        save()

    original_wait = journeys.wait
    journeys.wait = restart_observation_wait(original_wait, failed, restart, passed)
    try:
        receipt['original_journey'] = journeys.optional_grants_journey(browser, native, source, base,
            candidates, screenshots, permission_input=permission_input)
        assert receipt['fresh_grant_gate'] == 'failed-original-observation-assertion'
        assert receipt['restart_gate'] == 'passed-original-observation-wait'
        receipt['result'] = 'passed-explicit-restart-journey'
        return receipt
    finally:
        journeys.wait = original_wait
        save()
