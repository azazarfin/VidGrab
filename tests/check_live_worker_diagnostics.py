"""Opt-in redacted in-worker diagnostics for failed public live downloads.

Observes worker wire requests, fragment stages, ffprobe/ffmpeg postprocessing
and unhandled exception frames without altering headers, retries, responses,
exceptions or any production runtime file. Never retains raw messages, URLs,
titles, file paths, job/probe identities, cookies or response bodies.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import errno as errno_mod
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import platform
import secrets
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from uuid import uuid4

from tests import check_live_diagnostics as diag
from tests import check_live_downloads as dl
from tests import check_live_sites as live


ALLOWED_METHODS = frozenset({'GET', 'HEAD', 'POST', 'OPTIONS'})
WORKER_SIGNALS = {
    **diag.MESSAGE_SIGNALS,
    'http error 403': 'HTTP_403',
    '403: forbidden': 'HTTP_403',
    'http error 404': 'HTTP_404',
    'http error 429': 'HTTP_429',
    'sabr': 'SABR_NOTICE',
    'confirm you\u2019re not a bot': 'BOT_CHECK',
    "confirm you're not a bot": 'BOT_CHECK',
    'no video formats found': 'NO_FORMATS',
    'postprocessing': 'POSTPROCESS_NOTICE',
    'unable to extract metadata': 'UNABLE_TO_EXTRACT_METADATA',
    'ffprobe not found': 'FFPROBE_MISSING',
    'fixing mpeg-ts': 'FFMPEG_FIXUP_M3U8',
    'timed out': 'TIMED_OUT_NOTICE',
    'connection reset': 'CONNECTION_RESET_NOTICE',
    'ssl': 'SSL_NOTICE',
}
FFPROBE_STDERR_SIGNALS = {
    'no such file or directory': 'NO_SUCH_FILE',
    'permission denied': 'PERMISSION_DENIED',
    'invalid data found': 'INVALID_DATA',
    'protocol not found': 'PROTOCOL_NOT_FOUND',
    'exec format error': 'EXEC_FORMAT_ERROR',
    'cannot execute': 'NOT_EXECUTABLE',
    'moov atom not found': 'MOOV_NOT_FOUND',
    'input/output error': 'IO_ERROR',
    'invalid argument': 'INVALID_ARGUMENT',
    'could not find codec': 'CODEC_NOT_FOUND',
    'error opening input': 'ERROR_OPENING_INPUT',
}
WORKER_EXCEPTION_TYPES = diag.EXCEPTION_TYPES | frozenset({
    'JSONDecodeError', 'PostProcessingError', 'FreshAuthRequired',
    'OutputPathError', 'RuntimeOwnershipError', 'URLError', 'SSLError',
    'SSLCertVerificationError', 'ConnectionResetError', 'ConnectionAbortedError',
    'ConnectionRefusedError', 'BrokenPipeError', 'RemoteDisconnected',
    'IncompleteRead', 'BadStatusLine', 'HTTPException', 'SubprocessError',
    'CalledProcessError', 'FileNotFoundError', 'PermissionError', 'RuntimeError',
    'FormatUnavailable',
})
ALLOWED_FRAME_PREFIXES = (
    'backend', 'yt_dlp', 'json', 'urllib', 'http', 'ssl', 'socket', 'subprocess', 'os', 'io',
)
DIAGNOSTIC_CASE_IDS = frozenset({
    'youtube-zoo-control', 'mdn-flower-control', 'mux-bbb', 'akamai-envivio',
})


def _errno_name(error):
    code = getattr(error, 'errno', None)
    if type(code) is int:
        return errno_mod.errorcode.get(code, 'OTHER')
    return None


def _module_basename(filename):
    if not isinstance(filename, str):
        return None
    normalized = filename.replace('\\', '/')
    parts = [part for part in normalized.split('/') if part]
    for idx, part in enumerate(parts):
        if part in ALLOWED_FRAME_PREFIXES:
            rel = '.'.join(parts[idx:])
            if rel.endswith('.py'):
                rel = rel[:-3]
            return rel[:96]
    if parts:
        base = parts[-1]
        if base.endswith('.py'):
            base = base[:-3]
        if any(base == prefix or base.startswith(prefix + '_') for prefix in ALLOWED_FRAME_PREFIXES):
            return base[:96]
    return None


def summarize_probe_format(item):
    if not isinstance(item, dict):
        return {}
    return {
        'has_ext': bool(item.get('ext')),
        'has_height': item.get('height') is not None,
        'has_width': item.get('width') is not None,
        'has_resolution': bool(item.get('resolution')),
        'has_vcodec': item.get('vcodec') not in (None, 'none', ''),
        'has_acodec': item.get('acodec') not in (None, 'none', ''),
    }


def observed_entry_command(original_entry_command, diag_file, module, frozen_flag):
    if module == 'backend.engine.worker':
        return [sys.executable, '-B', '-m', 'tests.check_live_worker_diagnostics',
                '--observe-worker', str(diag_file)]
    return original_entry_command(module, frozen_flag)


class WorkerObserver:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.stages = []
        self.method_status = Counter()
        self.http_exceptions = set()
        self.os_errnos = set()
        self.signals = set()
        self.exception_types = set()
        self.request_failure_codes = set()
        self.first_non_2xx = None
        self.first_transport_error = None
        self.total_requests = 0
        self.fragment_stats = {
            'count': None,
            'head_attempts': 0,
            'head_validators_supported': None,
            'get_started': 0,
            'get_completed': 0,
            'fresh_fallback': False,
        }
        self.ffprobe_runs = []
        self.error_frames = []
        self._current_stage = 'init'
        self._in_transfer = False

    def stage(self, name):
        with self.lock:
            self._current_stage = name
            if not self.stages or self.stages[-1] != name:
                if len(self.stages) < 64:
                    self.stages.append(name)
        self.flush()

    def update_fragments(self, **changes):
        with self.lock:
            for key, value in changes.items():
                if key in self.fragment_stats:
                    self.fragment_stats[key] = value

    def message(self, text):
        if not isinstance(text, str):
            return
        lower = text.lower()
        matched = {tag for phrase, tag in WORKER_SIGNALS.items() if phrase in lower}
        if matched:
            with self.lock:
                self.signals.update(matched)

    def _collect_exception(self, error):
        from backend.services.retry_policy import RequestFailure, nested_errors
        for nested in nested_errors(error):
            name = type(nested).__name__
            if name in WORKER_EXCEPTION_TYPES:
                self.exception_types.add(name)
            err_code = _errno_name(nested)
            if err_code:
                self.os_errnos.add(err_code)
            if isinstance(nested, RequestFailure):
                self.request_failure_codes.add(nested.code)
            self.message(str(nested))

    def record_exception(self, error):
        frames = []
        tb = getattr(error, '__traceback__', None)
        if tb is not None:
            for frame in traceback.extract_tb(tb)[-16:]:
                mod = _module_basename(frame.filename)
                if mod:
                    frames.append({'module': mod, 'function': frame.name[:64]})
        with self.lock:
            self._collect_exception(error)
            if frames and not self.error_frames:
                self.error_frames = frames
        self.flush()

    def send(self, send_fn, request):
        raw_method = None
        if hasattr(request, 'get_method'):
            try:
                raw_method = request.get_method()
            except Exception:
                raw_method = None
        if not raw_method:
            raw_method = getattr(request, 'method', None) or 'GET'
        method = raw_method.upper() if isinstance(raw_method, str) and raw_method.upper() in ALLOWED_METHODS else 'OTHER'
        with self.lock:
            self.total_requests += 1
            req_index = self.total_requests
            current_stage = self._current_stage
            if self._in_transfer and method == 'HEAD':
                self.fragment_stats['head_attempts'] += 1
                if 'head_probe_start' not in self.stages:
                    self.stages.append('head_probe_start')
                    self._current_stage = 'head_probe_start'
                    current_stage = 'head_probe_start'
            elif self._in_transfer and method == 'GET':
                self.fragment_stats['get_started'] += 1
                if 'get_fragments_start' not in self.stages:
                    self.stages.append('get_fragments_start')
                    self._current_stage = 'get_fragments_start'
                    current_stage = 'get_fragments_start'
        try:
            response = send_fn(request)
        except Exception as error:
            with self.lock:
                self.method_status[f'{method}:EXCEPTION'] += 1
                exc_name = type(error).__name__
                if exc_name in WORKER_EXCEPTION_TYPES:
                    self.http_exceptions.add(exc_name)
                else:
                    self.http_exceptions.add('OTHER')
                err_name = _errno_name(error)
                if err_name:
                    self.os_errnos.add(err_name)
                self._collect_exception(error)
                if self.first_transport_error is None:
                    self.first_transport_error = {
                        'method': method,
                        'exception_type': exc_name if exc_name in WORKER_EXCEPTION_TYPES else 'OTHER',
                        'errno': err_name,
                        'request_index': req_index,
                        'stage': current_stage,
                    }
            self.flush()
            raise
        status = getattr(response, 'status', None)
        if type(status) is int and 100 <= status <= 599:
            with self.lock:
                self.method_status[f'{method}:{status}'] += 1
                if not (200 <= status < 300) and self.first_non_2xx is None:
                    self.first_non_2xx = {'method': method, 'status': status, 'request_index': req_index}
                if self._in_transfer and method == 'GET' and 200 <= status < 300:
                    self.fragment_stats['get_completed'] += 1
        if req_index == 1 or req_index % 10 == 0 or (type(status) is int and not (200 <= status < 300)):
            self.flush()
        return response

    def observe_popen(self, original_run, *args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        tool = None
        if isinstance(cmd, (list, tuple)) and cmd and isinstance(cmd[0], (str, os.PathLike)):
            base = Path(str(cmd[0])).name.lower()
            if base.startswith('ffprobe'):
                tool = 'ffprobe'
            elif base.startswith('ffmpeg'):
                tool = 'ffmpeg'
        if tool is None:
            return original_run(*args, **kwargs)
        result = original_run(*args, **kwargs)
        stdout, stderr, returncode = result
        out_len = len(stdout) if isinstance(stdout, (str, bytes)) else 0
        err_text = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr if isinstance(stderr, str) else '')
        lower = err_text.lower()
        signals = sorted(tag for phrase, tag in FFPROBE_STDERR_SIGNALS.items() if phrase in lower)
        if err_text.strip() and not signals:
            signals = ['STDERR_NONEMPTY_OTHER']
        with self.lock:
            if len(self.ffprobe_runs) < 16:
                self.ffprobe_runs.append({
                    'tool': tool,
                    'exit_code': returncode if type(returncode) is int else None,
                    'stdout_empty': out_len == 0,
                    'stdout_len': out_len,
                    'stderr_empty': not bool(err_text.strip()),
                    'stderr_signals': signals,
                })
        self.flush()
        return result

    def snapshot(self):
        with self.lock:
            return {
                'stages': list(self.stages),
                'total_requests': self.total_requests,
                'http_method_status_counts': dict(sorted(self.method_status.items())),
                'http_exception_types': sorted(self.http_exceptions),
                'os_errnos': sorted(self.os_errnos),
                'first_non_2xx': dict(self.first_non_2xx) if self.first_non_2xx else None,
                'first_transport_error': dict(self.first_transport_error) if self.first_transport_error else None,
                'fragment_stats': dict(self.fragment_stats),
                'ffprobe_runs': list(self.ffprobe_runs),
                'signals': sorted(self.signals),
                'exception_types': sorted(self.exception_types),
                'request_failure_codes': sorted(self.request_failure_codes),
                'error_frames': list(self.error_frames),
            }

    def flush(self):
        data = json.dumps(self.snapshot(), indent=2) + '\n'
        tmp = self.path.with_suffix(self.path.suffix + f'.{os.getpid()}.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding='utf-8')
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def install(self, stack):
        import backend.downloader as downloader_mod
        from backend.services.fragment_resume import VerifiedDashFD, VerifiedFragments, VerifiedHlsFD
        from backend.services.request_network import BudgetedRequests
        from yt_dlp.postprocessor.ffmpeg import FFmpegFixupM3u8PP, FFmpegPostProcessor
        from yt_dlp.utils import Popen as YtPopen

        for name in ('debug', 'info', 'warning', 'error'):
            original = getattr(downloader_mod.YtDlpLogger, name)
            def log(logger, msg, original=original):
                self.message(msg)
                return original(logger, msg)
            stack.enter_context(patch.object(downloader_mod.YtDlpLogger, name, log))

        wire = BudgetedRequests.open
        def open_observed(gate, send_fn, request):
            return wire(gate, lambda req: self.send(send_fn, req), request)
        stack.enter_context(patch.object(BudgetedRequests, 'open', open_observed))

        orig_run = downloader_mod.run_download
        def run_observed(*args, **kwargs):
            self.stage('run_download_enter')
            try:
                res = orig_run(*args, **kwargs)
                self.stage('run_download_return')
                return res
            except Exception as error:
                self.record_exception(error)
                self.stage('run_download_error')
                raise
        stack.enter_context(patch.object(downloader_mod, 'run_download', run_observed))

        orig_hls = VerifiedHlsFD.real_download
        def hls_observed(fd, filename, info_dict):
            self.stage('hls_real_download')
            return orig_hls(fd, filename, info_dict)
        stack.enter_context(patch.object(VerifiedHlsFD, 'real_download', hls_observed))

        orig_dash = VerifiedDashFD.real_download
        def dash_observed(fd, filename, info_dict):
            self.stage('dash_real_download')
            return orig_dash(fd, filename, info_dict)
        stack.enter_context(patch.object(VerifiedDashFD, 'real_download', dash_observed))

        orig_manifest = VerifiedFragments._manifest
        def manifest_observed(fd, url, info):
            self.stage('manifest_fetch_start')
            res = orig_manifest(fd, url, info)
            self.stage('manifest_fetch_done')
            return res
        stack.enter_context(patch.object(VerifiedFragments, '_manifest', manifest_observed))

        orig_fresh = VerifiedFragments._fresh
        def fresh_observed(fd, cls, filename, info):
            self.update_fragments(fresh_fallback=True)
            self.stage('fresh_fallback')
            return orig_fresh(fd, cls, filename, info)
        stack.enter_context(patch.object(VerifiedFragments, '_fresh', fresh_observed))

        orig_transfer = VerifiedFragments._transfer
        def transfer_observed(fd, filename, fragments, info, manifest):
            count = len(fragments) if isinstance(fragments, list) else None
            self.update_fragments(count=count)
            with self.lock:
                self._in_transfer = True
            self.stage('fragment_transfer_start')
            try:
                res = orig_transfer(fd, filename, fragments, info, manifest)
                self.stage('fragment_transfer_done')
                return res
            finally:
                with self.lock:
                    self._in_transfer = False
                self.flush()
        stack.enter_context(patch.object(VerifiedFragments, '_transfer', transfer_observed))

        orig_meta = FFmpegPostProcessor.get_metadata_object
        def meta_observed(pp, path, opts=[]):
            self.stage('postprocess_metadata_start')
            try:
                res = orig_meta(pp, path, opts)
                self.stage('postprocess_metadata_done')
                return res
            except Exception as error:
                self.record_exception(error)
                self.stage('postprocess_metadata_error')
                raise
        stack.enter_context(patch.object(FFmpegPostProcessor, 'get_metadata_object', meta_observed))

        orig_fixup = FFmpegFixupM3u8PP.run
        def fixup_observed(pp, info):
            self.stage('postprocess_fixup_m3u8_start')
            res = orig_fixup(pp, info)
            self.stage('postprocess_fixup_m3u8_done')
            return res
        stack.enter_context(patch.object(FFmpegFixupM3u8PP, 'run', fixup_observed))

        orig_popen_run = YtPopen.run
        @classmethod
        def popen_observed(cls, *args, **kwargs):
            return self.observe_popen(lambda *a, **kw: orig_popen_run(*a, **kw), *args, **kwargs)
        stack.enter_context(patch.object(YtPopen, 'run', popen_observed))


def run_observed_worker(diag_file):
    observer = WorkerObserver(diag_file)
    observer.stage('worker_start')
    with ExitStack() as stack:
        observer.install(stack)
        try:
            from backend.engine import worker
            worker.main()
        except Exception as error:
            observer.record_exception(error)
            raise
        finally:
            observer.stage('worker_exit')


async def run_worker_diagnostics(output, components, versions, selected_ids=None):
    import httpx
    from backend.engine import create_engine_app
    import backend.engine.execution as execution_mod
    from backend.runtime import RuntimeConfig

    root = Path(tempfile.mkdtemp(prefix='vidgrab-live-worker-diag-')).resolve()
    runtime = RuntimeConfig(root / 'state', root / 'media')
    token = secrets.token_hex(32)
    engine = create_engine_app(runtime, token=token, port=43210, components=components,
        component_versions=versions, media_mode='ordinary', start_queue=True)
    (output / 'runtime.json').write_text(json.dumps({'root': str(root),
        'scope': 'isolated public worker-diagnostic state/media; retained'}, indent=2) + '\n')

    cases = [dict(case) for case in dl.PUBLIC_CASES if case['id'] in DIAGNOSTIC_CASE_IDS]
    if selected_ids:
        cases = [case for case in cases if case['id'] in selected_ids]

    observer = diag.Observer()
    current_diag_file = [None]
    orig_entry = execution_mod.entry_command

    def entry_patched(module, frozen_flag):
        if current_diag_file[0] is not None:
            return observed_entry_command(orig_entry, current_diag_file[0], module, frozen_flag)
        return orig_entry(module, frozen_flag)

    rows = []
    with ExitStack() as stack, (output / 'rows.jsonl').open('x', encoding='utf-8') as stream:
        observer.install(stack)
        stack.enter_context(patch.object(execution_mod, 'entry_command', entry_patched))
        execute = execution_mod.ProcessDownloadExecutor.run
        async def execute_observed(executor, **options):
            try:
                return await execute(executor, **options)
            except Exception as error:
                observer.error(error)
                raise
        stack.enter_context(patch.object(execution_mod.ProcessDownloadExecutor, 'run', execute_observed))

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
                    row = {key: case[key] for key in ('id', 'media_type', 'authentication')}
                    row['phase'] = 'download-diagnostic'
                    worker_file = output / f"worker-{case['id']}.json"
                    current_diag_file[0] = worker_file
                    probe = await send('probes.create', {'url': case['url']})
                    if probe.get('ok') is not True:
                        row.update(dl.summarize_reply(case, probe, time.monotonic() - started))
                        row['phase'] = 'probe-before-download'
                    else:
                        data = probe['data']
                        selected = dl.select_format(data)
                        selected_meta = next((item for item in (data.get('formats') or [])
                            if item.get('format_id') == selected), None)
                        row['probe_format_summary'] = summarize_probe_format(selected_meta)
                        row['probe_format_count'] = data.get('format_count', 0)
                        if selected is None:
                            row.update(result='no_download', assessment='mismatch', error_category='EMPTY_METADATA')
                        else:
                            row['selection'] = 'probe-quality'
                            reply = await send('downloads.enqueue',
                                dl.enqueue_parameters(case, data, selected, use_defaults=False),
                                key='live-diag-enqueue-' + str(uuid4()))
                            if reply.get('ok') is not True:
                                row.update(dl.summarize_reply(case, reply, time.monotonic() - started))
                                row['phase'] = 'enqueue'
                            else:
                                job_id = reply['data']['job_id']
                                baseline_bytes = await asyncio.to_thread(dl.observed_bytes, root)
                                job_started = time.monotonic()
                                guard = None
                                while True:
                                    page = await send('downloads.list', {'limit': 50})
                                    if page.get('ok') is not True:
                                        raise RuntimeError('Job snapshot failed')
                                    job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
                                    if job['status'] in dl.TERMINAL:
                                        break
                                    size = max(0, (await asyncio.to_thread(dl.observed_bytes, root)) - baseline_bytes)
                                    reason = dl.stop_reason(job, time.monotonic() - job_started, size)
                                    if reason:
                                        guard = await dl.cancel_guarded(send, job, reason)
                                        page = await send('downloads.list', {'limit': 50})
                                        job = next(item for item in page['data']['jobs'] if item['id'] == job_id)
                                        if job['status'] != 'cancelled':
                                            raise RuntimeError('Guard cancellation did not join the job')
                                        break
                                    await asyncio.sleep(dl.SAMPLE_INTERVAL)
                                row.update(dl.summarize_job(job))
                                if guard:
                                    row.update(guard, assessment='mismatch')
                                if job['status'] == 'complete':
                                    try:
                                        row['output'] = await asyncio.to_thread(dl.verify_output, job['file_path'],
                                            runtime.download_dir, components, data.get('duration'))
                                    except Exception:
                                        row.update(assessment='mismatch', error_category='VERIFICATION_FAILED')
                    current_diag_file[0] = None
                    row['probe_diagnostics'] = observer.take()
                    if worker_file.is_file():
                        row['worker_diagnostics'] = json.loads(worker_file.read_text(encoding='utf-8'))
                    else:
                        row['worker_diagnostics'] = None
                    row['seconds'] = round(time.monotonic() - started, 3)
                    rows.append(row)
                    line = json.dumps(row)
                    stream.write(line + '\n'); stream.flush(); os.fsync(stream.fileno())
                    print(line, flush=True)

    audit = dl.audit_database(runtime.db_path)
    if (any(audit[name] for name in ('request_probes', 'request_leases', 'unfinished_jobs', 'unreleased_owners'))
            or engine.app.state.probes.inflight or engine.app.state.queue.active_tasks or engine.app.state.owner.active):
        raise RuntimeError('Isolated runtime did not finish with joined workers and released authority')
    return rows, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--tools', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--only', nargs='*', choices=sorted(DIAGNOSTIC_CASE_IDS))
    args = parser.parse_args()

    if args.observe_worker is not None:
        run_observed_worker(args.observe_worker)
        return

    if not args.allow_public_network:
        parser.error('Public worker diagnostics require --allow-public-network')
    if args.tools is None or args.output is None:
        parser.error('--tools and --output are required')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; previous evidence is preserved')

    from backend.engine.resources import check_components
    components, versions = check_components(args.tools.resolve(strict=True))
    args.output.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    inputs = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), Path(__file__).with_name('check_live_worker_diagnostic_helpers.py'),
                     source / 'tests/check_live_downloads.py', source / 'tests/check_live_diagnostics.py',
                     source / 'tests/check_live_sites.py')}
    report = {
        'date_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'yt_dlp': metadata.version('yt-dlp'),
        'components': versions,
        'input_sha256': inputs,
        'signed_in': 'pending by user choice',
        'scope': 'source-v1 public worker failure diagnostics; actual owned workers; no production repair',
        'limits': {'job_seconds': dl.TIME_LIMIT, 'observed_bytes': dl.BYTE_LIMIT,
                   'sample_interval': dl.SAMPLE_INTERVAL},
        'execution_completed': False,
    }
    started = time.monotonic()
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report['rows'], report['cleanup_audit'] = asyncio.run(run_worker_diagnostics(
            args.output, components, versions, selected_ids=set(args.only) if args.only else None))
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
