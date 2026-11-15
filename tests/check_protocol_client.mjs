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
console.log('Generated module/classic clients: two callers, strict schemas, response identity/version, typed conflict and no mutation replay passed');
