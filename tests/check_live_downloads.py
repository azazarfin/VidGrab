"""Opt-in bounded anonymous source-v1 downloads through actual owned workers.

Retain isolated state and media outside the repository. Reports omit source and
extracted URLs, titles, job/probe identities, raw errors and output paths.
"""

import argparse
import asyncio
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
import math
import os
from pathlib import Path
import platform
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from tests import check_live_sites as live
from tests.check_live_diagnostics import Observer, PUBLIC_CONTROL_CASES


TIME_LIMIT = 120
BYTE_LIMIT = 64 * 1024**2
SAMPLE_INTERVAL = 0.25
TERMINAL = frozenset({'complete', 'failed', 'cancelled'})
ERROR_CODES = live.ERROR_CATEGORIES | frozenset({'TIME_LIMIT', 'BYTE_LIMIT',
    'FORMAT_UNAVAILABLE', 'EMPTY_METADATA', 'VERIFICATION_FAILED', 'UNCLASSIFIED_FAILURE',
    'UPSTREAM_PERMANENT', 'UPSTREAM_TRANSIENT', 'UPSTREAM_RATE_LIMITED', 'TRANSPORT_ERROR',
    'ADMISSION_LIMITED', 'OWNERSHIP_REJECTED', 'BROWSER_CONTEXT_REQUIRED'})
PUBLIC_CASES = [dict(case) for case in PUBLIC_CONTROL_CASES] + [dict(case) for case in live.PUBLIC_CASES
    if case['id'] in {'mux-bbb', 'akamai-envivio', 'iana-example'}]


def select_format(probe):
    if probe.get('is_playlist'):
        return None
    def priority(item):
        video = item.get('vcodec') not in (None, 'none')
        audio = item.get('acodec') not in (None, 'none')
        kind = 0 if video and audio else 1 if audio else 2 if video else 3
        bitrate = item.get('tbr')
        return kind, bitrate if type(bitrate) in (int, float) and math.isfinite(bitrate) and bitrate > 0 else math.inf
    formats = [item for item in probe.get('formats') or [] if isinstance(item.get('format_id'), str)
               and item['format_id']]
    return min(formats, key=priority)['format_id'] if formats else None


def stop_reason(job, elapsed, observed_bytes):
    if job['status'] in TERMINAL:
        return None
    if job['status'] in {'retry_wait', 'waiting_for_auth'}:
        code = job.get('error_code')
        return code if code in ERROR_CODES else 'UNCLASSIFIED_FAILURE'
    if elapsed >= TIME_LIMIT:
        return 'TIME_LIMIT'
    if observed_bytes >= BYTE_LIMIT:
        return 'BYTE_LIMIT'
    return None


def summarize_job(job):
    row = {'result': job['status'], 'assessment': 'matched' if job['status'] == 'complete' else 'mismatch'}
    if job['status'] != 'complete':
        code = job.get('error_code')
        row['error_category'] = code if code in ERROR_CODES else 'UNCLASSIFIED_FAILURE'
    return row


def summarize_reply(case, reply, seconds):
    row = live.summarize_result(case, reply, seconds)
    code = reply.get('error', {}).get('code')
    if reply.get('ok') is not True and code in ERROR_CODES:
        row['error_category'] = code
    return row


def enqueue_parameters(case, probe, selected, *, use_defaults=False):
    if use_defaults:
        return {'url': case['url'], 'format_id': 'best'}
    return {'url': case['url'], 'format_id': selected, 'probe_id': probe['probe_id']}


async def cancel_guarded(send, job, reason):
    reply = await send('downloads.action', {'job_id': job['id'], 'expected_revision': job['revision'],
        'action': 'cancel'}, key='live-cancel-' + str(uuid4()))
    # A lost/failed mutation is never replayed automatically.
    if reply.get('ok') is not True:
        raise RuntimeError('Guard cancellation did not return a definite success')
    return {'error_category': reason if reason in ERROR_CODES else 'UNCLASSIFIED_FAILURE',
            'guard_cancelled': True}


def observed_bytes(root):
    total = 0
    for tree in (root / 'media', root / 'state' / 'attempts'):
        for path in tree.rglob('*'):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # An owned worker may rename/remove its own temporary file.
    return total


def verify_output(path, media, components, expected_duration=None):
    path = Path(path)
    try:
        valid = (not path.is_symlink() and path.is_file() and
                 path.resolve(strict=True).is_relative_to(media.resolve(strict=True)) and
                 0 < path.stat().st_size <= BYTE_LIMIT)
    except OSError:
        valid = False
    if not valid:
        raise ValueError('Output must be a nonempty regular file in the owned media directory')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    probe = subprocess.run([components['ffprobe'], '-v', 'error', '-show_entries',
        'format=duration:stream=codec_name,codec_type', '-of', 'json', str(path)],
        capture_output=True, timeout=20)
    probe.check_returncode()
    data = json.loads(probe.stdout)
    duration = float(data.get('format', {}).get('duration', 0))
    streams = data.get('streams') or []
    if not math.isfinite(duration) or duration <= 0 or not streams:
        raise ValueError('Output has no usable duration or streams')
    if expected_duration is not None and expected_duration > 0 and abs(duration - expected_duration) > max(3, expected_duration * 0.02):
        raise ValueError('Output duration does not match the full probed media')
    decode = subprocess.run([components['ffmpeg'], '-nostdin', '-v', 'error', '-xerror',
        '-i', str(path), '-map', '0:v?', '-map', '0:a?', '-f', 'null', '-'],
        capture_output=True, timeout=90)
    decode.check_returncode()
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Output bytes changed during verification')
    return {'size': path.stat().st_size, 'sha256': digest, 'duration': round(duration, 3),
            'video_streams': sum(item.get('codec_type') == 'video' for item in streams),
            'audio_streams': sum(item.get('codec_type') == 'audio' for item in streams),
            'full_decode_passed': True}


def audit_database(path):
    with closing(sqlite3.connect(path)) as db:
        audit = {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                 for name in ('downloads', 'download_attempts', 'request_probes', 'request_leases')}
        audit['unfinished_jobs'] = db.execute("SELECT count(*) FROM downloads WHERE status NOT IN ('complete','failed','cancelled')").fetchone()[0]
        audit['unreleased_owners'] = db.execute('SELECT count(*) FROM runtime_owners WHERE released_at IS NULL').fetchone()[0]
        audit['attempt_states'] = dict(db.execute('SELECT status,count(*) FROM download_attempts GROUP BY status'))
    return audit


async def run_downloads(output, components, versions, *, direct_defaults=False):
    import httpx
    from backend.engine import create_engine_app
    from backend.engine.execution import ProcessDownloadExecutor
    from backend.runtime import RuntimeConfig

    root = Path(tempfile.mkdtemp(prefix='vidgrab-live-downloads-')).resolve()
    runtime = RuntimeConfig(root / 'state', root / 'media')
    token = secrets.token_hex(32)
    engine = create_engine_app(runtime, token=token, port=43210, components=components,
        component_versions=versions, media_mode='ordinary', start_queue=True)
    (output / 'runtime.json').write_text(json.dumps({'root': str(root),
        'scope': 'isolated public download state/media; retained'}, indent=2) + '\n')
    observer = Observer()
    rows = []
    with ExitStack() as stack, (output / 'rows.jsonl').open('x', encoding='utf-8') as stream:
        observer.install(stack)
        execute = ProcessDownloadExecutor.run
        async def execute_observed(executor, **options):
            try:
                return await execute(executor, **options)
            except Exception as error:
                observer.error(error)
                raise
        stack.enter_context(patch.object(ProcessDownloadExecutor, 'run', execute_observed))
        async with engine.app.router.lifespan_context(engine.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine),
                    base_url='http://127.0.0.1:43210', headers={'Authorization': 'Bearer ' + token}) as client:
                async def send(operation, params, key=None):
                    request = {'protocol_version': 1, 'request_id': str(uuid4()),
                               'operation': operation, 'params': params}
                    if key is not None:
                        request['idempotency_key'] = key
                    return (await client.post('/v1/requests', json=request)).json()
                cases = [case for case in PUBLIC_CASES if case['media_type'] == 'direct'] if direct_defaults else PUBLIC_CASES
                for case in cases:
                    started = time.monotonic()
                    row = {key: case[key] for key in ('id', 'media_type', 'authentication')}
                    row['phase'] = 'download'
                    probe = await send('probes.create', {'url': case['url']})
                    if probe.get('ok') is not True:
                        row.update(summarize_reply(case, probe, time.monotonic() - started))
                        row['phase'] = 'probe-before-download'
                    else:
                        data = probe['data']
                        selected = select_format(data)
                        if selected is None or case['expected'] == 'unsupported':
                            row.update(result='no_download', assessment='mismatch', error_category='EMPTY_METADATA')
                        else:
                            row['selection'] = 'explicit-app-defaults' if direct_defaults else 'probe-quality'
                            reply = await send('downloads.enqueue', enqueue_parameters(case, data, selected,
                                use_defaults=direct_defaults), key='live-enqueue-' + str(uuid4()))
                            if reply.get('ok') is not True:
                                row.update(summarize_reply(case, reply, time.monotonic() - started))
                                row['phase'] = 'enqueue'
                            else:
                                job_id = reply['data']['job_id']
                                baseline_bytes = await asyncio.to_thread(observed_bytes, root)
                                job_started = time.monotonic()
                                guard = None
                                while True:
                                    page = await send('downloads.list', {'limit': 50})
                                    if page.get('ok') is not True:
                                        raise RuntimeError('Job snapshot failed')
                                    job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
                                    if job['status'] in TERMINAL:
                                        break
                                    size = max(0, (await asyncio.to_thread(observed_bytes, root)) - baseline_bytes)
                                    reason = stop_reason(job, time.monotonic() - job_started, size)
                                    if reason:
                                        guard = await cancel_guarded(send, job, reason)
                                        page = await send('downloads.list', {'limit': 50})
                                        job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
                                        if job['status'] != 'cancelled':
                                            raise RuntimeError('Guard cancellation did not join the job')
                                        break
                                    await asyncio.sleep(SAMPLE_INTERVAL)
                                row.update(summarize_job(job))
                                if guard:
                                    row.update(guard, assessment='mismatch')
                                if job['status'] == 'complete':
                                    try:
                                        row['output'] = await asyncio.to_thread(verify_output, job['file_path'],
                                            runtime.download_dir, components, data.get('duration'))
                                    except Exception:
                                        row.update(assessment='mismatch', error_category='VERIFICATION_FAILED')
                    row['diagnostics'] = observer.take()
                    row['seconds'] = round(time.monotonic() - started, 3)
                    rows.append(row)
                    line = json.dumps(row)
                    stream.write(line + '\n'); stream.flush(); os.fsync(stream.fileno())
                    print(line, flush=True)
    audit = audit_database(runtime.db_path)
    if any(audit[name] for name in ('request_probes', 'request_leases', 'unfinished_jobs', 'unreleased_owners')) or engine.app.state.probes.inflight or engine.app.state.queue.active_tasks or engine.app.state.owner.active:
        raise RuntimeError('Isolated runtime did not finish with joined workers and released authority')
    return rows, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--direct-defaults', action='store_true', help='Separate direct-MP4 lane with explicitly selected app defaults')
    args = parser.parse_args()
    if not args.allow_public_network:
        parser.error('Public downloads require --allow-public-network')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; previous evidence is preserved')
    from backend.engine.resources import check_components
    components, versions = check_components(args.tools.resolve(strict=True))
    args.output.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    inputs = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), Path(__file__).with_name('check_live_download_helpers.py'),
                     source / 'tests/check_live_sites.py', source / 'tests/check_live_diagnostics.py')}
    report = {'date_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
        'python': sys.version.split()[0], 'yt_dlp': metadata.version('yt-dlp'), 'components': versions,
        'input_sha256': inputs, 'signed_in': 'pending by user choice', 'playlist_download': 'pending',
        'scope': 'source-v1 public downloads; actual owned workers; frozen/native/browser acceptance separate',
        'direct_defaults': args.direct_defaults,
        'limits': {'job_seconds': TIME_LIMIT, 'observed_bytes': BYTE_LIMIT, 'sample_interval': SAMPLE_INTERVAL,
                   'scope': 'sampled observation then joined cancellation; not a hard wire byte ceiling'},
        'execution_completed': False}
    started = time.monotonic(); previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report['rows'], report['cleanup_audit'] = asyncio.run(run_downloads(args.output, components, versions,
            direct_defaults=args.direct_defaults))
        report['execution_completed'] = True
    except Exception:
        report['harness_error'] = 'HARNESS_ERROR'
    finally:
        logging.disable(previous)
        report['seconds'] = round(time.monotonic() - started, 3)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'execution_completed': report['execution_completed'], 'seconds': report['seconds']}), flush=True)
    raise SystemExit(0 if report['execution_completed'] else 1)


if __name__ == '__main__':
    main()
