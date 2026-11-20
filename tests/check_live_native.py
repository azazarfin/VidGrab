"""Opt-in bounded anonymous frozen, native-host, desktop, and browser live lanes.

Exercises real `vidgrab-native-host` -> `vidgrab-supervisor` / `vidgrab-desktop` ->
frozen `vidgrab-engine` -> frozen `--worker` execution on bounded public anonymous URLs,
preserving all prior probe/download/permission failures and keeping signed-in media pending.
Reports retain only redacted labels, allowlisted statuses/categories, and output hashes.
"""

import argparse
import base64
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import check_live_downloads as live_dl
from tests import check_live_sites as live_sites
from tests.check_live_diagnostics import PUBLIC_CONTROL_CASES
from tests.native_client_support import (
    Native,
    Page,
    WindowsBrowserTask,
    chrome_extension_worker,
    free_port,
    wait,
    windows_registrations,
)

CHROME_ID = 'ikjpekhbbaehdhigakbahlhafaojbjpm'
FIREFOX_ID = 'vidgrab@azaz.local'
FIREFOX_UUID = 'a853416a-1e23-4d8d-945d-b9f74f8649e4'
HOST = 'com.vidgrab.companion'

PUBLIC_NATIVE_CASES = [
    {**next(c for c in PUBLIC_CONTROL_CASES if c['id'] == 'mdn-flower-control'), 'use_defaults': True},
    {**next(c for c in live_sites.PUBLIC_CASES if c['id'] == 'mux-bbb'), 'use_defaults': False},
    {**next(c for c in PUBLIC_CONTROL_CASES if c['id'] == 'youtube-zoo-control'), 'use_defaults': False},
    {**next(c for c in live_sites.PUBLIC_CASES if c['id'] == 'iana-example'), 'use_defaults': False},
]

BANNED_KEYS = frozenset({
    'url', 'job_id', 'probe_id', 'file_path', 'title', 'cookies', 'headers',
    'stderr', 'stdout', 'error_message', 'root', 'data_dir', 'download_dir',
})
BANNED_SUBSTRINGS = (
    'http://', 'https://', 'file://', 'youtube.com', 'youtu.be',
    'mozilla.net', 'mux.dev', 'example.org', 'envivio',
)


def validate_case(case):
    if case.get('authentication') != 'public':
        raise ValueError('Live native lane accepts only public anonymous cases')
    url = case.get('url') or ''
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc:
        raise ValueError('Public case URL must be HTTPS')
    if parsed.username or parsed.password:
        raise ValueError('Public case URL must not include credentials')


def clean_environment(base_env=None):
    environment = dict(os.environ if base_env is None else base_env)
    for key in (
        'PYTHONPATH', 'PYTHONHOME', 'VIDGRAB_DATA_DIR', 'VIDGRAB_DOWNLOAD_DIR',
        'VIDGRAB_LOG_DIR', 'VIDGRAB_TEMP_DIR', 'MOZ_DISABLE_CONTENT_SANDBOX',
        'MOZ_DISABLE_GMP_SANDBOX', 'MOZ_DISABLE_RDD_SANDBOX',
    ):
        environment.pop(key, None)
    if os.name == 'nt':
        environment['PYTHONUTF8'] = '1'
    return environment


def expected_chrome_origin():
    manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text(encoding='utf-8'))
    public = base64.b64decode(manifest['key'])
    identifier = ''.join(chr(ord('a') + int(letter, 16)) for letter in hashlib.sha256(public).hexdigest()[:32])
    if identifier != CHROME_ID:
        raise ValueError('Extension key mismatch')
    return f'chrome-extension://{identifier}/'


def build_host_config(*, app, engine, data_dir, download_dir):
    app_path = Path(app).resolve(strict=True)
    engine_path = Path(engine).resolve(strict=True)
    return {
        'app': str(app_path),
        'engine': str(engine_path),
        'data_dir': str(Path(data_dir).resolve()),
        'download_dir': str(Path(download_dir).resolve()),
        'chrome_origin': expected_chrome_origin(),
        'firefox_id': FIREFOX_ID,
    }


def resolve_bundled_components(engine_path):
    suffix = '.exe' if os.name == 'nt' else ''
    tools = Path(engine_path).resolve(strict=True).parent / '_internal' / 'tools'
    components = {
        name: str((tools / f'{name}{suffix}').resolve(strict=True))
        for name in ('ffmpeg', 'ffprobe')
    }
    return components


def assert_redacted(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in BANNED_KEYS:
                raise ValueError(f'Banned unredacted field {key!r} in report')
            assert_redacted(item)
    elif isinstance(value, list):
        for item in value:
            assert_redacted(item)
    elif isinstance(value, str):
        lower = value.lower()
        for token in BANNED_SUBSTRINGS:
            if token in lower:
                raise ValueError(f'Banned literal token {token!r} in report string')


def native_v1_send(native, operation, params=None, key=None):
    request = {
        'protocol_version': 1,
        'request_id': str(uuid4()),
        'operation': operation,
        'params': params or {},
    }
    if key is not None:
        request['idempotency_key'] = key
    return native.outer('v1.request', request)


def native_cancel_guarded(native, job, reason):
    reply = native_v1_send(
        native,
        'downloads.action',
        {'job_id': job['id'], 'expected_revision': job['revision'], 'action': 'cancel'},
        key='live-native-cancel-' + str(uuid4()),
    )
    if reply.get('ok') is not True:
        raise RuntimeError('Guard cancellation did not return a definite success')
    return {
        'error_category': reason if reason in live_dl.ERROR_CODES else 'UNCLASSIFIED_FAILURE',
        'guard_cancelled': True,
    }


def stop_native_owner(config, environment, native=None):
    if os.name != 'nt':
        from tests.native_support import stop_owner
        stop_owner(config)
        return
    from tests.check_windows_native import ProcessHandle
    handles = {}
    if native is not None:
        try:
            status = native.outer('status', allow_unavailable=True)
            if status:
                for key in ('supervisor_pid', 'engine_pid'):
                    pid = status.get(key)
                    if pid:
                        handles[pid] = ProcessHandle(pid)
        except Exception:
            pass
    try:
        result = subprocess.run(
            [config['app'], '--config', str(Path(config['data_dir']).parent / 'host-config.json'), '--request', 'quit'],
            env=environment,
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        for handle in handles.values():
            assert handle.wait(12), 'Quit must stop the owned Windows supervisor and engine'
    finally:
        for handle in handles.values():
            handle.close()


def monitor_native_job(native, job_id, *, root, download_dir, components, expected_duration):
    baseline_bytes = live_dl.observed_bytes(root)
    job_started = time.monotonic()
    guard = None
    while True:
        page = native_v1_send(native, 'downloads.list', {'limit': 50})
        if page.get('ok') is not True:
            raise RuntimeError('Native job snapshot failed')
        job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
        if job['status'] in live_dl.TERMINAL:
            break
        size = max(0, live_dl.observed_bytes(root) - baseline_bytes)
        reason = live_dl.stop_reason(job, time.monotonic() - job_started, size)
        if reason:
            guard = native_cancel_guarded(native, job, reason)
            page = native_v1_send(native, 'downloads.list', {'limit': 50})
            job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
            if job['status'] != 'cancelled':
                raise RuntimeError('Guard cancellation did not join the native job')
            break
        time.sleep(live_dl.SAMPLE_INTERVAL)
    row = live_dl.summarize_job(job)
    if guard:
        row.update(guard, assessment='mismatch')
    if job['status'] == 'complete':
        try:
            row['output'] = live_dl.verify_output(
                job['file_path'], download_dir, components, expected_duration
            )
        except Exception:
            row.update(assessment='mismatch', error_category='VERIFICATION_FAILED')
    return row


def run_native_case(native, case, *, root, download_dir, components, use_defaults=None, transport='frozen-native'):
    validate_case(case)
    started = time.monotonic()
    row = {
        'id': case['id'],
        'media_type': case['media_type'],
        'authentication': case['authentication'],
        'transport': transport,
        'phase': 'download',
    }
    probe = native_v1_send(native, 'probes.create', {'url': case['url']})
    if probe.get('ok') is not True:
        row.update(live_dl.summarize_reply(case, probe, time.monotonic() - started))
        row['phase'] = 'probe-before-download'
    else:
        data = probe['data']
        selected = live_dl.select_format(data)
        defaults = case.get('use_defaults', False) if use_defaults is None else use_defaults
        if case['expected'] == 'unsupported':
            row.update(result='unexpected_metadata', assessment='mismatch', error_category='EMPTY_METADATA')
        elif not defaults and selected is None:
            row.update(result='no_download', assessment='mismatch', error_category='EMPTY_METADATA')
        else:
            row['selection'] = 'explicit-app-defaults' if defaults else 'probe-quality'
            params = live_dl.enqueue_parameters(case, data, selected, use_defaults=defaults)
            reply = native_v1_send(
                native, 'downloads.enqueue', params, key='live-native-enqueue-' + str(uuid4())
            )
            if reply.get('ok') is not True:
                row.update(live_dl.summarize_reply(case, reply, time.monotonic() - started))
                row['phase'] = 'enqueue'
            else:
                row.update(monitor_native_job(
                    native,
                    reply['data']['job_id'],
                    root=root,
                    download_dir=download_dir,
                    components=components,
                    expected_duration=data.get('duration'),
                ))
    row['seconds'] = round(time.monotonic() - started, 3)
    assert_redacted(row)
    return row


def make_runtime_root(prefix):
    temp_dir = '/tmp' if os.name != 'nt' and Path('/tmp').is_dir() else None
    return Path(tempfile.mkdtemp(prefix=prefix, dir=temp_dir)).resolve()


def verify_cleanup_audit(audit, platform_name=None):
    name = os.name if platform_name is None else platform_name
    for key in ('request_probes', 'request_leases', 'unfinished_jobs'):
        if audit[key]:
            raise RuntimeError(f'Runtime left active {key}: {audit}')
    max_owners = 1 if name == 'nt' else 0
    if audit['unreleased_owners'] > max_owners:
        raise RuntimeError(f'Runtime left unreleased_owners={audit["unreleased_owners"]}: {audit}')


def run_native_frozen_lane(args):
    suffix = '.exe' if os.name == 'nt' else ''
    app = (args.app or (args.bundle / ('vidgrab-supervisor' + suffix))).resolve(strict=True)
    engine = (args.engine or (args.bundle / 'engine' / ('vidgrab-engine' + suffix))).resolve(strict=True)
    components = resolve_bundled_components(engine)

    root = make_runtime_root('vgln-')
    data_dir = root / 'state'
    download_dir = root / 'media'
    host_copy = root / ('vidgrab-native-host' + suffix)
    shutil.copy2((args.bundle / host_copy.name).resolve(strict=True), host_copy)
    config = build_host_config(app=app, engine=engine, data_dir=data_dir, download_dir=download_dir)
    config_path = root / 'host-config.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    environment = clean_environment()
    (args.output / 'runtime.json').write_text(json.dumps({
        'root': str(root),
        'scope': 'isolated native-host/frozen public live state and media; retained',
    }, indent=2) + '\n', encoding='utf-8')

    native = Native(host_copy, config_path, environment)
    only = {item.strip() for item in args.only.split(',')} if args.only else None
    cases = [case for case in PUBLIC_NATIVE_CASES if only is None or case['id'] in only]
    if not cases:
        raise ValueError('No matching cases selected')

    rows = []
    ready = None
    try:
        ready = wait(
            lambda: s if (s := native.outer('status', allow_unavailable=True)) and s.get('state') == 'ready' else None,
            'native frozen engine readiness',
            timeout=30,
        )
        with (args.output / 'rows.jsonl').open('a', encoding='utf-8') as stream:
            for case in cases:
                row = run_native_case(
                    native, case, root=root, download_dir=download_dir,
                    components=components, transport='frozen-native',
                )
                rows.append(row)
                line = json.dumps(row)
                stream.write(line + '\n')
                stream.flush()
                os.fsync(stream.fileno())
                print(line, flush=True)
    finally:
        stop_native_owner(config, environment, native=native)
    audit = live_dl.audit_database(data_dir / 'vidgrab.db')
    verify_cleanup_audit(audit)
    owner_meta = {
        'owner_mode': app.stem,
        'engine_generation': ready.get('engine_generation') if ready else None,
        'component_versions': ready.get('component_versions') if ready else None,
    }
    assert_redacted(owner_meta)
    return rows, audit, owner_meta


class LiveBrowserSession:
    """Installed Chrome/Firefox session with public network permitted for live URL tests."""

    def __init__(self, kind, args, environment, root, playwright, manifests):
        self.kind = kind
        self.context = None
        self.driver = None
        self.chrome_browser = None
        self.chrome_task = None
        self.pages = []
        self.root = root / kind
        self.root.mkdir()
        if kind == 'chrome':
            profile = self.root / 'profile'
            if os.name != 'nt':
                directory = profile / 'NativeMessagingHosts'
                directory.mkdir(parents=True)
                shutil.copy2(manifests[r'Google\Chrome'], directory / (HOST + '.json'))
            if os.name == 'nt' and args.chrome_launcher:
                port = free_port()
                self.chrome_task = WindowsBrowserTask(args.chrome_launcher, self.root, args.chrome, profile, port)
                import urllib.request
                def endpoint():
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as resp:
                            return json.load(resp)
                    except OSError:
                        return None
                wait(endpoint, 'installed Windows Chrome CDP endpoint')
                self.chrome_browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                self.context = self.chrome_browser.contexts[0]
            else:
                self.context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    executable_path=str(args.chrome),
                    headless=True,
                    env=environment,
                    chromium_sandbox=True,
                    ignore_default_args=['--disable-extensions'],
                    args=[
                        '--enable-unsafe-extension-debugging',
                        '--disable-background-networking',
                        '--disable-component-update',
                        '--no-first-run',
                        '--no-default-browser-check',
                    ],
                )
            cdp = self.context.browser.new_browser_cdp_session()
            self.version = cdp.send('Browser.getVersion')
            identifier = cdp.send('Extensions.loadUnpacked', {'path': str(ROOT / 'extension')})['id']
            assert identifier == CHROME_ID, identifier
            self.worker = chrome_extension_worker(self.context, identifier)
            wait(
                lambda: self.worker.evaluate(
                    'Boolean(self.vidGrabAPI && self.vidGrabNative && self.vidGrabCompanion)'
                ),
                'extension worker bootstrap',
            )
            self.base = f'chrome-extension://{CHROME_ID}/'
        else:
            from tests.native_support import WebDriver
            extension = self.root / 'extension'
            shutil.copytree(ROOT / 'extension', extension)
            shutil.copy2(extension / 'manifest.firefox.json', extension / 'manifest.json')
            archive = self.root / 'extension.xpi'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
                for path in extension.rglob('*'):
                    if path.is_file():
                        out.write(path, path.relative_to(extension))
            self.driver = WebDriver(
                args.geckodriver, environment, self.root / 'gecko.log', arguments=('--allow-system-access',)
            )
            self.version = self.driver.start({
                'browserName': 'firefox',
                'moz:firefoxOptions': {
                    'binary': str(args.firefox),
                    'args': ['-headless'],
                    'prefs': {
                        'extensions.webextensions.uuids': json.dumps({FIREFOX_ID: FIREFOX_UUID}),
                        'app.update.auto': False,
                        'app.update.enabled': False,
                        'extensions.update.enabled': False,
                        'datareporting.policy.dataSubmissionEnabled': False,
                        'toolkit.telemetry.enabled': False,
                        'browser.shell.checkDefaultBrowser': False,
                    },
                },
            })
            identifier = self.driver.call('/moz/addon/install', {'path': str(archive), 'temporary': True})
            assert identifier == FIREFOX_ID, identifier
            self.base = f'moz-extension://{FIREFOX_UUID}/'
        self.popup = self.open('popup/popup.html')
        self.popup.has_text('#connection-status', 'Connected')

    def open(self, path):
        if self.context:
            page = Page(page=self.context.new_page())
            page.page.goto(self.base + path, wait_until='domcontentloaded')
        else:
            handle = self.driver.call('/window/new', {'type': 'tab'})['handle']
            page = Page(driver=self.driver, handle=handle)
            page.goto(self.base + path)
        self.pages.append(page)
        return page

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


def _pick_smallest_resolution_format(page):
    return page.script('''
        const sel = document.querySelector('#format-select');
        if (!sel) return null;
        const opts = [...sel.options].filter(o => o.value);
        if (!opts.length) return '';
        let best = opts[0], bestPixels = Infinity;
        for (const opt of opts) {
          const m = opt.textContent.match(/(\\d+)x(\\d+)/);
          const pixels = m ? Number(m[1]) * Number(m[2]) : Infinity;
          if (pixels < bestPixels) { bestPixels = pixels; best = opt; }
        }
        sel.value = best.value;
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        return best.value;
    ''')


def run_browser_live_lane(args):
    from playwright.sync_api import sync_playwright
    from tests.check_native_clients import Desktop

    suffix = '.exe' if os.name == 'nt' else ''
    app = (args.app or (args.bundle / ('vidgrab-desktop' + suffix))).resolve(strict=True)
    engine = (args.engine or (args.bundle / 'engine' / ('vidgrab-engine' + suffix))).resolve(strict=True)
    components = resolve_bundled_components(engine)

    if getattr(args, 'home', None) is not None:
        root = Path(args.home).resolve()
        if root.exists() or root.is_symlink():
            raise ValueError('Choose a fresh --home directory; previous runtime state is preserved')
        root.mkdir(parents=True, mode=0o700)
    else:
        root = make_runtime_root('vglb-')
    data_dir = root / 'state'
    download_dir = root / 'media'
    host_copy = root / ('vidgrab-native-host' + suffix)
    shutil.copy2((args.bundle / host_copy.name).resolve(strict=True), host_copy)
    config = build_host_config(app=app, engine=engine, data_dir=data_dir, download_dir=download_dir)
    config_path = root / 'host-config.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')

    environment = clean_environment({
        **os.environ,
        'HOME': str(root),
        'XDG_DATA_HOME': str(root / 'data'),
        'XDG_CONFIG_HOME': str(root / 'config'),
        'XDG_CACHE_HOME': str(root / 'cache'),
    })
    if os.name == 'nt':
        environment.update(APPDATA=str(root / 'roaming'), LOCALAPPDATA=str(root / 'local'))
    else:
        values = dict(
            line.split('=', 1)
            for line in subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True).splitlines()
            if '=' in line
        )
        assert values.get('XDG_SESSION_TYPE') == 'wayland', values.get('XDG_SESSION_TYPE')
        environment.update({key: values[key] for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_SESSION_TYPE')})
        environment.update(
            XDG_RUNTIME_DIR='/run/user/1000',
            DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus',
            GDK_BACKEND='wayland',
            LIBGL_ALWAYS_SOFTWARE='1',
            WEBKIT_DISABLE_DMABUF_RENDERER='1',
        )

    manifests = {}
    for browser_key in (r'Google\Chrome', 'Mozilla'):
        manifest = {'name': HOST, 'description': 'VidGrab live browser native lane', 'path': str(host_copy), 'type': 'stdio'}
        manifest.update(
            {'allowed_origins': [config['chrome_origin']]} if browser_key != 'Mozilla'
            else {'allowed_extensions': [FIREFOX_ID]}
        )
        dest = root / ('chrome-host.json' if browser_key != 'Mozilla' else 'firefox-host.json')
        dest.write_text(json.dumps(manifest), encoding='utf-8')
        manifests[browser_key] = dest
    if os.name != 'nt':
        mozilla_dir = root / '.mozilla' / 'native-messaging-hosts' / (HOST + '.json')
        mozilla_dir.parent.mkdir(parents=True)
        shutil.copy2(manifests['Mozilla'], mozilla_dir)

    (args.output / 'browser-runtime.json').write_text(json.dumps({
        'root': str(root),
        'scope': 'isolated compiled desktop + installed browser public live lane; retained',
    }, indent=2) + '\n', encoding='utf-8')

    native = Native(host_copy, config_path, environment)
    case_map = {case['id']: case for case in PUBLIC_NATIVE_CASES}
    targets = []
    if args.browser in ('chrome', 'both'):
        targets.append(('chrome', case_map[args.browser_case_chrome]))
    if args.browser in ('firefox', 'both'):
        targets.append(('firefox', case_map[args.browser_case_firefox]))

    class DesktopArgs:
        pass
    desktop_args = DesktopArgs()
    desktop_args.app = app
    desktop_args.home = root

    desktop = None
    rows = []
    browser_meta = []
    try:
        with windows_registrations(manifests) if os.name == 'nt' else nullcontext():
            with sync_playwright() as playwright:
                desktop = Desktop.__new__(Desktop)
                desktop.__init__(desktop_args, environment, native, playwright)
                with (args.output / 'rows.jsonl').open('a', encoding='utf-8') as stream:
                    for kind, case in targets:
                        validate_case(case)
                        browser = None
                        started = time.monotonic()
                        row = {
                            'id': case['id'],
                            'media_type': case['media_type'],
                            'authentication': case['authentication'],
                            'transport': f'browser-live-{kind}',
                            'phase': 'download',
                        }
                        try:
                            browser = LiveBrowserSession(kind, args, environment, root, playwright, manifests)
                            browser_meta.append({
                                'browser': kind,
                                'version': browser.version.get('product') or browser.version.get('value', {}).get('browserVersion')
                                if isinstance(browser.version, dict) else None,
                            })
                            before_ids = {j['id'] for j in native.jobs()}
                            browser.popup.fill('#video-url-input', case['url'])
                            browser.popup.click('#download-btn')
                            wait(
                                lambda: browser.popup.visible('#format-modal')
                                and not browser.popup.script("return document.querySelector('#modal-start-btn').disabled"),
                                f'{kind} popup live probe dialog',
                                timeout=35,
                            )
                            if case.get('use_defaults'):
                                row['selection'] = 'explicit-app-defaults'
                                assert browser.popup.value('#format-select') == ''
                            else:
                                row['selection'] = 'probe-quality'
                                picked = _pick_smallest_resolution_format(browser.popup)
                                assert picked, f'{kind} popup did not populate selectable formats'
                            browser.popup.click('#modal-start-btn')
                            created = wait(
                                lambda: next((j for j in native.jobs() if j['id'] not in before_ids), None),
                                f'{kind} popup live download enqueue',
                                timeout=25,
                            )
                            job_row = monitor_native_job(
                                native,
                                created['id'],
                                root=root,
                                download_dir=download_dir,
                                components=components,
                                expected_duration=None,
                            )
                            row.update(job_row)
                            if row['result'] == 'complete':
                                desktop.page.click('[data-desktop-tab="history"]')
                                desktop.page.has_text('.history-view', 'Finished')
                                wait(
                                    lambda: browser.popup.visible('#task-card-' + created['id']),
                                    f'{kind} popup recent list shows completed live download',
                                    timeout=25,
                                )
                        finally:
                            if browser is not None:
                                browser.close()
                        row['seconds'] = round(time.monotonic() - started, 3)
                        assert_redacted(row)
                        rows.append(row)
                        line = json.dumps(row)
                        stream.write(line + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                        print(line, flush=True)
                desktop.close()
                desktop = None
    finally:
        if desktop is not None:
            desktop.close()
    audit = live_dl.audit_database(data_dir / 'vidgrab.db')
    verify_cleanup_audit(audit)
    assert_redacted(browser_meta)
    return rows, audit, browser_meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--engine', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', help='Comma-separated public case IDs for the frozen/native lane')
    parser.add_argument('--skip-frozen-native', action='store_true')
    parser.add_argument('--browser-live', action='store_true')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--browser', choices=('chrome', 'firefox', 'both'), default='both')
    parser.add_argument('--chrome', type=Path)
    parser.add_argument('--firefox', type=Path)
    parser.add_argument('--geckodriver', type=Path)
    parser.add_argument('--chrome-launcher', type=Path)
    parser.add_argument('--browser-case-chrome', default='mdn-flower-control')
    parser.add_argument('--browser-case-firefox', default='mux-bbb')
    args = parser.parse_args()

    if not args.allow_public_network:
        parser.error('Public downloads require --allow-public-network')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; previous evidence is preserved')
    if args.browser_live:
        if args.browser in ('chrome', 'both') and not args.chrome:
            parser.error('--browser-live with chrome requires --chrome')
        if args.browser in ('firefox', 'both') and (not args.firefox or not args.geckodriver):
            parser.error('--browser-live with firefox requires --firefox and --geckodriver')

    args.output.mkdir(parents=True)
    report = {
        'date_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.platform(),
        'kernel': platform.release(),
        'python': sys.version.split()[0],
        'signed_in': 'pending by user choice',
        'scope': 'bounded anonymous frozen/native/browser live lanes; signed-in pending by user choice',
        'limits': {
            'job_seconds': live_dl.TIME_LIMIT,
            'observed_bytes': live_dl.BYTE_LIMIT,
            'sample_interval': live_dl.SAMPLE_INTERVAL,
        },
        'rows': [],
        'execution_completed': False,
    }
    started = time.monotonic()
    try:
        if not args.skip_frozen_native:
            rows, audit, owner_meta = run_native_frozen_lane(args)
            report['rows'].extend(rows)
            report['frozen_native_cleanup_audit'] = audit
            report['frozen_native_owner'] = owner_meta
        if args.browser_live:
            b_rows, b_audit, b_meta = run_browser_live_lane(args)
            report['rows'].extend(b_rows)
            report['browser_live_cleanup_audit'] = b_audit
            report['browser_live_clients'] = b_meta
        assert_redacted(report)
        report['execution_completed'] = True
    except Exception as exc:
        report['harness_error'] = type(exc).__name__
    finally:
        report['seconds'] = round(time.monotonic() - started, 3)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'execution_completed': report['execution_completed'], 'seconds': report['seconds']}), flush=True)
    raise SystemExit(0 if report['execution_completed'] else 1)


if __name__ == '__main__':
    main()
