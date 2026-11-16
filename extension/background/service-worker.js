if (typeof importScripts === 'function') {
  importScripts('../lib/protocol.js', '../lib/api.js', '../lib/utils.js', '../lib/native.js');
}

const api = self.vidGrabAPI;
const DEFAULT_SETTINGS = { cookieForwardingEnabled: false, notificationPreference: 'both' };
const read = keys => new Promise(resolve => chrome.storage.local.get(keys, resolve));

chrome.tabs?.onRemoved?.addListener(tabId => chrome.storage.local.remove('detected_videos_' + tabId));
chrome.runtime.onInstalled.addListener(async () => {
  const current = await read(Object.keys(DEFAULT_SETTINGS));
  const missing = Object.fromEntries(Object.entries(DEFAULT_SETTINGS).filter(([key]) => current[key] === undefined));
  if (Object.keys(missing).length) chrome.storage.local.set(missing);
  for (const [id, title, contexts] of [
    ['vidgrab-download-page', 'Download video from this page', ['page']],
    ['vidgrab-download-link', 'Download linked video', ['link']],
    ['vidgrab-download-video', 'Download this video', ['video']],
  ]) chrome.contextMenus.create({ id, title, contexts });
});

async function notify(id, title, message, kind) {
  const permission = await api.getPermissions();
  if (!permission.notifications_granted || !['both', kind].includes(permission.notification_preference)) return;
  chrome.notifications.create(id, { type: 'basic', iconUrl: chrome.runtime.getURL('icons/icon128.png'), title, message });
}

async function triggerQuickDownload(url) {
  try {
    await api.startDownload(url);
    await notify('download-started', 'VidGrab: Download Queued', 'Video has been added to the download queue.', 'completion');
    await api.sync();
  } catch (error) {
    await notify('download-error', 'VidGrab: Download Needs Attention', error.message, 'failure');
  }
}

chrome.contextMenus.onClicked.addListener(async info => {
  const url = info.menuItemId === 'vidgrab-download-video' ? info.srcUrl
    : info.menuItemId === 'vidgrab-download-link' ? info.linkUrl : info.pageUrl;
  if (url) await triggerQuickDownload(url);
});

function ownPage(sender) {
  if (sender.id !== chrome.runtime.id) return false;
  try {
    const url = new URL(sender.url);
    const base = new URL(chrome.runtime.getURL(''));
    return url.protocol === base.protocol && url.host === base.host
      && ['/popup/popup.html', '/options/options.html'].includes(url.pathname);
  } catch { return false; }
}

function content(sender) {
  if (sender.id !== chrome.runtime.id || !Number.isInteger(sender.tab?.id) || sender.tab.id < 0) return false;
  try { self.vidGrabMediaUrl(sender.url); return true; } catch { return false; }
}

function errorResponse(request, error) {
  return { protocol_version: 1, request_id: request?.request_id || null, ok: false,
    error: { code: error.code || 'INTERNAL_ERROR', message: error.message || 'The request could not be completed',
      request_id: request?.request_id || null, retryable: false, retry_at: null, provenance: 'local', details: error.details || {} } };
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (typeof message?.type !== 'string') return false;
  if (message.type.startsWith('companion.')) {
    if (!ownPage(sender)) {
      respond(errorResponse(message.request, { code: 'UNAUTHORIZED', message: 'Only VidGrab popup and settings pages may use this control request' }));
      return false;
    }
    void (async () => {
      try {
        if (message.type === 'companion.request') {
          if (message.request?.operation === 'auth.create') throw { code: 'UNSUPPORTED_OPERATION', message: 'Use the browser authentication action to share cookies' };
          respond(await api.forwardRequest(message.request, message.replay === true));
        } else if (message.type === 'companion.permissions') respond({ ok: true, data: await api.getPermissions(message.url) });
        else if (message.type === 'companion.context') {
          if (message.job_id !== null && message.job_id !== undefined && (typeof message.job_id !== 'string' || message.job_id.length > 256)) {
            throw { code: 'BAD_REQUEST', message: 'Invalid download identity' };
          }
          respond({ ok: true, data: await api.prepareAuth(message.url, message.job_id || null, message.fresh === true) });
        } else if (message.type === 'companion.pending') respond({ ok: true, data: await api.getPending() });
        else if (message.type === 'companion.dismiss') {
          await api.dismissPending(message.key);
          respond({ ok: true });
        }
        else throw { code: 'UNSUPPORTED_OPERATION', message: 'Unsupported companion message' };
      } catch (error) { respond(errorResponse(message.request, error)); }
    })();
    return true;
  }
  if (!content(sender)) return false;
  if (message.type === 'video_detected') {
    const candidates = Array.isArray(message.videos) ? message.videos.slice(0, 128) : [];
    const videos = candidates.flatMap(video => {
      try {
        self.vidGrabMediaUrl(video.url);
        return [{ url: video.url, title: String(video.title || '').slice(0, 512),
          thumbnail: typeof video.thumbnail === 'string' ? video.thumbnail.slice(0, 16384) : null,
          type: typeof video.type === 'string' ? video.type.slice(0, 32) : 'video' }];
      } catch { return []; }
    });
    const key = 'detected_videos_' + sender.tab.id;
    void read([key]).then(current => {
      const merged = new Map((Array.isArray(current[key]) ? current[key] : []).map(video => [video.url, video]));
      for (const video of videos) merged.set(video.url, video);
      const bounded = [...merged.values()].slice(-128);
      chrome.storage.local.set({ [key]: bounded });
      chrome.action.setBadgeText({ text: bounded.length ? String(bounded.length) : '', tabId: sender.tab.id });
      chrome.action.setBadgeBackgroundColor({ color: '#7C6BF0', tabId: sender.tab.id });
    });
  } else if (message.type === 'open_popup') {
    chrome.action?.openPopup?.();
  } else if (message.type === 'download_request') {
    void triggerQuickDownload(message.url);
  } else if (message.type === 'probe_request' || message.type === 'download_with_options') {
    void (async () => {
      try {
        const data = message.type === 'probe_request' ? await api.probeUrl(message.url)
          : await api.startDownload(message.url, message.formatId || null, message.title || null, message.thumbnailUrl || null, message.options || {});
        respond({ success: true, data });
      } catch (error) { respond({ success: false, error: error.message, code: error.code }); }
    })();
    return true;
  }
  return false;
});

let observedInstance = null;
let observedJobs = new Map();
api.addEventListener('snapshot', event => {
  const snapshot = event.detail.snapshot;
  chrome.action.setBadgeText({ text: snapshot.counts.unfinished ? String(snapshot.counts.unfinished) : '' });
  chrome.action.setBadgeBackgroundColor({ color: '#7C6BF0' });
  if (observedInstance === snapshot.instance_id) {
    for (const job of snapshot.jobs) {
      const previous = observedJobs.get(job.id);
      if (previous && previous.status !== job.status && ['complete', 'failed', 'waiting_for_auth'].includes(job.status)) {
        const success = job.status === 'complete';
        void notify((success ? 'success' : 'failure') + '|' + job.id, success ? 'Download Complete' : 'Download Needs Attention',
          success ? (job.title || 'Video') + ' finished downloading.' : job.status === 'waiting_for_auth'
            ? 'Open VidGrab and share fresh cookies for this download.' : (job.title || 'Video') + ' failed. Open VidGrab to review it.',
          success ? 'completion' : 'failure').catch(() => {});
      }
    }
  }
  observedInstance = snapshot.instance_id;
  observedJobs = new Map(snapshot.jobs.map(job => [job.id, job]));
});

chrome.notifications.onClicked.addListener(() => chrome.action?.openPopup?.());
chrome.notifications.onButtonClicked.addListener(() => chrome.action?.openPopup?.());
chrome.alarms.create('check-downloads', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'check-downloads') void api.sync().catch(() => {});
});
