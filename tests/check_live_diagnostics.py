"""Opt-in anonymous probe diagnostics with allowlisted observations only.

Observe the original source probe and wire boundaries without changing retries,
headers, responses, exceptions or production files. Never retain raw messages.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
from pathlib import Path
import platform
import sys
import time
from unittest.mock import patch

from tests import check_live_sites as live


MESSAGE_SIGNALS = {
    'video unavailable': 'VIDEO_UNAVAILABLE',
    'video is not available': 'VIDEO_UNAVAILABLE',
    'requested format is not available': 'FORMAT_UNAVAILABLE',
    'only images are available': 'IMAGES_ONLY',
    'sign in to confirm': 'SIGN_IN_CONFIRMATION',
    'private video': 'PRIVATE_VIDEO',
    'video has been removed': 'VIDEO_REMOVED',
    'this video is unavailable': 'VIDEO_UNAVAILABLE',
    'not available in your country': 'GEO_RESTRICTED',
    'nsig extraction failed': 'NSIG_EXTRACTION_FAILED',
    'signature extraction failed': 'SIGNATURE_EXTRACTION_FAILED',
    'challenge solving failed': 'JS_CHALLENGE_FAILED',
    'no supported javascript runtime': 'JS_RUNTIME_UNAVAILABLE',
    'missing a url': 'FORMATS_MISSING_URL',
    'po token': 'PO_TOKEN_NOTICE',
    'unsupported url': 'UNSUPPORTED_URL',
}
EXCEPTION_TYPES = frozenset({'DownloadError', 'ExtractorError', 'RequestFailure',
    'HTTPError', 'TransportError', 'TimeoutError', 'ConnectionError', 'OSError',
    'ValueError', 'ValidationError', 'UnsupportedError'})
PUBLIC_CONTROL_CASES = [
    {'id': 'youtube-zoo-control', 'media_type': 'extractor', 'authentication': 'public',
     'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'expected': 'media'},
    {'id': 'mdn-flower-control', 'media_type': 'direct', 'authentication': 'public',
     'url': 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4', 'expected': 'media'},
]


class Observer:
    def __init__(self):
        self.signals = set()
        self.statuses = Counter()
        self.types = set()
        self.codes = set()

    def message(self, message):
        # Match in memory, then discard the entire original text.
        if isinstance(message, str):
            lower = message.lower()
            self.signals.update(tag for phrase, tag in MESSAGE_SIGNALS.items() if phrase in lower)

    def error(self, error):
        from backend.services.retry_policy import nested_errors, RequestFailure
        for nested in nested_errors(error):
            name = type(nested).__name__
            if name in EXCEPTION_TYPES:
                self.types.add(name)
            if isinstance(nested, RequestFailure):
                self.codes.add(nested.code)
            self.message(str(nested))

    def send(self, send, request):
        try:
            response = send(request)
        except Exception as error:
            self.error(error)
            status = getattr(error, 'status', getattr(error, 'code', None))
            if type(status) is int and 100 <= status <= 599:
                self.statuses[str(status)] += 1
            raise
        status = getattr(response, 'status', None)
        if type(status) is int and 100 <= status <= 599:
            self.statuses[str(status)] += 1
        return response

    def take(self):
        row = {'signals': sorted(self.signals), 'http_status_counts': dict(sorted(self.statuses.items())),
               'exception_types': sorted(self.types), 'request_failure_codes': sorted(self.codes)}
        self.signals.clear()
        self.statuses.clear()
        self.types.clear()
        self.codes.clear()
        return row

    def install(self, stack):
        from backend.downloader import YtDlpLogger
        from backend.format_probe import ProbeService
        from backend.services.request_network import BudgetedRequests
        for name in ('debug', 'info', 'warning', 'error'):
            original = getattr(YtDlpLogger, name)
            def log(logger, message, original=original):
                self.message(message)
                return original(logger, message)
            stack.enter_context(patch.object(YtDlpLogger, name, log))
        extract = ProbeService._extract
        def extract_observed(service, *args, **kwargs):
            try:
                return extract(service, *args, **kwargs)
            except Exception as error:
                self.error(error)
                raise
        stack.enter_context(patch.object(ProbeService, '_extract', extract_observed))
        wire = BudgetedRequests.open
        def open_observed(gate, send, request):
            return wire(gate, lambda value: self.send(send, value), request)
        stack.enter_context(patch.object(BudgetedRequests, 'open', open_observed))
        summarize = live.summarize_result
        def summarize_observed(*args, **kwargs):
            row = summarize(*args, **kwargs)
            row['diagnostics'] = self.take()
            return row
        stack.enter_context(patch.object(live, 'summarize_result', summarize_observed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', type=Path, help='External public cases; never copied to reports')
    parser.add_argument('--public-controls', action='store_true', help='Separate named public sample controls')
    args = parser.parse_args()
    if not args.allow_public_network:
        parser.error('Public network requests require --allow-public-network')
    try:
        if args.cases and args.public_controls:
            raise ValueError('Choose either custom cases or named controls')
        cases = live.load_cases(args.cases) if args.cases else ([dict(case) for case in PUBLIC_CONTROL_CASES]
                    if args.public_controls else [case for case in live.load_cases()
                    if case['id'] in {'youtube-video', 'w3c-sintel'}])
    except (ValueError, OSError):
        parser.error('Invalid public case specification; private details omitted')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a fresh output directory; existing evidence is preserved')
    from backend.engine.resources import check_components
    components, versions = check_components(args.tools.resolve(strict=True))
    args.output.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    inputs = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (Path(__file__), Path(__file__).with_name('check_live_diagnostic_helpers.py'),
                           source / 'tests/check_live_sites.py')}
    report = {'date_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
              'python': sys.version.split()[0], 'yt_dlp': metadata.version('yt-dlp'), 'components': versions,
              'input_sha256': inputs, 'signed_in': 'pending by user choice',
              'scope': 'separate source-v1 public diagnostic probes; no download or production repair',
              'execution_completed': False}
    started = time.monotonic()
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with ExitStack() as stack:
            Observer().install(stack)
            report['rows'], report['cleanup_audit'] = asyncio.run(live.live_probe(cases, args.output, components, versions))
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
