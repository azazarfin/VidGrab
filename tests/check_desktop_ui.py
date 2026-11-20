"""Opt-in desktop render check; Tauri commands are mocked, network is local."""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.client_ui_support import ClientFixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--chrome', type=Path, default=Path('/opt/google/chrome/chrome'),
                        help='Explicit Chrome executable for the selected validation target')
    arguments = parser.parse_args()
    assert Path('desktop/dist/index.html').is_file(), 'Build the desktop renderer before checking it'
    from playwright.sync_api import expect, sync_playwright
    arguments.screenshots.mkdir(parents=True, exist_ok=False)
    errors = []
    external = []
    fixtures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(arguments.chrome), headless=True)
        try:
            for width, height in ((1280, 900), (620, 600), (390, 720)):
                page = browser.new_page(viewport={'width': width, 'height': height})
                fixture = ClientFixture(arguments.screenshots / f'{width}-runtime')
                fixtures.append(fixture)
                fixture.install(page.context)
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
                      application: JSON.parse(sessionStorage.getItem('application') ||
                        '{"kind":"appimage","installed":false,"app":"/tmp/VidGrab.AppImage","shortcut":null}'),
                      browser_connection: JSON.parse(sessionStorage.getItem('connection') ||
                        '{"enabled":true,"bridge_ready":true,"chrome":"registered","firefox":"registered"}')};
                    window.__connectionActions = 0;
                    window.__statusFailures = 1;
                    window.__statusCalls = 0;
                    window.__installActions = 0;
                    window.__removeActions = 0;
                    window.__TAURI_INTERNALS__ = {
                      invoke: async (command, arguments) => {
                        if (command === 'protocol_request') return window.__vidgrabControl({type:'fixture.invoke', command, arguments});
                        if (command === 'companion_status') {
                          window.__statusCalls++;
                          if (window.__statusFailures-- > 0) throw 'Temporary engine connection loss';
                          return structuredClone(window.__fixture);
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
                        if (command === 'install_appimage') {
                          window.__installActions++;
                          if (window.__installFailure) throw 'The Applications shortcut belongs to another installation';
                          window.__fixture.application = {kind:'appimage', installed:true,
                            app:'/tmp/data/vidgrab-p02/application/VidGrab.AppImage', shortcut:'/tmp/data/applications/com.vidgrab.P02.AppImage.desktop'};
                          sessionStorage.setItem('application', JSON.stringify(window.__fixture.application));
                          return structuredClone(window.__fixture.application);
                        }
                        if (command === 'remove_application') {
                          window.__removeActions++;
                          if (window.__fixture.jobs.some(job => !['complete','failed'].includes(job.status))) {
                            throw 'Finish active downloads before removing VidGrab';
                          }
                          window.__fixture.application.installed = false;
                          sessionStorage.setItem('application', JSON.stringify(window.__fixture.application));
                          return structuredClone(window.__fixture.application);
                        }
                        if (command === 'get_desktop_preferences' || command === 'set_desktop_preferences'
                            || command === 'diagnostics_report' || command === 'restart_engine'
                            || command.startsWith('plugin:')) {
                          return window.__vidgrabControl({type:'fixture.invoke', command, arguments});
                        }
                        if (command === 'quit_companion') return;
                        throw 'Unexpected command: ' + command;
                      },
                      transformCallback: () => 1,
                    };
                ''')
                page.goto('http://127.0.0.1:4173', wait_until='networkidle')
                page.evaluate('document.fonts.ready')
                expect(page.locator('.toolbar .brand')).to_contain_text('VidGrab')
                expect(page.get_by_role('heading', name='Queue', level=2)).to_be_visible()
                expect(page.locator('.connection')).to_have_text('Connected', timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(0)
                expect(page.get_by_text('No downloads in this view')).to_be_visible()
                expect(page.get_by_text('This desktop has no tray.', exact=False)).to_be_visible()
                page.locator('[data-desktop-tab="browser"]').click()
                installation = page.locator('.application-setup')
                installation.locator('summary').click(timeout=3000)
                page.evaluate('window.__installFailure = true')
                page.get_by_role('button', name='Install AppImage', exact=True).click()
                expect(page.get_by_role('alert')).to_contain_text('another installation')
                calls = page.evaluate('window.__statusCalls')
                page.wait_for_function('window.__statusCalls > ' + str(calls))
                assert page.evaluate('window.__installActions') == 1, 'Install actions must not retry automatically'
                page.evaluate('window.__installFailure = false')
                page.get_by_role('button', name='Install AppImage', exact=True).click()
                expect(installation.get_by_text('Installed in Applications', exact=True)).to_be_visible()
                page.get_by_role('button', name='Remove installed copy…', exact=True).click()
                expect(page.get_by_role('dialog')).to_be_visible()
                expect(page.get_by_role('button', name='Keep VidGrab', exact=True)).to_be_focused()
                page.keyboard.press('Escape')
                expect(page.get_by_role('dialog')).not_to_be_visible()
                assert page.evaluate('window.__removeActions') == 0
                page.screenshot(path=str(arguments.screenshots / f'{width}-installation.png'), full_page=True)
                installation.locator('summary').click()
                expect(page.get_by_role('heading', name='Browser connections')).to_be_visible()
                expect(page.locator('.browser-connection-summary')).to_have_text('Registered for Chrome and Firefox')
                page.get_by_role('button', name='Disconnect browsers', exact=True).click()
                expect(page.locator('.browser-connection-summary')).to_have_text('Disconnected')
                expect(page.locator('.connection')).to_have_text('Connected')
                page.locator('[data-desktop-tab="queue"]').click()
                expect(page.get_by_role('button', name='Download', exact=True)).to_be_enabled()
                page.reload(wait_until='networkidle')
                page.locator('[data-desktop-tab="browser"]').click()
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
                page.locator('[data-desktop-tab="queue"]').click()
                page.screenshot(path=str(arguments.screenshots / f'{width}-empty.png'), full_page=True)
                page.get_by_label('Media URL', exact=True).fill('file:///fixture.wav')
                page.get_by_role('button', name='Download', exact=True).click()
                expect(page.get_by_role('alert')).to_contain_text('HTTP or HTTPS')
                page.evaluate('window.__statusFailures = 1')
                expect(page.locator('.connection')).to_have_text('Reconnecting', timeout=5000)
                expect(page.get_by_role('button', name='Download', exact=True)).to_be_disabled()
                expect(page.locator('.connection')).to_have_text('Connected', timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(1)
                expect(page.get_by_role('alert')).to_contain_text('HTTP or HTTPS')
                page.get_by_label('Media URL', exact=True).fill('http://127.0.0.1:8080/fixture.wav')
                page.get_by_role('button', name='Options', exact=True).click()
                page.get_by_label('Audio only', exact=True).check()
                page.get_by_role('button', name='Download', exact=True).click()
                expect(page.get_by_role('status').filter(has_text='queued')).to_be_visible(timeout=5000)
                job = fixture.jobs()[0]
                fixture.set_status(job['id'], 'complete')
                page.locator('.queue .pill', has_text='All loaded').click()
                expect(page.get_by_text('Finished', exact=True)).to_be_visible(timeout=5000)
                expect(page.get_by_role('alert')).to_have_count(0)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Overflow at {width}px'
                page.screenshot(path=str(arguments.screenshots / f'{width}-complete.png'), full_page=True)
                page.locator('[data-desktop-tab="browser"]').click()
                installation.locator('summary').click()
                active = fixture.request('downloads.enqueue', {'url': 'http://127.0.0.1:8080/active.wav'}, key='active')['job_id']
                expect(page.get_by_role('button', name='Remove installed copy…', exact=True)).to_be_disabled(timeout=5000)
                expect(installation.get_by_text('Finish active downloads before removing VidGrab.', exact=True)).to_be_visible()
                fixture.set_status(active, 'complete')
                calls = page.evaluate("window.__fixture.downloads_active = true; window.__statusCalls")
                page.wait_for_function('window.__statusCalls > ' + str(calls + 1))
                expect(page.get_by_role('button', name='Remove installed copy…', exact=True)).to_be_disabled(timeout=5000)
                page.evaluate("window.__fixture.downloads_active = false")
                expect(page.get_by_role('button', name='Remove installed copy…', exact=True)).to_be_enabled(timeout=5000)
                page.get_by_role('button', name='Remove installed copy…', exact=True).click()
                expect(page.get_by_role('dialog')).to_be_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.screenshot(path=str(arguments.screenshots / f'{width}-remove-confirmation.png'), full_page=True)
                page.get_by_role('button', name='Remove and quit', exact=True).click()
                expect(page.get_by_role('button', name='Install AppImage', exact=True)).to_be_visible(timeout=5000)
                assert page.evaluate('window.__removeActions') == 1
                page.close()
                print(f'{width}x{height}: browser controls, help, recovery, download completion and overflow passed')
        finally:
            browser.close()
            for fixture in fixtures:
                fixture.write_report(fixture.root / 'fixture-report.json')
                fixture.close()
    assert not errors, errors
    assert not external, external


if __name__ == '__main__':
    main()
