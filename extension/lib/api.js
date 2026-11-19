/** Generated v1 client facade. Native transport and credentials belong to the background. */
(function (global) {
  const browserPage = typeof document !== 'undefined'
    && /\/(?:popup\/popup|options\/options)\.html$/u.test(global.location?.pathname || '');
  const identify = () => crypto.randomUUID();
  const failure = (code, message, details = {}) => new global.vidGrabProtocol.ProtocolError(code, message, null, details);
  const stored = keys => new Promise((resolve, reject) => chrome.storage.local.get(keys, value => {
    if (chrome.runtime.lastError) reject(failure('INTERNAL_ERROR', 'Browser settings could not be read'));
    else resolve(value);
  }));
  const save = values => new Promise((resolve, reject) => chrome.storage.local.set(values, () => {
    if (chrome.runtime.lastError) reject(failure('INTERNAL_ERROR', 'The request could not be recorded safely'));
    else resolve();
  }));
  const send = message => new Promise((resolve, reject) => chrome.runtime.sendMessage(message, response => {
    if (chrome.runtime.lastError) reject(failure(message.request && global.vidGrabProtocol.isMutation(message.request.operation)
      ? 'REQUEST_OUTCOME_UNKNOWN' : 'APP_UNAVAILABLE', 'The browser connection to VidGrab was interrupted'));
    else resolve(response);
  }));

  function mediaUrl(value) {
    try {
      if (typeof value !== 'string' || value.length > 16384 || /[\u0000-\u0020\u007f]/u.test(value)) throw new Error();
      const parsed = new URL(value);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
      return parsed;
    } catch { throw failure('BAD_REQUEST', 'Enter an HTTP or HTTPS media URL without embedded credentials'); }
  }

  function originGranted(pattern, url) {
    if (pattern === '<all_urls>') return true;
    const match = /^(\*|https?):\/\/([^/]+)\//u.exec(pattern);
    if (!match || (match[1] !== '*' && match[1] + ':' !== url.protocol)) return false;
    const host = match[2];
    return host === '*' || host === url.host || host === url.hostname
      || (host.startsWith('*.') && (url.hostname === host.slice(2) || url.hostname.endsWith(host.slice(1))));
  }

  const canonical = value => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  async function fingerprint(request, exact = false) {
    // The broad fingerprint fences duplicates after credentials expire; the
    // exact witness permits replay only with the original payload.
    const { auth_ref, probe_id, ...params } = request.params;
    const bytes = new TextEncoder().encode(JSON.stringify(canonical({ operation: request.operation,
      params: exact ? request.params : params })));
    return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }

  class VidGrabAPI extends EventTarget {
    constructor() {
      super();
      this.client = global.vidGrabProtocol.createClient(request => this.forwardRequest(request, this.replaying === true));
      this.session = global.vidGrabProtocol.createSession(this.client, identify);
      this.contexts = new Map();
      this.mutations = Promise.resolve();
      this.running = false;
      this.timer = null;
      this.refreshing = null;
      this.replaying = false;
    }

    async forwardRequest(request, replay = false) {
      if (!global.vidGrabProtocol.validateRequest(request)) throw failure('BAD_REQUEST', 'Invalid companion request');
      if (browserPage) return send({ type: 'companion.request', request, replay });
      if (!global.vidGrabProtocol.isMutation(request.operation) || request.operation === 'auth.create') {
        return global.vidGrabNative.request(request);
      }
      const execute = () => this.forwardMutation(request, replay);
      const current = this.mutations.then(execute, execute);
      this.mutations = current.catch(() => {});
      return current;
    }

    async forwardMutation(request, replay) {
      const [hash, payload] = await Promise.all([fingerprint(request), fingerprint(request, true)]);
      const store = await stored(['vidgrabPendingMutations']);
      const markers = store.vidgrabPendingMutations || {};
      const previous = markers[hash];
      if (previous && (!replay || previous.key !== request.idempotency_key)) {
        throw failure('REQUEST_OUTCOME_UNKNOWN', 'A previous request may have succeeded. Review its result or check the same request.',
          { idempotency_key: previous.key });
      }
      if (previous && previous.payload_fingerprint !== payload) {
        throw failure('REQUEST_OUTCOME_UNKNOWN', 'The original request payload is unavailable, so its reply cannot be checked. Review Downloads, then dismiss the notice in Settings.',
          { idempotency_key: previous.key, replay_available: false });
      }
      if (!previous && Object.keys(markers).length >= 16) {
        throw failure('CAPACITY_EXCEEDED', 'Check unresolved requests before submitting more changes');
      }
      markers[hash] = { key: request.idempotency_key, operation: request.operation, payload_fingerprint: payload };
      await save({ vidgrabPendingMutations: markers });
      let response;
      try {
        response = await global.vidGrabNative.request(request);
        await global.vidGrabProtocol.createClient(async () => response).request(request.operation, request.params,
          { request_id: request.request_id, idempotency_key: request.idempotency_key });
      } catch (error) {
        if (!['REQUEST_OUTCOME_UNKNOWN', 'BAD_RESPONSE', 'IDEMPOTENCY_CONFLICT'].includes(error.code)) {
          delete markers[hash];
          try { await save({ vidgrabPendingMutations: markers }); } catch {}
        }
        if (response) return response;
        throw error;
      }
      delete markers[hash];
      // A valid acknowledgement remains authoritative if browser storage fails.
      // The persisted notice is retained for explicit review in that case.
      try { await save({ vidgrabPendingMutations: markers }); } catch {}
      return response;
    }

    async getPermissions(url = null) {
      if (browserPage) {
        const response = await send({ type: 'companion.permissions', url });
        if (!response?.ok) throw failure(response?.error?.code || 'APP_UNAVAILABLE', response?.error?.message || 'Browser permissions are unavailable');
        return response.data;
      }
      const [preferences, permissions] = await Promise.all([
        stored(['cookieForwardingEnabled', 'requestObservationEnabled', 'notificationPreference']),
        new Promise(resolve => {
          if (!chrome.permissions?.getAll) { resolve(null); return; }
          chrome.permissions.getAll(value => resolve(chrome.runtime.lastError ? null : value));
        }),
      ]);
      const parsed = url ? mediaUrl(url) : null;
      const requestObservationEnabled = preferences.requestObservationEnabled === true;
      const requestObservationGranted = permissions?.permissions?.includes('webRequest') === true
        && permissions?.permissions?.includes('webNavigation') === true;
      const session = await global.vidGrabObservation?.syncSession?.(requestObservationGranted);
      return { cookie_forwarding_enabled: preferences.cookieForwardingEnabled === true,
        request_observation_enabled: requestObservationEnabled,
        request_observation_granted: requestObservationGranted,
        request_observation_restart_required: Boolean(requestObservationEnabled && requestObservationGranted && session?.pendingRestart),
        navigation_granted: permissions?.permissions?.includes('webNavigation') === true,
        notification_preference: preferences.notificationPreference || 'both', permission_api_available: permissions !== null,
        cookies_granted: permissions?.permissions?.includes('cookies') === true,
        native_granted: permissions?.permissions?.includes('nativeMessaging') === true,
        notifications_granted: permissions?.permissions?.includes('notifications') === true,
        site_granted: parsed !== null && permissions?.origins?.some(pattern => originGranted(pattern, parsed)) === true,
        origins: permissions?.origins || [] };
    }

    async frameEvidence(tabId, frameId) {
      return new Promise(resolve => {
        try {
          chrome.tabs.sendMessage(tabId, { type: 'vidgrab.frame_context' }, { frameId }, value =>
            resolve(chrome.runtime.lastError ? null : value));
        } catch { resolve(null); }
      });
    }

    async selectedCandidate(url, identity, permission) {
      if (identity === null) return null;
      if (typeof identity !== 'string' || !identity || identity.length > 128) throw failure('BAD_REQUEST', 'Invalid media selection');
      const tabs = await new Promise(resolve => chrome.tabs.query({ active: true, currentWindow: true }, resolve));
      const tab = tabs?.[0];
      if (!Number.isInteger(tab?.id)) throw failure('BROWSER_CONTEXT_EXPIRED', 'Return to the source tab and select the media again');
      const key = 'detected_videos_' + tab.id;
      const candidate = (await stored([key]))[key]?.find(item => item.id === identity && item.url === url);
      const context = candidate?.browser_context;
      if (!candidate || candidate.page_url !== tab.url || context?.tab_id !== tab.id
          || !Number.isInteger(context.frame_id) || context.frame_id < 0
          || (tab.cookieStoreId && context.cookie_store_id && tab.cookieStoreId !== context.cookie_store_id)
          || Boolean(context.incognito) !== Boolean(tab.incognito)) {
        throw failure('BROWSER_CONTEXT_EXPIRED', 'The source page changed. Select the media again from the current tab.');
      }
      const live = await this.frameEvidence(tab.id, context.frame_id);
      let verified = context.document_token && live?.frame_url === candidate.frame_url && live.document_token === context.document_token;
      // Request observations can precede the content-script report. Reuse then
      // requires the browser's exact current document ID and navigation grant.
      if (!verified && !context.document_token && candidate.source === 'request' && context.document_id && permission.navigation_granted) {
        const frame = await new Promise(resolve => chrome.webNavigation.getFrame({ tabId: tab.id, frameId: context.frame_id }, value =>
          resolve(chrome.runtime.lastError ? null : value)));
        verified = frame?.url === candidate.frame_url && frame.documentId === context.document_id;
      }
      if (!verified) throw failure('BROWSER_CONTEXT_EXPIRED', 'The source frame changed or closed. Select media from its current page again.');
      return { ...candidate, forwarded_referer: global.vidGrabMedia.referrer(candidate.frame_url, url, live?.referrer_policy) };
    }

    async prepareAuth(url, jobId = null, fresh = false, candidateId = null) {
      mediaUrl(url);
      if (browserPage) {
        const response = await send({ type: 'companion.context', url, job_id: jobId, fresh, candidate_id: candidateId });
        if (!response?.ok) throw failure(response?.error?.code || 'APP_UNAVAILABLE', response?.error?.message || 'Browser authentication is unavailable');
        return response.data;
      }
      const permission = await this.getPermissions(url);
      const candidate = await this.selectedCandidate(url, candidateId, permission);
      if (!permission.cookie_forwarding_enabled && !candidate) return null;
      if (candidate && (!permission.site_granted || [candidate.page_url, candidate.frame_url]
        .some(source => !permission.origins.some(pattern => originGranted(pattern, mediaUrl(source)))))) {
        throw failure('BROWSER_CONNECTION_UNAVAILABLE', 'Allow browser access to the source page and media site, then select the media again.');
      }
      if (permission.cookie_forwarding_enabled && (!permission.cookies_granted || !permission.site_granted)) {
        throw failure('BROWSER_CONNECTION_UNAVAILABLE', 'Cookie forwarding is enabled, but browser permission for this site is missing. Review extension permissions.');
      }
      await this.sync();
      const instance = this.session.state.capabilities.instance_id;
      if (candidate && !this.session.state.capabilities.features.includes('auth.headers')) {
        throw failure('APP_INCOMPATIBLE', 'Update VidGrab to use context-aware detected media');
      }
      let referer = candidate?.forwarded_referer || null;
      let sourceTab = null;
      if (!candidate) {
        const tabs = await new Promise(resolve => chrome.tabs.query({ active: true, currentWindow: true }, resolve));
        const tab = tabs?.[0];
        sourceTab = tab;
        if (global.vidGrabMedia.httpUrl(tab?.url)?.origin === new URL(url).origin) {
          const live = await this.frameEvidence(tab.id, 0);
          if (live?.frame_url === tab.url) referer = global.vidGrabMedia.referrer(tab.url, url, live.referrer_policy);
        }
      }
      let storeId = candidate?.browser_context?.cookie_store_id || sourceTab?.cookieStoreId || null;
      const cookieTabId = candidate?.browser_context?.tab_id ?? sourceTab?.id;
      if (permission.cookie_forwarding_enabled && !storeId && Number.isInteger(cookieTabId) && chrome.cookies.getAllCookieStores) {
        const stores = await new Promise((resolve, reject) => chrome.cookies.getAllCookieStores(items => {
          if (chrome.runtime.lastError) reject(failure('BROWSER_CONNECTION_UNAVAILABLE', 'The source browser cookie store could not be verified'));
          else resolve(items);
        }));
        storeId = stores?.find(store => store.tabIds?.includes(cookieTabId))?.id || null;
        if (!storeId) throw failure('BROWSER_CONNECTION_UNAVAILABLE', 'Return to the source tab and share its fresh browser context again');
      }
      const scope = JSON.stringify([url, jobId, candidate?.id || null, storeId,
        candidate?.browser_context?.document_token || null, referer]);
      const existing = this.contexts.get(scope);
      if (!fresh && existing?.instance_id === instance && existing.expires_at > Date.now() / 1000 + 5) return existing;
      this.contexts.delete(scope);
      const observedCookies = permission.cookie_forwarding_enabled ? await new Promise((resolve, reject) => chrome.cookies.getAll({ url, ...(storeId ? { storeId } : {}) }, items => {
        if (chrome.runtime.lastError) reject(failure('BROWSER_CONNECTION_UNAVAILABLE', 'Browser cookies could not be read. Review site access.'));
        else resolve(items);
      })) : [];
      const cookies = observedCookies.filter(cookie => !cookie.partitionKey && !cookie.firstPartyDomain);
      if (observedCookies.length && !cookies.length) {
        throw failure('BROWSER_CONNECTION_UNAVAILABLE', 'Open the source site in its own tab and share fresh cookies there. This embedded session uses partitioned cookies.');
      }
      if (!cookies?.length && !candidate) return null;
      const permitted = ['domain', 'path', 'name', 'value', 'secure', 'expirationDate'];
      const payload = cookies.map(cookie => Object.fromEntries(permitted.filter(key => cookie[key] !== undefined).map(key => [key, cookie[key]])));
      const params = { instance_id: instance, url, cookies: payload, ttl_seconds: 300,
        ...(jobId ? { job_id: jobId } : {}), ...(global.navigator?.userAgent ? { user_agent: global.navigator.userAgent } : {}), ...(referer ? { referer } : {}) };
      const result = await this.client.request('auth.create', params, { request_id: identify(), idempotency_key: identify() });
      for (const [key, value] of this.contexts) if (value.expires_at <= Date.now() / 1000 || value.instance_id !== instance) this.contexts.delete(key);
      if (this.contexts.size >= 128) this.contexts.delete(this.contexts.keys().next().value);
      this.contexts.set(scope, result);
      return result;
    }

    async requireFeature(feature, message) {
      if (!this.session.state.connected) await this.sync();
      if (!this.session.state.capabilities.features.includes(feature)) throw failure('APP_INCOMPATIBLE', message);
    }

    async probeUrl(url, { fresh = false, job_id = null, candidate_id = null } = {}) {
      await this.requireFeature('probes.fresh', 'Update VidGrab to review media formats and refresh expired selections');
      const context = await this.prepareAuth(url, job_id, fresh, candidate_id);
      return this.session.read('probes.create', { url, format_limit: 40, entry_limit: 128,
        ...(fresh ? { fresh: true } : {}),
        ...(context ? { auth_ref: context.reference } : {}), ...(job_id ? { job_id } : {}) });
    }

    async startDownload(url, formatId = null, title = null, thumbnailUrl = null, options = {}, { candidate_id = null, probe_id = null } = {}) {
      mediaUrl(url);
      if (this.session.pending) throw failure('REQUEST_OUTCOME_UNKNOWN', 'Check the previous request before adding another download');
      const optionKeys = ['download_subtitles', 'subtitle_langs', 'subtitle_format', 'embed_subtitles', 'embed_thumbnail', 'is_playlist', 'playlist_items'];
      if (Object.keys(options).some(key => ![...optionKeys, 'audio_only', 'audio_format'].includes(key))) {
        throw failure('UNSUPPORTED_OPTION', 'Change the save location and other download defaults in Settings');
      }
      if (probe_id !== null) await this.requireFeature('probes.fresh', 'Update VidGrab to download a reviewed media format');
      const context = await this.prepareAuth(url, null, false, candidate_id);
      const params = { url, format_id: formatId || null, title, thumbnail_url: thumbnailUrl,
        audio_only: options.audio_only === true, audio_format: options.audio_format || 'mp3',
        options: Object.fromEntries(optionKeys.filter(key => options[key] !== undefined).map(key => [key, options[key]])),
        ...(context ? { auth_ref: context.reference } : {}), ...(probe_id !== null ? { probe_id } : {}) };
      return this.session.mutate('downloads.enqueue', params);
    }

    async cancelDownload(job) {
      return this.session.mutate('downloads.action', { job_id: job.id, expected_revision: job.revision, action: 'cancel' });
    }

    async retryDownload(job, { candidate_id = null } = {}) {
      if (this.session.pending) throw failure('REQUEST_OUTCOME_UNKNOWN', 'Check the previous request before retrying this download');
      const context = await this.prepareAuth(job.url, job.id, true, candidate_id);
      return this.session.mutate('downloads.action', { job_id: job.id, expected_revision: job.revision, action: 'retry',
        ...(context ? { auth_ref: context.reference } : {}) });
    }

    async authenticate(job, { candidate_id = null } = {}) {
      if (this.session.pending) throw failure('REQUEST_OUTCOME_UNKNOWN', 'Check the previous request before sharing fresh browser context');
      const context = await this.prepareAuth(job.url, job.id, true, candidate_id);
      if (!context) throw failure('AUTH_REQUIRED', 'Sign in to the source site, enable cookie forwarding in Settings, then share fresh cookies here');
      return this.session.mutate('downloads.authenticate', { job_id: job.id, expected_revision: job.revision, auth_ref: context.reference });
    }

    async retryPending() {
      this.replaying = true;
      try { return await this.session.replay(); }
      finally { this.replaying = false; }
    }

    async getPending() {
      if (browserPage) {
        const response = await send({ type: 'companion.pending' });
        return response?.ok ? response.data : [];
      }
      return Object.values((await stored(['vidgrabPendingMutations'])).vidgrabPendingMutations || {});
    }

    async removeCandidates(tabId, ids) {
      if (browserPage) {
        const response = await send({ type: 'companion.discovery.remove', tab_id: tabId, ids });
        if (!response?.ok) throw failure('BAD_REQUEST', 'The media selections could not be removed');
      } else await global.vidGrabDiscovery.removeCandidates(tabId, ids);
    }

    async dismissPending(key) {
      if (typeof key !== 'string' || !/^[!-~]{1,128}$/u.test(key)) {
        throw failure('BAD_REQUEST', 'Invalid request notice identity');
      }
      if (browserPage) {
        const response = await send({ type: 'companion.dismiss', key });
        if (!response?.ok) throw failure(response?.error?.code || 'INTERNAL_ERROR', response?.error?.message || 'The request notice could not be dismissed');
        this.session.dismiss(key);
        return;
      }
      const execute = async () => {
        const store = await stored(['vidgrabPendingMutations']);
        const markers = store.vidgrabPendingMutations || {};
        for (const [hash, marker] of Object.entries(markers)) if (marker.key === key) delete markers[hash];
        await save({ vidgrabPendingMutations: markers });
        this.session.dismiss(key);
      };
      const current = this.mutations.then(execute, execute);
      this.mutations = current.catch(() => {});
      return current;
    }

    async updateConfig(patch, expectedRevision) {
      return this.session.mutate('settings.patch', { expected_revision: expectedRevision, patch });
    }

    async sync(force = false) {
      if (!this.refreshing) this.refreshing = (async () => {
        const previous = this.session.state.snapshot?.instance_id;
        try {
          const state = await this.session.sync(force);
          if (previous && previous !== state.snapshot.instance_id) this.contexts.clear();
          this.dispatchEvent(new CustomEvent('snapshot', { detail: state }));
          this.dispatchEvent(new CustomEvent('connection_change', { detail: { connected: true } }));
          return state;
        } catch (error) {
          this.dispatchEvent(new CustomEvent('connection_change', { detail: { connected: false, error } }));
          throw error;
        }
      })().finally(() => { this.refreshing = null; });
      return this.refreshing;
    }

    async getStatus() {
      const state = await this.sync();
      return { status: 'online', ffmpeg_available: Boolean(state.capabilities.component_versions.ffmpeg),
        yt_dlp_version: state.capabilities.component_versions.yt_dlp || state.capabilities.component_versions['yt-dlp'] || 'unknown',
        capabilities: state.capabilities };
    }

    async getConfig() { return (await this.sync()).snapshot.settings; }
    async listDownloads() { return (await this.sync()).snapshot.jobs; }
    async loadMore() {
      const state = await this.session.loadMore();
      this.dispatchEvent(new CustomEvent('snapshot', { detail: state }));
      return state;
    }

    connect() {
      if (this.running) return;
      this.running = true;
      const refresh = async () => {
        try { await this.sync(); } catch {}
        finally { if (this.running) this.timer = setTimeout(refresh, 2000); }
      };
      void refresh();
    }

    disconnect() { this.running = false; clearTimeout(this.timer); this.contexts.clear(); }
  }

  global.VidGrabAPI = VidGrabAPI;
  global.vidGrabAPI = new VidGrabAPI();
  if (browserPage) global.vidGrabCompanion = global.vidGrabAPI.client;
  global.vidGrabMediaUrl = mediaUrl;
  global.vidGrabOriginGranted = originGranted;
})(globalThis);
