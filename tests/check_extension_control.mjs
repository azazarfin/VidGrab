// Both actual backgrounds with browser/native boundaries replaced by local fakes.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { test } from 'node:test';

const schema = JSON.parse(readFileSync('packages/protocol/schema-v1.json', 'utf8'));
const settings = schema.operations['settings.get'].result;
const properties = settings.$defs[settings.properties.values.$ref.split('/').at(-1)].properties;
const config = Object.fromEntries(Object.entries(properties).map(([key, item]) => [key, item.default]));
config.download_dir = '/fixture/media';
const plain = value => JSON.parse(JSON.stringify(value));
const id = 'ikjpekhbbaehdhigakbahlhafaojbjpm';
const url = 'https://fixture.invalid/media.mp4';

function environment(browser, storage = {}, engine = {}, sessionStorage = {}) {
  const browserId = browser === 'firefox' ? 'vidgrab@azaz.local' : id;
  const base = browser === 'firefox' ? 'moz-extension://fixture-extension/' : 'chrome-extension://' + id + '/';
  const listeners = {};
  const event = name => ({ addListener(callback) { (listeners[name] ||= []).push(callback); },
    removeListener(callback) { listeners[name] = (listeners[name] || []).filter(item => item !== callback); } });
  const calls = [], cookieReads = [], notifications = [];
  let storageWrites = 0;
  engine.receipts ||= new Map();
  engine.jobs ||= [];
  const chrome = {
    runtime: { id: browserId, lastError: null, onInstalled: event('installed'), onMessage: event('message'),
      getURL: path => base + path,
      sendMessage() { throw new Error('A background page must use native transport directly'); },
      sendNativeMessage(_host, outer, callback) {
        calls.push(plain(outer));
        const request = outer.params;
        let data;
        if (outer.op !== 'v1.request') data = { state: 'ready' };
        else if (request.operation === 'capabilities.get') data = { protocol_min: 1, protocol_max: 1,
          max_message_bytes: 65536, max_payload_bytes: 64512, instance_id: 'engine', fixture_only: false,
          paired_loopback: false, features: ['events.snapshots', 'settings.revisions', 'auth.scoped', 'auth.headers', 'probes.fresh', 'downloads.source']
            .filter(feature => !engine.excludedFeatures?.includes(feature)), component_versions: { ffmpeg: '8', 'yt_dlp': '2026.6.9' } };
        else if (request.operation === 'events.read') data = { instance_id: 'engine', cursor: 0, resnapshot: true,
          has_more: false, events: [], snapshot: { instance_id: 'engine', cursor: 0,
            settings: { revision: 0, values: config }, jobs: engine.jobs, next_after: null,
            counts: { total: engine.jobs.length, unfinished: engine.jobs.length } } };
        else if (request.operation === 'settings.get') data = { revision: 0, values: config };
        else if (request.operation === 'probes.create') data = { probe_id: 'probe', url, title: 'Fixture', thumbnail: null,
          duration: 1, uploader: null, is_playlist: false, entries: null, formats: [], subtitles: null,
          format_count: 0, entry_count: 0, formats_truncated: false, entries_truncated: false };
        else if (request.operation === 'auth.create') data = { reference: engine.reference || 'volatile-reference', instance_id: 'engine', expires_at: Date.now() / 1000 + 300 };
        else if (request.operation === 'downloads.enqueue') {
          if (!engine.receipts.has(request.idempotency_key)) {
            const job = { id: 'job-' + engine.receipts.size, url: request.params.url, revision: 0, title: 'Fixture',
              status: 'queued', progress: 0, file_path: null, error_code: null, retry_at: null, parent_id: null };
            engine.jobs.push(job);
            engine.receipts.set(request.idempotency_key, { job_id: job.id });
          }
          data = engine.receipts.get(request.idempotency_key);
          if (engine.lost) {
            chrome.runtime.lastError = { message: 'Reply lost' }; callback(); chrome.runtime.lastError = null; return;
          }
        } else if (['downloads.action', 'downloads.authenticate'].includes(request.operation)) {
          data = { job_id: request.params.job_id, action: request.params.action || 'authenticate',
            accepted_revision: request.params.expected_revision + 1,
            accepted_status: request.params.action === 'cancel' ? 'cancelled' : 'queued' };
        } else throw new Error('Unexpected native operation: ' + request.operation);
        callback({ spike_version: 1, id: outer.id, ok: true, data: outer.op === 'v1.request'
          ? { protocol_version: 1, request_id: request.request_id, ok: true, data } : data });
      } },
    storage: { onChanged: event('storage'),
      local: {
        get(keys, callback) { callback(keys === null ? plain(storage) : Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(key => [key, storage[key]]))); },
        set(values, callback) {
          if (++storageWrites === engine.failStorageWrite) {
            chrome.runtime.lastError = {message: 'Fixture storage failure'};
            callback?.(); chrome.runtime.lastError = null; return;
          }
          Object.assign(storage, plain(values)); callback?.();
        },
        remove(keys, callback) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete storage[key]; callback?.(); },
      },
      session: {
        get(keys, callback) { callback(keys === null ? plain(sessionStorage) : Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(key => [key, sessionStorage[key]]))); },
        set(values, callback) { Object.assign(sessionStorage, plain(values)); callback?.(); },
        remove(keys, callback) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete sessionStorage[key]; callback?.(); },
      } },
    permissions: { getAll(callback) { callback({ permissions: engine.permissions ?? ['cookies', 'nativeMessaging', 'notifications'], origins: engine.origins ?? ['<all_urls>'] }); }, onAdded: event('permissions-added'), onRemoved: event('permissions-removed') },
    cookies: { getAll(filter, callback) { cookieReads.push(filter); callback(engine.cookies ?? [{ domain: '.fixture.invalid', path: '/', name: 'session', value: 'cookie-secret', secure: true,
      httpOnly: true, storeId: 'ignored', expirationDate: Date.now() / 1000 + 3600 }]); },
      getAllCookieStores(callback) { callback([{ id: engine.storeId || '0', tabIds: [0] }]); } },
    tabs: { onRemoved: event('removed'), onUpdated: event('tab-updated'),
      get(tabId, callback) { callback({ id: tabId, url: engine.tabUrl || url, cookieStoreId: engine.storeId }); },
      query(_query, callback) { callback([{ id: 0, url: engine.tabUrl || url, cookieStoreId: engine.storeId }]); },
      sendMessage(_tabId, _message, { frameId }, callback) {
        const frame = Object.hasOwn(engine.liveFrames || {}, frameId) ? engine.liveFrames[frameId] : {
          frame_url: frameId === 0 ? engine.tabUrl || url : 'https://frame.fixture.invalid/player',
          document_token: 'fixture-document', referrer_policy: 'strict-origin-when-cross-origin' };
        if (frame === null) {
          chrome.runtime.lastError = { message: 'The frame was removed' }; callback(); chrome.runtime.lastError = null;
        } else callback(frame);
      } },
    webRequest: { onHeadersReceived: event('headers-received') },
    webNavigation: { onCommitted: event('frame-committed'), onHistoryStateUpdated: event('frame-history'),
      getFrame({ frameId }, callback) { callback(engine.frames?.[frameId] || { url: frameId === 0 ? engine.tabUrl || url : 'https://frame.fixture.invalid/player',
        documentId: 'document-' + frameId }); } },
    contextMenus: { create() {}, onClicked: event('context') },
    notifications: { create(...args) { notifications.push(args); }, clear() {}, onButtonClicked: event('notification-button'), onClicked: event('notification') },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {}, openPopup() {} },
    alarms: { create() {}, onAlarm: event('alarm') },
  };
  const context = vm.createContext({ chrome, console, EventTarget, Event, URL, TextEncoder, Date, navigator: { userAgent: 'Fixture Browser' },
    ...(browser === 'firefox' ? { document: {}, location: new URL(base + '_generated_background_page.html') } : {}),
    CustomEvent: class extends Event { constructor(name, options) { super(name); this.detail = options?.detail; } },
    crypto: webcrypto, fetch() { throw new Error('Legacy HTTP is forbidden'); }, WebSocket: class { constructor() { throw new Error('Legacy WebSockets are forbidden'); } },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {} });
  context.self = context;
  const load = path => vm.runInContext(readFileSync('extension/' + path, 'utf8'), context, { filename: path });
  const manifest = JSON.parse(readFileSync('extension/' + (browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json')));
  assert.doesNotThrow(() => {
    if (browser === 'chrome') { context.importScripts = (...paths) => paths.forEach(path => load('background/' + path)); load(manifest.background.service_worker); }
    else manifest.background.scripts.forEach(load);
  }, 'Actual backgrounds must start without legacy HTTP or WebSockets');
  const message = (value, sender) => new Promise(resolve => {
    let asynchronous = false;
    for (const listener of listeners.message || []) asynchronous = listener(value, sender, resolve) || asynchronous;
    if (!asynchronous) resolve(undefined);
  });
  return { context, chrome, storage, sessionStorage, engine, calls, cookieReads, listeners, notifications, message };
}

for (const browser of ['chrome', 'firefox']) {
  const pageUrl = 'https://page.fixture.invalid/watch';
  const frameUrl = 'https://frame.fixture.invalid/player';
  const mediaUrl = 'https://cdn.fixture.invalid/media.mp4?sig=a%2Fb+Z';
  const sender = (e, frameId = 0, frame = pageUrl) => ({ id: e.chrome.runtime.id, frameId,
    documentId: 'document-' + frameId, url: frame, tab: { id: 0, url: pageUrl, cookieStoreId: 'firefox-container-2' } });
  const report = (frame, videos, sequence = 1) => ({ type: 'video_detected', frame_url: frame,
    document_token: 'fixture-document', sequence, videos });

  test(browser + ': frame reports retain authoritative page/browser context', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl, storeId: 'firefox-container-2' });
    await Promise.all([
      e.message(report(pageUrl, [{ url: mediaUrl, title: 'Top', browser_context: { tab_id: 99 } }]), sender(e)),
      e.message(report(frameUrl, [{ url: mediaUrl, title: 'Frame', observed_content_type: 'video/mp4' }]), sender(e, 7, frameUrl)),
    ]);
    await new Promise(setImmediate);
    const candidates = e.storage.detected_videos_0;
    assert.equal(candidates.length, 2, 'Distinct referer/frame scopes must not collapse');
    assert.equal(candidates[1].page_url, pageUrl);
    assert.equal(candidates[1].frame_url, frameUrl);
    assert.equal(candidates[1].media_url, mediaUrl);
    assert.equal(candidates[0].browser_context.tab_id, 0);
    assert.equal(candidates[1].browser_context.frame_id, 7);
    assert.equal(candidates[1].browser_context.cookie_store_id, 'firefox-container-2');
    assert.equal(e.calls.length, 0, 'Discovery must not wake the engine');
  });

  test(browser + ': request observation requires both preference and actual optional grants', async () => {
    const e = environment(browser, { requestObservationEnabled: true }, { tabUrl: pageUrl });
    assert.ok(e.context.vidGrabObservation, 'The optional request observer must load in both backgrounds');
    await e.context.vidGrabObservation.configure();
    const permission = await e.context.vidGrabAPI.getPermissions(mediaUrl);
    assert.equal(permission.request_observation_enabled, true);
    assert.equal(permission.request_observation_granted, false);
    assert.equal(e.listeners['headers-received']?.length || 0, 0);
    e.engine.permissions = ['webRequest', 'webNavigation'];
    e.storage.requestObservationEnabled = false;
    await e.context.vidGrabObservation.configure();
    assert.equal(e.listeners['headers-received']?.length || 0, 0);
    const manifest = JSON.parse(readFileSync('extension/' + (browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json')));
    assert.ok(manifest.optional_permissions.includes('webRequest'));
    assert.ok(manifest.optional_permissions.includes('webNavigation'));
  });

  test(browser + ': response metadata discovers extensionless HLS/DASH without body or credential capture', async () => {
    const e = environment(browser, { requestObservationEnabled: true }, { tabUrl: pageUrl, permissions: ['webRequest', 'webNavigation'] });
    assert.ok(e.context.vidGrabObservation);
    await e.context.vidGrabObservation.configure();
    assert.equal(e.listeners['headers-received'].length, 1);
    for (const [suffix, mime] of [['hls', 'application/vnd.apple.mpegurl'], ['dash', 'application/dash+xml']]) {
      await e.listeners['headers-received'][0]({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200,
        type: 'xmlhttprequest', url: 'https://cdn.fixture.invalid/manifest?sig=' + suffix + '%2F+bytes',
        responseHeaders: [{ name: 'Content-Type', value: mime }, { name: 'Set-Cookie', value: 'observed-cookie-secret' }],
        requestHeaders: [{ name: 'Authorization', value: 'observed-auth-secret' }] });
    }
    assert.deepEqual(e.storage.detected_videos_0.map(item => item.kind), ['hls', 'dash']);
    assert.equal(e.storage.detected_videos_0[0].frame_url, frameUrl);
    assert.equal(e.storage.detected_videos_0[0].page_url, pageUrl);
    assert.equal(e.storage.detected_videos_0[0].source, 'request');
    assert.ok(e.storage.detected_videos_0[0].media_url.endsWith('%2F+bytes'));
    assert.ok(!JSON.stringify(e.storage).includes('observed-cookie-secret'));
    assert.ok(!JSON.stringify(e.storage).includes('observed-auth-secret'));
    assert.equal(e.calls.length, 0);
    assert.equal(e.cookieReads.length, 0);
  });

  test(browser + ': request and DOM evidence coalesce and revocation clears only request evidence', async () => {
    const e = environment(browser, { requestObservationEnabled: true }, { tabUrl: pageUrl, permissions: ['webRequest', 'webNavigation'] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const identity = e.storage.detected_videos_0[0].id;
    assert.ok(e.context.vidGrabObservation);
    await e.context.vidGrabObservation.configure();
    const observe = e.listeners['headers-received'][0];
    await observe({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200, type: 'media', url: mediaUrl,
      responseHeaders: [{ name: 'Content-Type', value: 'video/mp4' }] });
    await observe({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200, type: 'xmlhttprequest', url: 'https://cdn.fixture.invalid/stream.m3u8' });
    assert.equal(e.storage.detected_videos_0.length, 2);
    assert.equal(e.storage.detected_videos_0[0].id, identity);
    assert.equal(e.storage.detected_videos_0[0].observed_content_type, 'video/mp4');
    e.engine.permissions = [];
    await e.context.vidGrabObservation.configure();
    assert.equal(e.listeners['headers-received'].length, 0);
    assert.equal(e.storage.detected_videos_0.length, 1);
    assert.equal(e.storage.detected_videos_0[0].id, identity);
    assert.equal(e.storage.detected_videos_0[0].observed_content_type, null);
  });

  test(browser + ': observation excludes fragments, rejected responses, stale documents and missing site access', async () => {
    const e = environment(browser, { requestObservationEnabled: true }, { tabUrl: pageUrl, permissions: ['webRequest', 'webNavigation'] });
    assert.ok(e.context.vidGrabObservation);
    await e.context.vidGrabObservation.configure();
    const observe = e.listeners['headers-received'][0];
    for (const details of [{ url: 'https://cdn.fixture.invalid/fragment.ts', responseHeaders: [{ name: 'Content-Type', value: 'video/mp2t' }] },
      { url: 'https://cdn.fixture.invalid/fragment.m4s', responseHeaders: [{ name: 'Content-Type', value: 'video/mp4' }] },
      { url: mediaUrl, statusCode: 403 }, { url: mediaUrl, documentId: 'old-document' }, { url: 'https://user:secret@cdn.fixture.invalid/video.mp4' }]) {
      await observe({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200, type: 'media', ...details });
    }
    assert.deepEqual(e.storage.detected_videos_0 || [], []);
    e.engine.origins = ['https://cdn.fixture.invalid/*'];
    await observe({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200, type: 'media', url: mediaUrl });
    assert.deepEqual(e.storage.detected_videos_0 || [], []);
  });

  test(browser + ': committed child navigation removes that frame and keeps the parent', async () => {
    const e = environment(browser, { requestObservationEnabled: true }, { tabUrl: pageUrl, permissions: ['webRequest', 'webNavigation'] });
    await e.message(report(pageUrl, [{ url: mediaUrl }]), sender(e));
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    assert.ok(e.context.vidGrabObservation);
    await e.context.vidGrabObservation.configure();
    await e.listeners['frame-committed'][0]({ tabId: 0, frameId: 7, url: 'https://frame.fixture.invalid/next' });
    assert.equal(e.storage.detected_videos_0.length, 1);
    assert.equal(e.storage.detected_videos_0[0].frame_url, pageUrl);
  });

  test(browser + ': selected frame probe/enqueue share referer-only context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: false }, { tabUrl: pageUrl, cookies: [] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    await e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: candidate.id });
    await e.context.vidGrabAPI.startDownload(mediaUrl, null, null, null, {}, { candidate_id: candidate.id });
    const auth = e.calls.find(item => item.params.operation === 'auth.create')?.params.params;
    assert.ok(auth, 'A selected Referer context must exist without cookies');
    assert.equal(auth.referer, new URL(frameUrl).origin + '/');
    assert.deepEqual(auth.cookies, []);
    const probe = e.calls.find(item => item.params.operation === 'probes.create').params.params;
    const enqueue = e.calls.find(item => item.params.operation === 'downloads.enqueue').params.params;
    assert.equal(probe.auth_ref, enqueue.auth_ref);
    assert.equal(e.cookieReads.length, 0);
    assert.ok(!JSON.stringify(e.storage).includes('volatile-reference'));
  });

  test(browser + ': cookie lookup is bound to the selected tab container', async () => {
    const target = 'https://fixture.invalid/media.mp4';
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl, storeId: 'firefox-container-2' });
    await e.message(report(frameUrl, [{ url: target }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    await e.context.vidGrabAPI.probeUrl(target, { candidate_id: candidate.id });
    assert.equal(e.cookieReads[0].storeId, 'firefox-container-2');
    assert.equal(e.calls.find(item => item.params.operation === 'auth.create').params.params.referer, new URL(frameUrl).origin + '/');
  });

  test(browser + ': a removed candidate cannot reuse its old scoped context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl });
    await e.message(report(frameUrl, [{ url }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    await e.context.vidGrabAPI.probeUrl(url, { candidate_id: candidate.id });
    await e.message(report(frameUrl, [], 2), sender(e, 7, frameUrl));
    const before = e.cookieReads.length;
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, null, null, null, {}, { candidate_id: candidate.id }),
      error => error.code === 'BROWSER_CONTEXT_EXPIRED');
    assert.equal(e.cookieReads.length, before);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
  });

  test(browser + ': a removed live frame cannot reuse stored cookies or a cached context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    await e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: candidate.id });
    e.engine.liveFrames = { 7: null };
    await assert.rejects(e.context.vidGrabAPI.startDownload(mediaUrl, null, null, null, {}, { candidate_id: candidate.id }),
      error => error.code === 'BROWSER_CONTEXT_EXPIRED');
    assert.equal(e.cookieReads.length, 1);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
  });

  test(browser + ': replacing the frame document at the same URL expires a selection', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    e.engine.liveFrames = { 7: { frame_url: frameUrl, document_token: 'new-document', referrer_policy: 'origin' } };
    await assert.rejects(e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: candidate.id }),
      error => error.code === 'BROWSER_CONTEXT_EXPIRED');
    assert.equal(e.cookieReads.length, 0);
  });

  test(browser + ': source permission revocation blocks selected frame credentials', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl, origins: ['https://cdn.fixture.invalid/*'] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    await assert.rejects(e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: e.storage.detected_videos_0[0].id }),
      error => error.code === 'BROWSER_CONNECTION_UNAVAILABLE');
    assert.equal(e.cookieReads.length, 0);
  });

  test(browser + ': a changed restrictive referrer policy invalidates a cached context', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    await e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: candidate.id });
    e.engine.liveFrames = { 7: { frame_url: frameUrl, document_token: 'fixture-document', referrer_policy: 'no-referrer' } };
    await e.context.vidGrabAPI.startDownload(mediaUrl, null, null, null, {}, { candidate_id: candidate.id });
    const contexts = e.calls.filter(item => item.params.operation === 'auth.create');
    assert.equal(contexts.length, 2);
    assert.equal(contexts[1].params.params.referer ?? null, null);
  });

  test(browser + ': partitioned cookies never become unpartitioned worker cookies', async () => {
    const ordinary = { domain: '.fixture.invalid', path: '/', name: 'session', value: 'ordinary', secure: true };
    const e = environment(browser, { cookieForwardingEnabled: true }, { tabUrl: pageUrl, cookies: [ordinary,
      { ...ordinary, value: 'partitioned-secret', partitionKey: { topLevelSite: 'https://other.invalid' } },
      { ...ordinary, value: 'first-party-secret', firstPartyDomain: 'other.invalid' }] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    await e.context.vidGrabAPI.probeUrl(mediaUrl, { candidate_id: e.storage.detected_videos_0[0].id });
    const auth = e.calls.find(item => item.params.operation === 'auth.create').params.params;
    assert.equal(auth.cookies.length, 1);
    assert.equal(auth.cookies[0].value, 'ordinary');
    assert.ok(!JSON.stringify(e.calls).includes('partitioned-secret'));
    assert.ok(!JSON.stringify(e.calls).includes('first-party-secret'));
  });

  test(browser + ': manual contexts use the active cookie store and cannot reuse another container', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { storeId: 'container-one' });
    await e.context.vidGrabAPI.probeUrl(url);
    assert.equal(e.cookieReads[0].storeId, 'container-one');
    e.engine.storeId = 'container-two';
    await e.context.vidGrabAPI.startDownload(url);
    assert.equal(e.cookieReads[1].storeId, 'container-two');
    assert.equal(e.calls.filter(item => item.params.operation === 'auth.create').length, 2);
  });

  test(browser + ': empty reports remove one frame while preserving another', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: mediaUrl }]), sender(e));
    await e.message(report(frameUrl, [{ url: 'https://fixture.invalid/audio.mp3' }]), sender(e, 7, frameUrl));
    await e.message(report(pageUrl, [], 2), sender(e));
    await new Promise(setImmediate);
    assert.equal(e.storage.detected_videos_0.length, 1);
    assert.equal(e.storage.detected_videos_0[0].frame_url, frameUrl);
  });

  test(browser + ': removing one candidate keeps other frame scopes and dismissal through rescans', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: mediaUrl }]), sender(e));
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const identity = e.storage.detected_videos_0[0].id;
    const response = await e.message({ type: 'companion.discovery.remove', tab_id: 0, ids: [identity] },
      { id: e.chrome.runtime.id, url: e.chrome.runtime.getURL('popup/popup.html') });
    assert.equal(response.ok, true);
    assert.equal(e.storage.detected_videos_0.length, 1);
    assert.equal(e.storage.detected_videos_0[0].frame_url, frameUrl);
    await e.message(report(pageUrl, [{ url: mediaUrl }], 2), sender(e));
    assert.equal(e.storage.detected_videos_0.length, 1);
  });

  test(browser + ': content scripts cannot dismiss discovered media', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: mediaUrl }]), sender(e));
    const identity = e.storage.detected_videos_0[0].id;
    const response = await e.message({ type: 'companion.discovery.remove', tab_id: 0, ids: [identity] }, sender(e));
    assert.equal(response.error.code, 'UNAUTHORIZED');
    assert.equal(e.storage.detected_videos_0.length, 1);
  });

  test(browser + ': repeated observations keep selection identities and reject late reports', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: mediaUrl }], 3), sender(e));
    await new Promise(setImmediate);
    const identity = e.storage.detected_videos_0[0].id;
    assert.equal(typeof identity, 'string');
    await e.message(report(pageUrl, [{ url: mediaUrl, observed_content_type: 'video/mp4' }], 4), sender(e));
    await e.message(report(pageUrl, [], 2), sender(e));
    await new Promise(setImmediate);
    assert.equal(e.storage.detected_videos_0[0].id, identity);
    assert.equal(e.storage.detected_videos_0[0].observed_content_type, 'video/mp4');
  });

  test(browser + ': navigation clears observations and refuses stale page reports', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: mediaUrl }]), sender(e));
    await new Promise(setImmediate);
    assert.ok(e.listeners['tab-updated']?.length, 'Tab navigation needs a cleanup listener');
    e.engine.tabUrl = 'https://page.fixture.invalid/next';
    await Promise.all(e.listeners['tab-updated'].map(listener => listener(0, { url: e.engine.tabUrl }, { url: e.engine.tabUrl })));
    await e.message(report(pageUrl, [{ url: mediaUrl }], 2), sender(e));
    await new Promise(setImmediate);
    assert.deepEqual(e.storage.detected_videos_0 || [], []);
  });

  test(browser + ': overlay selection hands off an observed scoped candidate', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(frameUrl, [{ url: mediaUrl, title: 'Frame title' }]), sender(e, 7, frameUrl));
    const result = await e.message({ type: 'video_selected', frame_url: frameUrl, media_url: mediaUrl }, sender(e, 7, frameUrl));
    assert.equal(result?.success, true);
    assert.equal(e.storage.pendingVideoCandidate.media_url, mediaUrl);
    assert.equal(e.storage.pendingVideoCandidate.frame_url, frameUrl);
    assert.equal(e.storage.pendingVideoCandidate.browser_context.frame_id, 7);
    const refused = await e.message({ type: 'video_selected', frame_url: frameUrl, media_url: 'https://foreign.invalid/not-observed' }, sender(e, 7, frameUrl));
    assert.equal(refused?.success, false);
    assert.equal(e.calls.length, 0);
  });

  test(browser + ': content pages cannot trigger probes or downloads through legacy aliases', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    for (const type of ['download_request', 'probe_request', 'download_with_options']) {
      const result = await e.message({ type, url: mediaUrl }, sender(e));
      assert.notEqual(result?.success, true);
    }
    await new Promise(setImmediate);
    assert.equal(e.calls.length, 0);
  });

  test(browser + ': malformed media observations never enter browser storage', async () => {
    const e = environment(browser, {}, { tabUrl: pageUrl });
    await e.message(report(pageUrl, [{ url: 'file:///secret' }, { url: 'https://user:secret@fixture.invalid/x' },
      { url: mediaUrl, title: 'Safe', cookies: [{ value: 'injected-cookie' }], headers: { Authorization: 'injected-auth' } }]), sender(e));
    await new Promise(setImmediate);
    assert.equal(e.storage.detected_videos_0.length, 1);
    assert.ok(!JSON.stringify(e.storage).includes('injected-'));
    assert.ok(!JSON.stringify(e.storage).includes('user:secret'));
  });

  test(browser + ': context-menu downloads use app defaults', async () => {
    const e = environment(browser, { defaultFormat: 'old-browser-default' });
    await e.context.vidGrabAPI.startDownload(url, null);
    const request = e.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    assert.equal(request.params.format_id ?? null, null);
    assert.ok(!JSON.stringify(request).includes('old-browser-default'));
  });

  test(browser + ': an explicit fresh public probe bypasses cached metadata at the engine', async () => {
    const e = environment(browser);
    await e.context.vidGrabAPI.probeUrl(url, { fresh: true });
    assert.equal(e.calls.find(item => item.params.operation === 'probes.create').params.params.fresh, true);
  });

  test(browser + ': older engines give upgrade guidance before witnessed media requests', async () => {
    const e = environment(browser, {}, { excludedFeatures: ['probes.fresh'] });
    await assert.rejects(e.context.vidGrabAPI.probeUrl(url),
      error => error.code === 'APP_INCOMPATIBLE' && error.message.includes('Update VidGrab'));
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, 'one', null, null, {}, { probe_id: 'probe-original' }),
      error => error.code === 'APP_INCOMPATIBLE');
    assert.equal(e.calls.filter(item => ['probes.create', 'downloads.enqueue'].includes(item.params?.operation)).length, 0);
  });

  test(browser + ': selected format submissions retain their witness and fence lost-reply reconstruction', async () => {
    const e = environment(browser, {}, { lost: true });
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, 'one', null, null, {}, { probe_id: 'probe-original' }),
      error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    const original = e.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    assert.equal(original.params.probe_id, 'probe-original');
    e.engine.lost = false;
    const resumed = environment(browser, e.storage, e.engine);
    await assert.rejects(resumed.context.vidGrabAPI.startDownload(url, 'one', null, null, {}, { probe_id: 'probe-new' }),
      error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    await assert.rejects(resumed.context.vidGrabAPI.retryPending(), error => error.details.replay_available === false);
    assert.equal(resumed.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
    assert.equal(resumed.engine.jobs.length, 1);
    assert.ok(!JSON.stringify(resumed.storage).includes('probe-original'));
  });

  test(browser + ': a waiting header job can refresh its explicitly selected frame context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: false }, { tabUrl: pageUrl, cookies: [] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    const job = { id: 'header-job', url: mediaUrl, revision: 2, status: 'waiting_for_auth', error_code: 'BROWSER_CONTEXT_REQUIRED' };
    const accepted = await e.context.vidGrabAPI.authenticate(job, { candidate_id: candidate.id });
    assert.equal(accepted.accepted_status, 'queued');
    const auth = e.calls.find(item => item.params.operation === 'auth.create').params.params;
    assert.equal(auth.job_id, job.id);
    assert.equal(auth.referer, new URL(frameUrl).origin + '/');
    assert.deepEqual(auth.cookies, []);
    assert.equal(e.cookieReads.length, 0);
  });

  test(browser + ': a retry can refresh its explicitly selected header-only frame', async () => {
    const e = environment(browser, { cookieForwardingEnabled: false }, { tabUrl: pageUrl, cookies: [] });
    await e.message(report(frameUrl, [{ url: mediaUrl }]), sender(e, 7, frameUrl));
    const candidate = e.storage.detected_videos_0[0];
    const job = { id: 'header-job', url: mediaUrl, revision: 2, status: 'failed' };
    await e.context.vidGrabAPI.retryDownload(job, { candidate_id: candidate.id });
    const auth = e.calls.find(item => item.params.operation === 'auth.create').params.params;
    assert.equal(auth.job_id, job.id);
    assert.equal(auth.referer, new URL(frameUrl).origin + '/');
    assert.equal(e.calls.find(item => item.params.operation === 'downloads.action').params.params.auth_ref, 'volatile-reference');
    assert.equal(e.cookieReads.length, 0);
  });
  test(browser + ': content scripts cannot send control mutations', async () => {
    const e = environment(browser);
    const before = e.calls.length;
    const response = await e.message({ type: 'companion.request', request: { protocol_version: 1, request_id: 'content', operation: 'settings.patch',
      params: { expected_revision: 0, patch: { max_concurrent: 8 } }, idempotency_key: 'content' } }, { id: e.chrome.runtime.id, tab: { id: 0 }, url });
    assert.equal(response?.ok, false);
    assert.equal(response?.error?.code, 'UNAUTHORIZED');
    assert.equal(e.calls.length, before);
  });
  test(browser + ': actual permissions remain separate from forwarding preference', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { permissions: ['nativeMessaging'], origins: [] });
    const permissions = await e.context.vidGrabAPI.getPermissions(url);
    assert.equal(permissions.cookie_forwarding_enabled, true);
    assert.equal(permissions.cookies_granted, false);
    assert.equal(permissions.site_granted, false);
    await assert.rejects(e.context.vidGrabAPI.probeUrl(url), error => error.code === 'BROWSER_CONNECTION_UNAVAILABLE');
    assert.equal(e.cookieReads.length, 0);
  });
  test(browser + ': probe and enqueue share a volatile scoped context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true });
    await e.context.vidGrabAPI.probeUrl(url);
    await e.context.vidGrabAPI.startDownload(url, null);
    const auth = e.calls.find(item => item.params.operation === 'auth.create').params.params;
    assert.equal(auth.cookies[0].value, 'cookie-secret');
    assert.ok(!Object.hasOwn(auth.cookies[0], 'storeId'));
    const probe = e.calls.find(item => item.params.operation === 'probes.create').params.params;
    const enqueue = e.calls.find(item => item.params.operation === 'downloads.enqueue').params.params;
    assert.equal(probe.auth_ref, enqueue.auth_ref);
    assert.equal(e.cookieReads.length, 1);
    assert.ok(!JSON.stringify(e.storage).includes('cookie-secret'));
    assert.ok(!JSON.stringify(e.storage).includes('volatile-reference'));
    assert.ok(!Object.hasOwn(enqueue.options || {}, 'cookies'));
  });
  test(browser + ': unresolved keys survive background restart without automatic replay', async () => {
    const e = environment(browser, {}, { lost: true });
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, null), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    const original = e.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    const resumed = environment(browser, e.storage, e.engine);
    resumed.engine.lost = false;
    await resumed.context.vidGrabAPI.getStatus();
    assert.equal(resumed.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
    await assert.rejects(resumed.context.vidGrabAPI.startDownload(url, null), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal(resumed.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
    await resumed.context.vidGrabAPI.retryPending();
    const replay = resumed.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    assert.equal(replay.idempotency_key, original.idempotency_key);
    assert.equal(resumed.engine.jobs.length, 1);
    assert.ok(!JSON.stringify(resumed.storage).includes(url));
  });
  test(browser + ': acknowledged work survives failure to retire its browser marker', async () => {
    const e = environment(browser, {}, { failStorageWrite: 2 });
    const accepted = await e.context.vidGrabAPI.startDownload(url, null);
    assert.equal(accepted.job_id, e.engine.jobs[0].id);
    assert.equal(e.context.vidGrabAPI.session.pending, null);
    assert.equal((await e.context.vidGrabAPI.getPending()).length, 1);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 1);
  });
  test(browser + ': authenticated reconstruction cannot replay a different volatile context', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { lost: true, reference: 'original-context' });
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, null), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    const original = e.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    e.engine.lost = false; e.engine.reference = 'replacement-context';
    const resumed = environment(browser, e.storage, e.engine);
    await assert.rejects(resumed.context.vidGrabAPI.startDownload(url, null), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    await assert.rejects(resumed.context.vidGrabAPI.retryPending(), error => error.code === 'REQUEST_OUTCOME_UNKNOWN'
      && error.details.replay_available === false);
    assert.equal(resumed.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 0);
    assert.equal(resumed.engine.jobs.length, 1);
    assert.equal((await resumed.context.vidGrabAPI.getPending()).length, 1);
    await resumed.context.vidGrabAPI.dismissPending(original.idempotency_key);
    assert.equal((await resumed.context.vidGrabAPI.getPending()).length, 0);
    assert.equal(resumed.context.vidGrabAPI.session.pending, null);
    assert.ok(!JSON.stringify(resumed.storage).includes(url));
    assert.ok(!JSON.stringify(resumed.storage).includes('context'));
    assert.ok(!JSON.stringify(resumed.storage).includes('cookie-secret'));
  });
  test(browser + ': only an owned extension page may dismiss a reviewed notice', async () => {
    const e = environment(browser, {}, { lost: true });
    await assert.rejects(e.context.vidGrabAPI.startDownload(url, null));
    const key = e.calls.find(item => item.params.operation === 'downloads.enqueue').params.idempotency_key;
    const denied = await e.message({ type: 'companion.dismiss', key }, { id: e.chrome.runtime.id, tab: { id: 0 }, url });
    assert.equal(denied.error.code, 'UNAUTHORIZED');
    assert.equal((await e.context.vidGrabAPI.getPending()).length, 1);
    const accepted = await e.message({ type: 'companion.dismiss', key }, { id: e.chrome.runtime.id, url: e.chrome.runtime.getURL('options/options.html') });
    assert.equal(accepted.ok, true);
    assert.equal((await e.context.vidGrabAPI.getPending()).length, 0);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 1);
  });
  test(browser + ': reviewed notices accept punctuation in contract identifiers', async () => {
    const e = environment(browser, {}, { lost: true });
    const key = 'review[1]/+?';
    const sender = { id: e.chrome.runtime.id, url: e.chrome.runtime.getURL('options/options.html') };
    const request = { protocol_version: 1, request_id: 'printable-key', operation: 'downloads.enqueue',
      params: { url }, idempotency_key: key };
    const lost = await e.message({ type: 'companion.request', request }, sender);
    assert.equal(lost.error.code, 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal((await e.context.vidGrabAPI.getPending())[0].key, key);
    const invalid = await e.message({ type: 'companion.dismiss', key: 'bad\nkey' }, sender);
    assert.equal(invalid.error.code, 'BAD_REQUEST');
    assert.equal((await e.context.vidGrabAPI.getPending()).length, 1);
    const dismissed = await e.message({ type: 'companion.dismiss', key }, sender);
    assert.equal(dismissed.ok, true);
    assert.equal((await e.context.vidGrabAPI.getPending()).length, 0);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.enqueue').length, 1);
  });
  test(browser + ': retries share a fresh job-scoped context when forwarding is enabled', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { reference: 'enqueue-context' });
    await e.context.vidGrabAPI.startDownload(url);
    const job = e.engine.jobs[0];
    e.engine.reference = 'fresh-job-context';
    await e.context.vidGrabAPI.retryDownload(job);
    const auth = e.calls.filter(item => item.params.operation === 'auth.create').at(-1).params.params;
    assert.equal(auth.job_id, job.id);
    assert.equal(auth.url, job.url);
    const retry = e.calls.find(item => item.params.operation === 'downloads.action').params.params;
    assert.equal(retry.auth_ref, 'fresh-job-context');
    assert.equal(e.cookieReads.length, 2);
    assert.ok(!JSON.stringify(e.storage).includes('fresh-job-context'));
    assert.ok(!JSON.stringify(e.storage).includes('cookie-secret'));
  });
  test(browser + ': an unresolved request blocks retry before reading more cookies', async () => {
    const e = environment(browser, { cookieForwardingEnabled: true }, { lost: true });
    await assert.rejects(e.context.vidGrabAPI.startDownload(url));
    const before = e.cookieReads.length;
    await assert.rejects(e.context.vidGrabAPI.retryDownload(e.engine.jobs[0]), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal(e.cookieReads.length, before);
    assert.equal(e.calls.filter(item => item.params.operation === 'downloads.action').length, 0);
  });
  test(browser + ': fresh request-observation grants track Chrome session restart requirement without automatic reload or replay', async () => {
    const manifest = JSON.parse(readFileSync('extension/' + (browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json'), 'utf8'));
    assert.ok(manifest.optional_permissions.includes('webRequest'));
    assert.ok(manifest.optional_permissions.includes('webNavigation'));
    assert.ok(!manifest.permissions.includes('webRequest'));
    assert.ok(!manifest.permissions.includes('webNavigation'));

    // Fresh session where grants are absent -> not restart-required yet.
    const e = environment(browser, { requestObservationEnabled: false }, { tabUrl: pageUrl, permissions: ['cookies', 'nativeMessaging'] });
    await e.context.vidGrabObservation.configure();
    let permission = await e.context.vidGrabAPI.getPermissions(mediaUrl);
    assert.equal(permission.request_observation_restart_required, false);

    // Fresh grant transition false -> true inside the same session.
    e.engine.permissions = ['cookies', 'nativeMessaging', 'webRequest', 'webNavigation'];
    e.storage.requestObservationEnabled = true;
    for (const listener of e.listeners['permissions-added'] || []) await listener({ permissions: ['webRequest', 'webNavigation'] });
    for (const listener of e.listeners.storage || []) await listener({ requestObservationEnabled: { oldValue: false, newValue: true } }, 'local');
    await e.context.vidGrabObservation.configure();
    permission = await e.context.vidGrabAPI.getPermissions(mediaUrl);
    assert.equal(permission.request_observation_granted, true);
    assert.equal(permission.request_observation_restart_required, browser === 'chrome');
    assert.equal(e.calls.length, 0, 'Fresh-grant tracking must never invoke companion mutations');

    // Service worker wake/restart in the same browser session preserves pending restart state.
    const sameSession = environment(browser, e.storage, e.engine, e.sessionStorage);
    await sameSession.context.vidGrabObservation.configure();
    permission = await sameSession.context.vidGrabAPI.getPermissions(mediaUrl);
    assert.equal(permission.request_observation_restart_required, browser === 'chrome');

    // Toggling preference off hides notice; turning back on in the same session restores it.
    sameSession.storage.requestObservationEnabled = false;
    for (const listener of sameSession.listeners.storage || []) await listener({ requestObservationEnabled: { oldValue: true, newValue: false } }, 'local');
    await sameSession.context.vidGrabObservation.configure();
    assert.equal((await sameSession.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, false);
    sameSession.storage.requestObservationEnabled = true;
    for (const listener of sameSession.listeners.storage || []) await listener({ requestObservationEnabled: { oldValue: false, newValue: true } }, 'local');
    await sameSession.context.vidGrabObservation.configure();
    assert.equal((await sameSession.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, browser === 'chrome');

    // Receiving an actual onHeadersReceived observation clears the restart notice (self-heals).
    await sameSession.listeners['headers-received'][0]({ tabId: 0, frameId: 7, documentId: 'document-7', statusCode: 200,
      type: 'xmlhttprequest', url: 'https://cdn.fixture.invalid/manifest.m3u8',
      responseHeaders: [{ name: 'Content-Type', value: 'application/vnd.apple.mpegurl' }] });
    assert.equal((await sameSession.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, false);

    // Revoking and re-granting in Chrome marks restart required again; full browser restart (fresh empty sessionStorage) clears it.
    sameSession.engine.permissions = ['cookies', 'nativeMessaging'];
    for (const listener of sameSession.listeners['permissions-removed'] || []) await listener({ permissions: ['webRequest', 'webNavigation'] });
    await sameSession.context.vidGrabObservation.configure();
    assert.equal((await sameSession.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, false);
    sameSession.engine.permissions = ['cookies', 'nativeMessaging', 'webRequest', 'webNavigation'];
    for (const listener of sameSession.listeners['permissions-added'] || []) await listener({ permissions: ['webRequest', 'webNavigation'] });
    await sameSession.context.vidGrabObservation.configure();
    assert.equal((await sameSession.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, browser === 'chrome');

    // Simulated full browser restart on same granted profile -> fresh empty chrome.storage.session.
    const restarted = environment(browser, plain(sameSession.storage), { tabUrl: pageUrl, permissions: ['cookies', 'nativeMessaging', 'webRequest', 'webNavigation'] }, {});
    await restarted.context.vidGrabObservation.configure();
    assert.equal((await restarted.context.vidGrabAPI.getPermissions(mediaUrl)).request_observation_restart_required, false);
  });
}
