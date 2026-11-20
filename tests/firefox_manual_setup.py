"""Visible guide and Firefox settings input, only in the disposable portal VM."""

import json
from pathlib import Path
import subprocess
import sys

from tests.native_support import WebDriver, local_call, stop_owner, wait_for

PREFERENCE = 'widget.use-xdg-desktop-portal.native-messaging'


def browser_exited(process, owned):
    # poll() reaps our direct child. A /proc-only check waits on its own zombie.
    return process.poll() is not None and not any(Path(f'/proc/{pid}').exists() for pid in owned)


def chrome_read(driver, script, *arguments):
    driver.call('/moz/context', {'context': 'chrome'})
    try:
        return driver.script(script, *arguments)
    finally:
        driver.call('/moz/context', {'context': 'content'})


def preference(driver):
    return chrome_read(driver, '''const key = arguments[0];
        return {type: Services.prefs.getPrefType(key),
            user_set: Services.prefs.prefHasUserValue(key),
            value: Services.prefs.getIntPref(key, -1),
            proxy: Services.prefs.getIntPref(key + '-proxy', -1)};''', PREFERENCE)


def addon_info(driver, identifier):
    driver.call('/moz/context', {'context': 'chrome'})
    try:
        return driver.script('''const done = arguments[arguments.length - 1];
            const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            AddonManager.getAddonByID(arguments[0]).then(addon => done({present: !!addon,
                temporary: addon?.temporarilyInstalled ?? null}));''', identifier, asynchronous=True)
    finally:
        driver.call('/moz/context', {'context': 'content'})


def rectangle(driver, selector, *, frame=None, preference_row=False):
    value = driver.script('''const row = arguments[2]
        ? [...document.querySelectorAll('#prefs tr')].find(
            row => row.querySelector('th')?.textContent === arguments[1]) : document;
        const element = row?.querySelector(arguments[0]);
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
            innerX: window.mozInnerScreenX ?? null, innerY: window.mozInnerScreenY ?? null,
            viewportWidth: innerWidth, viewportHeight: innerHeight, scale: devicePixelRatio};''',
        selector, PREFERENCE, preference_row)
    assert value and value['width'] > 0 and value['height'] > 0 and value['scale'] == 1, value
    assert 0 <= value['x'] < value['viewportWidth'] and 0 <= value['y'] < value['viewportHeight'], value
    if frame:
        x = frame['x'] + (frame['width'] - value['viewportWidth']) / 2
        y = frame['y'] + frame['height'] - value['viewportHeight']
    else:
        x, y = value['innerX'], value['innerY']
        assert x is not None and y is not None, value
    return {key: value[key] for key in ('width', 'height')} | {'x': x + value['x'], 'y': y + value['y']}


class VirtualSetup:
    def __init__(self, desktop, pointer_reply):
        self.desktop = desktop
        self.pointer_reply = pointer_reply

    def rectangle(self, driver, selector, **options):
        windows = [window for window in self.desktop.snapshot()['windows']
            if window['focused'] and window['visible']]
        assert len(windows) == 1, 'Select one visible focused Firefox window'
        return rectangle(driver, selector, frame=windows[0]['frame'], **options)

    def input(self, label, observe, *, text_progress=None, **action):
        assert sum(key in action for key in ('rectangle', 'text', 'keys')) == 1, action
        if 'text' in action:
            assert callable(text_progress), 'Observe every text-input character'
        print(json.dumps({'event': 'setup-input', 'label': label,
            'snapshot': self.desktop.snapshot(), **action}), flush=True)
        received = 0
        for line in sys.stdin:
            request = json.loads(line)
            if request['op'] == 'setup-text-progress':
                prefix = request['prefix']
                assert 'text' in action and prefix == action['text'][:received + 1] and len(prefix) == received + 1
                wait_for(lambda: text_progress(prefix), timeout=10, label=label + ': ' + prefix)
                received += 1
                print(json.dumps({'reply': request['op'], 'observed_prefix': prefix}), flush=True)
                continue
            if request['op'] == 'setup-input-complete':
                assert 'text' not in action or received == len(action['text']), 'Observe complete text delivery'
                observed = wait_for(observe, timeout=10, label=label)
                print(json.dumps({'reply': request['op'], 'observed': observed}), flush=True)
                return observed
            self.pointer_reply(self.desktop, request)
        raise AssertionError('Host must acknowledge the observed manual setup input')


def setup_firefox(driver, virtual):
    initial = preference(driver)
    assert initial['type'] == 64 and initial['value'] == 0 and not initial['user_set'], initial
    virtual.input('Focus Firefox address bar',
        lambda: chrome_read(driver, 'return window.gURLBar.focused'), keys=['ctrl', 'l'])
    virtual.input('Type about:config',
        lambda: chrome_read(driver, 'return window.gURLBar.value === "about:config"'), text='about:config',
        text_progress=lambda prefix: chrome_read(driver, 'const input = window.gURLBar.inputField;'
            ' return input.value.slice(0, input.selectionStart) === arguments[0]', prefix))
    virtual.input('Open Firefox advanced settings',
        lambda: driver.script('return location.href === "about:config"'), keys=['ret'])
    if driver.script('return !!document.querySelector("#warningButton")'):
        virtual.input('Accept Firefox advanced-settings notice',
            lambda: driver.script('return !!document.querySelector("#about-config-search")'),
            rectangle=virtual.rectangle(driver, '#warningButton'))
    virtual.input('Focus exact preference search',
        lambda: driver.script('return document.activeElement?.id === "about-config-search"'),
        rectangle=virtual.rectangle(driver, '#about-config-search'))
    virtual.input('Search the existing native portal preference',
        lambda: driver.script('return document.querySelector("#about-config-search").value === arguments[0]', PREFERENCE),
        text=PREFERENCE, text_progress=lambda prefix: driver.script(
            'return document.querySelector("#about-config-search").value === arguments[0]', prefix))
    # Firefox's delayed search can otherwise end an edit after the click. Enter
    # cancels that pending filter and renders the final query immediately.
    virtual.input('Confirm the exact preference search',
        lambda: driver.script('return [...document.querySelectorAll("#prefs th")].some('
            'row => row.textContent === arguments[0])', PREFERENCE), keys=['ret'])
    virtual.input('Edit the existing Number preference',
        lambda: driver.script('const input = document.querySelector("#form-edit input");'
            ' return input?.type === "number" && input.getAttribute("aria-label") === arguments[0]', PREFERENCE),
        rectangle=virtual.rectangle(driver, '.button-edit', preference_row=True))
    virtual.input('Focus the Number field',
        lambda: driver.script('return document.activeElement?.getAttribute("aria-label") === arguments[0]', PREFERENCE),
        rectangle=virtual.rectangle(driver, '#form-edit input'))
    virtual.input('Select the existing Number value',
        lambda: driver.script('return document.activeElement?.type === "number"'), keys=['ctrl', 'a'])
    virtual.input('Type Number value 1',
        lambda: driver.script('return document.querySelector("#form-edit input").value === "1"'), text='1',
        text_progress=lambda prefix: driver.script(
            'return document.querySelector("#form-edit input").value === arguments[0]', prefix))
    virtual.input('Save Number value 1 with the checkmark',
        lambda: driver.script('return !document.querySelector("#form-edit")'),
        rectangle=virtual.rectangle(driver, '.button-save', preference_row=True))
    saved = preference(driver)
    assert saved == {**initial, 'value': 1, 'user_set': True}, saved
    return {'initial': initial, 'saved': saved, 'input': 'QMP virtual mouse and keyboard'}


def packaged_guide(app, home, environment, desktop, virtual, focus):
    home.mkdir(mode=0o700)
    selected = {**environment, 'HOME': str(home), 'XDG_DATA_HOME': str(home / 'data'),
        'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache')}
    from tests.check_packaged_desktop import configure

    config = configure(app, home / 'fixture', selected)
    driver = WebDriver('/usr/bin/WebKitWebDriver',
        {**selected, 'TAURI_WEBVIEW_AUTOMATION': 'true'}, home / 'driver.log')
    try:
        driver.start({'webkitgtk:browserOptions': {'binary': str(app), 'args': []}})
        wait_for(lambda: driver.script('return document.querySelector(".connection")?.textContent === "Connected"'),
            label='rebuilt package guide ready')
        pid = local_call(config)['supervisor_pid']
        focus(desktop, pid)
        window = desktop.window(pid)
        driver.script('document.querySelector(\'[data-desktop-tab="browser"]\')?.click()')
        driver.script('document.querySelector(".browser-setup summary").scrollIntoView({block:"center"})')
        virtual.input('Open the packaged Firefox Flatpak setup help',
            lambda: driver.script('return document.querySelector(".browser-setup").open'),
            rectangle=rectangle(driver, '.browser-setup summary', frame=window['frame']))
        guide = driver.script('return {text: document.querySelector(".browser-setup").innerText,'
            'preference: document.querySelector(".portal-preference").textContent,'
            'overflow: document.documentElement.scrollWidth > innerWidth}')
        assert guide['preference'] == PREFERENCE and not guide['overflow'], guide
        for text in ('Number', '1', 'Fully quit Firefox', 'temporary VidGrab add-on', 'Allow', 'regular extension popup'):
            assert text in guide['text'], guide
        steps = driver.script('return [...document.querySelectorAll(".browser-setup li")].map(item => item.innerText)')
        assert next(index for index, text in enumerate(steps) if 'Fully quit Firefox' in text) \
            < next(index for index, text in enumerate(steps) if 'temporary VidGrab add-on' in text)
        driver.screenshot(home / 'packaged-guide.png')
        return {'guide': guide, 'window': window}
    finally:
        try:
            stop_owner(config)
        finally:
            try:
                driver.close()
            finally:
                subprocess.run([str(app), '--unregister-browser-host'], env=selected,
                    capture_output=True, text=True, check=True, timeout=30)
