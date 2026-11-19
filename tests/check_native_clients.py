"""Opt-in compiled WebKit/WebView2 and installed Chrome/Firefox v1 acceptance.

Uses actual native transports and executing frozen workers with loopback media.
Two explicit delivery faults discard/filter an already returned native response;
neither replaces the transport or writes backend state. Retains owned test files.
"""

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import traceback
from uuid import uuid4
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_client_support import Native, Page, WindowsBrowserTask, chrome_extension_worker, free_port, media_fixture, wait, windows_registrations

ROOT = Path(__file__).resolve().parents[1]
CHROME_ID = 'ikjpekhbbaehdhigakbahlhafaojbjpm'
FIREFOX_ID = 'vidgrab@azaz.local'
FIREFOX_UUID = 'a853416a-1e23-4d8d-945d-b9f74f8649e4'
HOST = 'com.vidgrab.companion'

INSTRUMENT = r"""
const scope = this, desktopTransport = arguments[2];
const state = scope.__nativeAcceptance = {trace:[],calls:[],dropped:[],lose:null,strip:false,desktopTransport};
function delivered(request, response) {
  state.calls.push({command:desktopTransport ? 'protocol_request' : null,
    operation:request?.operation || null,ok:response?.ok ?? null,
    resultKeys:response && typeof response==='object' ? Object.keys(response) : []});
  if (!request?.operation) return response;
  state.trace.push({operation:request.operation,key:request.idempotency_key || null});
  if (request.operation === state.lose && response?.ok) {
    state.lose=null;
    state.dropped.push({operation:request.operation,key:request.idempotency_key,
      job_id:response.data.job_id || null,revision:response.data.revision ?? null});
    return {protocol_version:1,request_id:request.request_id,ok:false,error:{
      code:'REQUEST_OUTCOME_UNKNOWN',message:'The selected native reply was lost',
      retryable:false,retry_at:null,request_id:request.request_id,provenance:'local',details:{}}};
  }
  if (state.strip && request.operation === 'capabilities.get' && response?.ok) {
    return {...response,data:{...response.data,features:response.data.features.filter(f => f !== 'downloads.source')}};
  }
  return response;
}
if (desktopTransport) {
  // Tauri's invoke and IPC methods are readonly. Observe unchanged serialization
  // and its mutable callback collection; filter only an actual returned result.
  const internals = scope.__TAURI_INTERNALS__, requests = new Map();
  const descriptor = Object.getOwnPropertyDescriptor(internals, 'invoke');
  state.invokeDescriptor = {writable:descriptor.writable,configurable:descriptor.configurable};
  const stringify = scope.JSON.stringify;
  Object.defineProperty(scope.JSON, 'stringify', {configurable:true,writable:true,value:function(value,...args) {
    const request = value?.request || value?.payload?.request;
    if (request?.protocol_version === 1 && typeof request.request_id === 'string') {
      requests.set(request.request_id, {request_id:request.request_id,operation:request.operation,
        idempotency_key:request.idempotency_key || null});
      if (requests.size > 512) requests.delete(requests.keys().next().value);
    }
    return stringify.call(this,value,...args);
  }});
  const callbacks = internals.callbacks, set = callbacks.set.bind(callbacks);
  Object.defineProperty(callbacks, 'set', {configurable:true,writable:true,value:function(identifier,callback) {
    return set(identifier,response => {
      const request = requests.get(response?.request_id);
      if (request) {
        requests.delete(response.request_id);
        response = delivered(request,response);
      }
      return callback(response);
    });
  }});
} else {
  const target = scope[arguments[0]], method = arguments[1], original = target[method].bind(target);
  const wrapped = async (...args) => delivered(args[0],await original(...args));
  target[method] = wrapped;
  if (target[method] !== wrapped) throw new Error('The selected browser delivery hook is readonly');
}
return true;
"""


class Browser:
    def __init__(self, kind, args, environment, root, playwright, manifests):
        self.kind, self.context, self.driver = kind, None, None
        self.permission_events = getattr(args, 'permission_events', False)
        self.chrome_browser, self.chrome_task = None, None
        self.pages = []
        self.network_guards = []
        self.sandbox = None
        self.root = root / kind
        self.root.mkdir()
        if kind == 'chrome':
            profile = self.root / 'profile'
            if os.name != 'nt':
                directory = profile / 'NativeMessagingHosts'
                directory.mkdir(parents=True)
                shutil.copy2(manifests[r'Google\Chrome'], directory / (HOST+'.json'))
            if os.name=='nt' and getattr(args,'chrome_launcher',None):
                port=free_port()
                self.chrome_task=WindowsBrowserTask(args.chrome_launcher,self.root,args.chrome,profile,port)
                import urllib.request
                def endpoint():
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',timeout=1) as response:
                            return json.load(response)
                    except OSError:
                        return None
                wait(endpoint,'owned installed Chrome debugging endpoint')
                self.chrome_browser=playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                assert len(self.chrome_browser.contexts)==1
                self.context=self.chrome_browser.contexts[0]
                processes=self.chrome_browser.new_browser_cdp_session().send('SystemInfo.getProcessInfo')['processInfo']
                assert any(p['type']=='browser' and int(p['id'])==self.chrome_task.audit['pid'] for p in processes)
            else:
                self.context = playwright.chromium.launch_persistent_context(str(profile),
                    executable_path=str(args.chrome), headless=not getattr(args, 'permission_events', False), env=environment, chromium_sandbox=True,
                    ignore_default_args=['--disable-extensions'],
                    args=['--enable-unsafe-extension-debugging','--disable-background-networking',
                        '--disable-component-update','--no-first-run','--no-default-browser-check']
                        + (['--force-renderer-accessibility'] if getattr(args, 'permission_events', False) else []))
            if not getattr(args, 'permission_events', False):
                self.context.route('**/*', lambda route: route.abort() if route.request.url.startswith(('http:', 'https:'))
                    and not route.request.url.startswith('http://127.0.0.1:') else route.continue_())
            cdp = self.context.browser.new_browser_cdp_session()
            self.version = cdp.send('Browser.getVersion')
            developer_mode = None
            if self.permission_events:
                manager = self.context.new_page()
                manager.goto('chrome://extensions/')
                toggle = manager.locator('extensions-toolbar #devMode')
                before = toggle.evaluate('control => control.checked') is True
                if not before:
                    toggle.click()
                wait(lambda: toggle.evaluate('control => control.checked') is True, 'actual private Chrome Developer mode control')
                developer_mode = {'before': before, 'after': True}
                manager.screenshot(path=str(self.root / 'developer-mode.png'))
                manager.close()
            identifier = cdp.send('Extensions.loadUnpacked', {'path':str(ROOT/'extension')})['id']
            assert identifier == CHROME_ID, identifier
            self.worker = chrome_extension_worker(self.context, identifier)
            self.background_diagnostics = {
                'selected_url': self.worker.url,
                'workers': [worker.url for worker in self.context.service_workers],
                'targets': cdp.send('Target.getTargets')['targetInfos'],
                'developer_mode': developer_mode,
            }
            self.bootstrap=[]
            def background_ready():
                observed=self.worker.evaluate('({api:Boolean(self.vidGrabAPI),native:Boolean(self.vidGrabNative),client:Boolean(self.vidGrabCompanion),url:self.location.href,kind:self.constructor.name,imports:typeof importScripts,extensionId:chrome.runtime?.id})')
                self.bootstrap.append(observed)
                return observed['api'] and observed['native'] and observed['client']
            wait(background_ready,'actual installed extension background bootstrap')
            if os.name != 'nt':
                from tests.check_installed_browsers import chrome_renderer_sandbox
                self.sandbox = chrome_renderer_sandbox(cdp)
            self.base = f'chrome-extension://{CHROME_ID}/'
        else:
            from tests.native_support import WebDriver
            extension = self.root / 'extension'
            shutil.copytree(ROOT/'extension', extension)
            shutil.copy2(extension/'manifest.firefox.json', extension/'manifest.json')
            archive = self.root/'extension.xpi'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
                for path in extension.rglob('*'):
                    if path.is_file():
                        out.write(path,path.relative_to(extension))
            self.driver = WebDriver(args.geckodriver, environment, self.root/'gecko.log', arguments=('--allow-system-access',))
            self.version = self.driver.start({'browserName':'firefox','moz:firefoxOptions':{
                'binary':str(args.firefox),'args':[] if getattr(args, 'permission_events', False) else ['-headless'], 'prefs':{
                    'extensions.webextensions.uuids':json.dumps({FIREFOX_ID:FIREFOX_UUID}),
                    'app.update.auto':False,'app.update.enabled':False,'extensions.update.enabled':False,
                    'datareporting.policy.dataSubmissionEnabled':False,
                    'toolkit.telemetry.enabled':False,'browser.shell.checkDefaultBrowser':False}}})
            identifier = self.driver.call('/moz/addon/install', {'path':str(archive),'temporary':True})
            assert identifier == FIREFOX_ID, identifier
            self.base = f'moz-extension://{FIREFOX_UUID}/'
        self.popup = self.open('popup/popup.html')
        self.popup.has_text('#connection-status','Connected')
        self.options = self.open('options/options.html')
        self.options.has_text('#status-message','Settings loaded')
        assert self.background('return Boolean(this.vidGrabCompanion && this.vidGrabAPI && this.vidGrabNative)')
        self.background(INSTRUMENT,'vidGrabNative','request',False)
        if kind=='firefox' and os.name!='nt':
            from tests.check_installed_browsers import firefox_content_sandbox
            self.popup.activate()
            self.sandbox=firefox_content_sandbox(self.driver)

    def open(self, path):
        if self.context:
            page = Page(page=self.context.new_page())
        else:
            handle = self.driver.call('/window/new',{'type':'tab'})['handle']
            page = Page(driver=self.driver,handle=handle)
        if self.permission_events and page.page:
            guard = self.context.new_cdp_session(page.page)
            guard.send('Network.enable')
            guard.send('Network.setBlockedURLs', {'urls': ['https://fonts.googleapis.com/*', 'https://fonts.gstatic.com/*']})
            self.network_guards.append(guard)
            page.page.goto(self.base+path, wait_until='domcontentloaded')
        else:
            page.goto(self.base+path)
        self.pages.append(page)
        return page

    def background(self, body, *arguments):
        if self.context:
            return self.worker.evaluate('args => (function(){'+body+'}).apply(self,args)',list(arguments))
        reply = self.popup.asynchronous("const args=arguments[0],done=arguments[arguments.length-1];"
            "browser.runtime.getBackgroundPage().then(bg => (function(){"+body+"}).apply(bg,args))"
            ".then(value=>done({ok:true,value}),error=>done({ok:false,message:error.message}));",list(arguments))
        assert reply['ok'],reply
        return reply.get('value')

    def state(self):
        return self.background('return this.__nativeAcceptance')

    def permissions(self, url):
        return self.background('return this.vidGrabAPI.getPermissions(arguments[0])',url)

    def close(self):
        if self.driver:
            self.driver.close()
        if self.chrome_task:
            try:
                if self.chrome_browser:
                    try:
                        self.chrome_browser.new_browser_cdp_session().send('Browser.close')
                    except Exception:
                        pass
            finally:
                self.chrome_task.close()
                if self.chrome_browser:
                    self.chrome_browser.close()
        elif self.context:
            self.context.close()


class Desktop:
    def __init__(self, args, environment, native, playwright):
        self.driver, self.browser, self.process = None, None, None
        self.native, self.environment = native, environment
        if os.name == 'nt':
            port = free_port()
            env = {**environment,'WEBVIEW2_USER_DATA_FOLDER':str(args.home/'webview-profile'),
                'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS':f'--remote-debugging-port={port} --disable-background-networking'}
            self.log = (args.home/'desktop.log').open('wb')
            self.process = subprocess.Popen([str(args.app),'--config',str(native.path)],env=env,
                stdout=self.log,stderr=subprocess.STDOUT)
            import urllib.request
            def endpoint():
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',timeout=1) as response:
                        return json.load(response)
                except OSError:
                    return None
            self.webview = wait(endpoint,'actual WebView2 debugging endpoint')
            self.browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            self.webview['targets']=self.browser.new_browser_cdp_session().send('Target.getTargets')
            assert len(self.browser.contexts) == 1, 'Expected the owned WebView2 context'
            context = self.browser.contexts[0]
            page = context.pages[0] if context.pages else context.wait_for_event('page',timeout=25000)
            # Playwright's synchronous event loop must run while navigation is
            # pending. Sleeping around Page.url leaves its initial value stale.
            page.wait_for_url(re.compile(r'^https?://tauri\.localhost/'),timeout=25000)
            self.renderer_errors = []
            page.on('pageerror',lambda error:self.renderer_errors.append(str(error)))
            page.on('console',lambda message:self.renderer_errors.append(message.text)
                if message.type=='error' else None)
            self.page = Page(page=page)
        else:
            from tests.native_support import WebDriver
            self.driver = WebDriver('/usr/bin/WebKitWebDriver',
                {**environment,'TAURI_WEBVIEW_AUTOMATION':'true'},args.home/'webkit.log')
            self.webview = self.driver.start({'webkitgtk:browserOptions':{
                'binary':str(args.app),'args':['--config',str(native.path)]}})
            self.page = Page(driver=self.driver)
        self.page.has_text('.connection','Connected')
        self.ready = native.outer('status')
        self.page.script(INSTRUMENT,'__TAURI_INTERNALS__','invoke',True)
        if os.name != 'nt':
            from tests.check_wayland_desktop import Desktop as GNOME
            self.window = wait(lambda: GNOME().window(self.ready['supervisor_pid']),'actual GNOME Wayland window')
            assert self.window['wayland'] and not self.window['hidden'], self.window
        else:
            import ctypes
            from ctypes import wintypes
            user=ctypes.WinDLL('user32',use_last_error=True)
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
            user.EnumWindows.argtypes=(callback_type,wintypes.LPARAM)
            user.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
            user.IsWindowVisible.argtypes=(wintypes.HWND,)
            user.GetWindowTextW.argtypes=(wintypes.HWND,wintypes.LPWSTR,ctypes.c_int)
            kernel.ProcessIdToSessionId.argtypes=(wintypes.DWORD,ctypes.POINTER(wintypes.DWORD))
            def window():
                matches=[]
                @callback_type
                def observe(hwnd,_):
                    pid=wintypes.DWORD()
                    user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
                    if pid.value==self.ready['supervisor_pid'] and user.IsWindowVisible(hwnd):
                        title=ctypes.create_unicode_buffer(256)
                        user.GetWindowTextW(hwnd,title,len(title))
                        if title.value=='VidGrab':
                            matches.append({'pid':pid.value,'hwnd':int(hwnd),'title':title.value,'visible':True})
                    return True
                user.EnumWindows(observe,0)
                return matches[0] if matches else None
            self.window=wait(window,'visible actual Win32 VidGrab window')
            session=wintypes.DWORD()
            assert kernel.ProcessIdToSessionId(self.ready['supervisor_pid'],ctypes.byref(session))
            assert session.value==kernel.WTSGetActiveConsoleSessionId(),session.value
            self.window.update(session=session.value,engine='WebView2',page_url=self.page.script('return location.href'))

    def state(self):
        return self.page.script('return this.__nativeAcceptance')

    def close(self):
        if os.name != 'nt':
            from tests.native_support import stop_owner
            stop_owner(self.native.config)
        else:
            try:
                self.page.button('Quit VidGrab')
            except Exception:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
            if self.process:
                self.process.wait(timeout=10)
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if getattr(self,'log',None):
                self.log.close()
        if self.driver:
            self.driver.close()


def lost_reply(surface, page, native, url, start, check):
    state = surface.state()
    before = len(native.jobs())
    if isinstance(surface, Browser):
        surface.background("this.__nativeAcceptance.lose='downloads.enqueue'")
    else:
        page.script("this.__nativeAcceptance.lose='downloads.enqueue'")
    start(url)
    try:
        page.has_text('body','may have')
    except AssertionError as error:
        raise AssertionError({'message':str(error),'delivery_state':surface.state()}) from error
    dropped = wait(lambda: surface.state()['dropped'],'accepted native reply was deliberately discarded')[-1]
    assert native.job(url)['id'] == dropped['job_id']
    reads = sum(e['operation']=='events.read' for e in surface.state()['trace'])
    wait(lambda: sum(e['operation']=='events.read' for e in surface.state()['trace']) >= reads+2,
        'two native refreshes while the mutation remains unresolved')
    assert len(native.jobs()) == before+1
    attempted = [e for e in surface.state()['trace'] if e['operation']=='downloads.enqueue' and e['key']==dropped['key']]
    assert len(attempted)==1, attempted
    check()
    wait(lambda: not page.visible('#check-request-btn') if isinstance(surface,Browser)
        else not page.script("return [...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='Check request')"),
        'explicit Check request acknowledges the original receipt')
    attempted = [e for e in surface.state()['trace'] if e['operation']=='downloads.enqueue' and e['key']==dropped['key']]
    assert len(attempted)==2 and len(native.jobs())==before+1,attempted
    return dropped


def reconnect(native):
    jobs=wait(lambda: jobs if (jobs:=native.jobs()) and all(j['status']=='complete' for j in jobs)
        else None,'downloads finish before engine recovery',40)
    before=native.outer('status')
    if os.name=='nt':
        from tests.check_windows_native import ProcessHandle
        handle=ProcessHandle(before['engine_pid'])
        try:
            handle.kill()
        finally:
            handle.close()
    else:
        os.kill(before['engine_pid'],signal.SIGKILL)
    observations=[]
    def recovered_status():
        current=native.outer('status',allow_unavailable=True)
        observations.append({'error':'ENGINE_UNAVAILABLE'} if current is None else
            {'state':current.get('state'),'engine_generation':current.get('engine_generation')})
        return current if current and current.get('state')=='ready' \
            and current.get('engine_generation',0)>before['engine_generation'] else None
    recovered=wait(recovered_status,'native engine recovery')
    assert recovered['supervisor_pid']==before['supervisor_pid']
    assert {j['id'] for j in native.jobs()}=={j['id'] for j in jobs}
    recovered['status_observations']=observations
    return recovered


def browser_journey(browser, desktop, native, base, fixture, screenshots):
    kind, options, popup, page = browser.kind, browser.options, browser.popup, desktop.page
    initial=native.settings()
    changed_format='best' if initial['values']['default_format']!='best' else 'bestaudio/best'
    assert options.value('#input-download-dir')==initial['values']['download_dir']
    options.select('#input-max-concurrent','6')
    options.click('#btn-save-settings')
    options.has_text('#status-message','saved')
    assert native.settings()['values']['max_concurrent']==6
    page.click('.download-defaults > summary') if not page.visible('#default-format') else None
    wait(lambda:page.value('#concurrent-downloads')=='6','native desktop receives browser defaults event')
    page.script('''this.__nativeDefaultsInput = [];
        document.querySelector('#default-format').addEventListener('input', event =>
            this.__nativeDefaultsInput.push({type:event.type,trusted:event.isTrusted,value:event.target.value}));''')
    page.fill('#default-format',changed_format)
    draft = page.script('''return {value:document.querySelector('#default-format').value,
        save_disabled:document.querySelector('.defaults-form button.primary').disabled,
        input:this.__nativeDefaultsInput};''')
    (screenshots/(kind+'-desktop-draft.json')).write_text(json.dumps(draft,indent=2)+'\n')
    assert draft['value'] == changed_format and not draft['save_disabled'], draft
    options.select('#input-max-concurrent','7')
    options.click('#btn-save-settings')
    options.has_text('#status-message','saved')
    committed = wait(lambda:(value if (value:=native.settings())['values']['max_concurrent']==7 else None),
        'the second browser defaults change commits before desktop conflict review')
    (screenshots/(kind+'-desktop-commit.json')).write_text(json.dumps({'revision':committed['revision'],
        'default_format':committed['values']['default_format'],'draft':page.value('#default-format')},indent=2)+'\n')
    page.has_text('body','Your edits are kept')
    assert page.value('#default-format')==changed_format
    page.button('Save defaults')
    page.has_text('body','changed')
    page.button('Reload defaults')
    wait(lambda:page.value('#concurrent-downloads')=='7','desktop conflict reload')
    options.select('#input-max-concurrent','8')
    page.fill('#default-format',changed_format)
    page.button('Save defaults')
    page.has_text('body','Defaults saved')
    options.click('#btn-save-settings')
    options.has_text('#status-message','changed')
    assert options.value('#input-max-concurrent')=='8'
    options.click('#btn-reload-settings')
    wait(lambda:options.value('#input-max-concurrent')=='7','browser dirty draft conflict reload')

    def enqueue(url):
        popup.fill('#video-url-input',url)
        popup.click('#download-btn')
        wait(lambda:popup.visible('#format-modal') and not popup.script("return document.querySelector('#modal-start-btn').disabled"),'actual native probe dialog')
        assert popup.value('#modal-download-dir-input')==native.settings()['values']['download_dir']
        popup.click('#modal-start-btn')

    ordinary=base+'/'+kind+'-ordinary.wav'
    enqueue(ordinary)
    complete=wait(lambda:native.job(ordinary,'complete'),'installed popup executes frozen media',40)
    page.has_text('body','Finished')
    assert 'বাংলা' in complete['file_path'] and Path(complete['file_path']).is_file()
    popup.click('[data-tab="history"]')
    wait(lambda:popup.visible('#task-card-'+complete['id']),'same completed job in installed popup')

    protected=base+'/'+kind+'-protected.wav'
    fixture['release'].clear()
    page.fill('#media-url',protected)
    page.click('.download-form button.primary')
    pending=wait(lambda:native.job(protected,'waiting_for_auth'),'actual HTTP 403 enters fresh-auth wait',40)
    page.has_text('body','Fresh browser context needed')
    popup.click('[data-tab="queue"]')
    popup.button('Share fresh cookies')
    popup.has_text('#client-status','cookie forwarding')
    assert native.job(protected)['status']=='waiting_for_auth'
    permission=browser.permissions(protected)
    assert permission['native_granted'] and permission['cookies_granted'] and permission['site_granted']
    assert permission['cookie_forwarding_enabled'] is False
    options.click('#btn-tab-advanced')
    options.has_text('#permission-status','Cookies: granted')
    values=native.settings()['values']
    options.click('label[for="chk-cookie-forwarding"]')
    options.click('#btn-save-settings')
    options.has_text('#status-message','saved')
    assert native.settings()['values']==values,'Browser preference entered SQLite settings'
    assert browser.permissions(protected)['cookie_forwarding_enabled'] is True
    login=browser.open('popup/popup.html')
    login.goto(base+'/login')
    popup.button('Share fresh cookies')
    active=wait(lambda:native.job(protected,'downloading'),'fresh browser cookie authenticates the same job',40)
    assert active['id']==pending['id']
    popup.button('Cancel')
    wait(lambda:native.job(protected,'cancelled'),'protected native cancellation')
    popup.click('[data-tab="history"]')
    popup.button('Retry')
    wait(lambda:native.job(protected,'downloading'),'protected retry obtains new job context',40)
    fixture['release'].set()
    authenticated=wait(lambda:native.job(protected,'complete'),'protected retry completion',40)
    assert authenticated['id']==pending['id']
    assert any(r['path'].endswith(kind+'-protected.wav') and r['authenticated'] for r in fixture['requests'])
    storage=browser.background('return new Promise(resolve=>this.chrome.storage.local.get(null,resolve))')
    assert 'vidgrab_validation_session' not in json.dumps(storage)
    popup.click('[data-tab="detected"]')
    replay=lost_reply(browser,popup,native,base+'/'+kind+'-lost.wav',enqueue,lambda:popup.click('#check-request-btn'))
    mutations=[e for e in browser.state()['trace'] if e['key']]
    recovered=reconnect(native)
    page.has_text('.connection','Connected')
    popup.has_text('#connection-status','Connected')
    options.click('#btn-reload-settings')
    options.has_text('#status-message','Current app settings loaded.')
    current_settings=native.settings()['values']
    assert options.value('#input-download-dir')==current_settings['download_dir']
    assert options.value('#input-max-concurrent')==str(current_settings['max_concurrent'])
    popup.click('[data-tab="history"]')
    wait(lambda:popup.visible('#task-card-'+authenticated['id']),'installed browser reconnect retains same history')
    assert [e for e in browser.state()['trace'] if e['key']]==mutations,'Reconnect replayed a mutation'
    popup.screenshot(screenshots/(kind+'-native-popup.png'))
    options.screenshot(screenshots/(kind+'-native-options.png'))
    browser.background('this.__nativeAcceptance.strip=true')
    popup.goto(browser.base+'popup/popup.html')
    popup.has_text('#connection-status','Update required')
    popup.has_text('#backend-error-overlay','Update required')
    browser.background('this.__nativeAcceptance.strip=false')
    popup.goto(browser.base+'popup/popup.html')
    popup.has_text('#connection-status','Connected')
    return {'browser':kind,'version':browser.version,'sandbox':browser.sandbox,'permissions':permission,
        'bootstrap':getattr(browser,'bootstrap',None),
        'launcher':browser.chrome_task.audit if browser.chrome_task else None,
        'ordinary_job':complete,'authenticated_job':authenticated,'lost_reply':replay,'recovery':recovered,
        'checks':['shared native defaults/events','desktop and browser dirty conflicts',
            'ordinary frozen media','actual grants distinct from cookie preference',
            '403 to fresh cookies for same job','protected cancellation/fresh-context retry',
            'accepted reply loss, two refreshes without replay, explicit keyed Check request',
            'actual browser/desktop engine reconnect with identical jobs and no mutation replay',
            'missing capability upgrade message after a real native response']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','app','home','report','screenshots','chrome','firefox','geckodriver'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--browser',choices=('chrome','firefox','both'),default='both')
    parser.add_argument('--chrome-launcher',type=Path,help='Owned Windows task descriptor for the installed Chrome diagnostic token')
    parser.add_argument('--discovery',action='store_true',help='Include actual P05 document/frame and context journeys')
    parser.add_argument('--format-review',action='store_true',help='Separate real HTTP failures and native format/context review lane')
    parser.add_argument('--format-expiry',action='store_true',help='Wait for the unchanged 300-second probe TTL in the review lane')
    parser.add_argument('--discovery-grants',action='store_true',help='Request and revoke actual optional permissions in the temporary extension')
    parser.add_argument('--permission-events',action='store_true',help='Visible selected-VM browser dialogs with bounded native input')
    parser.add_argument('--browser-manager-revocation',action='store_true',help='Use the visible Firefox Add-ons manager to revoke navigation permission')
    args=parser.parse_args()
    if args.format_expiry and not args.format_review:
        parser.error('--format-expiry requires --format-review')
    if args.format_review and (args.discovery or args.permission_events):
        parser.error('--format-review is a separate lane from discovery and permission input')
    if args.permission_events and (not args.discovery or not args.discovery_grants):
        parser.error('--permission-events requires --discovery --discovery-grants')
    if args.browser_manager_revocation and (not args.permission_events or args.browser != 'firefox'):
        parser.error('--browser-manager-revocation requires --permission-events --browser firefox')
    if os.name=='nt':
        if os.environ.get('COMPUTERNAME')!='DESKTOP-OSOIBNA' or os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED')!='1':
            parser.error('Select the explicitly isolated Windows validation VM')
    elif Path('/etc/hostname').read_text().strip()!='vidgrab-p02-ubuntu-validation' or os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED')!='1':
        parser.error('Select the explicitly isolated Ubuntu validation VM')
    if args.home.exists() or args.report.exists() or args.screenshots.exists():
        parser.error('Select fresh home/report/screenshots paths')
    args.home.mkdir(parents=True,mode=0o700)
    args.screenshots.mkdir(parents=True)
    environment={**os.environ,'HOME':str(args.home),'XDG_DATA_HOME':str(args.home/'data'),
        'XDG_CONFIG_HOME':str(args.home/'config'),'XDG_CACHE_HOME':str(args.home/'cache')}
    if os.name=='nt':
        environment.update(APPDATA=str(args.home/'roaming'),LOCALAPPDATA=str(args.home/'local'))
    for key in ('PYTHONPATH','PYTHONHOME','VIDGRAB_DATA_DIR','VIDGRAB_DOWNLOAD_DIR','VIDGRAB_LOG_DIR','VIDGRAB_TEMP_DIR',
        'MOZ_DISABLE_CONTENT_SANDBOX','MOZ_DISABLE_GMP_SANDBOX','MOZ_DISABLE_RDD_SANDBOX'):
        environment.pop(key,None)
    if os.name!='nt':
        values=dict(line.split('=',1) for line in subprocess.check_output(
            ['systemctl','--user','show-environment'],text=True).splitlines() if '=' in line)
        assert values.get('XDG_SESSION_TYPE')=='wayland',values.get('XDG_SESSION_TYPE')
        environment.update({key:values[key] for key in ('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','XDG_SESSION_TYPE')})
        environment.update(XDG_RUNTIME_DIR='/run/user/1000',DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus',
            GDK_BACKEND='wayland',LIBGL_ALWAYS_SOFTWARE='1',WEBKIT_DISABLE_DMABUF_RENDERER='1')
        from tests.check_wayland_desktop import Desktop as GNOME
        assert GNOME().snapshot()['wayland']
    suffix='.exe' if os.name=='nt' else ''
    host=args.home/('vidgrab-native-host'+suffix)
    shutil.copy2(args.bundle/host.name,host)
    config={'app':str(args.app.resolve()),'engine':str((args.bundle/'engine'/('vidgrab-engine'+suffix)).resolve()),
        'data_dir':str(args.home/'state'),'download_dir':str(args.home/'Media বাংলা'),
        'chrome_origin':f'chrome-extension://{CHROME_ID}/','firefox_id':FIREFOX_ID}
    path=args.home/'host-config.json'
    path.write_text(json.dumps(config))
    native=Native(host,path,environment)
    manifests={}
    for browser in (r'Google\Chrome','Mozilla'):
        manifest={'name':HOST,'description':'VidGrab isolated native acceptance','path':str(host), 'type':'stdio'}
        manifest.update({'allowed_origins':[config['chrome_origin']]} if browser!= 'Mozilla'
            else {'allowed_extensions':[FIREFOX_ID]})
        destination=args.home/('chrome-host.json' if browser!='Mozilla' else 'firefox-host.json')
        destination.write_text(json.dumps(manifest))
        manifests[browser]=destination
    if os.name!='nt':
        destination=args.home/'.mozilla/native-messaging-hosts'/(HOST+'.json')
        destination.parent.mkdir(parents=True)
        shutil.copy2(manifests['Mozilla'],destination)
    receipt={'result':'running','os':platform.platform(),'kernel':platform.release(),'config':config,'browsers':[],
        'limits':['isolated selected VM and temporary browser profiles; development extension load',
            'actual native transports/frozen workers; loopback media only',
            'lost/missing-feature replies are deliberate filters after actual native responses',
            'software/driver input; no physical input, installer, Snap/Flatpak or public-site claim']}
    desktop=None
    def checkpoint():
        args.report.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n')
    from playwright.sync_api import sync_playwright
    try:
        with windows_registrations(manifests) if os.name=='nt' else nullcontext():
            with sync_playwright() as playwright,media_fixture(discovery=args.discovery,review_failures=args.format_review) as (base,fixture):
                desktop=Desktop.__new__(Desktop)
                desktop.__init__(args,environment,native,playwright)
                receipt['desktop']={'webview':desktop.webview,'window':desktop.window,'ready':desktop.ready}
                page=desktop.page
                page.click('.download-defaults > summary')
                page.fill('#default-format','bestvideo+bestaudio/best')
                if not page.script("return document.querySelector('.defaults-form button.primary').disabled"):
                    page.button('Save defaults')
                    page.has_text('body','Defaults saved')
                receipt['desktop']['lost_reply']=lost_reply(desktop,page,native,base+'/desktop-lost.wav',
                    lambda url:(page.fill('#media-url',url),page.click('.download-form button.primary')),
                    lambda:page.button('Check request'))
                for kind in ('chrome','firefox') if args.browser=='both' else (args.browser,):
                    browser=None
                    try:
                        browser=Browser.__new__(Browser)
                        browser.__init__(kind,args,environment,args.home,playwright,manifests)
                        if args.format_review:
                            from tests.native_review_journeys import review_journey
                            receipt.setdefault('format_review', []).append(review_journey(
                                browser,native,base,fixture,args.screenshots,args.bundle,expiry=args.format_expiry))
                            checkpoint()
                        elif args.permission_events:
                            if os.name == 'nt':
                                from tests.native_windows_grant_input import permission_input
                            else:
                                from tests.native_grant_input import permission_input
                            from tests.native_discovery_journeys import visible_grants_journey
                            revocation_input = None
                            if args.browser_manager_revocation:
                                from tests.native_browser_permission_review import revoke_navigation
                                revocation_input = revoke_navigation
                            receipt.setdefault('permission_journeys', []).append({'browser': kind,
                                'version': browser.version, 'sandbox': browser.sandbox,
                                'background_diagnostics': getattr(browser, 'background_diagnostics', None),
                                'journey': visible_grants_journey(browser,native,base,args.screenshots,permission_input,
                                    revocation_input=revocation_input)})
                            checkpoint()
                        elif args.discovery:
                            from tests.native_discovery_journeys import discovery_journey
                            receipt.setdefault('discovery', []).append(discovery_journey(
                                browser,native,base,fixture,args.screenshots,grants=args.discovery_grants))
                            checkpoint()
                        if not args.permission_events and not args.format_review:
                            receipt['browsers'].append(browser_journey(browser,desktop,native,base,fixture,args.screenshots))
                        checkpoint()
                    except BaseException:
                        receipt['media_requests']=list(fixture['requests'])
                        receipt['browser_partial']={'browser':kind,'version':getattr(browser,'version',None),
                            'bootstrap':getattr(browser,'bootstrap',None),
                            'background_diagnostics':getattr(browser,'background_diagnostics',None)}
                        try:
                            receipt['browser_partial'].update(delivery_state=browser.state(),
                                popup=browser.popup.text(),options=browser.options.text())
                            browser.popup.screenshot(args.screenshots/(kind+'-failure.png'))
                        except Exception as error:
                            receipt['browser_partial']['capture_failure']=str(error)
                        raise
                    finally:
                        if browser:
                            browser.close()
                page.screenshot(args.screenshots/'desktop-native.png')
                receipt['media_requests']=list(fixture['requests'])
                page.fill('#media-url',base+'/desktop-incompatible.wav')
                wait(lambda:not page.script("return document.querySelector('.download-form button.primary').disabled"),
                    'compatible native contract enables a valid download URL')
                page.script('this.__nativeAcceptance.strip=true')
                page.button('Reload defaults')
                page.has_text('body','Update VidGrab and the browser extension to compatible versions')
                wait(lambda:page.script("return document.querySelector('.download-form button.primary').disabled"),
                    'incompatible native contract disables download submission')
                page.script('this.__nativeAcceptance.strip=false')
                page.has_text('.connection','Connected')
                with sqlite3.connect(Path(config['data_dir'])/'vidgrab.db') as db:
                    dump='\n'.join(db.iterdump())
                    assert 'vidgrab_validation_session' not in dump
                receipt['jobs']=native.jobs()
                receipt['media_requests']=fixture['requests']
                receipt['result']='passed'
                desktop.close()
                desktop=None
    except BaseException:
        receipt.update(result='failed',failure=traceback.format_exc())
        if 'fixture' in locals():
            receipt['media_requests']=list(fixture['requests'])
        if desktop:
            receipt['desktop_partial']={'webview':getattr(desktop,'webview',None),
                'ready':getattr(desktop,'ready',None),'window':getattr(desktop,'window',None),
                'renderer_errors':getattr(desktop,'renderer_errors',[])}
            try:
                receipt['page']=desktop.page.text()
                desktop.page.screenshot(args.screenshots/'desktop-failure.png')
            except Exception as error:
                receipt['page_capture_error']=str(error)
        raise
    finally:
        if desktop:
            try:
                desktop.close()
            except Exception:
                receipt['cleanup_failure']=traceback.format_exc()
                receipt['result']='failed'
        checkpoint()
        print(json.dumps({'result':receipt['result'],'report':str(args.report),'browsers':len(receipt['browsers'])}),flush=True)


if __name__=='__main__':
    main()
