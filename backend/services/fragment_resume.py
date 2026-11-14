"""Verified static, clear, full-file HLS/DASH fragments in attempt-owned streams.

The ordered manifest and every fragment's current strong HTTP validator bind one
aggregate prefix record. Only fsynced whole-fragment boundaries can be reused.
No yt-dlp .ytdl file or fragment filename grants ownership.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from yt_dlp.downloader.dash import DashSegmentsFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import parse_m3u8_attributes

from backend.runtime import OutputPathError
from backend.services import resume

MANIFEST_LIMIT = 1024 * 1024
FRAGMENT_LIMIT = 4096


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _length(response):
    value = response.headers.get("Content-Length")
    if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
            or not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 19):
        return None
    value = int(value)
    return value if 0 < value <= 2**63 - 1 else None


def _validator(response, format_id):
    if response.status != 200 or response.headers.get("Content-Range"):
        return None
    return resume.source_validator(response.url, format_id, response.headers.get("ETag"), _length(response))


class VerifiedFragments:
    def __init__(self, ydl, params, runtime, attempt):
        super().__init__(ydl, {**params, "continuedl": False, "noresume": True, "nopart": False})
        self.runtime, self.attempt = runtime, attempt

    def _open(self, request):
        try:
            return resume.VerifiedHttpFD._open(self, request)
        except HTTPError as error:
            error.response.close()
            raise

    def _owner(self):
        with closing(resume._connect(self.runtime)) as db, db:
            resume._active(db, self.attempt)

    def _target(self, filename):
        partial = resume._target(self.runtime, self.attempt, str(filename) + ".part")
        if os.path.lexists(filename) or os.path.lexists(partial) or os.path.lexists(str(filename) + ".ytdl"):
            raise OutputPathError("Existing fragment output or partial is not owned by this transfer")
        return partial

    def _headers(self, info):
        headers = dict(info.get("http_headers") or {})
        if any(key.lower() in ("range", "if-range") for key in headers):
            raise OutputPathError("Caller-supplied fragment ranges cannot use verified resume")
        headers["Accept-Encoding"] = "identity"
        return headers

    def _manifest(self, url, info):
        self._owner()
        with closing(self._open(Request(url, headers=self._headers(info)))) as response:
            if response.status != 200 or response.headers.get("Content-Range"):
                raise OutputPathError("Unexpected manifest HTTP response")
            length = _length(response)
            if length is not None and length > MANIFEST_LIMIT:
                raise OutputPathError("Manifest exceeds its bounded size limit")
            chunks, size = [], 0
            while block := response.read(min(65536, MANIFEST_LIMIT + 1 - size)):
                chunks.append(block)
                size += len(block)
                if size > MANIFEST_LIMIT:
                    raise OutputPathError("Manifest exceeds its bounded size limit")
            body = b"".join(chunks)
            if length is not None and len(body) != length:
                raise OutputPathError("Manifest body does not match its declared length")
            return response.url, body

    def _fresh(self, cls, filename, info):
        self._owner()
        self._target(filename)
        prefix = Path(filename).name
        if any(path.name.startswith((prefix + ".part-Frag", prefix + "-Frag"))
               for path in self.attempt.workspace.iterdir()):
            raise OutputPathError("Existing native fragment is not owned by this transfer")
        fd = cls(self.ydl, self.params)
        for hook in self._progress_hooks:
            fd.add_progress_hook(hook)
        return fd.real_download(filename, info)

    def _progress(self, filename, partial, count, total, start, offset, info, *, finished=False, fragment_index=0):
        now = time.time()
        self._hook_progress({"status": "finished" if finished else "downloading",
            "filename": str(filename), "tmpfilename": str(partial), "downloaded_bytes": count,
            "total_bytes": total, "fragment_index": fragment_index, "speed": self.calc_speed(start, now, count - offset),
            "eta": self.calc_eta(start, now, total - offset, count - offset) if total else None}, info)

    def _transfer(self, filename, fragments, info, manifest):
        self._owner()
        filename = Path(filename)
        partial = self._target(filename)
        if not isinstance(fragments, list) or not 0 < len(fragments) <= FRAGMENT_LIMIT:
            raise OutputPathError("Fragment plan exceeds its bounded count limit")
        urls = [fragment.get("url") for fragment in fragments]
        if any(not isinstance(url, str) or len(url) > 65536 or urlsplit(url).scheme not in ("http", "https") for url in urls):
            raise OutputPathError("Unsupported fragment URL in static manifest")
        if sum(len(url.encode("utf-8")) for url in urls) > MANIFEST_LIMIT:
            raise OutputPathError("Resolved fragment plan exceeds its bounded size limit")
        if any(fragment.get("byte_range") or fragment.get("decrypt_info", {}).get("METHOD", "NONE") != "NONE" for fragment in fragments):
            raise OutputPathError("Fragment reuse requires clear full-file fragments")
        headers = self._headers(info)
        format_id = str(info.get("format_id") or "")
        if len(format_id) > 1024:
            raise OutputPathError("Fragment format identity exceeds its size limit")
        plan = _hash([manifest[0], hashlib.sha256(manifest[1]).hexdigest(), format_id, info.get("ext"), urls])
        validators = []
        start = time.time()
        # HEAD is only an opt-in reuse gate. Servers without it download fresh.
        # Every later GET checks the same effective URL, ETag, size and encoding.
        for url in urls:
            self._owner()
            self._progress(filename, partial, 0, None, start, 0, info)
            try:
                with closing(self._open(Request(url, headers=headers, method="HEAD"))) as response:
                    validator = _validator(response, format_id)
            except HTTPError as error:
                if error.status not in (405, 501):
                    raise
                error.response.close()
                validator = None
            if validator is None:
                validators = []
                break
            validators.append(validator)
        total = sum(item["length"] for item in validators) if validators else None
        source = resume.source_validator(manifest[0], "fragments-v1:" + plan, '"' + _hash(validators) + '"', total) if validators else None
        if validators and source is None:
            raise OutputPathError("Fragment stream exceeds its bounded byte size")
        offset = 0
        if source is not None:
            try:
                offset = resume.restore(self.runtime, self.attempt, partial, source)
            except (OSError, ValueError) as error:
                raise OutputPathError("Partial fragment ownership could not be verified; files retained") from error
        boundary, completed = 0, 0
        for item in validators:
            if boundary == offset:
                break
            boundary += item["length"]
            completed += 1
        if boundary != offset:
            raise OutputPathError("Partial prefix is not a completed fragment boundary")
        digest = hashlib.sha256()
        if offset:
            with resume._file(partial) as file:
                while block := file.read(resume.COPY_BLOCK):
                    digest.update(block)
        self._owner()
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | (os.O_APPEND if offset else os.O_CREAT | os.O_EXCL)
        with os.fdopen(os.open(partial, flags, 0o600), "ab") as output:
            metadata = os.fstat(output.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != offset:
                raise OutputPathError("Fragment partial changed before append")
            output.flush()
            os.fsync(output.fileno())
            if source is not None:
                resume.checkpoint(self.runtime, self.attempt, partial, source, offset, digest.hexdigest())
            count = offset
            for index, url in enumerate(urls[completed:], completed):
                self._owner()
                with closing(self._open(Request(url, headers=headers))) as response:
                    if response.status != 200 or response.headers.get("Content-Range"):
                        raise OutputPathError("Unexpected fragment HTTP response; partial retained")
                    if source is not None and _validator(response, format_id) != validators[index]:
                        raise OutputPathError("Fragment source validator changed before append; partial retained")
                    expected, downloaded = _length(response), 0
                    while block := response.read(65536):
                        downloaded += len(block)
                        if expected is not None and downloaded > expected:
                            raise OutputPathError("Fragment exceeds its declared length; partial retained")
                        output.write(block)
                        digest.update(block)
                        count += len(block)
                        self.slow_down(start, time.time(), count - offset)
                        # Progress/cancellation may observe an uncommitted tail.
                        # Its witness advances only after the complete fragment.
                        self._progress(filename, partial, count, total, start, offset, info)
                    if expected is not None and downloaded != expected:
                        raise RuntimeError("Fragment interrupted before its declared length")
                    output.flush()
                    os.fsync(output.fileno())
                    if source is not None:
                        resume.checkpoint(self.runtime, self.attempt, partial, source, count, digest.hexdigest())
                    else:
                        self._owner()
                    self._progress(filename, partial, count, total, start, offset, info, fragment_index=index + 1)
        self._owner()
        os.link(partial, filename)
        partial.unlink()
        self._progress(filename, partial, count, count, start, offset, info, finished=True)
        return True


class VerifiedHlsFD(VerifiedFragments, HlsFD):
    def real_download(self, filename, info_dict):
        self._owner()
        self._target(filename)
        manifest = self._manifest(info_dict["url"], info_dict)
        try:
            text = manifest[1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise OutputPathError("Manifest is not valid UTF-8") from error
        info = {**info_dict, "url": manifest[0], "hls_media_playlist_data": text}
        lines = [line.strip() for line in text.splitlines()]
        encrypted = any(parse_m3u8_attributes(line.split(":", 1)[1]).get("METHOD") != "NONE"
                        for line in lines if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")))
        if ("#EXT-X-ENDLIST" not in lines or info.get("is_live") or info.get("ext") == "vtt"
                or info.get("hls_aes") or encrypted or info.get("request_data")
                or "#EXT-X-BYTERANGE" in text or re.search(r"#EXT-X-MAP:.*BYTERANGE", text)
                or not HlsFD.can_download(text, info, False)):
            return self._fresh(HlsFD, filename, info)
        self.manifest = manifest
        return super().real_download(filename, info)

    def _prepare_and_start_frag_download(self, ctx, info_dict):
        # Let the pinned native parser resolve fragments without creating a .ytdl
        # or opening the aggregate stream before manifest verification.
        ctx["fragment_index"] = 0

    def download_and_append_fragments(self, ctx, fragments, info_dict, **kwargs):
        return self._transfer(ctx["filename"], fragments, info_dict, self.manifest)


class VerifiedDashFD(VerifiedFragments, DashSegmentsFD):
    def real_download(self, filename, info_dict):
        self._owner()
        self._target(filename)
        fragments = info_dict.get("fragments")
        if (not isinstance(fragments, list) or not 0 < len(fragments) <= FRAGMENT_LIMIT
                or info_dict.get("is_live") or info_dict.get("is_from_start") or info_dict.get("requested_formats")
                or not info_dict.get("manifest_url") or info_dict.get("request_data")
                or info_dict.get("protocol") != "http_dash_segments" or info_dict.get("has_drm")
                or any(fragment.get("byte_range") or fragment.get("range") for fragment in fragments)):
            return self._fresh(DashSegmentsFD, filename, info_dict)
        manifest = self._manifest(info_dict["manifest_url"], info_dict)
        try:
            text = manifest[1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise OutputPathError("DASH manifest is not valid UTF-8") from error
        if "\0" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise OutputPathError("DASH manifest declarations are unsupported")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            raise OutputPathError("DASH manifest is not valid XML") from error
        if (root.tag not in ("MPD", "{urn:mpeg:dash:schema:mpd:2011}MPD") or root.get("type", "static") != "static"
                or any(node.tag.rsplit("}", 1)[-1] == "ContentProtection" for node in root.iter())):
            return self._fresh(DashSegmentsFD, filename, info_dict)
        ctx = {"fragment_index": 0}
        # Preserve the pinned parser's relative URL and query handling.
        import urllib.parse
        extra = info_dict.get("extra_param_to_segment_url")
        query = urllib.parse.parse_qs(extra) if extra else None
        resolved = list(self._get_fragments(info_dict, ctx, query))
        return self._transfer(filename, resolved, info_dict, manifest)
