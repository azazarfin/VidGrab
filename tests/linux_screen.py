"""Read-only text/geometry observations for a guarded Ubuntu GUI screenshot."""

import csv
import io
import math
import os
from pathlib import Path
import struct
import subprocess


class Screen:
    def __init__(self, capture, *, segmentation=11, crop=None):
        if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
                Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation':
            raise RuntimeError('Screen checks require the named disposable Ubuntu VM')
        capture = Path(capture)
        if not capture.is_relative_to('/mnt/inputs/release-ux-screen-captures') or capture.is_symlink():
            raise ValueError('Select a fresh screenshot from the private VM control directory')
        with capture.open('rb') as stream:
            header = stream.read(24)
        if header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
            raise ValueError('Select a PNG screenshot')
        self.width, self.height = struct.unpack('>II', header[16:24])
        if not 1 <= self.width <= 4096 or not 1 <= self.height <= 4096 or capture.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('The VM screenshot exceeds the observation bound')
        if segmentation not in (6, 11):
            raise ValueError('Select supported screenshot text segmentation')
        offset_x = offset_y = 0
        source, image_data = str(capture), None
        if crop:
            offset_x, offset_y = math.floor(crop['x']), math.floor(crop['y'])
            width, height = math.ceil(crop['width']), math.ceil(crop['height'])
            if min(offset_x, offset_y) < 0 or min(width, height) < 1 \
                    or offset_x + width > self.width or offset_y + height > self.height:
                raise ValueError('The text crop must fit inside the captured screen')
            import gi
            gi.require_version('GdkPixbuf', '2.0')
            from gi.repository import GdkPixbuf
            pixels = GdkPixbuf.Pixbuf.new_from_file(str(capture)).new_subpixbuf(offset_x, offset_y, width, height)
            saved, image_data = pixels.save_to_bufferv('png', [], [])
            assert saved
            source = 'stdin'
        result = subprocess.run(['/usr/bin/tesseract', source, 'stdout', '-l', 'eng', '--psm', str(segmentation), 'tsv'],
            input=image_data, capture_output=True, check=True, timeout=15)
        if len(result.stdout) > 1024 * 1024:
            raise ValueError('The screen text exceeds the observation bound')
        self.words = []
        for row in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter='\t'):
            if row['level'] != '5' or not row['text'].strip():
                continue
            x, y, width, height = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
            x += offset_x
            y += offset_y
            if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > self.width or y + height > self.height:
                raise ValueError('A text observation points outside the screenshot')
            self.words.append({'text': row['text'], 'confidence': float(row['conf']),
                'line': tuple(int(row[key]) for key in ('page_num', 'block_num', 'par_num', 'line_num')),
                'word': int(row['word_num']), 'x': x, 'y': y, 'width': width, 'height': height})

    def find(self, text, *, region=None, confidence=75):
        expected = text.casefold().split()
        if not expected:
            raise ValueError('Select an exact visible label')
        lines = {}
        for word in self.words:
            lines.setdefault(word['line'], []).append(word)
        matches = []
        for line in lines.values():
            line.sort(key=lambda word: word['word'])
            for index in range(len(line) - len(expected) + 1):
                phrase = line[index:index + len(expected)]
                if [word['text'].casefold() for word in phrase] != expected or any(word['confidence'] < confidence for word in phrase):
                    continue
                left = min(word['x'] for word in phrase); top = min(word['y'] for word in phrase)
                right = max(word['x'] + word['width'] for word in phrase)
                bottom = max(word['y'] + word['height'] for word in phrase)
                if region and not (region['x'] <= left and region['y'] <= top
                        and right <= region['x'] + region['width'] and bottom <= region['y'] + region['height']):
                    continue
                matches.append({'x': left, 'y': top, 'width': right - left, 'height': bottom - top})
        if len(matches) > 1:
            raise AssertionError('The screen label is ambiguous: ' + text)
        return matches[0] if matches else None
