/** Literal media URLs and bounded discovery metadata shared by classic scripts. */
(function (global) {
  'use strict';
  const MAX_CANDIDATES = 128;
  const sites = ['youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com', 'twitch.tv',
    'twitter.com', 'x.com', 'reddit.com', 'facebook.com', 'instagram.com', 'tiktok.com', 'bilibili.com'];

  function httpUrl(value) {
    if (typeof value !== 'string' || !value || value.length > 16384 || /[\u0000-\u0020\u007f]/u.test(value)) return null;
    try {
      const parsed = new URL(value);
      return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed : null;
    } catch { return null; }
  }

  function resolve(value, base) {
    // Parsing is for validation. An already absolute signed URL keeps its bytes.
    if (httpUrl(value)) return value;
    if (typeof value !== 'string' || !value || value.length > 16384 || /[\u0000-\u0020\u007f]/u.test(value)) return null;
    try {
      if (value.startsWith('blob:') && new URL(value).protocol === 'blob:') return value;
      const absolute = new URL(value, base).href;
      return httpUrl(absolute) ? absolute : null;
    } catch { return null; }
  }

  function contentType(value) {
    if (typeof value !== 'string' || value.length > 256) return null;
    const mime = value.split(';')[0].trim().toLowerCase();
    return /^[a-z0-9.+-]+\/[a-z0-9.+-]+$/u.test(mime) ? mime : null;
  }

  function kind(url, mime = null, fallback = 'video') {
    const path = httpUrl(url)?.pathname.toLowerCase() || '';
    if (['application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'].includes(mime)
        || path.endsWith('.m3u8')) return 'hls';
    if (mime === 'application/dash+xml' || path.endsWith('.mpd')) return 'dash';
    if (mime?.startsWith('audio/') || /\.(?:mp3|wav|ogg|m4a|flac|aac|opus|weba)$/u.test(path)) return 'audio';
    if (mime?.startsWith('video/') || /\.(?:mp4|webm|mov|m4v|mkv|avi|ogv)$/u.test(path)) return 'video';
    return fallback;
  }

  function mediaHint(url, mime) { return kind(url, contentType(mime), null) !== null; }
  function referrerPolicy(value) {
    const policy = typeof value === 'string' ? value.trim().toLowerCase() : null;
    return ['no-referrer', 'no-referrer-when-downgrade', 'origin', 'origin-when-cross-origin', 'same-origin',
      'strict-origin', 'strict-origin-when-cross-origin', 'unsafe-url'].includes(policy) ? policy : null;
  }

  function referrer(source, target, value) {
    const from = httpUrl(source), to = httpUrl(target), policy = referrerPolicy(value);
    if (!from || !to || !policy || policy === 'no-referrer' || (from.protocol === 'https:' && to.protocol === 'http:')
        || (policy === 'same-origin' && from.origin !== to.origin)) return null;
    // Context forwarding is deliberately narrower than permissive page policies.
    // Paths, queries and fragments are never needed to identify the source origin.
    return from.origin + '/';
  }
  function knownPage(url) {
    const host = httpUrl(url)?.hostname.toLowerCase();
    return Boolean(host && sites.some(site => host === site || host.endsWith('.' + site)));
  }

  function candidate(input, scope) {
    if (!input || typeof input !== 'object' || !httpUrl(scope.frame_url) || !httpUrl(scope.page_url)) return null;
    const media = input.media_url ?? input.url;
    if (typeof media !== 'string' || media.length > 16384 || /[\u0000-\u0020\u007f]/u.test(media)) return null;
    const browserLocal = media.startsWith('blob:') && resolve(media, scope.frame_url) === media;
    if (!browserLocal && !httpUrl(media)) return null;
    const mime = contentType(input.observed_content_type);
    const source = ['element', 'source', 'page', 'metadata', 'link', 'request'].includes(input.source) ? input.source : 'element';
    return { schema_version: 1, url: browserLocal ? scope.frame_url : media, media_url: media,
      page_url: scope.page_url, frame_url: scope.frame_url, observed_content_type: mime,
      title: typeof input.title === 'string' ? input.title.slice(0, 512) : '',
      kind: browserLocal ? 'browser_stream' : kind(media, mime, input.kind === 'audio' ? 'audio' : source === 'page' ? 'page' : 'video'),
      source, confidence: source === 'page' || source === 'link' || browserLocal ? 'hint' : 'observed',
      browser_local: browserLocal, browser_context: scope.browser_context || null };
  }

  function deduplicate(candidates) {
    const merged = new Map();
    for (const item of candidates) {
      if (!item) continue;
      const previous = merged.get(item.media_url);
      if (previous) {
        if (!previous.observed_content_type && item.observed_content_type) {
          previous.observed_content_type = item.observed_content_type; previous.kind = item.kind;
        }
        if (previous.confidence === 'hint' && item.confidence === 'observed') {
          previous.confidence = item.confidence; previous.source = item.source;
        }
      } else if (merged.size < MAX_CANDIDATES) merged.set(item.media_url, item);
    }
    return [...merged.values()];
  }

  global.vidGrabMedia = { MAX_CANDIDATES, httpUrl, resolve, contentType, kind, mediaHint, referrerPolicy, referrer, knownPage, candidate, deduplicate };
})(globalThis);
