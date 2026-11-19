"""Opt-in read-only bundled ffprobe/ffmpeg MPEG-TS SDT static-glibc gconv diagnosis.

Directly inspects bundled ffprobe/ffmpeg binary markers, host/container gconv
directory presence, and read-only MPEG-TS probe/decode behavior under default
and isolated GCONV_PATH environments without modifying any production file.
Never retains file paths, URLs, titles, service tags or raw stdout/stderr text.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

from backend.engine.resources import check_components


EXPECTED_MPEGTS_SHA256 = 'dca82b4374ee198b5a605dc03b0924152f6122e594df577a3e9ce8b63a9db0d3'
TRACE_SIGNAL_PATTERNS = (
    ('7.0.2-static', 'STATIC_7_0_2_BANNER'),
    ('debian 8.3.0-6', 'BUILT_DEBIAN_8_GCC_8'),
    ('probing mpegts score:50', 'MPEGTS_SCORE_50'),
    ('format mpegts probed', 'MPEGTS_PROBED'),
    ('] sdt:', 'MPEGTS_SDT_SECTION'),
    ('tag: 0x48 len=23', 'MPEGTS_SDT_TAG_0X48_DVB_SERVICE'),
    ('] pat:', 'MPEGTS_PAT_SECTION'),
    ('] pmt:', 'MPEGTS_PMT_SECTION'),
    ('tuning done', 'MPEGTS_TUNING_DONE'),
    ('before avformat_find_stream_info()', 'AVFORMAT_FIND_STREAM_INFO_ENTER'),
    ('decoding vui', 'H264_DECODING_VUI'),
    ('format yuv420p chosen', 'H264_DECODE_READY'),
    ('after avformat_find_stream_info()', 'AVFORMAT_FIND_STREAM_INFO_DONE'),
)


def summarize_trace_stderr(stderr_text):
    if not isinstance(stderr_text, str):
        return {'signals': [], 'last_signal': None, 'line_count': 0}
    ordered = []
    seen = set()
    lines = stderr_text.splitlines()
    for line in lines:
        lower = line.lower()
        for needle, code in TRACE_SIGNAL_PATTERNS:
            if needle in lower and code not in seen:
                seen.add(code)
                ordered.append(code)
    return {
        'signals': ordered,
        'last_signal': ordered[-1] if ordered else None,
        'line_count': len(lines),
    }


def inspect_binary_markers(binary_path):
    path = Path(binary_path)
    data = path.read_bytes()
    is_elf = data[:4] == b'\x7fELF'
    is_pe = data[:2] == b'MZ'
    return {
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'is_elf': is_elf,
        'is_pe': is_pe,
        'has_embedded_multiarch_gconv_path': b'/usr/lib/x86_64-linux-gnu/gconv' in data,
        'has_gconv_modules_literal': b'gconv-modules' in data,
        'has_gconv_get_path_symbol': b'__gconv_get_path' in data,
        'has_debian_8_marker': b'Debian 8.3.0-6' in data,
        'has_7_0_2_static_marker': b'7.0.2-static' in data,
    }


def inspect_gconv_paths(
    multiarch_dir=Path('/usr/lib/x86_64-linux-gnu/gconv'),
    lib64_dir=Path('/usr/lib64/gconv'),
):
    multiarch = Path(multiarch_dir)
    lib64 = Path(lib64_dir)
    return {
        'multiarch_gconv_dir_exists': multiarch.is_dir(),
        'multiarch_gconv_cache_exists': (multiarch / 'gconv-modules.cache').is_file(),
        'multiarch_iso6937_exists': (multiarch / 'ISO6937.so').is_file(),
        'lib64_gconv_dir_exists': lib64.is_dir(),
        'lib64_gconv_cache_exists': (lib64 / 'gconv-modules.cache').is_file(),
    }


def summarize_ffprobe_json(stdout_text):
    if not isinstance(stdout_text, str) or not stdout_text.strip():
        return {
            'json_valid': False,
            'format_name': None,
            'stream_count': 0,
            'video_streams': 0,
            'audio_streams': 0,
            'has_duration': False,
        }
    try:
        payload = json.loads(stdout_text)
    except Exception:
        return {
            'json_valid': False,
            'format_name': None,
            'stream_count': 0,
            'video_streams': 0,
            'audio_streams': 0,
            'has_duration': False,
        }
    if not isinstance(payload, dict):
        return {
            'json_valid': False,
            'format_name': None,
            'stream_count': 0,
            'video_streams': 0,
            'audio_streams': 0,
            'has_duration': False,
        }
    fmt = payload.get('format') if isinstance(payload.get('format'), dict) else {}
    streams = payload.get('streams') if isinstance(payload.get('streams'), list) else []
    format_name = fmt.get('format_name') if isinstance(fmt.get('format_name'), str) else None
    video_count = sum(1 for s in streams if isinstance(s, dict) and s.get('codec_type') == 'video')
    audio_count = sum(1 for s in streams if isinstance(s, dict) and s.get('codec_type') == 'audio')
    return {
        'json_valid': True,
        'format_name': format_name[:32] if format_name else None,
        'stream_count': len(streams),
        'video_streams': video_count,
        'audio_streams': audio_count,
        'has_duration': bool(fmt.get('duration')),
    }


def _signal_name(returncode):
    if type(returncode) is int and returncode < 0:
        sig_num = -returncode
        try:
            return signal.Signals(sig_num).name
        except ValueError:
            return f'SIG_{sig_num}'
    return None


def build_probe_run_record(*, mode, tool, gconv_mode, returncode, stdout, stderr, elapsed):
    stdout_text = stdout if isinstance(stdout, str) else ''
    stderr_text = stderr if isinstance(stderr, str) else ''
    return {
        'mode': mode,
        'tool': tool,
        'gconv_mode': gconv_mode,
        'exit_code': returncode,
        'signal_name': _signal_name(returncode),
        'elapsed_seconds': round(float(elapsed), 3),
        'stdout_len': len(stdout_text),
        'stderr_len': len(stderr_text),
        'json_summary': summarize_ffprobe_json(stdout_text),
        'trace_summary': summarize_trace_stderr(stderr_text),
    }


def assess_ffprobe_diagnosis(runs, gconv_paths, binary_markers):
    by_key = {(r['mode'], r['tool'], r['gconv_mode']): r for r in runs}
    default_json = by_key.get(('json_probe', 'ffprobe', 'unset'), {})
    default_trace = by_key.get(('trace_probe', 'ffprobe', 'unset'), {})
    default_decode = by_key.get(('null_decode', 'ffmpeg', 'unset'), {})
    empty_json = by_key.get(('json_probe', 'ffprobe', 'empty'), {})
    empty_decode = by_key.get(('null_decode', 'ffmpeg', 'empty'), {})

    crashes_default = (
        default_json.get('exit_code') == -11
        and default_trace.get('exit_code') == -11
        and default_decode.get('exit_code') == -11
    )
    recovers_empty = (
        empty_json.get('exit_code') == 0
        and empty_json.get('json_summary', {}).get('json_valid') is True
        and empty_decode.get('exit_code') == 0
    )
    all_pass = (
        default_json.get('exit_code') == 0
        and default_json.get('json_summary', {}).get('json_valid') is True
        and default_decode.get('exit_code') == 0
        and empty_json.get('exit_code') == 0
        and empty_decode.get('exit_code') == 0
    )
    crash_last_signal = default_trace.get('trace_summary', {}).get('last_signal') if crashes_default else None

    if crashes_default and recovers_empty and gconv_paths.get('multiarch_gconv_cache_exists'):
        classification = 'STATIC_GLIBC_GCONV_ABI_CRASH_CONFIRMED'
    elif all_pass and binary_markers.get('is_elf') and not gconv_paths.get('multiarch_gconv_dir_exists'):
        classification = 'STATIC_GLIBC_MULTIARCH_GCONV_ABSENT_NO_CRASH'
    elif all_pass and binary_markers.get('is_pe'):
        classification = 'WINDOWS_PE_GCONV_NOT_APPLICABLE_PASS'
    elif all_pass:
        classification = 'ALL_PROBES_PASSED'
    else:
        classification = 'UNEXPECTED_PROBE_OUTCOME'

    return {
        'classification': classification,
        'crashes_with_default_env': crashes_default,
        'recovers_with_empty_gconv_path': recovers_empty,
        'all_probes_pass': all_pass,
        'crash_last_trace_signal': crash_last_signal,
    }


def _run_tool(executable, args, gconv_mode, mode, tool_name, timeout=45):
    env = dict(os.environ)
    if gconv_mode == 'unset':
        env.pop('GCONV_PATH', None)
    elif gconv_mode == 'empty':
        env['GCONV_PATH'] = ''
    elif gconv_mode == 'nonexistent':
        env['GCONV_PATH'] = '/nonexistent-gconv-isolation'
    else:
        raise ValueError(f'Unsupported gconv_mode: {gconv_mode}')

    started = time.monotonic()
    completed = subprocess.run(
        [str(executable), *args],
        capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    elapsed = time.monotonic() - started
    return build_probe_run_record(
        mode=mode,
        tool=tool_name,
        gconv_mode=gconv_mode,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed=elapsed,
    )


def run_ffprobe_diagnosis(tools_dir, sample_path, output_dir):
    tools_dir = Path(tools_dir).resolve()
    sample_path = Path(sample_path).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError('Output directory must not exist')
    if not sample_path.is_file():
        raise ValueError('Retained MPEG-TS sample file is missing')

    before_sha = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    before_size = sample_path.stat().st_size
    paths, versions = check_components(tools_dir)
    ffprobe_exe = Path(paths['ffprobe'])
    ffmpeg_exe = Path(paths['ffmpeg'])

    ffprobe_markers = inspect_binary_markers(ffprobe_exe)
    ffmpeg_markers = inspect_binary_markers(ffmpeg_exe)
    gconv_paths = inspect_gconv_paths()

    file_arg = f'file:{sample_path}'
    json_args = ['-hide_banner', '-show_format', '-show_streams', '-print_format', 'json', file_arg]
    trace_args = ['-v', 'trace', '-hide_banner', '-show_format', '-show_streams', '-print_format', 'json', file_arg]
    decode_args = ['-hide_banner', '-i', file_arg, '-f', 'null', '-']

    runs = []
    for gconv_mode in ('unset', 'empty', 'nonexistent'):
        runs.append(_run_tool(ffprobe_exe, json_args, gconv_mode, 'json_probe', 'ffprobe'))
        runs.append(_run_tool(ffprobe_exe, trace_args, gconv_mode, 'trace_probe', 'ffprobe'))
        runs.append(_run_tool(ffmpeg_exe, decode_args, gconv_mode, 'null_decode', 'ffmpeg'))

    after_sha = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    after_size = sample_path.stat().st_size
    if after_sha != before_sha or after_size != before_size:
        raise RuntimeError('Retained MPEG-TS sample changed during read-only diagnosis')

    assessment = assess_ffprobe_diagnosis(runs, gconv_paths, ffprobe_markers)
    output_dir.mkdir(parents=True)
    report = {
        'schema': 1,
        'scope': 'read-only bundled ffprobe/ffmpeg MPEG-TS SDT static-glibc gconv diagnosis',
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'platform': {
            'system': platform.system(),
            'release': platform.release(),
            'machine': platform.machine(),
            'python': sys.version.split()[0],
            'glibc': '.'.join(part for part in platform.libc_ver() if part) or None,
        },
        'components': versions,
        'sample_verification': {
            'size': before_size,
            'sha256_before': before_sha,
            'sha256_after': after_sha,
            'unchanged': before_sha == after_sha,
            'matches_ubuntu_retained_mpegts': before_sha == EXPECTED_MPEGTS_SHA256,
        },
        'binary_markers': {
            'ffprobe': ffprobe_markers,
            'ffmpeg': ffmpeg_markers,
        },
        'gconv_environment': gconv_paths,
        'runs': runs,
        'assessment': assessment,
    }
    (output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_ffprobe_diagnosis(args.tools, args.sample, args.output)
    except Exception as error:
        parser.error(str(error))
    print(json.dumps({
        'classification': report['assessment']['classification'],
        'crashes_with_default_env': report['assessment']['crashes_with_default_env'],
        'recovers_with_empty_gconv_path': report['assessment']['recovers_with_empty_gconv_path'],
        'sample_unchanged': report['sample_verification']['unchanged'],
    }, indent=2))


if __name__ == '__main__':
    main()
