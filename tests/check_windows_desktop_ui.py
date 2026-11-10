"""Opt-in mocked Windows desktop rendering; no installer or real registration."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path, required=True)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    args.screenshots.mkdir(parents=True, exist_ok=False)
    errors = []
    external = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
        try:
            for width, height in ((1280, 900), (620, 640), (390, 720)):
                page = browser.new_page(viewport={'width': width, 'height': height})
                page.on('pageerror', lambda e: errors.append(str(e)))
                def route(request):
                    url = urlsplit(request.request.url)
                    if url.hostname == '127.0.0.1' and url.port == 4173:
                        request.continue_()
                    else:
                        external.append(request.request.url)
                        request.abort()
                page.route('**/*', route)
                page.add_init_script("""
                    window.__calls = [];
                    window.__fixture = {state:'ready',tray_available:true,jobs:[],application:{kind:'windows',installed:true,app:'C:\\Users\\Preview বাংলা\\AppData\\Local\\VidGrab\\VidGrab.exe'},browser_connection:{enabled:true,bridge_ready:true,chrome:'registered',firefox:'registered'}};
                    window.__TAURI_INTERNALS__ = {invoke:async (name,args) => {
                        if (name === 'companion_status') return structuredClone(window.__fixture);
                        window.__calls.push(name);
                        if (name === 'remove_application') return {...window.__fixture.application,installed:false};
                        return {};
                    }};
                """)
                page.goto('http://127.0.0.1:4173', wait_until='networkidle')
                expect(page.locator('.connection')).to_have_text('Connected')
                page.locator('.application-setup summary').click()
                expect(page.get_by_text('Installed for this Windows user', exact=True)).to_be_visible()
                expect(page.get_by_text('Firefox Flatpak setup', exact=True)).to_have_count(0)
                page.get_by_role('button', name='Open Installed apps', exact=True).click()
                assert page.evaluate('window.__calls') == ['open_software_manager']
                page.evaluate('window.__fixture.downloads_active = true')
                expect(page.get_by_role('button', name='Prepare to remove…', exact=True)).to_be_disabled(timeout=5000)
                page.evaluate('window.__fixture.downloads_active = false')
                expect(page.get_by_role('button', name='Prepare to remove…', exact=True)).to_be_enabled(timeout=5000)
                page.get_by_role('button', name='Prepare to remove…', exact=True).click()
                expect(page.get_by_role('dialog')).to_contain_text('Installed apps')
                expect(page.get_by_role('button', name='Keep VidGrab', exact=True)).to_be_focused()
                page.keyboard.press('Escape')
                assert page.evaluate('window.__calls') == ['open_software_manager']
                page.get_by_role('button', name='Prepare to remove…', exact=True).click()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.screenshot(path=str(args.screenshots / f'{width}-windows.png'), full_page=True)
                page.get_by_role('button', name='Disconnect and quit', exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                assert page.evaluate('window.__calls') == ['open_software_manager','remove_application']
                page.close()
                print(f'{width}x{height}: Windows guidance, removal, cancellation and overflow pass')
        finally:
            browser.close()
    assert not errors, errors
    assert not external, external


if __name__ == '__main__':
    main()
