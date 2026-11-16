/** Serial tab-local snapshots prevent frames and asynchronous storage from racing. */
(function (global) {
  'use strict';
  const media = global.vidGrabMedia;
  const chains = new Map();
  const key = tabId => 'vidgrab_discovery_' + tabId;
  const videosKey = tabId => 'detected_videos_' + tabId;
  const read = keys => new Promise((resolve, reject) => chrome.storage.local.get(keys, value => {
    if (chrome.runtime.lastError) reject(new Error('Media observations could not be read'));
    else resolve(value);
  }));
  const save = value => new Promise((resolve, reject) => chrome.storage.local.set(value, () => {
    if (chrome.runtime.lastError) reject(new Error('Media observations could not be saved'));
    else resolve();
  }));
  const remove = keys => new Promise(resolve => chrome.storage.local.remove(keys, resolve));

  function serial(tabId, work) {
    const current = (chains.get(tabId) || Promise.resolve()).then(work, work);
    chains.set(tabId, current);
    void current.finally(() => { if (chains.get(tabId) === current) chains.delete(tabId); }).catch(() => {});
    return current;
  }

  async function scope(sender, frameUrl, selectedTab = null) {
    const tab = selectedTab || await new Promise(resolve => chrome.tabs.get(sender.tab.id, value => resolve(chrome.runtime.lastError ? null : value)));
    if (!tab || !media.httpUrl(tab.url) || !media.httpUrl(frameUrl) || frameUrl !== sender.url
        || (sender.tab.url && sender.tab.url !== tab.url)) return null;
    const frameId = Number.isInteger(sender.frameId) && sender.frameId >= 0 ? sender.frameId : 0;
    if (frameId === 0 && frameUrl !== tab.url) return null;
    return { page_url: tab.url, frame_url: frameUrl, browser_context: {
      tab_id: tab.id, frame_id: frameId, document_id: typeof sender.documentId === 'string' ? sender.documentId.slice(0, 128) : null,
      cookie_store_id: typeof tab.cookieStoreId === 'string' ? tab.cookieStoreId.slice(0, 128) : null,
      incognito: tab.incognito === true,
    } };
  }

  function combined(frame) {
    return media.deduplicate([...frame.candidates, ...(frame.requests || [])].map(item => ({ ...item,
      browser_context: { ...item.browser_context, document_token: frame.document_token || null } })));
  }

  async function publish(tabId, state) {
    state.frames = state.frames.slice(-32);
    let remaining = media.MAX_CANDIDATES, dismissals = media.MAX_CANDIDATES;
    for (let position = state.frames.length - 1; position >= 0; position--) {
      const frame = state.frames[position];
      const candidates = combined(frame).slice(0, remaining);
      const allowed = new Set(candidates.map(item => item.media_url));
      frame.candidates = frame.candidates.filter(item => allowed.has(item.media_url));
      frame.requests = (frame.requests || []).filter(item => allowed.has(item.media_url));
      frame.dismissed = (frame.dismissed || []).slice(0, dismissals);
      remaining -= candidates.length; dismissals -= frame.dismissed.length;
    }
    const videos = state.frames.flatMap(combined);
    await save({ [key(tabId)]: state, [videosKey(tabId)]: videos });
    const stored = await read(['pendingVideoCandidate']);
    if (stored.pendingVideoCandidate?.browser_context?.tab_id === tabId
        && !videos.some(item => item.id === stored.pendingVideoCandidate.id)) await remove(['pendingVideoCandidate', 'pendingVideoUrl']);
    chrome.action.setBadgeText({ text: videos.length ? String(videos.length) : '', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#7C6BF0', tabId });
    return videos;
  }

  async function clear(tabId) {
    return serial(tabId, async () => {
      const stored = await read(['pendingVideoCandidate']);
      await remove([key(tabId), videosKey(tabId), ...(stored.pendingVideoCandidate?.browser_context?.tab_id === tabId
        ? ['pendingVideoCandidate', 'pendingVideoUrl'] : [])]);
      chrome.action.setBadgeText({ text: '', tabId });
    });
  }

  async function report(message, sender) {
    return serial(sender.tab.id, async () => {
      const selectedScope = await scope(sender, message.frame_url || sender.url);
      if (!selectedScope) return { success: false };
      const stored = await read([key(sender.tab.id), 'pendingVideoCandidate']);
      const state = stored[key(sender.tab.id)]?.page_url === selectedScope.page_url ? stored[key(sender.tab.id)]
        : { page_url: selectedScope.page_url, frames: [] };
      const frameId = selectedScope.browser_context.frame_id;
      const index = state.frames.findIndex(frame => frame.frame_id === frameId);
      const old = state.frames[index];
      const token = typeof message.document_token === 'string' ? message.document_token.slice(0, 128) : null;
      const documentId = selectedScope.browser_context.document_id;
      const sameDocument = old && old.document_id === documentId && old.document_token === token;
      const sequence = Number.isSafeInteger(message.sequence) && message.sequence >= 0 ? message.sequence : (old?.sequence || 0) + 1;
      if (sameDocument && sequence <= old.sequence) return { success: false };
      const sameFrame = old && old.document_id === documentId && old.frame_url === selectedScope.frame_url
        && (documentId !== null || old.document_token === null || old.document_token === token);
      const previous = new Map((sameDocument || sameFrame ? combined(old) : []).map(item => [item.media_url, item.id]));
      const dismissed = sameDocument ? old.dismissed || [] : [];
      const candidates = media.deduplicate((Array.isArray(message.videos) ? message.videos.slice(0, media.MAX_CANDIDATES) : [])
        .map(item => media.candidate(item, selectedScope))).filter(item => !dismissed.includes(item.media_url));
      for (const item of candidates) item.id = previous.get(item.media_url) || crypto.randomUUID();
      const frame = { frame_id: frameId, document_id: documentId, document_token: token,
        frame_url: selectedScope.frame_url, sequence, candidates, dismissed, requests: sameFrame ? old.requests || [] : [] };
      if (index >= 0) state.frames.splice(index, 1);
      state.frames.push(frame);
      await publish(sender.tab.id, state);
      return { success: true };
    });
  }

  async function select(message, sender) {
    return serial(sender.tab.id, async () => {
      const selectedScope = await scope(sender, message.frame_url || sender.url);
      if (!selectedScope) return { success: false };
      const stored = await read([key(sender.tab.id)]);
      const state = stored[key(sender.tab.id)];
      const candidate = state?.page_url === selectedScope.page_url && state.frames
        .find(frame => frame.frame_id === selectedScope.browser_context.frame_id && frame.frame_url === selectedScope.frame_url)
        && state.frames.flatMap(combined).find(item => item.browser_context.frame_id === selectedScope.browser_context.frame_id
          && item.frame_url === selectedScope.frame_url && item.media_url === message.media_url);
      if (!candidate) return { success: false };
      await save({ pendingVideoCandidate: candidate });
      return { success: true };
    });
  }

  async function removeCandidates(tabId, ids) {
    if (!Number.isInteger(tabId) || tabId < 0 || !Array.isArray(ids) || ids.length > media.MAX_CANDIDATES
        || ids.some(id => typeof id !== 'string' || !id || id.length > 16384)) throw new Error('Invalid media selections');
    return serial(tabId, async () => {
      const stored = await read([key(tabId), videosKey(tabId), 'pendingVideoCandidate']);
      const state = stored[key(tabId)];
      if (state) for (const frame of state.frames) {
        frame.dismissed = [...new Set([...(frame.dismissed || []), ...combined(frame)
          .filter(item => ids.includes(item.id)).map(item => item.media_url)])].slice(-media.MAX_CANDIDATES);
        frame.candidates = frame.candidates.filter(item => !ids.includes(item.id));
        frame.requests = (frame.requests || []).filter(item => !ids.includes(item.id));
      }
      const videos = (stored[videosKey(tabId)] || []).filter(item => !ids.includes(item.id || item.url));
      if (state) await publish(tabId, state);
      else await save({ [videosKey(tabId)]: videos });
      if (ids.includes(stored.pendingVideoCandidate?.id)) await remove(['pendingVideoCandidate', 'pendingVideoUrl']);
      chrome.action.setBadgeText({ text: videos.length ? String(videos.length) : '', tabId });
    });
  }

  async function observe(details, frame, tab) {
    return serial(tab.id, async () => {
      const selectedScope = await scope({ id: chrome.runtime.id, url: frame.url, frameId: details.frameId,
        documentId: frame.documentId, tab }, frame.url, tab);
      if (!selectedScope) return;
      const stored = await read([key(tab.id)]);
      const state = stored[key(tab.id)]?.page_url === tab.url ? stored[key(tab.id)] : { page_url: tab.url, frames: [] };
      let current = state.frames.find(item => item.frame_id === details.frameId);
      if (!current || current.frame_url !== frame.url || current.document_id !== selectedScope.browser_context.document_id) {
        state.frames = state.frames.filter(item => item.frame_id !== details.frameId);
        current = { frame_id: details.frameId, document_id: selectedScope.browser_context.document_id, document_token: null,
          frame_url: frame.url, sequence: 0, candidates: [], requests: [], dismissed: [] };
        state.frames.push(current);
      }
      if (current.dismissed?.includes(details.url)) return;
      const mime = details.responseHeaders?.find(item => item.name.toLowerCase() === 'content-type')?.value;
      const item = media.candidate({ media_url: details.url, observed_content_type: mime, source: 'request', title: tab.title || 'Observed media' }, selectedScope);
      if (!item) return;
      item.id = combined(current).find(value => value.media_url === item.media_url)?.id || crypto.randomUUID();
      current.requests = (current.requests || []).filter(value => value.media_url !== item.media_url);
      current.requests.push(item);
      current.requests = current.requests.slice(-media.MAX_CANDIDATES);
      await publish(tab.id, state);
    });
  }

  async function clearFrame(tabId, frameId) {
    if (frameId === 0) return clear(tabId);
    return serial(tabId, async () => {
      const stored = await read([key(tabId)]);
      const state = stored[key(tabId)];
      if (!state) return;
      state.frames = state.frames.filter(frame => frame.frame_id !== frameId);
      await publish(tabId, state);
    });
  }

  async function clearRequests() {
    const stored = await read(null);
    await Promise.all(Object.keys(stored).filter(name => /^vidgrab_discovery_\d+$/u.test(name)).map(name => {
      const tabId = Number(name.slice('vidgrab_discovery_'.length));
      return serial(tabId, async () => {
        const current = (await read([name]))[name];
        if (!current) return;
        for (const frame of current.frames) frame.requests = [];
        await publish(tabId, current);
      });
    }));
  }

  global.vidGrabDiscovery = { report, select, clear, removeCandidates, observe, clearFrame, clearRequests };
})(globalThis);
