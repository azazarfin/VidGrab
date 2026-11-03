// Runs both classic-script backgrounds without browser/network access.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import vm from 'node:vm';

const extension = resolve('extension');
const event = () => ({ addListener() {}, removeListener() {} });
for (const browser of ['chrome', 'firefox']) {
  const manifest = JSON.parse(readFileSync(resolve(extension, browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json')));
  assert.ok(manifest.permissions.includes('nativeMessaging'), `${browser} must grant native messaging`);
  assert.ok(manifest.permissions.includes('alarms'), `${browser} uses the alarms API`);
  const nativeCalls = [];
  const chrome = {
    runtime: {
      id: 'test-extension', lastError: null, onInstalled: event(), onMessage: event(),
      getURL: path => `chrome-extension://test-extension/${path}`,
      sendNativeMessage(host, message, callback) {
        nativeCalls.push({ host, message });
        callback({ spike_version: 1, id: message.id, ok: true, data: { state: 'ready' } });
      },
    },
    tabs: { onRemoved: event() },
    storage: { onChanged: event(), local: { get(_keys, callback) { callback({}); }, set() {}, remove() {} } },
    contextMenus: { create() {}, onClicked: event() },
    notifications: { create() {}, clear() {}, onButtonClicked: event(), onClicked: event() },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
    alarms: { create() {}, onAlarm: event() },
  };
  const context = vm.createContext({
    chrome, console, EventTarget, Event, URL,
    CustomEvent: class extends Event { constructor(name, opts) { super(name); this.detail = opts?.detail; } },
    crypto: { randomUUID: () => 'bootstrap-request' },
    WebSocket: class { static OPEN = 1; constructor() { this.readyState = 0; } close() {} },
    fetch() { throw new Error('Network is forbidden'); },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {},
  });
  context.self = context;
  const load = path => vm.runInContext(readFileSync(resolve(extension, path), 'utf8'), context, { filename: path, timeout: 1000 });
  if (browser === 'chrome') {
    context.importScripts = (...paths) => paths.forEach(path => load(`background/${path}`));
    load(manifest.background.service_worker);
    assert.ok(manifest.key, 'Chrome needs a stable development public key');
  } else {
    manifest.background.scripts.forEach(load);
    assert.equal(manifest.browser_specific_settings.gecko.id, 'vidgrab@azaz.local');
  }
  assert.ok(context.vidGrabAPI, `${browser} must load its shared API`);
  assert.ok(context.vidGrabNative, `${browser} must load its native bridge`);
  assert.deepEqual(JSON.parse(JSON.stringify(await context.vidGrabNative.send('status'))), { state: 'ready' });
  assert.equal(nativeCalls[0].host, 'com.vidgrab.companion');
  assert.equal(nativeCalls[0].message.spike_version, 1);
  assert.equal(nativeCalls[0].message.op, 'status');
  await assert.rejects(context.vidGrabNative.send('exec'), /Unsupported/);
  assert.equal(nativeCalls.length, 1, 'Unrecognized operations must not reach the host');
  console.log(`${browser}: background imports, permissions, stable identity and native request passed`);
}
