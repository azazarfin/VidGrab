/** Optional, non-blocking HTTP response metadata. Never read bodies or credentials. */
(function (global) {
  'use strict';
  const media = global.vidGrabMedia, pending = new Map();
  const SESSION_KEY = 'vidgrabRequestObservationSession';
  const isChromium = () => !chrome.runtime?.getURL?.('').startsWith('moz-extension://');
  let enabled = null, configuring = Promise.resolve(), sessionQueue = Promise.resolve(), memorySession = null;

  function readSession() {
    if (!chrome.storage?.session?.get) return Promise.resolve(memorySession);
    return new Promise(resolve => chrome.storage.session.get([SESSION_KEY], value =>
      resolve(chrome.runtime.lastError ? memorySession : value?.[SESSION_KEY] || null)));
  }

  function writeSession(state) {
    memorySession = state;
    if (!chrome.storage?.session?.set) return Promise.resolve();
    return new Promise(resolve => chrome.storage.session.set({ [SESSION_KEY]: state }, () => resolve()));
  }

  async function syncSessionState(granted) {
    const current = await readSession();
    let next;
    if (!isChromium()) {
      next = { wasUngranted: !granted, pendingRestart: false, delivered: false };
    } else if (!current) {
      next = { wasUngranted: !granted, pendingRestart: false, delivered: false };
    } else if (!granted) {
      next = { wasUngranted: true, pendingRestart: false, delivered: false };
    } else if (current.delivered) {
      next = { wasUngranted: false, pendingRestart: false, delivered: true };
    } else if (current.wasUngranted || current.pendingRestart) {
      next = { wasUngranted: false, pendingRestart: true, delivered: false };
    } else {
      next = { wasUngranted: false, pendingRestart: false, delivered: false };
    }
    if (!current || current.wasUngranted !== next.wasUngranted || current.pendingRestart !== next.pendingRestart || current.delivered !== next.delivered) {
      await writeSession(next);
    }
    return next;
  }

  function syncSession(granted) {
    const run = sessionQueue.then(() => syncSessionState(granted), () => syncSessionState(granted));
    sessionQueue = run.catch(() => {});
    return run;
  }

  function markDelivered() {
    const run = sessionQueue.then(async () => {
      const current = await readSession();
      if (!isChromium() || (current && current.delivered && !current.pendingRestart)) return;
      await writeSession({ wasUngranted: false, pendingRestart: false, delivered: true });
    }, () => {});
    sessionQueue = run.catch(() => {});
    return run;
  }

  async function observe(details) {
    if (!enabled || !Number.isInteger(details.tabId) || details.tabId < 0 || !Number.isInteger(details.frameId)
        || details.frameId < 0 || !media.httpUrl(details.url)) return;
    await markDelivered();
    if (details.statusCode && (details.statusCode < 200 || details.statusCode >= 300)) return;
    const mime = media.contentType(details.responseHeaders?.find(item => item.name.toLowerCase() === 'content-type')?.value);
    const path = media.httpUrl(details.url).pathname;
    if (mime === 'video/mp2t' || /\.(?:ts|m4s|m4f|cmfv|cmfa)$/iu.test(path) || !media.mediaHint(details.url, mime)) return;
    const identity = JSON.stringify([details.tabId, details.frameId, details.documentId || null, details.url, mime]);
    if (pending.has(identity)) return pending.get(identity);
    if (pending.size >= 128) return;
    const operation = (async () => {
      const permissions = await global.vidGrabAPI.getPermissions(details.url);
      if (!enabled || !permissions.request_observation_enabled || !permissions.request_observation_granted || !permissions.site_granted) return;
      const [frame, tab] = await Promise.all([
        new Promise(resolve => chrome.webNavigation.getFrame({ tabId: details.tabId, frameId: details.frameId }, value => resolve(chrome.runtime.lastError ? null : value))),
        new Promise(resolve => chrome.tabs.get(details.tabId, value => resolve(chrome.runtime.lastError ? null : value))),
      ]);
      if (!frame || !tab || !media.httpUrl(frame.url) || !media.httpUrl(tab.url)
          || (details.documentId && frame.documentId && details.documentId !== frame.documentId)
          || [frame.url, tab.url].some(url => !permissions.origins.some(pattern => global.vidGrabOriginGranted(pattern, new URL(url))))) return;
      await global.vidGrabDiscovery.observe({ tabId: details.tabId, frameId: details.frameId, url: details.url,
        responseHeaders: mime ? [{ name: 'Content-Type', value: mime }] : [] }, frame, tab);
    })();
    pending.set(identity, operation);
    try { await operation; } finally { pending.delete(identity); }
  }

  const received = details => observe(details).catch(() => {});
  const committed = details => global.vidGrabDiscovery.clearFrame(details.tabId, details.frameId).catch(() => {});
  function configure() {
    configuring = configuring.then(async () => {
      const permissions = await global.vidGrabAPI.getPermissions();
      const next = permissions.request_observation_enabled && permissions.request_observation_granted
        && Boolean(chrome.webRequest?.onHeadersReceived && chrome.webNavigation?.getFrame);
      if (next === enabled) return;
      enabled = next;
      if (enabled) {
        chrome.webRequest.onHeadersReceived.addListener(received, { urls: ['http://*/*', 'https://*/*'] }, ['responseHeaders']);
        chrome.webNavigation.onCommitted.addListener(committed);
        chrome.webNavigation.onHistoryStateUpdated?.addListener(committed);
      } else {
        chrome.webRequest?.onHeadersReceived?.removeListener(received);
        chrome.webNavigation?.onCommitted?.removeListener(committed);
        chrome.webNavigation?.onHistoryStateUpdated?.removeListener(committed);
        await Promise.allSettled([...pending.values()]);
        await global.vidGrabDiscovery.clearRequests();
      }
    }).catch(() => {});
    return configuring;
  }

  chrome.storage.onChanged.addListener((changes, area) => { if (area === 'local' && changes.requestObservationEnabled) void configure(); });
  chrome.permissions?.onAdded?.addListener(() => { void configure(); });
  chrome.permissions?.onRemoved?.addListener(() => { void configure(); });
  global.vidGrabObservation = { configure, syncSession };
  void configure();
})(globalThis);
