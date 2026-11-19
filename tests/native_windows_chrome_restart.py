"""Separate Windows Chrome restart fixture; importing starts nothing."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4


def validate_install_reply(reply, expected):
    assert reply.get('result') == 'passed', reply
    assert reply.get('acknowledgement') == expected, 'Foreign folder acknowledgement'
    assert reply.get('input') == 'UIAutomation Load unpacked; Win32 WM_SETTEXT and BM_CLICK'
    assert reply.get('folder') == expected['extension'], 'Different native folder selected'
    provider = reply.get('provider', {})
    assert provider.get('parent_pid') == expected['pid']
    assert provider.get('creation_filetime', 0) >= expected['creation_filetime']
    for field in ('program', 'program_sha256', 'session', 'profile'):
        assert provider.get(field) == expected[field], 'Foreign folder provider ' + field
    assert reply.get('target', {}).get('pid') == provider.get('pid')
    assert reply['target'].get('browser_pid') == expected['pid']
    assert reply['target'].get('name') == 'Select Folder'
    return reply


def validate_relaunch(original, restarted, profile):
    for field in ('program', 'program_sha256', 'session'):
        assert original[field] == restarted[field], 'Relaunch changed ' + field
    assert restarted['session'] == 1
    assert (original['pid'], original['creation_filetime']) != (
        restarted['pid'], restarted['creation_filetime']), 'Original browser is still selected'
    assert '--user-data-dir=' + str(profile) in restarted['command'], 'Different profile selected'


def validate_install_armed(receipt, expected):
    assert receipt.get('stage') == 'armed-before-dialog'
    assert receipt.get('acknowledgement') == expected
    assert isinstance(receipt.get('initial_hwnds'), list)
    assert all(type(handle) is int for handle in receipt['initial_hwnds'])
    return receipt


def validate_closed_profile(witness, nonce, profile):
    import re
    assert witness.get('nonce') == nonce and witness.get('profile') == str(profile)
    assert re.fullmatch(r'[0-9a-f]{64}', witness.get('preferences_sha256', ''))
    assert isinstance(witness.get('extension_entry'), dict)
    records = witness.get('records', {})
    assert set(records) == {'Preferences', 'Secure Preferences'}
    selected = witness.get('selected_file')
    assert selected in records
    assert records[selected]['sha256'] == witness['preferences_sha256']
    assert records[selected]['extension_entry'] == witness['extension_entry']
    for record in records.values():
        assert re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
        assert record['extension_entry'] is None or record['extension_entry'] == witness['extension_entry']
    return witness


def validate_manager_receipt(receipt):
    assert receipt['host_access'] == ['ON_ALL_SITES', 'ON_CLICK', 'ON_ALL_SITES']
    for phase in ('before', 'withheld', 'restored'):
        state = receipt[phase]
        assert state['request_observation_enabled'] and state['request_observation_granted'] and state['navigation_granted']
        assert state['site_granted'] is (phase != 'withheld')
    assert receipt['restored'] == receipt['before']
    assert receipt['counts'] == {'before': 1, 'withheld': 0, 'restored': 1}
    return receipt


def run_bound_task(metadata):
    import re
    name, digest = metadata['name'], metadata['xml_sha256']
    assert re.fullmatch(r'VidGrab-P05-native-install-[a-z0-9]+-\d+', name)
    assert re.fullmatch(r'[0-9a-f]{64}', digest)
    script = "$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;$s.Connect();" \
        "$t=$s.GetFolder('\\').GetTask('" + name + "');$sha=[Security.Cryptography.SHA256]::Create();" \
        "$h=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$t.Xml))).Replace('-','').ToLower();" \
        "if($h -cne '" + digest + "'){throw 'Owned install task changed'};$t.Run($null)|Out-Null"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        check=True, capture_output=True, timeout=20)


def load_unpacked_ui(browser, extension):
    from tests.native_windows_grant_input import browser_identity, reserve_reply
    from tests.native_client_support import windows_wait_file
    assert os.name == 'nt' and os.environ.get('COMPUTERNAME') == 'DESKTOP-OSOIBNA'
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    assert browser.kind == 'chrome' and browser.permission_events
    assert extension.is_absolute() and extension.is_dir() and not extension.is_symlink()
    started = time.monotonic()
    expected = {'browser': 'chrome', 'choice': 'load-unpacked', 'nonce': str(uuid4()),
        **browser_identity(browser), 'extension': str(extension), 'profile': str(browser.root / 'profile')}
    task = browser.chrome_task.metadata['install_task']
    request, report = Path(task['request']), Path(task['report'])
    assert request == browser.root / 'native-install.request.json'
    assert report == browser.root / 'native-install.reply.json'
    with request.open('x', encoding='utf-8') as stream:
        json.dump(expected, stream)
    reserve_reply(report)
    armed = report.with_suffix('.armed.json')
    reserve_reply(armed)
    manager = browser.context.new_page()
    try:
        manager.goto('chrome://extensions/')
        run_bound_task(task)
        validate_install_armed(windows_wait_file(armed, max(0.01, 25 - (time.monotonic() - started))), expected)
        reply = windows_wait_file(report, max(0.01, 25 - (time.monotonic() - started)))
        validate_install_reply(reply, expected)
        from tests.native_chrome_restart import standalone_inventory
        from tests.check_native_clients import CHROME_ID
        items = standalone_inventory(browser, CHROME_ID)
        manager.screenshot(path=str(browser.root / 'native-install.png'))
        return {'id': items[0]['id']}
    finally:
        manager.close()


def restart_browser(browser, source, args, environment, playwright, identifier, record):
    from tests.native_chrome_restart import standalone_inventory, extension_inventory
    from tests.native_windows_grant_input import browser_identity
    from tests.native_client_support import WindowsBrowserTask, chrome_extension_worker, free_port, wait
    from tests.check_native_clients import INSTRUMENT
    assert os.name == 'nt' and os.environ.get('COMPUTERNAME') == 'DESKTOP-OSOIBNA'
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    original = browser_identity(browser)
    profile = browser.root / 'profile'
    assert profile.is_dir() and not profile.is_symlink()
    identity = (profile.stat().st_dev, profile.stat().st_ino)
    before = browser.permissions(source.page.url)
    assert before['request_observation_granted'] and before['navigation_granted'] and before['request_observation_enabled']
    inventory = standalone_inventory(browser, identifier)
    pages = [(page, page.page.url) for page in browser.pages]
    version = browser.version
    cdp = browser.context.browser.new_browser_cdp_session()
    processes = cdp.send('SystemInfo.getProcessInfo')['processInfo']
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handles, snapshots = [], []
    try:
        for item in processes:
            pid = int(item['id'])
            handle = kernel.OpenProcess(0x101000, False, pid)
            if not handle:
                assert ctypes.get_last_error() == 87 and pid != original['pid'], (pid, ctypes.get_last_error())
                continue
            handles.append(handle)
            times = [wintypes.FILETIME() for _ in range(4)]
            assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
            created = times[0].dwLowDateTime + (times[0].dwHighDateTime << 32)
            program, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            assert kernel.QueryFullProcessImageNameW(handle, 0, program, ctypes.byref(size))
            assert os.path.normcase(program.value) == os.path.normcase(original['program'])
            if pid == original['pid']:
                assert created == original['creation_filetime']
            snapshots.append({'pid': pid, 'creation_filetime': created, 'program': program.value, 'type': item['type']})
        assert any(item['pid'] == original['pid'] for item in snapshots)
        record({'phase': 'before-explicit-restart', 'permissions': before, 'inventory': inventory,
            'profile': str(profile), 'profile_identity': identity, 'processes': snapshots, 'launcher': browser.chrome_task.audit})
        deadline = time.monotonic() + 25
        try:
            cdp.send('Browser.close')
        except Exception:
            pass
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000))) == 0, \
                'Selected Chrome process is still alive'
        browser.chrome_task.close()
        browser.chrome_browser.close()
        record({'phase': 'full-browser-exit-joined', 'processes': snapshots, 'joined_handles': len(handles),
            'launcher_cleanup': browser.chrome_task.audit['cleanup']})
        witness = validate_closed_profile(browser.chrome_task.audit['cleanup']['closed_profile'],
            browser.chrome_task.nonce, profile)
        record({'phase': 'closed-profile-installation', **witness})
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
    assert (profile.stat().st_dev, profile.stat().st_ino) == identity
    selected = browser.root.parent / 'chrome-restarted'
    selected.mkdir()
    descriptor = browser.chrome_task.metadata['restart_descriptor']
    port = free_port()
    browser.chrome_task = WindowsBrowserTask(descriptor, selected, args.chrome, profile, port)
    validate_relaunch(original, browser.chrome_task.audit, profile)
    import urllib.request
    def endpoint():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
                return json.load(response)
        except OSError:
            return None
    wait(endpoint, 'same-profile restarted Chrome debugging endpoint')
    browser.chrome_browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
    assert len(browser.chrome_browser.contexts) == 1
    browser.context = browser.chrome_browser.contexts[0]
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
        current = next(int(item['id']) for item in cdp.send('SystemInfo.getProcessInfo')['processInfo'] if item['type'] == 'browser')
        assert current == browser.chrome_task.audit['pid']
    finally:
        cdp.detach()
    after = browser.permissions(source.page.url)
    assert before['request_observation_restart_required'] is True
    assert after['request_observation_restart_required'] is False
    assert {**after, 'request_observation_restart_required': True} == before
    record({'phase': 'same-granted-profile-reopened', 'version': version, 'permissions': after,
        'inventory': standalone_inventory(browser, identifier), 'profile_identity': identity,
        'launcher': browser.chrome_task.audit, 'extension_loaded_again': False})


def inspect_manager(browser, screenshots):
    """Capture the actual manager before separate original API cleanup."""
    from tests.check_native_clients import CHROME_ID
    page = browser.context.new_page()
    try:
        page.goto('chrome://extensions/?id=' + CHROME_ID)
        page.wait_for_selector('extensions-detail-view')
        data = page.evaluate('''() => {
            const result=[];
            function walk(root) {
                for (const node of root.querySelectorAll('*')) {
                    if (node.getClientRects().length && (['BUTTON','SELECT','INPUT','CR-TOGGLE','A'].includes(node.tagName)
                        || node.getAttribute('role'))) result.push({tag:node.tagName,id:node.id,role:node.getAttribute('role'),
                            text:node.innerText,label:node.getAttribute('aria-label'),checked:node.checked,
                            disabled:node.disabled,value:node.value});
                    if (node.shadowRoot) walk(node.shadowRoot);
                }
            }
            walk(document);
            return {url:location.href,title:document.title,controls:result,
                detail:document.querySelector('extensions-manager')?.shadowRoot?.querySelector('extensions-detail-view')?.shadowRoot?.innerHTML};
        }''')
        path = screenshots / 'chrome-manager-inspection.json'
        with path.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
        page.screenshot(path=str(screenshots / 'chrome-manager-inspection.png'), full_page=True)
        if browser.manager_site_access:
            manager_site_access(browser, page, screenshots)
    finally:
        page.close()


def manager_site_access(browser, page, screenshots):
    """Withhold and restore host access through the real browser manager."""
    from tests.native_client_support import wait
    import traceback
    base = browser.fixture_base
    source = next(item for item in browser.pages if item.page.url.startswith(base + '/discovery/watch'))
    selector = page.locator('extensions-detail-view #hostAccess')
    assert selector.is_visible() and selector.is_enabled()
    assert selector.input_value() == 'ON_ALL_SITES'
    receipt = {'result': 'running', 'host_access': [],
        'scope': 'Chrome manager site-host withholding/restoration; API grants and enabled preference retained; original API cleanup is separate'}
    report = screenshots / 'chrome-manager-site-access.json'
    def save():
        report.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    def access():
        return page.evaluate('''() => new Promise(resolve => chrome.developerPrivate.getExtensionsInfo(
            {includeDisabled:true,includeTerminated:true}, items => resolve(items.find(item =>
                item.id === 'ikjpekhbbaehdhigakbahlhafaojbjpm').permissions.runtimeHostPermissions.hostAccess)))''')
    def fetch(phase):
        source.asynchronous('''fetch(arguments[0], {cache:'no-store'}).then(response => response.text())
            .then(() => arguments[arguments.length-1](true));''', base + '/discovery/manifest.m3u8?manager=' + phase)
    browser.background('''const base=arguments[0];const scope=this;
        scope.__managerObservation={counts:{before:0,withheld:0,restored:0},scoped:true};
        scope.__managerListener=details => {
            for(const phase of ['before','withheld','restored']) if(details.url===base+'/discovery/manifest.m3u8?manager='+phase) {
                scope.__managerObservation.counts[phase]++;
                scope.__managerObservation.scoped &&= details.frameId===0 && details.tabId>=0;
            }
        };
        chrome.webRequest.onHeadersReceived.addListener(scope.__managerListener,
            {urls:[base+'/*']},['responseHeaders']);''', base)
    def observed(phase):
        return browser.background('return this.__managerObservation.counts[arguments[0]]', phase)
    try:
        receipt['before'] = browser.permissions(base)
        receipt['host_access'].append(access())
        fetch('before')
        wait(lambda: observed('before') == 1, 'actual response before manager withholding')
        page.bring_to_front()
        selector.select_option('ON_CLICK', timeout=25000)
        wait(lambda: access() == 'ON_CLICK' and not browser.permissions(base)['site_granted'],
            'visible manager withheld actual host access')
        receipt['withheld'] = browser.permissions(base)
        receipt['host_access'].append(access())
        page.screenshot(path=str(screenshots / 'chrome-manager-withheld.png'), full_page=True)
        fetch('withheld')
        page.bring_to_front()
        selector.select_option('ON_ALL_SITES', timeout=25000)
        wait(lambda: access() == 'ON_ALL_SITES' and browser.permissions(base)['site_granted'],
            'visible manager restored actual host access')
        receipt['restored'] = browser.permissions(base)
        receipt['host_access'].append(access())
        fetch('restored')
        wait(lambda: observed('restored') == 1, 'actual response after manager restoration')
        observation = browser.background('return this.__managerObservation')
        assert observation['scoped']
        receipt['counts'] = observation['counts']
        validate_manager_receipt(receipt)
        page.screenshot(path=str(screenshots / 'chrome-manager-restored.png'), full_page=True)
        receipt['result'] = 'passed'
        browser.background('chrome.webRequest.onHeadersReceived.removeListener(this.__managerListener)')
    except BaseException:
        receipt['failure'] = traceback.format_exc()
        raise
    finally:
        save()


def main():
    """Run the original client file through separate installation/restart adapters."""
    from tests import check_native_clients as clients, native_discovery_journeys as journeys
    from tests import native_chrome_restart as restart
    from playwright.sync_api import CDPSession
    import sys
    site_access = '--manager-site-access' in sys.argv
    if site_access:
        sys.argv.remove('--manager-site-access')
    original_initialize = clients.Browser.__init__
    original_visible = journeys.visible_grants_journey
    original_restart = restart.restart_browser

    def initialize(browser, kind, args, environment, root, playwright, manifests):
        raw = CDPSession.send
        def send(session, method, params=None):
            if method == 'Extensions.loadUnpacked':
                return load_unpacked_ui(browser, Path(params['path']))
            return raw(session, method, params)
        CDPSession.send = send
        try:
            original_initialize(browser, kind, args, environment, root, playwright, manifests)
        finally:
            CDPSession.send = raw
        browser.restart_arguments = (args, environment, playwright)
        browser.manager_site_access = site_access
        raw_background = browser.background
        def background(body, *arguments):
            # Inspect only after the explicit restart; the original mutation is
            # still executed once by the original journey.
            if 'this.chrome.permissions.remove' in body and browser.chrome_task.metadata.get('restarted') \
                    and not getattr(browser, 'manager_inspected', False):
                browser.manager_inspected = True
                inspect_manager(browser, args.screenshots)
            return raw_background(body, *arguments)
        browser.background = background

    def visible(browser, native, base, screenshots, permission_input, revocation_input=None):
        assert revocation_input is None, 'This lane has manager inspection and separate original API removal'
        args, environment, playwright = browser.restart_arguments
        browser.fixture_base = base
        return restart.restart_grants_journey(browser, native, base, screenshots, permission_input,
            args, environment, playwright)

    clients.Browser.__init__ = initialize
    journeys.visible_grants_journey = visible
    restart.restart_browser = restart_browser
    try:
        clients.main()
    finally:
        clients.Browser.__init__ = original_initialize
        journeys.visible_grants_journey = original_visible
        restart.restart_browser = original_restart


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
