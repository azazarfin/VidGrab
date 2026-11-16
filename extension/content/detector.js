/** Passive, bounded discovery. Only the user's selection may initiate a probe. */
(function () {
  'use strict';
  const media = globalThis.vidGrabMedia;
  const documentToken = crypto.randomUUID();
  let sequence = 0, previous = '', lastUrl = location.href, scanTimer = null;

  function scope() { return { page_url: location.href, frame_url: location.href }; }
  function fromElement(element) {
    const values = [];
    const add = (value, mime, source) => {
      const url = media.resolve(value, document.baseURI || location.href);
      if (url) values.push(media.candidate({ media_url: url, observed_content_type: mime,
        title: document.title || 'Embedded media', kind: element.localName === 'audio' ? 'audio' : 'video', source }, scope()));
    };
    add(element.currentSrc, element.getAttribute('type'), 'element');
    add(element.getAttribute('src'), element.getAttribute('type'), 'element');
    for (const source of Array.prototype.slice.call(element.querySelectorAll('source'), 0, 32)) {
      add(source.getAttribute('src'), source.getAttribute('type'), 'source');
    }
    return values;
  }

  function scan() {
    const values = [];
    if (media.knownPage(location.href) || media.mediaHint(location.href, document.contentType)) {
      values.push(media.candidate({ media_url: location.href, title: document.title || 'Media page',
        observed_content_type: document.contentType, source: 'page' }, scope()));
    }
    const players = Array.prototype.slice.call(document.querySelectorAll('video, audio'), 0, 128);
    const hints = Array.prototype.slice.call(document.querySelectorAll('a[href], meta[property], meta[name]'), 0, 512 - players.length);
    const nodes = [...players, ...hints];
    for (const node of nodes) {
      if (['video', 'audio'].includes(node.localName)) values.push(...fromElement(node));
      else if (node.localName === 'a') {
        const url = media.resolve(node.getAttribute('href'), document.baseURI || location.href);
        if (url && media.mediaHint(url)) values.push(media.candidate({ media_url: url,
          title: node.textContent || document.title, source: 'link' }, scope()));
      } else if (node.localName === 'meta') {
        const key = (node.getAttribute('property') || node.getAttribute('name') || '').toLowerCase();
        if (['og:video', 'og:video:url', 'og:video:secure_url', 'og:audio', 'og:audio:url', 'twitter:player:stream'].includes(key)) {
          const url = media.resolve(node.getAttribute('content'), document.baseURI || location.href);
          if (url) values.push(media.candidate({ media_url: url, title: document.title, source: 'metadata',
            kind: key.startsWith('og:audio') ? 'audio' : 'video' }, scope()));
        }
      }
    }
    const candidates = media.deduplicate(values);
    const serialized = JSON.stringify([location.href, candidates]);
    if (serialized !== previous) {
      previous = serialized;
      try {
        chrome.runtime.sendMessage({ type: 'video_detected', frame_url: location.href, document_token: documentToken,
          sequence: ++sequence, videos: candidates }, () => { void chrome.runtime.lastError; });
      } catch { /* The extension may have been reloaded while this document remains. */ }
    }
    return candidates;
  }

  function scheduleScan() {
    if (scanTimer !== null) return;
    scanTimer = setTimeout(() => { scanTimer = null; lastUrl = location.href; scan(); }, 150);
  }

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (message?.type !== 'vidgrab.frame_context' || sender?.id !== chrome.runtime.id || sender.tab) return;
    const metadata = document.querySelectorAll('meta[name]');
    const meta = Array.prototype.slice.call(metadata, 0, 512)
      .filter(node => node.getAttribute('name')?.toLowerCase() === 'referrer').at(-1);
    respond({ document_token: documentToken, frame_url: location.href,
      referrer_policy: media.referrerPolicy(document.referrerPolicy) || (metadata.length <= 512 ? media.referrerPolicy(meta?.getAttribute('content')) : null) });
  });

  const observer = new MutationObserver(scheduleScan);
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['src', 'type', 'href', 'content'] });
  for (const event of ['loadstart', 'loadedmetadata', 'emptied']) document.addEventListener(event, scheduleScan, true);
  for (const event of ['popstate', 'hashchange', 'pageshow']) window.addEventListener(event, scheduleScan);
  // pushState/replaceState do not emit popstate, and need no DOM mutation.
  setInterval(() => { if (location.href !== lastUrl) scheduleScan(); }, 1000);
  window.__vidgrab_getVideos = scan;
  window.__vidgrab_candidateFor = element => {
    scan();
    return media.deduplicate(fromElement(element))[0] || null;
  };
  scan();
})();
