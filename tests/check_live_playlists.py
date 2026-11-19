"""Opt-in bounded anonymous source-v1 playlist downloads through actual owned workers.

Executes bounded public playlist probe + v1 playlist enqueue + child expansion/download
with allowlisted redacted in-worker diagnostics, 120-second / 64 MiB group guards and
full decode/hash verification of any completed child media. Reports omit source and
extracted URLs, titles, job/probe IDs, file paths, cookies and raw error messages.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from tests import check_live_downloads as dl
from tests import check_live_sites as live
from tests import check_live_worker_diagnostics as wdiag
from tests.check_live_diagnostics import Observer


DEFAULT_PLAYLIST_CASES = [
    {
        'id': 'youtube-playlist',
        'media_type': 'playlist',
        'authentication': 'public',
        'url': 'https://www.youtube.com/playlist?list=PLx-_-Kk4c89oOHEDQAojOXzEzemXxoqx6',
        'expected': 'playlist',
        'playlist_items': '1-2',
    },
]
WAITING_STATES = frozenset({'retry_wait', 'waiting_for_auth'})
REQUIRED_KEYS = frozenset({'id', 'media_type', 'authentication', 'url', 'expected', 'playlist_items'})


def _parse_bounded_items(spec):
    if not isinstance(spec, str) or not spec or len(spec) > 64:
        raise ValueError('playlist_items must be a short positive selection string')
    total = 0
    for part in spec.split(','):
        if not re.fullmatch(r'[1-9][0-9]*(?:-[1-9][0-9]*)?', part):
            raise ValueError('playlist_items must use positive integers or ascending ranges')
        if '-' in part:
            start_s, end_s = part.split('-', 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                raise ValueError('playlist_items ranges must be ascending')
            total += end - start + 1
        else:
            total += 1
    if not 1 <= total <= 16:
        raise ValueError('Bounded live playlist runner allows between 1 and 16 selected items')
    return total


def load_playlist_cases(path=None):
    cases = json.loads(Path(path).read_text(encoding='utf-8')) if path else DEFAULT_PLAYLIST_CASES
    if not isinstance(cases, list) or not 1 <= len(cases) <= 4:
        raise ValueError('Provide between one and four bounded public playlist cases')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != REQUIRED_KEYS:
            raise ValueError('Playlist case fields must match the public playlist specification')
        if not isinstance(case['id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', case['id']) or case['id'] in seen:
            raise ValueError('Case identifiers must be unique, non-sensitive labels')
        seen.add(case['id'])
        if case['media_type'] != 'playlist' or case['expected'] != 'playlist':
            raise ValueError('Playlist runner accepts only playlist cases')
        if case['authentication'] != 'public':
            raise ValueError('Signed-in execution is pending; this runner accepts public cases only')
        if not isinstance(case['url'], str) or not 1 <= len(case['url']) <= 2048:
            raise ValueError('A bounded HTTP(S) URL is required')
        try:
            parsed = urlsplit(case['url'])
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.username is None and parsed.password is None
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('A credential-free HTTP(S) URL is required')
        _parse_bounded_items(case['playlist_items'])
    return [dict(case) for case in cases]


def playlist_enqueue_parameters(case):
    return {
        'url': case['url'],
        'format_id': 'best',
        'options': {'is_playlist': True, 'playlist_items': case['playlist_items']},
    }


def _safe_error_code(code):
    return code if code in dl.ERROR_CODES else 'UNCLASSIFIED_FAILURE'


def playlist_stop_reason(parent, children, elapsed, observed_bytes):
    all_terminal = parent['status'] in dl.TERMINAL and all(c['status'] in dl.TERMINAL for c in children)
    if all_terminal:
        return None
    if parent['status'] in WAITING_STATES:
        return _safe_error_code(parent.get('error_code'))
    if children:
        non_terminal = [c for c in children if c['status'] not in dl.TERMINAL]
        if non_terminal and all(c['status'] in WAITING_STATES for c in non_terminal):
            return _safe_error_code(non_terminal[0].get('error_code'))
    if elapsed >= dl.TIME_LIMIT:
        return 'TIME_LIMIT'
    if observed_bytes >= dl.BYTE_LIMIT:
        return 'BYTE_LIMIT'
    return None


def summarize_playlist_group(parent, children, verified_outputs=None):
    verified_outputs = verified_outputs or {}
    child_summaries = []
    status_counts = Counter()
    for index, child in enumerate(children):
        st = child['status']
        status_counts[st] += 1
        entry = {'index': index + 1, 'status': st}
        if st != 'complete':
            if child.get('error_code') is not None:
                entry['error_category'] = _safe_error_code(child.get('error_code'))
        elif index in verified_outputs:
            entry['output'] = verified_outputs[index]
        child_summaries.append(entry)
    all_complete = (
        parent['status'] == 'complete'
        and len(children) > 0
        and all(c['status'] == 'complete' and idx in verified_outputs for idx, c in enumerate(children))
    )
    summary = {
        'result': parent['status'],
        'assessment': 'matched' if all_complete else 'mismatch',
        'child_count': len(children),
        'child_status_counts': dict(sorted(status_counts.items())),
        'children': child_summaries,
    }
    if parent['status'] != 'complete' and parent.get('error_code') is not None:
        summary['error_category'] = _safe_error_code(parent.get('error_code'))
    return summary


def make_multi_worker_entry_command(original_entry_command, output_dir, case_id, allocated):
    def wrapper(module_name, flag):
        if module_name == 'backend.engine.worker':
            idx = len(allocated) + 1
            diag_file = Path(output_dir) / f'worker-{case_id}-{idx}.json'
            allocated.append(diag_file)
            return wdiag.observed_entry_command(original_entry_command, diag_file, module_name, flag)
        return original_entry_command(module_name, flag)
    return wrapper


def audit_playlist_database(path):
    with closing(sqlite3.connect(path)) as db:
        audit = {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                 for name in ('downloads', 'download_attempts', 'request_probes', 'request_leases')}
        audit['playlist_parents'] = db.execute("SELECT count(*) FROM downloads WHERE job_kind='playlist' AND parent_id IS NULL").fetchone()[0]
        audit['playlist_children'] = db.execute('SELECT count(*) FROM downloads WHERE parent_id IS NOT NULL').fetchone()[0]
        audit['unfinished_jobs'] = db.execute("SELECT count(*) FROM downloads WHERE status NOT IN ('complete','failed','cancelled')").fetchone()[0]
        audit['unreleased_owners'] = db.execute('SELECT count(*) FROM runtime_owners WHERE released_at IS NULL').fetchone()[0]
        audit['attempt_states'] = dict(db.execute('SELECT status,count(*) FROM download_attempts GROUP BY status'))
    return audit


async def run_playlists(output, components, versions, cases):
    import httpx
    from backend.engine import create_engine_app
    from backend.engine import execution as engine_execution
    from backend.engine.execution import ProcessDownloadExecutor
    from backend.runtime import RuntimeConfig

    root = Path(tempfile.mkdtemp(prefix='vidgrab-live-playlists-')).resolve()
    runtime = RuntimeConfig(root / 'state', root / 'media')
    token = secrets.token_hex(32)
    engine = create_engine_app(runtime, token=token, port=43210, components=components,
        component_versions=versions, media_mode='ordinary', start_queue=True)
    (output / 'runtime.json').write_text(json.dumps({'root': str(root),
        'scope': 'isolated public playlist download state/media; retained'}, indent=2) + '\n', encoding='utf-8')

    observer = Observer()
    rows = []
    original_entry_command = engine_execution.entry_command
    active_wrapper = [original_entry_command]

    def dispatch_entry_command(module_name, flag):
        return active_wrapper[0](module_name, flag)

    with ExitStack() as stack, (output / 'rows.jsonl').open('x', encoding='utf-8') as stream:
        observer.install(stack)
        stack.enter_context(patch.object(engine_execution, 'entry_command', dispatch_entry_command))
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

                for case in cases:
                    started = time.monotonic()
                    allocated_workers = []
                    active_wrapper[0] = make_multi_worker_entry_command(
                        original_entry_command, output, case['id'], allocated_workers)
                    row = {key: case[key] for key in ('id', 'media_type', 'authentication', 'playlist_items')}
                    row['phase'] = 'playlist-download'
                    probe = await send('probes.create', {'url': case['url']})
                    if probe.get('ok') is not True:
                        row.update(dl.summarize_reply(case, probe, time.monotonic() - started))
                        row['phase'] = 'probe-before-playlist'
                    else:
                        data = probe['data']
                        row['probe_is_playlist'] = data.get('is_playlist') is True
                        row['probe_entry_count'] = data.get('entry_count', 0)
                        if not row['probe_is_playlist'] or row['probe_entry_count'] <= 0:
                            row.update(result='no_download', assessment='mismatch', error_category='EMPTY_METADATA')
                        else:
                            reply = await send('downloads.enqueue', playlist_enqueue_parameters(case),
                                key='live-playlist-enqueue-' + str(uuid4()))
                            if reply.get('ok') is not True:
                                row.update(dl.summarize_reply(case, reply, time.monotonic() - started))
                                row['phase'] = 'enqueue'
                            else:
                                parent_id = reply['data']['job_id']
                                baseline_bytes = await asyncio.to_thread(dl.observed_bytes, root)
                                job_started = time.monotonic()
                                guard = None
                                pre_cancel_children = []
                                while True:
                                    page = await send('downloads.list', {'limit': 50})
                                    if page.get('ok') is not True:
                                        raise RuntimeError('Playlist snapshot failed')
                                    jobs = page['data']['jobs']
                                    parent = next(item for item in jobs if item['id'] == parent_id)
                                    children = [item for item in jobs if item.get('parent_id') == parent_id]
                                    if parent['status'] in dl.TERMINAL and all(c['status'] in dl.TERMINAL for c in children):
                                        break
                                    size = max(0, (await asyncio.to_thread(dl.observed_bytes, root)) - baseline_bytes)
                                    reason = playlist_stop_reason(parent, children, time.monotonic() - job_started, size)
                                    if reason:
                                        pre_cancel_children = [
                                            {'status': c['status'],
                                             'error_category': _safe_error_code(c.get('error_code')) if c.get('error_code') else None}
                                            for c in children
                                        ]
                                        guard = await dl.cancel_guarded(send, parent, reason)
                                        page = await send('downloads.list', {'limit': 50})
                                        jobs = page['data']['jobs']
                                        parent = next(item for item in jobs if item['id'] == parent_id)
                                        children = [item for item in jobs if item.get('parent_id') == parent_id]
                                        if parent['status'] != 'cancelled' or any(c['status'] not in dl.TERMINAL for c in children):
                                            raise RuntimeError('Playlist guard cancellation did not join the group')
                                        break
                                    await asyncio.sleep(dl.SAMPLE_INTERVAL)

                                verified = {}
                                verify_failed = False
                                for idx, child in enumerate(children):
                                    if child['status'] == 'complete' and child.get('file_path'):
                                        try:
                                            verified[idx] = await asyncio.to_thread(
                                                dl.verify_output, child['file_path'], runtime.download_dir, components, None)
                                        except Exception:
                                            verify_failed = True
                                row.update(summarize_playlist_group(parent, children, verified))
                                if pre_cancel_children:
                                    row['pre_cancel_children'] = pre_cancel_children
                                if verify_failed:
                                    row.update(assessment='mismatch', error_category='VERIFICATION_FAILED')
                                if guard:
                                    row.update(guard, assessment='mismatch')
                    active_wrapper[0] = original_entry_command
                    row['probe_diagnostics'] = observer.take()
                    worker_diags = []
                    for wf in allocated_workers:
                        if wf.is_file():
                            worker_diags.append(json.loads(wf.read_text(encoding='utf-8')))
                    row['worker_count'] = len(allocated_workers)
                    row['worker_diagnostics'] = worker_diags
                    row['seconds'] = round(time.monotonic() - started, 3)
                    rows.append(row)
                    line = json.dumps(row)
                    stream.write(line + '\n'); stream.flush(); os.fsync(stream.fileno())
                    print(line, flush=True)

    audit = audit_playlist_database(runtime.db_path)
    if (any(audit[name] for name in ('request_probes', 'request_leases', 'unfinished_jobs', 'unreleased_owners'))
            or engine.app.state.probes.inflight or engine.app.state.queue.active_tasks or engine.app.state.owner.active):
        raise RuntimeError('Isolated runtime did not finish with joined workers and released authority')
    return rows, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--tools', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cases', type=Path)
    args = parser.parse_args()

    if not args.allow_public_network:
        parser.error('Public playlist downloads require --allow-public-network')
    if args.tools is None or args.output is None:
        parser.error('--tools and --output are required')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; previous evidence is preserved')

    cases = load_playlist_cases(args.cases)
    from backend.engine.resources import check_components
    components, versions = check_components(args.tools.resolve(strict=True))
    args.output.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    inputs = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), Path(__file__).with_name('check_live_playlist_helpers.py'),
                     source / 'tests/check_live_worker_diagnostics.py',
                     source / 'tests/check_live_downloads.py',
                     source / 'tests/check_live_diagnostics.py',
                     source / 'tests/check_live_sites.py')}
    report = {
        'date_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'yt_dlp': metadata.version('yt-dlp'),
        'components': versions,
        'input_sha256': inputs,
        'signed_in': 'pending by user choice',
        'scope': 'source-v1 bounded public playlist download; actual owned workers; no production repair',
        'limits': {'job_seconds': dl.TIME_LIMIT, 'observed_bytes': dl.BYTE_LIMIT,
                   'sample_interval': dl.SAMPLE_INTERVAL},
        'execution_completed': False,
    }
    started = time.monotonic()
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report['rows'], report['cleanup_audit'] = asyncio.run(run_playlists(
            args.output, components, versions, cases))
        report['execution_completed'] = True
    except Exception:
        report['harness_error'] = 'HARNESS_ERROR'
    finally:
        logging.disable(previous)
        report['seconds'] = round(time.monotonic() - started, 3)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'execution_completed': report['execution_completed'], 'seconds': report['seconds']}), flush=True)
    raise SystemExit(0 if report['execution_completed'] else 1)


if __name__ == '__main__':
    main()
