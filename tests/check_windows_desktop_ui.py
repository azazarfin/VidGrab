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
                    window.__fixture = {state:'ready',tray_available:true,jobs:[],application:{kind:'windows',installed:true,app:'C:\\\\Users\\\\Preview বাংলা\\\\AppData\\\\Local\\\\VidGrab\\\\VidGrab.exe'},browser_connection:{enabled:true,bridge_ready:true,chrome:'registered',firefox:'registered'}};
                    window.__TAURI_INTERNALS__ = {
                      invoke: async (name, args) => {
                        if (name === 'companion_status') return structuredClone(window.__fixture);
                        if (name === 'protocol_request') {
                          const req = args.request;
                          const settings = {revision:1,values:{download_dir:'C:\\\\Users\\\\Preview বাংলা\\\\Downloads',max_concurrent:3,default_format:'bestvideo+bestaudio/best',default_quality:'best',filename_template:'%(title)s.%(ext)s',speed_limit:0,auto_update_ytdlp:false,sponsorblock_enabled:false,sponsorblock_categories:['sponsor'],sponsorblock_mode:'remove',postprocess_codec:'none',postprocess_normalize_audio:false,custom_ytdlp_args:'',download_threads:4,gpu_accelerated_conversion:false,max_retries:3,transliterate_bangla_filenames:true}};
                          const counts = {total:0,unfinished:0,paused:0,scheduled:0,history:0};
                          if (req.operation === 'capabilities.get') {
                            return {protocol_version:1,request_id:req.request_id,ok:true,data:{protocol_min:1,protocol_max:1,max_message_bytes:65536,max_payload_bytes:64512,fixture_only:false,instance_id:'win-mock',paired_loopback:false,features:['downloads.source','downloads.priority','downloads.schedule','downloads.pause_resume','downloads.delete_history','diagnostics.get'],component_versions:{}}};
                          }
                          if (req.operation === 'events.read') {
                            return {protocol_version:1,request_id:req.request_id,ok:true,data:{instance_id:'win-mock',cursor:1,resnapshot:true,has_more:false,events:[],snapshot:{jobs:[],next_after:null,counts,instance_id:'win-mock',cursor:1,settings}}};
                          }
                          if (req.operation === 'settings.get') {
                            return {protocol_version:1,request_id:req.request_id,ok:true,data:settings};
                          }
                          if (req.operation === 'downloads.list') {
                            return {protocol_version:1,request_id:req.request_id,ok:true,data:{jobs:[],next_after:null,counts}};
                          }
                          return {protocol_version:1,request_id:req.request_id,ok:true,data:{}};
                        }
                        if (name === 'get_desktop_preferences') return {preferences_version:1,start_at_login:false,close_to_tray:true,start_minimized:false,onboarding_completed:true,tray_available:true,packaged:true,autostart_state:'missing'};
                        if (name === 'update_status') return {app_version:'0.1.0',engine_bundle_version:'0.1.0',protocol_version:1,platform:'windows-x64',channel:'stable',rollback_available:false,previous_version:'',pending_update:null,rollback_notice:null,last_check:null,backups_count:0,backups:[]};
                        if (name === 'check_for_updates') return {app_version:'0.1.0',engine_bundle_version:'0.1.0',protocol_version:1,platform:'windows-x64',channel:'stable',rollback_available:false,previous_version:'',pending_update:null,rollback_notice:null,last_check:{checked_at:1700000000,status:'offline',available_version:null,channel:'stable',notes:null,message:'Update server unreachable'},backups_count:0,backups:[]};
                        if (name === 'apply_update' || name === 'rollback_update') return {app_version:'0.1.0',engine_bundle_version:'0.1.0',protocol_version:1,platform:'windows-x64',channel:'stable',rollback_available:false,previous_version:'',pending_update:null,rollback_notice:null,last_check:null,backups_count:1,backups:[]};
                        if (name === 'pick_and_import_legacy_config') return null;
                        if (name === 'diagnostics_report') return {supervisor:structuredClone(window.__fixture),engine:{error:'offline'},desktop_preferences:{preferences_version:1,start_at_login:false,close_to_tray:true,start_minimized:false,onboarding_completed:true,tray_available:true,packaged:true,autostart_state:'missing'},updates:{app_version:'0.1.0',engine_bundle_version:'0.1.0',protocol_version:1,platform:'windows-x64',channel:'stable',rollback_available:false,backups_count:0}};
                        if (name.startsWith('plugin:')) return 1;
                        window.__calls.push(name);
                        if (name === 'remove_application') return {...window.__fixture.application,installed:false};
                        return {};
                      },
                      transformCallback: () => 1,
                    };
                """)
                page.goto('http://127.0.0.1:4173', wait_until='networkidle')
                expect(page.locator('.connection')).to_have_text('Connected')
                page.locator('[data-desktop-tab="browser"]').click()
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
