"""Visible Firefox Add-ons manager revocation in an owned native journey."""

from tests.native_client_support import Page, wait


def revoke_navigation(browser, screenshots):
    assert browser.kind == 'firefox' and browser.permission_events
    handle = browser.driver.call('/window/new', {'type': 'tab'})['handle']
    manager = Page(driver=browser.driver, handle=handle)
    manager.goto('about:addons')
    manager.click('#category-extension')
    card = 'addon-card[addon-id="vidgrab@azaz.local"]'
    wait(lambda: manager.visible(card + ' .addon-name-link'), 'visible owned VidGrab Add-ons manager card')
    manager.click(card + ' .addon-name-link')
    manager.click('button[name="permissions"]')
    selector = card + ' moz-toggle[permission-key="webNavigation"]'

    def state():
        return manager.script('''const control=document.querySelector(arguments[0]);
            const header=document.querySelector('addon-page-header');
            return control && {url:location.href,addon:header?.getAttribute('current-param'),
                key:control.getAttribute('permission-key'),label:control.getAttribute('label'),
                visible:Boolean(control.getClientRects().length),pressed:control.pressed === true,
                disabled:control.disabled === true};''', selector)

    before = wait(lambda: (value if (value := state()) and value['visible'] and value['pressed'] else None),
        'visible granted navigation toggle in the browser manager')
    assert before['url'] == 'about:addons' and before['addon'] == 'vidgrab@azaz.local'
    assert before['key'] == 'webNavigation' and not before['disabled']
    manager.screenshot(screenshots / (browser.kind + '-manager-before.png'))
    manager.click(selector)
    after = wait(lambda: (value if (value := state()) and not value['pressed'] else None),
        'browser-manager navigation toggle switched off')
    manager.screenshot(screenshots / (browser.kind + '-manager-after.png'))
    browser.options.activate()
    return {'input': 'WebDriver click on the visible Firefox browser-manager toggle',
        'before': before, 'after': after, 'permission': 'webNavigation',
        'cleanup': 'The original finally boundary removes remaining API grants separately'}
