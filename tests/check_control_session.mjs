// State and mutation journeys through both generated clients, without I/O.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';
import * as moduleClient from '../packages/protocol/generated/client.mjs';

const context = vm.createContext({ TextEncoder });
vm.runInContext(readFileSync('packages/protocol/generated/client.js', 'utf8'), context);
const schema = JSON.parse(readFileSync('packages/protocol/schema-v1.json', 'utf8'));
const settingsSchema = schema.operations['settings.get'].result;
const definition = settingsSchema.$defs[settingsSchema.properties.values.$ref.split('/').at(-1)];
const values = Object.fromEntries(Object.entries(definition.properties).map(([key, item]) => [key, item.default]));
values.download_dir = '/fixture/media';
const plain = value => JSON.parse(JSON.stringify(value));
const job = (id, revision = 0) => ({ id, url: 'https://fixture.invalid/' + id, revision, title: id,
  status: 'queued', progress: 0, file_path: null, error_code: null, retry_at: null, parent_id: null,
  priority: 0, scheduled_at: null, created_at: null, completed_at: null, speed: 0, eta: null,
  file_size: null, error_message: null, job_kind: 'media', resume_mode: 'not_pausable' });

function fixture(api, options = {}) {
  assert.equal(typeof api.createSession, 'function', 'Generated clients must expose a shared control session');
  const calls = [];
  let number = 0;
  const engine = { instance: 'engine-one', cursor: 0, revision: 0, jobs: [job('first')],
    events: [], mode: 'ok', protocol_min: 1, pageFailures: 0, more: false,
    features: ['events.snapshots', 'settings.revisions', 'downloads.source'] };
  const transport = async request => {
    calls.push(plain(request));
    let data;
    const page = () => ({ jobs: engine.jobs.slice(0, request.params.limit || 25), next_after: engine.more ? engine.jobs.at(-1).id : null,
      counts: { total: engine.jobs.length, unfinished: engine.jobs.filter(item => item.status === 'queued').length,
        paused: 0, scheduled: 0, history: 0 } });
    const settings = () => ({ revision: engine.revision, values: { ...values, max_concurrent: 3 + engine.revision } });
    const fail = (code, details = {}) => ({ protocol_version: 1, request_id: request.request_id, ok: false,
      error: { code, message: code, request_id: request.request_id, retryable: false, retry_at: null, provenance: 'local', details } });
    if (request.operation === 'capabilities.get') data = { protocol_min: engine.protocol_min, protocol_max: 2,
      max_message_bytes: 65536, max_payload_bytes: 64512, instance_id: engine.instance, fixture_only: false,
      paired_loopback: false, features: engine.features, component_versions: {} };
    else if (request.operation === 'events.read') {
      if (engine.mode === 'offline') throw new Error('connection lost');
      if (engine.pageFailures-- > 0) return fail('MESSAGE_TOO_LARGE');
      const resnapshot = request.params.instance_id !== engine.instance;
      data = { instance_id: engine.instance, cursor: engine.cursor, resnapshot, has_more: false,
        events: resnapshot ? [] : engine.events,
        snapshot: resnapshot ? { ...page(), instance_id: engine.instance, cursor: engine.cursor, settings: settings() } : null };
    } else if (request.operation === 'settings.get') {
      if (engine.mode === 'read-failure') throw new Error('read failed');
      data = settings();
    } else if (request.operation === 'downloads.list') {
      if (request.params.after) data = { jobs: [job('older')], next_after: null, counts: { total: 2, unfinished: 2, paused: 0, scheduled: 0, history: 0 } };
      else data = page();
    } else if (request.operation === 'settings.patch') {
      if (engine.mode === 'lost') throw new Error('reply lost');
      if (engine.mode === 'conflict') return fail('REVISION_CONFLICT', { settings: settings() });
      data = { revision: 1, values: { ...values, ...request.params.patch } };
    } else if (request.operation === 'downloads.enqueue') {
      if (engine.mode === 'lost') throw new Error('reply lost');
      data = { job_id: 'accepted-once' };
    } else if (request.operation === 'downloads.action') {
      data = { job_id: request.params.job_id, action: request.params.action, accepted_status: 'cancelling', accepted_revision: 1 };
    } else throw new Error('Unexpected operation: ' + request.operation);
    return { protocol_version: 1, request_id: request.request_id, ok: true, data };
  };
  return { engine, calls, session: api.createSession(api.createClient(transport), () => 'intent-' + (++number), options) };
}

for (const [name, api] of [['module', moduleClient], ['classic', context.vidGrabProtocol]]) {
  test(name + ': snapshots negotiate and preserve complete counts', async () => {
    const { session, calls } = fixture(api);
    const state = await session.sync();
    assert.equal(state.snapshot.jobs[0].url, 'https://fixture.invalid/first');
    assert.equal(state.snapshot.settings.values.default_format, values.default_format);
    assert.equal(state.snapshot.counts.unfinished, 1);
    assert.equal(calls[0].operation, 'capabilities.get');
    assert.equal(calls[1].operation, 'events.read');
    assert.ok(calls[1].params.limit <= 25);
    assert.equal(calls.length, 2);
  });
  test(name + ': incompatible capabilities stop mutations', async () => {
    const { session, engine, calls } = fixture(api);
    engine.protocol_min = 2;
    await assert.rejects(session.mutate('downloads.enqueue', { url: 'https://fixture.invalid/media' }), error => error.code === 'APP_INCOMPATIBLE');
    assert.deepEqual(calls.map(item => item.operation), ['capabilities.get']);
  });
  test(name + ': engines without exact source URLs report an upgrade before reading jobs', async () => {
    const { session, engine, calls } = fixture(api);
    engine.features = ['events.snapshots', 'settings.revisions'];
    await assert.rejects(session.sync(), error => error.code === 'APP_INCOMPATIBLE');
    assert.deepEqual(calls.map(item => item.operation), ['capabilities.get']);
  });
  test(name + ': direct reads also negotiate compatibility before accessing settings', async () => {
    const { session, engine, calls } = fixture(api);
    engine.features = [];
    await assert.rejects(session.read('settings.get'), error => error.code === 'APP_INCOMPATIBLE');
    assert.deepEqual(calls.map(item => item.operation), ['capabilities.get']);
  });
  test(name + ': simultaneous initial mutations submit only one intent', async () => {
    const { session, calls } = fixture(api);
    const outcomes = await Promise.allSettled([
      session.mutate('downloads.enqueue', { url: 'https://fixture.invalid/one' }),
      session.mutate('downloads.enqueue', { url: 'https://fixture.invalid/two' }),
    ]);
    assert.equal(outcomes.filter(item => item.status === 'fulfilled').length, 1);
    assert.equal(outcomes.find(item => item.status === 'rejected').reason.code, 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal(calls.filter(item => item.operation === 'downloads.enqueue').length, 1);
  });
  test(name + ': simultaneous reconnect replays submit the retained intent once', async () => {
    const { session, engine, calls } = fixture(api);
    engine.mode = 'lost';
    await assert.rejects(session.mutate('downloads.enqueue', { url: 'https://fixture.invalid/one' }));
    engine.mode = 'offline';
    await assert.rejects(session.sync());
    engine.mode = 'ok';
    const outcomes = await Promise.allSettled([session.replay(), session.replay()]);
    assert.equal(outcomes.filter(item => item.status === 'fulfilled').length, 1);
    assert.equal(outcomes.find(item => item.status === 'rejected').reason.code, 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal(calls.filter(item => item.operation === 'downloads.enqueue').length, 2);
  });
  test(name + ': invalidations synchronize settings and downloads', async () => {
    const { session, engine } = fixture(api);
    await session.sync();
    engine.revision = 1; engine.cursor = 2; engine.jobs = [job('second', 1)];
    engine.events = [ { sequence: 1, topic: 'settings', entity_id: null, revision: 1, kind: 'changed' },
      { sequence: 2, topic: 'downloads', entity_id: 'second', revision: 1, kind: 'changed' } ];
    const state = await session.sync();
    assert.equal(state.snapshot.settings.revision, 1);
    assert.equal(state.snapshot.jobs[0].id, 'second');
    assert.equal(state.snapshot.cursor, 2);
  });
  test(name + ': failed reads cannot consume the event cursor', async () => {
    const { session, engine } = fixture(api);
    await session.sync();
    engine.cursor = 1; engine.revision = 1; engine.mode = 'read-failure';
    engine.events = [{ sequence: 1, topic: 'settings', entity_id: null, revision: 1, kind: 'changed' }];
    await assert.rejects(session.sync());
    assert.equal(session.state.snapshot.cursor, 0);
    engine.mode = 'ok';
    assert.equal((await session.sync()).snapshot.settings.revision, 1);
  });
  test(name + ': reconnect snapshots replace the previous engine view', async () => {
    const { session, engine } = fixture(api);
    await session.sync(); engine.mode = 'offline';
    await assert.rejects(session.sync());
    assert.equal(session.state.connected, false);
    engine.mode = 'ok'; engine.instance = 'engine-two'; engine.jobs = [job('replacement')];
    const state = await session.sync();
    assert.equal(state.capabilities.instance_id, 'engine-two');
    assert.equal(state.snapshot.jobs[0].id, 'replacement');
  });
  test(name + ': oversized reads reduce their bounded page', async () => {
    const { session, engine, calls } = fixture(api);
    engine.pageFailures = 2;
    await session.sync();
    const limits = calls.filter(item => item.operation === 'events.read').map(item => item.params.limit);
    assert.equal(limits.length, 3);
    assert.ok(limits[2] < limits[1] && limits[1] < limits[0]);
  });
  test(name + ': lost enqueue replies need explicit replay with the original intent', async () => {
    const { session, engine, calls } = fixture(api);
    await session.sync(); engine.mode = 'lost';
    const params = { url: 'https://fixture.invalid/media' };
    await assert.rejects(session.mutate('downloads.enqueue', params), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    params.url = 'https://fixture.invalid/changed';
    engine.mode = 'ok'; await session.sync();
    assert.equal(calls.filter(item => item.operation === 'downloads.enqueue').length, 1);
    await assert.rejects(session.mutate('downloads.enqueue', params), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal((await session.replay()).job_id, 'accepted-once');
    const writes = calls.filter(item => item.operation === 'downloads.enqueue');
    assert.equal(writes.length, 2);
    assert.deepEqual(writes[1].params, writes[0].params);
    assert.equal(writes[1].idempotency_key, writes[0].idempotency_key);
    assert.equal(session.pending, null);
  });
  test(name + ': retained nested options cannot be edited through the public intent', async () => {
    const { session, engine } = fixture(api);
    engine.mode = 'lost';
    await assert.rejects(session.mutate('downloads.enqueue', { url: 'https://fixture.invalid/media',
      options: { subtitle_langs: ['en'] } }));
    assert.throws(() => { session.pending.params.options.subtitle_langs.push('fr'); }, error => error.name === 'TypeError');
    assert.throws(() => { session.pending.key = 'changed'; }, TypeError);
    assert.deepEqual(plain(session.pending.params.options.subtitle_langs), ['en']);
  });
  test(name + ': settings conflicts preserve the server view without a retry', async () => {
    const { session, engine, calls } = fixture(api);
    await session.sync(); engine.mode = 'conflict';
    await assert.rejects(session.mutate('settings.patch', { expected_revision: 0, patch: { max_concurrent: 6 } }),
      error => error.code === 'REVISION_CONFLICT');
    assert.equal(session.state.snapshot.settings.revision, 0);
    assert.equal(session.pending, null);
    assert.equal(calls.filter(item => item.operation === 'settings.patch').length, 1);
  });
  test(name + ': credential payloads cannot enter retained session mutations', async () => {
    const { session, calls } = fixture(api);
    await assert.rejects(session.mutate('auth.create', { cookies: [{ value: 'secret' }] }), error => error.code === 'BAD_REQUEST');
    assert.equal(session.pending, null);
    assert.equal(calls.length, 0);
  });
  test(name + ': older jobs require an explicit bounded page read', async () => {
    const { session, engine, calls } = fixture(api);
    engine.more = true;
    await session.sync();
    assert.equal(calls.filter(item => item.operation === 'downloads.list').length, 0);
    const state = await session.loadMore();
    assert.deepEqual(plain(state.snapshot.jobs).map(item => item.id), ['first', 'older']);
    assert.equal(calls.at(-1).params.after, 'first');
    assert.ok(calls.at(-1).params.limit <= 25);
  });
  test(name + ': explicit reads cannot submit a mutation', async () => {
    const { session, calls } = fixture(api);
    await assert.rejects(session.read('downloads.enqueue', { url: 'https://fixture.invalid/media' }), error => error.code === 'BAD_REQUEST');
    assert.equal(calls.length, 0);
  });
}
