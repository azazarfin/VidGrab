"""Native P05 review with real HTTP errors, media workers and wall-clock expiry.

This opt-in lane observes returned native replies without changing requests,
responses, clocks, backend state or production permission declarations.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from tests.native_review_fixture import FragmentServer
from tests.native_client_support import wait


OBSERVE = r"""
const original = this.vidGrabNative.request.bind(this.vidGrabNative);
this.__nativeReview = [];
this.vidGrabNative.request = async request => {
    const response = await original(request);
    if (['probes.create','downloads.enqueue'].includes(request.operation)) {
        this.__nativeReview.push({operation:request.operation, ok:response.ok,
            code:response.error?.code || null, retry_at:response.error?.retry_at ?? null,
            witnessed:Boolean(request.params?.probe_id), context:Boolean(request.params?.auth_ref),
            fresh:request.params?.fresh === true, format:request.params?.format_id || null,
            formats:response.data?.formats?.map(row => row.format_id) || null});
    }
    return response;
};
return true;
"""


def review_journey(browser, native, base, fixture, screenshots, bundle, *, expiry=False):
    page = browser.popup
    root = screenshots / (browser.kind + '-review')
    root.mkdir()
    browser.background(OBSERVE)
    browser.background('return new Promise(resolve => chrome.storage.local.set({cookieForwardingEnabled:false},resolve))')
    tools = bundle / 'engine/_internal/tools'
    manifest = json.loads((tools / 'manifest.json').read_text(encoding='utf-8'))
    ffmpeg = tools / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    assert hashlib.sha256(ffmpeg.read_bytes()).hexdigest() == manifest['ffmpeg']['sha256']
    versions = {'ffmpeg_sha256': manifest['ffmpeg']['sha256']}
    result = {'browser': browser.kind, 'versions': versions, 'checks': [], 'expiry_requested': expiry}

    def trace():
        return browser.background('return this.__nativeReview')

    def save():
        result['wire'] = trace()
        (root / 'review.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')

    def disabled(selector):
        return page.script('return document.querySelector(arguments[0]).disabled', selector)

    def open_probe(url):
        page.goto(browser.base + 'popup/popup.html')
        page.has_text('#connection-status', 'Connected')
        page.fill('#video-url-input', url)
        page.click('#download-btn')
        wait(lambda: page.visible('#format-modal'), 'native review dialog')

    def selected_probe(url):
        open_probe(url)
        wait(lambda: not disabled('#modal-start-btn'), 'actual native format list')
        selected = page.script('''const row = Array.from(document.querySelector('#format-select').options)
            .find(option => option.value); return row?.value;''')
        assert selected, page.text()
        page.select('#format-select', selected)
        return selected

    def counts():
        wire = trace()
        return {operation: sum(row['operation'] == operation for row in wire)
                for operation in ('probes.create', 'downloads.enqueue')}

    try:
        for name, code, label in (
                ('login', 'AUTH_REQUIRED', 'Open source page'),
                ('throttle', 'RATE_LIMITED', 'Retry after'),
                ('unsupported', 'UNSUPPORTED_MEDIA', 'Choose another source')):
            before = len(native.jobs())
            url = base + ('/review-protected.wav' if name == 'login' else '/review-' + name + '.wav')
            # Request cooldowns bind the hostname, including other ports. Keep
            # the deliberate 429 on a separate loopback hostname so it cannot
            # suppress the subsequent real media/context probes.
            if name == 'throttle' and os.name != 'nt':
                url = url.replace('://127.0.0.1:', '://localhost:')
            start = counts()
            open_probe(url)
            page.has_text('#probe-action-btn', label)
            assert disabled('#modal-start-btn') and page.visible('#probe-action-btn')
            error = trace()[-1]
            assert error['operation'] == 'probes.create' and error['code'] == code, error
            assert len(native.jobs()) == before and counts()['probes.create'] == start['probes.create'] + 1
            if name == 'throttle':
                assert disabled('#probe-action-btn') and error['retry_at'] > time.time()
            elif name == 'login':
                assert page.visible('#fresh-probe-btn')
            else:
                page.click('#probe-action-btn')
                assert not page.visible('#format-modal')
            page.screenshot(root / (name + '.png'))
            result['checks'].append('actual ' + code + ' guidance without enqueue or automatic reprobe')
            save()
            if name == 'throttle' and os.name == 'nt':
                # Use the original Windows numeric loopback lane and honor its
                # real host cooldown before the next explicit fixture probe.
                deadline = error['retry_at'] + 1
                while time.time() < deadline:
                    print(json.dumps({'event': 'native-cooldown-wait', 'browser': browser.kind,
                                      'seconds_remaining': round(deadline - time.time(), 1)}), flush=True)
                    time.sleep(min(30, max(0, deadline - time.time())))

        with FragmentServer('dash', ffmpeg, name=browser.kind + '-dash-review') as server:
            server.gated = False
            shutil.copytree(server.root, root / 'dash-fixture')
            before = len(native.jobs())
            selected = selected_probe(server.url)
            start = counts()
            login = browser.open('popup/popup.html')
            login.goto(base + '/login')
            browser.background('return new Promise(resolve => chrome.storage.local.set({cookieForwardingEnabled:true},resolve))')
            page.click('#modal-start-btn')
            page.has_text('#modal-author', 'same media and browser context')
            assert disabled('#modal-start-btn') and page.visible('#fresh-probe-btn')
            assert trace()[-1]['code'] == 'PROBE_CONTEXT_SCOPE', trace()[-1]
            assert len(native.jobs()) == before and counts()['probes.create'] == start['probes.create']
            page.screenshot(root / 'context-rejected.png')
            page.click('#fresh-probe-btn')
            wait(lambda: not disabled('#modal-start-btn'), 'explicit fresh native context probe')
            assert counts()['probes.create'] == start['probes.create'] + 1 and len(native.jobs()) == before
            page.select('#format-select', selected)
            # Rotate the real DASH representation after review. The owned engine
            # must retain the verified audio intent through its fresh extraction.
            server.manifest = server.manifest.replace(b'id="audio"', b'id="fresh-audio"')
            page.click('#modal-start-btn')
            complete = wait(lambda: native.job(server.url, 'complete'), 'native context/DASH repair completion', 40)
            assert len(native.jobs()) == before + 1
            assert server.decoded_hash(complete['file_path']) == server.decoded
            result['context_job'] = complete
            result['checks'].extend(['context change rejected before enqueue; explicit fresh probe and Start',
                                     'DASH representation rotation preserves decoded audio through frozen worker'])
            page.screenshot(root / 'context-complete.png')
            save()

        if expiry:
            browser.background('return new Promise(resolve => chrome.storage.local.set({cookieForwardingEnabled:false},resolve))')
            with FragmentServer('hls', ffmpeg, name=browser.kind + '-hls-review') as server:
                server.gated = False
                server.parts['/variant.m3u8'] = server.manifest
                server.manifest = (b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\nvariant.m3u8\n')
                shutil.copytree(server.root, root / 'hls-fixture')
                before = len(native.jobs())
                selected = selected_probe(server.url)
                start = counts()
                title = browser.kind + ' expiry বাংলা'
                page.fill('#custom-title-input', title)
                began = time.monotonic()
                deadline = began + 302
                while time.monotonic() < deadline:
                    print(json.dumps({'event': 'native-expiry-wait', 'browser': browser.kind,
                                      'seconds_remaining': round(deadline - time.monotonic(), 1)}), flush=True)
                    time.sleep(min(30, max(0, deadline - time.monotonic())))
                server.manifest = server.manifest.replace(b'BANDWIDTH=64000', b'BANDWIDTH=96000')
                page.click('#modal-start-btn')
                page.has_text('#modal-author', 'refreshed once')
                wait(lambda: not disabled('#modal-start-btn'), 'native expiry refresh ready')
                wire = trace()
                errors = [row['code'] for row in wire if not row['ok']]
                assert errors[-1] == 'PROBE_EXPIRED', errors
                replacement = page.value('#format-select')
                assert replacement and replacement != selected, (selected, replacement)
                assert page.value('#custom-title-input') == title
                assert counts()['probes.create'] == start['probes.create'] + 1 and len(native.jobs()) == before
                page.screenshot(root / 'expired-review.png')
                page.click('#modal-start-btn')
                complete = wait(lambda: native.job(server.url, 'complete'), 'explicit native expiry Start completes', 40)
                assert len(native.jobs()) == before + 1
                assert server.decoded_hash(complete['file_path']) == server.decoded
                assert counts()['probes.create'] == start['probes.create'] + 1
                result['expiry'] = {'wait_seconds': time.monotonic() - began, 'original_format': selected,
                                    'replacement_format': replacement, 'job': complete, 'ttl_seconds': 300}
                result['checks'].append('actual 300-second expiry; one equivalent refresh; title kept; second explicit Start')
                save()
        dump = json.dumps(browser.background('return new Promise(resolve => chrome.storage.local.get(null,resolve))'))
        assert 'vidgrab_validation_session' not in dump
        result['result'] = 'passed'
        save()
        return result
    finally:
        save()
