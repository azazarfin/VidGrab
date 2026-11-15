// Transport-independent generated-client checks; no network or child processes.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createClient as createModuleClient } from '../packages/protocol/generated/client.mjs';

const path = 'packages/protocol/generated/client.js';
assert.ok(existsSync(path), 'P04 must generate a classic-script client');
const context = vm.createContext({ TextEncoder });
vm.runInContext(readFileSync(path, 'utf8'), context, { filename: path });
const { createClient } = context.vidGrabProtocol;
const schema = JSON.parse(readFileSync('packages/protocol/schema-v1.json', 'utf8'));
const settingsSchema = schema.operations['settings.get'].result;
const settingsName = settingsSchema.properties.values.$ref.split('/').at(-1);
const values = Object.fromEntries(Object.entries(settingsSchema.$defs[settingsName].properties)
  .map(([key, value]) => [key, value.default]));
values.download_dir = '/fixture/media';
const snapshot = { revision: 0, values };
let calls = [];
let mode = 'success';
const transport = async request => {
  calls.push(request);
  if (mode === 'lost') throw new Error('connection closed');
  if (mode === 'conflict' || mode === 'wrong-error-id') return { protocol_version: 1, request_id: request.request_id, ok: false,
    error: { code: 'REVISION_CONFLICT', message: 'Reload current settings', retryable: false,
      retry_at: null, request_id: mode === 'wrong-error-id' ? 'other-request' : request.request_id,
      provenance: 'local', details: { settings: snapshot } } };
  return { protocol_version: mode === 'version' ? 2 : 1,
    request_id: mode === 'wrong-id' ? 'other-request' : request.request_id,
    ok: true, data: mode === 'bad-data' ? { revision: 0 } : mode === 'bad-settings' ? { revision: 0, values: {} } : snapshot };
};
const desktop = createModuleClient(transport);
const extension = createClient(transport);
assert.equal(JSON.stringify(await desktop.request('settings.get', {}, { request_id: 'desktop' })), JSON.stringify(snapshot));
assert.equal(JSON.stringify(await extension.request('settings.get', {}, { request_id: 'extension' })), JSON.stringify(snapshot));
for (const [testMode, code] of [['wrong-id', 'BAD_RESPONSE'], ['wrong-error-id', 'BAD_RESPONSE'], ['version', 'APP_INCOMPATIBLE'], ['bad-data', 'BAD_RESPONSE'], ['bad-settings', 'BAD_RESPONSE'], ['conflict', 'REVISION_CONFLICT']]) {
  mode = testMode;
  await assert.rejects(extension.request('settings.get', {}, { request_id: testMode }), error => error.code === code);
}
const before = calls.length;
for (const operation of ['process.execute', '__proto__', 'constructor']) {
  await assert.rejects(extension.request(operation, {}, { request_id: 'invalid' }), error => error.code === 'BAD_REQUEST');
}
await assert.rejects(extension.request('settings.patch', { expected_revision: '0', patch: {} },
  { request_id: 'invalid', idempotency_key: 'invalid' }), error => error.code === 'BAD_REQUEST');
assert.equal(calls.length, before, 'Invalid requests must not reach the transport');
await assert.rejects(extension.request('settings.patch', { expected_revision: 0, patch: { default_format: 'a'.repeat(64900) } },
  { request_id: 'size-limit', idempotency_key: 'size-limit' }), error => error.code === 'BAD_REQUEST');
assert.equal(calls.length, before, 'The compatibility envelope must retain space inside the native frame');
mode = 'lost';
await assert.rejects(extension.request('settings.patch', { expected_revision: 0, patch: { max_concurrent: 5 } },
  { request_id: 'lost', idempotency_key: 'stable-key' }), error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
assert.equal(calls.length, before + 1, 'Lost mutations must never be retried automatically');

const mediaUrl = 'https://fixture.invalid/media.mp4';
const authParams = { instance_id: 'engine', url: mediaUrl,
  cookies: [{ domain: '.fixture.invalid', name: 'session', value: 'private-context', expirationDate: 1900000000 }] };
const mediaCases = [
  ['probes.create', { url: mediaUrl, format_limit: 1 }, { probe_id: 'probe', url: mediaUrl, title: 'Fixture',
    thumbnail: null, duration: 12, uploader: null, is_playlist: false, entries: null, formats: [], subtitles: null,
    format_count: 0, entry_count: 0, formats_truncated: false, entries_truncated: false }],
  ['auth.create', authParams, { reference: 'reference', instance_id: 'engine', expires_at: 1900000000 }],
  ['downloads.action', { job_id: 'job', expected_revision: 2, action: 'retry', auth_ref: 'reference' },
    { job_id: 'job', action: 'retry', accepted_status: 'queued', accepted_revision: 3 }],
  ['downloads.authenticate', { job_id: 'job', expected_revision: 2, auth_ref: 'reference' },
    { job_id: 'job', action: 'authenticate', accepted_status: 'queued', accepted_revision: 3 }],
  ['downloads.enqueue', { url: mediaUrl, auth_ref: 'reference',
    options: { download_subtitles: true, subtitle_langs: ['en', 'pt-BR'], embed_thumbnail: true } }, { job_id: 'job' }],
];
for (const create of [createModuleClient, createClient]) {
  let mediaCalls = 0;
  let replyMode = 'success';
  let result;
  const client = create(async request => {
    mediaCalls++;
    if (replyMode === 'lost') throw new Error('connection closed');
    if (replyMode === 'error') return { protocol_version: 1, request_id: request.request_id, ok: false,
      error: { code: 'AUTH_CONTEXT_EXPIRED', message: 'Share fresh authentication', retryable: false,
        retry_at: null, request_id: request.request_id, provenance: 'local', details: {} } };
    return { protocol_version: 1, request_id: request.request_id, ok: true, data: result };
  });
  for (const [operation, params, expected] of mediaCases) {
    result = expected;
    const actual = await client.request(operation, params, { request_id: 'media', idempotency_key: 'stable-media' });
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
  }
  const acceptedCalls = mediaCalls;
  for (const [operation, params] of [
    ['probes.create', { url: mediaUrl, format_limit: 101 }],
    ['probes.create', { url: mediaUrl, entry_limit: 129 }],
    ['auth.create', { ...authParams, ttl_seconds: 901 }],
    ['auth.create', { ...authParams, cookies: Array(129).fill(authParams.cookies[0]) }],
    ['auth.create', { ...authParams, headers: { Authorization: 'private-context' } }],
    ['downloads.action', { job_id: 'job', expected_revision: 2, action: 'execute' }],
    ['downloads.authenticate', { job_id: 'job', expected_revision: '2', auth_ref: 'reference' }],
    ['downloads.enqueue', { url: mediaUrl, options: { extra_args: { exec: 'command' } } }],
  ]) {
    await assert.rejects(client.request(operation, params, { request_id: 'invalid-media', idempotency_key: 'invalid-media' }),
      error => error.code === 'BAD_REQUEST');
  }
  assert.equal(mediaCalls, acceptedCalls, 'Unsupported options and bounds must fail before transport');
  replyMode = 'error';
  await assert.rejects(client.request('downloads.authenticate', mediaCases[3][1],
    { request_id: 'expired-context', idempotency_key: 'expired-context' }), error => error.code === 'AUTH_CONTEXT_EXPIRED');
  replyMode = 'lost';
  for (const [operation, params] of mediaCases.filter(([operation]) => operation !== 'probes.create')) {
    const sent = mediaCalls;
    await assert.rejects(client.request(operation, params, { request_id: 'lost-media', idempotency_key: 'lost-media' }),
      error => error.code === 'REQUEST_OUTCOME_UNKNOWN');
    assert.equal(mediaCalls, sent + 1, `${operation} must be forwarded exactly once after a lost reply`);
  }
}
console.log('Generated module/classic clients: strict settings/media contracts, typed errors, bounded options and no mutation replay passed');
