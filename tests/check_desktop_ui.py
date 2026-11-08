"""Opt-in desktop render check; Tauri commands are mocked, network is local."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path, required=True)
    arguments = parser.parse_args()
    assert Path('desktop/dist/index.html').is_file(), 'P02 needs a built desktop connection window'
    from playwright.sync_api import expect, sync_playwright
    arguments.screenshots.mkdir(parents=True, exist_ok=True)
    errors = []
    external = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
        try:
            for width, height in ((1280, 900), (620, 600), (390, 720)):
                page = browser.new_page(viewport={'width': width, 'height': height})
                page.on('pageerror', lambda error: errors.append(str(error)))
                def route(request):
                    parsed = urlsplit(request.request.url)
                    if parsed.hostname == '127.0.0.1' and parsed.port == 4173:
                        request.continue_()
                    else:
                        external.append(request.request.url)
                        request.abort()
                page.route('**/*', route)
                page.add_init_script('''
                    window.__fixture = {state: 'ready', tray_available: false, jobs: [],
                      browser_connection: JSON.parse(sessionStorage.getItem('connection') ||
                        '{"enabled":true,"bridge_ready":true,"chrome":"registered","firefox":"registered"}')};
                    window.__connectionActions = 0;
                    window.__statusFailures = 1;
                    window.__statusCalls = 0;
                    window.__TAURI_INTERNALS__ = {
                      invoke: async (command, arguments) => {
                        if (command === 'companion_status') {
                          window.__statusCalls++;
                          if (window.__statusFailures-- > 0) throw 'Temporary engine connection loss';
                          return structuredClone(window.__fixture);
                        }
                        if (command === 'download_fixture') {
                          const url = new URL(arguments.url);
                          if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.pathname !== '/fixture.wav') {
                            throw 'Use a numeric loopback fixture.wav URL';
                          }
                          window.__fixture.jobs = [{id:'fixture', title:'VidGrab test audio', status:'downloading', progress:35}];
                          setTimeout(() => { window.__fixture.jobs[0].status='complete'; window.__fixture.jobs[0].progress=100; window.__fixture.jobs[0].file_path='/tmp/media/fixture.mp3'; }, 500);
                          return {task_id:'fixture'};
                        }
                        if (command === 'set_browser_connection') {
                          window.__connectionActions++;
                          if (window.__registrationFailure) throw 'An existing browser host manifest belongs to another installation';
                          const enabled = arguments.enabled;
                          const registration = {enabled, bridge_ready:true,
                            chrome:enabled?'registered':'missing', firefox:enabled?'registered':'missing'};
                          window.__fixture.browser_connection = registration;
                          sessionStorage.setItem('connection', JSON.stringify(registration));
                          return registration;
                        }
                        if (command === 'quit_companion') return;
                        throw 'Unexpected command';
                      }
                    };
                ''')
                page.goto('http://127.0.0.1:4173', wait_until='networkidle')
                page.evaluate('document.fonts.ready')
                expect(page.get_by_role('heading', name='Connection test')).to_be_visible()
                expect(page.locator('.connection')).to_have_text('Connected', timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(0)
                expect(page.get_by_text('No test downloads yet')).to_be_visible()
                expect(page.get_by_text('This desktop has no tray.', exact=False)).to_be_visible()
                expect(page.get_by_role('heading', name='Browser connections')).to_be_visible()
                expect(page.locator('.browser-connection-summary')).to_have_text('Registered for Chrome and Firefox')
                page.get_by_role('button', name='Disconnect browsers', exact=True).click()
                expect(page.locator('.browser-connection-summary')).to_have_text('Disconnected')
                expect(page.locator('.connection')).to_have_text('Connected')
                expect(page.get_by_role('button', name='Download test audio')).to_be_enabled()
                page.reload(wait_until='networkidle')
                expect(page.locator('.browser-connection-summary')).to_have_text('Disconnected')
                page.evaluate('window.__registrationFailure = true')
                page.get_by_role('button', name='Connect browsers', exact=True).click()
                expect(page.get_by_role('alert')).to_contain_text('another installation')
                expect(page.locator('.browser-connection-summary')).to_have_text('Disconnected')
                calls = page.evaluate('window.__statusCalls')
                page.wait_for_function('window.__statusCalls > ' + str(calls))
                assert page.evaluate('window.__connectionActions') == 1, 'Connection mutations must not retry automatically'
                page.evaluate('window.__registrationFailure = false')
                page.get_by_role('button', name='Connect browsers', exact=True).click()
                expect(page.locator('.browser-connection-summary')).to_have_text('Registered for Chrome and Firefox')
                expect(page.get_by_role('alert')).to_have_count(0)
                help = page.locator('.browser-setup')
                summary = help.locator('summary')
                summary.focus()
                summary.press('Enter')
                expect(help.get_by_text('temporary VidGrab add-on', exact=False)).to_be_visible()
                steps = help.locator('li').all_text_contents()
                restart = next(index for index, text in enumerate(steps) if 'Fully quit Firefox' in text)
                load = next(index for index, text in enumerate(steps) if 'temporary VidGrab add-on' in text)
                assert restart < load, 'The temporary add-on must be loaded after the Firefox restart'
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Help overflow at {width}px'
                page.screenshot(path=str(arguments.screenshots / f'{width}-setup.png'), full_page=True)
                summary.press('Space')
                expect(help).not_to_have_attribute('open', '')
                page.screenshot(path=str(arguments.screenshots / f'{width}-empty.png'), full_page=True)
                page.get_by_label('Fixture URL').fill('https://example.com/fixture.wav')
                page.get_by_role('button', name='Download test audio').click()
                expect(page.get_by_role('alert')).to_contain_text('loopback')
                page.evaluate('window.__statusFailures = 1')
                expect(page.locator('.connection')).to_have_text('Reconnecting', timeout=5000)
                expect(page.get_by_role('button', name='Download test audio')).to_be_disabled()
                expect(page.locator('.connection')).to_have_text('Connected', timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(1)
                expect(page.get_by_role('alert')).to_contain_text('loopback')
                page.get_by_label('Fixture URL').fill('http://127.0.0.1:8080/fixture.wav')
                page.get_by_role('button', name='Download test audio').click()
                expect(page.get_by_text('Finished', exact=True)).to_be_visible(timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(0)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Overflow at {width}px'
                page.screenshot(path=str(arguments.screenshots / f'{width}-complete.png'), full_page=True)
                page.close()
                print(f'{width}x{height}: browser controls, help, recovery, download completion and overflow passed')
        finally:
            browser.close()
    assert not errors, errors
    assert not external, external


if __name__ == '__main__':
    main()
