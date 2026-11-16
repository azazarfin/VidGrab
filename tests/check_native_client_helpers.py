"""Opt-in regressions for the actual-client diagnostic delivery boundary."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_native_clients import INSTRUMENT
from tests.native_client_support import chrome_extension_worker, media_fixture


class DeliveryBoundaryTests(unittest.TestCase):
    def test_installed_chrome_ignores_an_unrelated_extension_worker_event(self):
        identifier = 'ikjpekhbbaehdhigakbahlhafaojbjpm'
        expected = SimpleNamespace(url=f'chrome-extension://{identifier}/background/service-worker.js')
        unrelated = SimpleNamespace(url='chrome-extension://admccjkmockfdflocgggjfgdacdodkdf/background.js')

        class Context:
            service_workers = []

            def wait_for_event(self, event, *, timeout, predicate=lambda worker: True):
                assert event == 'serviceworker' and timeout == 15000
                return next(worker for worker in (unrelated, expected) if predicate(worker))

        self.assertIs(chrome_extension_worker(Context(), identifier), expected)

    def test_loopback_media_returns_a_valid_suffix_for_verified_resume(self):
        with media_fixture() as (base, fixture):
            fixture['release'].set()
            with urlopen(base + '/resume.wav', timeout=5) as response:
                payload, validator = response.read(), response.headers['ETag']
            start = 65536
            request = Request(base + '/resume.wav', headers={
                'Range': f'bytes={start}-', 'If-Range': validator})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers['Content-Range'],
                    f'bytes {start}-{len(payload)-1}/{len(payload)}')
                self.assertEqual(response.headers['ETag'], validator)
                self.assertEqual(response.read(), payload[start:])

    def test_readonly_tauri_filters_only_an_actual_returned_reply(self):
        script = r"""
const assert = require('node:assert/strict');
const callbacks = new Map(), deliveries = [], wire = [];
global.window = global;
const internals = {};
Object.defineProperty(window, '__TAURI_INTERNALS__', {value: internals});
Object.defineProperty(internals, 'callbacks', {value: callbacks});
const invoke = (command, payload) => new Promise(resolve => {
  const callback = wire.length + 1;
  callbacks.set(callback, resolve);
  wire.push(JSON.stringify(payload));
  deliveries.push(response => callbacks.get(callback)(response));
});
Object.defineProperty(internals, 'invoke', {value: invoke});
new Function(SOURCE).call(window, '__TAURI_INTERNALS__', 'invoke', true);
(async () => {
  window.__nativeAcceptance.lose = 'downloads.enqueue';
  const request = {protocol_version:1, request_id:'request-one',
    operation:'downloads.enqueue', idempotency_key:'original-key', params:{url:'http://127.0.0.1/media.wav'}};
  const pending = internals.invoke('protocol_request', {request});
  assert.equal(window.__nativeAcceptance.dropped.length, 0, 'No loss before the native response');
  assert.equal(wire[0], '{"request":'+JSON.stringify(request)+'}', 'The native request is unchanged');
  const response = {protocol_version:1,request_id:request.request_id,ok:true,data:{job_id:'same-job'}};
  deliveries[0](response);
  const filtered = await pending;
  assert.equal(filtered.ok, false, 'The returned native response is filtered');
  assert.equal(filtered.error.code, 'REQUEST_OUTCOME_UNKNOWN');
  assert.equal(response.ok, true, 'The actual native response remains intact');
  assert.equal(internals.invoke, invoke, 'Readonly native invoke is untouched');
  assert.equal(window.__nativeAcceptance.dropped.length, 1);
  assert.equal(window.__nativeAcceptance.trace.filter(e => e.operation==='downloads.enqueue').length, 1);
  window.__nativeAcceptance.strip = true;
  const capabilities = {protocol_version:1,request_id:'request-two',operation:'capabilities.get',params:{}};
  const read = internals.invoke('protocol_request', {request: capabilities});
  const nativeCapabilities = {protocol_version:1,request_id:capabilities.request_id,ok:true,
    data:{features:['downloads.source','settings.revisions']}};
  deliveries[1](nativeCapabilities);
  assert.deepEqual((await read).data.features, ['settings.revisions']);
  assert.deepEqual(nativeCapabilities.data.features, ['downloads.source','settings.revisions']);
  assert.equal(wire.length, 2, 'The hook sends no extra requests');
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([os.environ.get('VIDGRAB_NATIVE_HELPER_NODE','node'), '-'], input='const SOURCE='+json.dumps(INSTRUMENT)+';\n'+script,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
