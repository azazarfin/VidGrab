// Shared view state; all transport calls still pass through the generated client.
const retainedMutations = new Set(['settings.patch', 'downloads.enqueue', 'downloads.action', 'downloads.authenticate', 'downloads.clear_history']);

function immutable(value) {
  if (value && typeof value === 'object') {
    for (const item of Object.values(value)) immutable(item);
    Object.freeze(value);
  }
  return value;
}

export function createSession(client, identify) {
  let state = { connected: false, capabilities: null, snapshot: null, has_more: false };
  let pending = null;
  let syncing = null;
  let writing = false;
  let reset = true;
  let limit = 25;

  const request = (operation, params, key) => client.request(operation, params,
    { request_id: identify(), ...(key ? { idempotency_key: key } : {}) });
  const incompatible = () => new ProtocolError('APP_INCOMPATIBLE', 'Update VidGrab and the browser extension to compatible versions');
  const unknown = () => new ProtocolError('REQUEST_OUTCOME_UNKNOWN', 'The previous request may have succeeded. Check it before submitting another request.');

  async function capabilities() {
    const current = await request('capabilities.get', {});
    if (current.protocol_min > 1 || current.protocol_max < 1 || !current.features.includes('downloads.source')) throw incompatible();
    return current;
  }

  async function page(operation, params) {
    for (;;) {
      try { return await request(operation, { ...params, limit }); }
      catch (error) {
        if (error.code !== 'MESSAGE_TOO_LARGE' || limit === 1) throw error;
        limit = Math.max(1, Math.floor(limit / 2));
      }
    }
  }

  async function synchronize(force) {
    try {
      const fresh = force || reset || !state.capabilities;
      let current = fresh ? await capabilities() : state.capabilities;
      const batch = await page('events.read', fresh ? {} : {
        instance_id: state.snapshot.instance_id, cursor: state.snapshot.cursor,
      });
      if (batch.instance_id !== current.instance_id) current = await capabilities();
      if (batch.instance_id !== current.instance_id) throw new ProtocolError('ENGINE_UNAVAILABLE', 'VidGrab is restarting. Reconnect to refresh its state.');
      let snapshot;
      if (batch.resnapshot) {
        snapshot = batch.snapshot;
        if (!snapshot || snapshot.instance_id !== batch.instance_id || snapshot.cursor !== batch.cursor) {
          throw new ProtocolError('BAD_RESPONSE', 'The companion returned an inconsistent snapshot');
        }
      } else {
        if (!state.snapshot) throw new ProtocolError('BAD_RESPONSE', 'A reconnect snapshot is required');
        const topics = new Set(batch.events.map(event => event.topic));
        const [settings, downloads] = await Promise.all([
          topics.has('settings') ? request('settings.get', {}) : state.snapshot.settings,
          topics.has('downloads') ? page('downloads.list', {}) : null,
        ]);
        snapshot = { ...state.snapshot, ...(downloads || {}), settings, cursor: batch.cursor };
      }
      state = { connected: true, capabilities: current, snapshot, has_more: batch.has_more };
      reset = false;
      return state;
    } catch (error) {
      state = { ...state, connected: false };
      reset = true;
      throw error;
    }
  }

  async function sendPending() {
    const intent = pending;
    writing = true;
    try {
      const result = await request(intent.operation, intent.params, intent.key);
      pending = null;
      return result;
    } catch (error) {
      if (['REQUEST_OUTCOME_UNKNOWN', 'BAD_RESPONSE'].includes(error.code)) {
        if (typeof error.details?.idempotency_key === 'string') pending.key = error.details.idempotency_key;
      } else pending = null;
      throw error;
    } finally { writing = false; }
  }

  return {
    get state() { return state; },
    get pending() { return pending ? Object.freeze({ ...pending }) : null; },
    async read(operation, params = {}) {
      if (retainedMutations.has(operation) || operation === 'auth.create') {
        throw new ProtocolError('BAD_REQUEST', 'Use an explicit action to submit a mutation');
      }
      if (!state.connected) await this.sync();
      return request(operation, params);
    },
    sync(force = false) {
      if (!syncing) syncing = synchronize(force).finally(() => { syncing = null; });
      return syncing;
    },
    async loadMore() {
      if (!state.snapshot || !state.snapshot.next_after) return state;
      const previous = state.snapshot;
      const downloads = await page('downloads.list', { after: previous.next_after });
      // A simultaneous resnapshot owns the view; do not append an older page to it.
      if (state.snapshot !== previous) return state;
      const jobs = new Map(previous.jobs.map(job => [job.id, job]));
      for (const job of downloads.jobs) jobs.set(job.id, job);
      state = { ...state, snapshot: { ...previous, ...downloads, jobs: [...jobs.values()] } };
      return state;
    },
    async mutate(operation, params) {
      if (!retainedMutations.has(operation)) throw new ProtocolError('BAD_REQUEST', 'This operation cannot retain a mutation intent');
      if (pending) throw unknown();
      if (!state.connected) await this.sync();
      if (pending || writing) throw unknown();
      pending = { operation, params: immutable(JSON.parse(JSON.stringify(params))), key: identify() };
      return sendPending();
    },
    async replay() {
      if (!pending) throw new ProtocolError('BAD_REQUEST', 'There is no unresolved request to check');
      if (writing) throw unknown();
      if (!state.connected) await this.sync();
      if (writing) throw unknown();
      if (!pending) throw new ProtocolError('BAD_REQUEST', 'There is no unresolved request to check');
      return sendPending();
    },
    dismiss(key) {
      if (writing) throw unknown();
      if (pending?.key === key) pending = null;
    },
  };
}
