"""Standalone clear HLS/DASH fixture for minimal native UI environments.

Uses the existing fragment fixture's actual FFmpeg and HTTP behavior without
importing the source-engine test stack. Outputs are copied before cleanup.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


class FragmentServer:
    def __init__(self, protocol, ffmpeg, *, name='fixture'):
        self.ffmpeg = str(ffmpeg)
        self.endpoint = '/' + name + '.wav'
        self.temporary = tempfile.TemporaryDirectory(prefix="vidgrab-fragments-media-")
        self.root = Path(self.temporary.name)
        command = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=8", "-c:a", "aac", "-b:a", "64k",
            "-f", "hls", "-hls_time", "1", "-hls_segment_type", "fmp4", "-hls_playlist_type", "vod",
            "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "segment%d.m4s", "fixture.m3u8"]
        try:
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=25, check=True, close_fds=True, cwd=self.root,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
            self.segments = sorted(self.root.glob("segment*.m4s"), key=lambda path: int(path.stem[7:]))
            if len(self.segments) < 3:
                raise AssertionError("The fragment fixture needs multiple media segments")
            self.fragments = [self.root / "init.mp4", *self.segments]
            self.parts = {"/" + path.name: path.read_bytes() for path in self.fragments}
            self.media = b"".join(self.parts["/" + path.name] for path in self.fragments)
            self.expected = hashlib.sha256(self.media).hexdigest()
            reference = self.root / "reference.mp4"
            reference.write_bytes(self.media)
            self.decoded = self.decoded_hash(reference)
            if protocol == "hls":
                self.manifest = (self.root / "fixture.m3u8").read_bytes()
                self.content_type = "application/vnd.apple.mpegurl"
            else:
                segment_list = "".join('<SegmentURL media="' + path.name + '"/>' for path in self.segments)
                self.manifest = ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT8S">'
                    '<Period duration="PT8S"><AdaptationSet mimeType="audio/mp4" contentType="audio" codecs="mp4a.40.2">'
                    '<Representation id="audio" bandwidth="64000" audioSamplingRate="16000">'
                    '<SegmentList timescale="16000" duration="16000"><Initialization sourceURL="init.mp4"/>'
                    + segment_list + '</SegmentList></Representation></AdaptationSet></Period></MPD>').encode()
                self.content_type = "application/dash+xml"
            self.protocol = protocol
            self.release = threading.Event()
            self.entered = threading.Event()
            self.gated = True
            self.gate_path = "/" + self.segments[0].name
            self.auth = False
            self.cookie_seen = False
            self.requests = []
            fixture = self
            class Handler(BaseHTTPRequestHandler):
                def do_HEAD(self):
                    self.respond(False)

                def do_GET(self):
                    self.respond(True)

                def respond(self, body):
                    fixture.requests.append((self.command, self.path))
                    fresh = "session=fresh" in self.headers.get("Cookie", "")
                    fixture.cookie_seen |= fresh
                    if fixture.auth and not fresh:
                        self.send_error(401)
                        return
                    payload = fixture.manifest if self.path == fixture.endpoint else fixture.parts.get(self.path)
                    if payload is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Content-Type", fixture.content_type if self.path == fixture.endpoint else "audio/mp4")
                    self.send_header("ETag", '"' + hashlib.sha256(payload).hexdigest() + '"')
                    self.end_headers()
                    if not body:
                        return
                    if fixture.gated and self.path == fixture.gate_path:
                        fixture.entered.set()
                        if not fixture.release.wait(18):
                            return
                    try:
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

                def log_message(self, *_):
                    pass
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        except BaseException:
            self.temporary.cleanup()
            raise

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}" + self.endpoint

    def decoded_hash(self, path):
        output = subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"], capture_output=True, timeout=8, check=True,
            creationflags=0x08000000 if sys.platform == "win32" else 0)
        return hashlib.sha256(output.stdout).hexdigest()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.gated = False
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()
