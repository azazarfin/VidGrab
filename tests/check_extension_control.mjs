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

function environment(browser, storage = {}, engine = {}) {
  const browserId = browser === 'firefox' ? 'vidgrab@azaz.local' : id;
  const base = browser === 'firefox' ? 'moz-extension://fixture-extension/' : 'chrome-extension://' + id + '/';
  const listeners = {};
  const event = name => ({ addListener(callback) { (listeners[name] ||= []).push(callback); } });
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
          paired_loopback: false, features: ['events.snapshots', 'settings.revisions', 'auth.scoped', 'downloads.source'], component_versions: { ffmpeg: '8', 'yt_dlp': '2026.6.9' } };
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
        } else if (request.operation === 'downloads.action') {
          data = { job_id: request.params.job_id, action: request.params.action,
            accepted_revision: request.params.expected_revision + 1,
            accepted_status: request.params.action === 'cancel' ? 'cancelled' : 'queued' };
        } else throw new Error('Unexpected native operation: ' + request.operation);
        callback({ spike_version: 1, id: outer.id, ok: true, data: outer.op === 'v1.request'
          ? { protocol_version: 1, request_id: request.request_id, ok: true, data } : data });
      } },
    storage: { onChanged: event('storage'), local: {
      get(keys, callback) { callback(keys === null ? plain(storage) : Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(key => [key, storage[key]]))); },
      set(values, callback) {
        if (++storageWrites === engine.failStorageWrite) {
          chrome.runtime.lastError = {message: 'Fixture storage failure'};
          callback?.(); chrome.runtime.lastError = null; return;
        }
        Object.assign(storage, plain(values)); callback?.();
      },
      remove(keys, callback) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete storage[key]; callback?.(); },
    } },
    permissions: { getAll(callback) { callback({ permissions: engine.permissions ?? ['cookies', 'nativeMessaging', 'notifications'], origins: engine.origins ?? ['<all_urls>'] }); }, onAdded: event('permissions'), onRemoved: event('permissions') },
    cookies: { getAll(filter, callback) { cookieReads.push(filter); callback([{ domain: '.fixture.invalid', path: '/', name: 'session', value: 'cookie-secret', secure: true,
      httpOnly: true, storeId: 'ignored', expirationDate: Date.now() / 1000 + 3600 }]); } },
    tabs: { onRemoved: event('removed'), query(_query, callback) { callback([{ id: 0, url }]); } },
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
  return { context, chrome, storage, engine, calls, cookieReads, listeners, notifications, message };
}

for (const browser of ['chrome', 'firefox']) {
  test(browser + ': context-menu downloads use app defaults', async () => {
    const e = environment(browser, { defaultFormat: 'old-browser-default' });
    await e.context.vidGrabAPI.startDownload(url, null);
    const request = e.calls.find(item => item.params.operation === 'downloads.enqueue').params;
    assert.equal(request.params.format_id ?? null, null);
    assert.ok(!JSON.stringify(request).includes('old-browser-default'));
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
}
