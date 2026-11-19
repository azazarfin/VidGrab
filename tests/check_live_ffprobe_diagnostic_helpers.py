"""Offline redaction, ELF/gconv inspection and CLI checks for live ffprobe diagnostics."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LiveFfprobeDiagnosticHelpers(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('check_live_ffprobe_diagnostics.py')
        self.assertTrue(path.is_file(), 'Live ffprobe diagnostic harness is missing')
        spec = importlib.util.spec_from_file_location('live_ffprobe_diagnostics', path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_classify_trace_lines_captures_mpegts_sdt_and_avoids_raw_paths(self):
        raw = '\n'.join([
            "ffprobe version 7.0.2-static https://johnvansickle.com/ffmpeg/",
            "  built with gcc 8 (Debian 8.3.0-6)",
            "[AVFormatContext @ 0x43440a40] Opening '/private/attempt/x36xhzz.mp4' for reading",
            "Probing mpegts score:50 size:2048",
            "[mpegts @ 0x43440a40] Format mpegts probed with size=2048 and score=50",
            "[mpegts @ 0x43440a40] SDT:",
            "[mpegts @ 0x43440a40] tag: 0x48 len=23",
            "[h264 @ 0x1aac1280] Format yuv420p chosen by get_format().",
        ])
        summary = self.runner.summarize_trace_stderr(raw)
        self.assertIn('STATIC_7_0_2_BANNER', summary['signals'])
        self.assertIn('BUILT_DEBIAN_8_GCC_8', summary['signals'])
        self.assertIn('MPEGTS_PROBED', summary['signals'])
        self.assertIn('MPEGTS_SDT_SECTION', summary['signals'])
        self.assertIn('MPEGTS_SDT_TAG_0X48_DVB_SERVICE', summary['signals'])
        self.assertIn('H264_DECODE_READY', summary['signals'])
        self.assertEqual(summary['last_signal'], 'H264_DECODE_READY')
        dumped = json.dumps(summary)
        self.assertNotIn('/private/attempt', dumped)
        self.assertNotIn('x36xhzz', dumped)

    def test_summarize_trace_stops_at_sdt_tag_0x48_on_crash(self):
        crash_stderr = '\n'.join([
            "ffprobe version 7.0.2-static https://johnvansickle.com/ffmpeg/",
            "  built with gcc 8 (Debian 8.3.0-6)",
            "[mpegts @ 0x43440a40] Format mpegts probed with size=2048 and score=50",
            "[mpegts @ 0x43440a40] SDT:",
            "[mpegts @ 0x43440a40] tag: 0x48 len=23",
        ])
        summary = self.runner.summarize_trace_stderr(crash_stderr)
        self.assertEqual(summary['last_signal'], 'MPEGTS_SDT_TAG_0X48_DVB_SERVICE')
        self.assertNotIn('H264_DECODE_READY', summary['signals'])

    def test_inspect_binary_gconv_strings_detects_static_debian_markers(self):
        fake_bytes = (
            b'\x7fELF...gconv-modules\x00/usr/lib/x86_64-linux-gnu/gconv\x00'
            b'__gconv_get_path\x00Debian 8.3.0-6\x007.0.2-static\x00'
        )
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'ffprobe'
            binary.write_bytes(fake_bytes)
            info = self.runner.inspect_binary_markers(binary)
            self.assertTrue(info['is_elf'])
            self.assertTrue(info['has_embedded_multiarch_gconv_path'])
            self.assertTrue(info['has_gconv_get_path_symbol'])
            self.assertTrue(info['has_debian_8_marker'])
            self.assertTrue(info['has_7_0_2_static_marker'])

    def test_inspect_system_gconv_environment_reports_booleans_without_leaking_custom_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            multiarch = Path(temporary) / 'x86_64-linux-gnu' / 'gconv'
            multiarch.mkdir(parents=True)
            (multiarch / 'gconv-modules.cache').write_bytes(b'cache')
            lib64 = Path(temporary) / 'lib64' / 'gconv'
            info = self.runner.inspect_gconv_paths(multiarch_dir=multiarch, lib64_dir=lib64)
            self.assertEqual(info, {
                'multiarch_gconv_dir_exists': True,
                'multiarch_gconv_cache_exists': True,
                'multiarch_iso6937_exists': False,
                'lib64_gconv_dir_exists': False,
                'lib64_gconv_cache_exists': False,
            })

    def test_summarize_probe_json_extracts_format_and_stream_counts_only(self):
        raw_json = json.dumps({
            'format': {
                'filename': '/secret/path/x36xhzz.mp4',
                'format_name': 'mpegts',
                'duration': '634.633000',
                'size': '20049872',
                'tags': {'service_name': 'Secret Service'},
            },
            'streams': [
                {'codec_type': 'audio', 'codec_name': 'aac'},
                {'codec_type': 'video', 'codec_name': 'h264'},
            ],
        })
        parsed = self.runner.summarize_ffprobe_json(raw_json)
        self.assertEqual(parsed, {
            'json_valid': True,
            'format_name': 'mpegts',
            'stream_count': 2,
            'video_streams': 1,
            'audio_streams': 1,
            'has_duration': True,
        })
        self.assertNotIn('x36xhzz', json.dumps(parsed))
        self.assertNotIn('Secret Service', json.dumps(parsed))

    def test_summarize_probe_run_redacts_paths_and_captures_exit_code(self):
        record = self.runner.build_probe_run_record(
            mode='default_env',
            tool='ffprobe',
            gconv_mode='unset',
            returncode=-11,
            stdout='',
            stderr="[AVFormatContext @ 0x1] Opening '/secret/x36xhzz.mp4' for reading\n[mpegts @ 0x1] tag: 0x48 len=23\n",
            elapsed=0.052,
        )
        self.assertEqual(record['exit_code'], -11)
        self.assertEqual(record['signal_name'], 'SIGSEGV')
        self.assertEqual(record['gconv_mode'], 'unset')
        self.assertFalse(record['json_summary']['json_valid'])
        self.assertEqual(record['trace_summary']['last_signal'], 'MPEGTS_SDT_TAG_0X48_DVB_SERVICE')
        self.assertNotIn('/secret', json.dumps(record))
        self.assertNotIn('x36xhzz', json.dumps(record))

    def test_assess_platform_matrix_recognizes_ubuntu_crash_and_gconv_recovery(self):
        runs = [
            {'mode': 'json_probe', 'tool': 'ffprobe', 'gconv_mode': 'unset', 'exit_code': -11,
             'json_summary': {'json_valid': False}},
            {'mode': 'trace_probe', 'tool': 'ffprobe', 'gconv_mode': 'unset', 'exit_code': -11,
             'trace_summary': {'last_signal': 'MPEGTS_SDT_TAG_0X48_DVB_SERVICE'}},
            {'mode': 'null_decode', 'tool': 'ffmpeg', 'gconv_mode': 'unset', 'exit_code': -11},
            {'mode': 'json_probe', 'tool': 'ffprobe', 'gconv_mode': 'empty', 'exit_code': 0,
             'json_summary': {'json_valid': True, 'format_name': 'mpegts', 'stream_count': 2}},
            {'mode': 'null_decode', 'tool': 'ffmpeg', 'gconv_mode': 'empty', 'exit_code': 0},
        ]
        gconv = {'multiarch_gconv_dir_exists': True, 'multiarch_gconv_cache_exists': True}
        markers = {'is_elf': True, 'has_embedded_multiarch_gconv_path': True}
        assessment = self.runner.assess_ffprobe_diagnosis(runs, gconv, markers)
        self.assertEqual(assessment['classification'], 'STATIC_GLIBC_GCONV_ABI_CRASH_CONFIRMED')
        self.assertTrue(assessment['crashes_with_default_env'])
        self.assertTrue(assessment['recovers_with_empty_gconv_path'])
        self.assertEqual(assessment['crash_last_trace_signal'], 'MPEGTS_SDT_TAG_0X48_DVB_SERVICE')

    def test_cli_requires_explicit_tools_sample_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / 'existing'
            existing.mkdir()
            p1 = subprocess.run(
                [sys.executable, '-B', '-m', 'tests.check_live_ffprobe_diagnostics',
                 '--tools', temporary, '--output', str(existing)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(p1.returncode, 0)


if __name__ == '__main__':
    unittest.main()
