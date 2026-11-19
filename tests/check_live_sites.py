"""Opt-in public live probe compatibility; never enqueue or load browser cookies.

Reports contain counts and error categories, not URLs or extracted metadata.
The isolated runtime is retained outside the repository for inspection.
"""

import argparse
import asyncio
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
from urllib.parse import urlsplit
from uuid import uuid4


PUBLIC_CASES = [
    {'id': 'youtube-video', 'media_type': 'extractor', 'authentication': 'public',
     'url': 'https://www.youtube.com/watch?v=BaW_jenozKc', 'expected': 'media'},
    {'id': 'w3c-sintel', 'media_type': 'direct', 'authentication': 'public',
     'url': 'https://media.w3.org/2010/05/sintel/trailer.mp4', 'expected': 'media'},
    {'id': 'mux-bbb', 'media_type': 'hls', 'authentication': 'public',
     'url': 'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8', 'expected': 'media'},
    {'id': 'akamai-envivio', 'media_type': 'dash', 'authentication': 'public',
     'url': 'https://dash.akamaized.net/envivio/EnvivioDash3/manifest.mpd', 'expected': 'media'},
    {'id': 'youtube-playlist', 'media_type': 'playlist', 'authentication': 'public',
     'url': 'https://www.youtube.com/playlist?list=PLx-_-Kk4c89oOHEDQAojOXzEzemXxoqx6', 'expected': 'playlist'},
    {'id': 'iana-example', 'media_type': 'unsupported', 'authentication': 'public',
     'url': 'https://example.com/', 'expected': 'unsupported'},
]

ERROR_CATEGORIES = frozenset({'AUTH_REQUIRED', 'RATE_LIMITED', 'CAPACITY_EXCEEDED',
    'REQUEST_BUDGET_EXHAUSTED', 'NETWORK_ERROR', 'HTTP_ERROR', 'UNSUPPORTED_MEDIA',
    'INTERNAL_ERROR', 'REQUEST_REJECTED', 'HARNESS_ERROR'})


def load_cases(path=None):
    cases = json.loads(Path(path).read_text(encoding='utf-8')) if path else PUBLIC_CASES
    if not isinstance(cases, list) or not 1 <= len(cases) <= 16:
        raise ValueError('Provide between one and sixteen public cases')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'media_type', 'authentication', 'url', 'expected'}:
            raise ValueError('Case fields must contain only the public probe specification')
        if not isinstance(case['id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', case['id']) or case['id'] in seen:
            raise ValueError('Case identifiers must be unique, non-sensitive labels')
        seen.add(case['id'])
        if case['media_type'] not in {'extractor', 'direct', 'hls', 'dash', 'playlist', 'unsupported'}:
            raise ValueError('Unsupported media category')
        if case['authentication'] != 'public':
            raise ValueError('Signed-in execution is pending; this runner accepts public cases only')
        if case['expected'] not in {'media', 'playlist', 'unsupported'}:
            raise ValueError('Unsupported result expectation')
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
    return [dict(case) for case in cases]


def summarize_result(case, response, seconds):
    row = {key: case[key] for key in ('id', 'media_type', 'authentication', 'expected')}
    row.update(seconds=round(seconds, 3), phase='probe', assessment='mismatch')
    if response.get('ok') is True:
        data = response.get('data', {})
        row.update(result='metadata', is_playlist=data.get('is_playlist') is True,
                   format_count=data.get('format_count', 0), entry_count=data.get('entry_count', 0))
        for name in ('format_count', 'entry_count'):
            if type(row[name]) is not int or row[name] < 0:
                row[name] = 0
        if case['expected'] == 'playlist' and row['is_playlist'] and row['entry_count'] > 0:
            row['assessment'] = 'matched'
        elif case['expected'] == 'media' and not row['is_playlist'] and row['format_count'] > 0:
            row['assessment'] = 'matched'
        else:
            row['error_category'] = 'EMPTY_METADATA' if row['format_count'] == row['entry_count'] == 0 else 'UNEXPECTED_METADATA'
    else:
        code = response.get('error', {}).get('code')
        row.update(result='error', error_category=code if code in ERROR_CATEGORIES else 'UNCLASSIFIED_FAILURE')
        if case['expected'] == 'unsupported' and code == 'UNSUPPORTED_MEDIA':
            row['assessment'] = 'matched'
    return row


async def run_cases(cases, dispatch, on_row=None):
    rows = []
    for case in cases:
        started = time.monotonic()
        try:
            response = await dispatch(case)
            row = summarize_result(case, response, time.monotonic() - started)
        except Exception:
            row = summarize_result(case, {'ok': False, 'error': {'code': 'HARNESS_ERROR'}}, time.monotonic() - started)
        rows.append(row)
        if on_row is not None:
            on_row(row)
    return rows


async def live_probe(cases, output, components, versions):
    import httpx
    from backend.engine import create_engine_app
    from backend.runtime import RuntimeConfig

    root = Path(tempfile.mkdtemp(prefix='vidgrab-live-probes-')).resolve()
    runtime = RuntimeConfig(root / 'state', root / 'media')
    token = secrets.token_hex(32)
    engine = create_engine_app(runtime, token=token, port=43210, components=components,
                               component_versions=versions, media_mode='ordinary', start_queue=False)
    (output / 'runtime.json').write_text(json.dumps({'root': str(root), 'scope': 'isolated public probe state; retained'}, indent=2) + '\n')
    with (output / 'rows.jsonl').open('x', encoding='utf-8') as stream:
        def record(row):
            line = json.dumps(row)
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            print(line, flush=True)
        async with engine.app.router.lifespan_context(engine.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine),
                    base_url='http://127.0.0.1:43210', headers={'Authorization': 'Bearer ' + token}) as client:
                async def dispatch(case):
                    response = await client.post('/v1/requests', json={'protocol_version': 1,
                        'request_id': str(uuid4()), 'operation': 'probes.create', 'params': {'url': case['url']}})
                    return response.json()
                rows = await run_cases(cases, dispatch, record)
    with sqlite3.connect(runtime.db_path) as db:
        audit = {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                 for name in ('downloads', 'download_attempts', 'request_probes', 'request_leases')}
        audit['unreleased_owners'] = db.execute('SELECT count(*) FROM runtime_owners WHERE released_at IS NULL').fetchone()[0]
    if any(audit.values()) or engine.app.state.probes.inflight or engine.app.state.owner.active:
        raise RuntimeError('Isolated runtime did not finish with joined probes and released ownership')
    return rows, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-public-network', action='store_true', help='Required opt-in to real public requests')
    parser.add_argument('--cases', type=Path, help='Optional external JSON; never copied into reports')
    parser.add_argument('--tools', type=Path, required=True, help='Existing hash-verified bundled tools')
    parser.add_argument('--output', type=Path, required=True, help='Fresh report directory')
    args = parser.parse_args()
    if not args.allow_public_network:
        parser.error('Public network requests require --allow-public-network')
    try:
        cases = load_cases(args.cases)
    except (ValueError, OSError):
        parser.error('Invalid public case specification; details omitted to keep URLs private')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; existing evidence is preserved')
    from backend.engine.resources import check_components
    components, versions = check_components(args.tools.resolve(strict=True))
    args.output.mkdir(parents=True)
    started = time.monotonic()
    source = Path(__file__).resolve().parents[1]
    inputs = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in [source / 'tests/check_live_sites.py', source / 'tests/check_live_site_helpers.py']}
    report = {'date_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
              'python': sys.version.split()[0], 'yt_dlp': metadata.version('yt-dlp'), 'components': versions,
              'input_sha256': inputs, 'scope': 'source v1/public probes; joined threads; no downloads or browser acceptance',
              'signed_in': 'pending by user choice', 'execution_completed': False}
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report['rows'], report['cleanup_audit'] = asyncio.run(live_probe(cases, args.output, components, versions))
        report['execution_completed'] = True
    except Exception:
        report['harness_error'] = 'HARNESS_ERROR'
    finally:
        logging.disable(previous)
        report['seconds'] = round(time.monotonic() - started, 3)
        with (args.output / 'report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    print(json.dumps({'execution_completed': report['execution_completed'], 'seconds': report['seconds']}), flush=True)
    raise SystemExit(0 if report['execution_completed'] else 1)


if __name__ == '__main__':
    main()
