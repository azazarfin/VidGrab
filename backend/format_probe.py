import time
import logging
import asyncio
import math
from collections import OrderedDict
from http.cookiejar import Cookie
from pathlib import Path
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import yt_dlp

from backend.sanitizer import sanitize_text
from backend.runtime import RuntimeConfig
from backend.engine.execution import join_owned_task
from backend.services.retry_policy import RequestFailure, retry_after

logger = logging.getLogger("vidgrab.format_probe")

CACHE_TTL = 300  # 5 minutes

class ProbeRequest(BaseModel):
    url: str

class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: Optional[str] = None
    filesize: Optional[int] = None
    filesize_approx: Optional[int] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    fps: Optional[float] = None
    tbr: Optional[float] = None  # Total bitrate
    container: Optional[str] = None

class PlaylistEntry(BaseModel):
    id: str
    title: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    url: str

class ProbeResponse(BaseModel):
    url: str
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    uploader: Optional[str] = None
    is_playlist: bool
    entries: Optional[List[PlaylistEntry]] = None
    formats: Optional[List[FormatInfo]] = None
    subtitles: Optional[Dict[str, List[Dict[str, str]]]] = None

def _run_probe(url: str, runtime: RuntimeConfig, *, request_context=None, cancelled=lambda: False,
               auth_context=None, components=None) -> ProbeResponse:
    """Synchronous yt-dlp call to extract info. Run in thread executor."""
    ydl_opts = {
        'extract_flat': 'in_playlist',  # Don't resolve playlist entries fully immediately
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'js_runtimes': {'deno': {'path': None}, 'node': {'path': None}},
        'remote_components': [],
        'cachedir': str(runtime.temp_dir / 'yt-dlp-cache'),
    }

    from backend.services.request_network import constrain_network_options, install_request_budget
    constrain_network_options(ydl_opts)
    if components is not None:
        ydl_opts['ffmpeg_location'] = str(Path(components['ffmpeg']).parent)
        ydl_opts['js_runtimes'] = {'deno': {'path': components['deno']}}
    if auth_context is not None:
        ydl_opts['http_headers'] = {key: value for key, value in
                                   (("User-Agent", auth_context.get("user_agent")), ("Referer", auth_context.get("referer"))) if value}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if auth_context is not None:
            for cookie in auth_context['cookies']:
                domain = cookie['domain']
                expiry = cookie.get('expirationDate')
                ydl.cookiejar.set_cookie(Cookie(0, cookie['name'], cookie['value'], None, False,
                    domain, domain.startswith('.'), domain.startswith('.'), cookie.get('path', '/'), True,
                    cookie.get('secure', False), int(expiry) if expiry else None, not bool(expiry), None, None, {}, False))
        if request_context is None:
            raise ValueError("Probing requires an initialized runtime owner")
        budget = install_request_budget(ydl, runtime, request_context, cancelled,
                                        credential_url=auth_context.get('scope_url') if auth_context else None)
        try:
            info = ydl.extract_info(url, download=False)
            budget.check()
            if info is None:
                raise ValueError("Failed to extract video information (returned None)")
            parsed = _parse_probe_result(url, info)
            budget.check()
            return parsed
        finally:
            budget.close()

def _parse_probe_result(url: str, info: Dict[str, Any]) -> ProbeResponse:
    is_playlist = info.get('_type') == 'playlist' or 'entries' in info
    
    entries = None
    formats = []
    subtitles = {}

    if is_playlist:
        entries = []
        from backend.services.playlists import MAX_CHILDREN
        for index, entry in enumerate(info.get('entries') or []):
            if index >= MAX_CHILDREN:
                raise ValueError("Probe playlist exceeds its 128-entry limit")
            if entry:
                # Resolve URL if it's just an ID
                entry_url = entry.get('url') or entry.get('webpage_url')
                if not entry_url and entry.get('id'):
                    # Default fallback for YouTube
                    entry_url = f"https://www.youtube.com/watch?v={entry['id']}"
                
                entries.append(PlaylistEntry(
                    id=str(entry.get('id', '')),
                    title=sanitize_text(entry.get('title', 'Unknown Video')),
                    duration=entry.get('duration'),
                    thumbnail=entry.get('thumbnail'),
                    url=entry_url or ""
                ))
    else:
        # Parse formats
        raw_formats = info.get('formats', [])
        for f in raw_formats:
            # Skip formats without clear type unless no other options
            resolution = f.get('resolution') or f.get('format_note') or f.get('height')
            formats.append(FormatInfo(
                format_id=f.get('format_id', ''),
                ext=f.get('ext', ''),
                resolution=str(resolution) if resolution is not None else None,
                filesize=f.get('filesize'),
                filesize_approx=f.get('filesize_approx'),
                vcodec=f.get('vcodec'),
                acodec=f.get('acodec'),
                fps=f.get('fps'),
                tbr=f.get('tbr'),
                container=f.get('container')
            ))
            
        # Parse subtitles
        raw_subs = info.get('subtitles', {})
        raw_auto_subs = info.get('automatic_captions', {})
        
        # Merge subtitles and tag them
        for lang, items in raw_subs.items():
            subtitles[lang] = [{"ext": item.get("ext", ""), "name": "Official"} for item in items]
        for lang, items in raw_auto_subs.items():
            if lang not in subtitles:
                subtitles[lang] = [{"ext": item.get("ext", ""), "name": "Auto-generated"} for item in items]

    return ProbeResponse(
        url=url,
        title=sanitize_text(info.get('title', 'Unknown Title')),
        thumbnail=info.get('thumbnail'),
        duration=info.get('duration'),
        uploader=sanitize_text(info.get('uploader') or info.get('playlist_uploader')),
        is_playlist=is_playlist,
        entries=entries,
        formats=formats if not is_playlist else None,
        subtitles=subtitles if not is_playlist else None
    )

class ProbeService:
    def __init__(self, runtime: RuntimeConfig, *, runner=None, clock=time.time, owner=None, components=None):
        self.runtime = runtime
        self.runner = _run_probe if runner is None else runner
        self.clock = clock
        self.owner = owner
        self.cache = OrderedDict()
        self.inflight = {}
        self.stopping = False
        self.components = components

    async def initialize(self):
        self.stopping = False

    async def stop(self):
        self.stopping = True
        if self.inflight:
            await asyncio.gather(*(join_owned_task(task) for task in list(self.inflight.values())), return_exceptions=True)

    def _extract(self, url, auth_context):
        if self.runner is not _run_probe:
            options = {} if auth_context is None else {"auth_context": auth_context}
            return _parse_probe_result(url, self.runner(url, self.runtime, **options))
        from backend.services.request_budget import begin_probe, end_probe
        context = begin_probe(self.runtime, self.owner, url, self.clock())
        try:
            return self.runner(url, self.runtime, request_context=context,
                               cancelled=lambda: self.stopping or not self.owner.healthy,
                               auth_context=auth_context, components=self.components)
        finally:
            end_probe(self.runtime, context)

    async def _execute(self, url, key, auth_context):
        try:
            parsed = await join_owned_task(asyncio.create_task(asyncio.to_thread(self._extract, url, auth_context)))
            self.cache[key] = (self.clock(), parsed)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return parsed
        finally:
            self.inflight.pop(key, None)

    async def probe(self, url: str, *, auth_context=None) -> ProbeResponse:
        url = url.strip()
        if not url:
            raise ValueError("URL cannot be empty")
        key = url if auth_context is None else (url, auth_context['identity'])
        now = self.clock()
        if key in self.cache:
            timestamp, data = self.cache[key]
            if now - timestamp < CACHE_TTL:
                self.cache.move_to_end(key)
                return data
            del self.cache[key]
        if self.stopping:
            raise RequestFailure("REQUEST_REJECTED")
        if key not in self.inflight:
            if len(self.inflight) >= 2:
                raise RequestFailure("ADMISSION_LIMITED", retry_at=now + 1, consumes_retry=False)
            self.inflight[key] = asyncio.create_task(self._execute(url, key, auth_context))
        return await join_owned_task(self.inflight[key])


def create_probe_router(service: ProbeService) -> APIRouter:
    router = APIRouter(tags=["probe"])

    @router.post("/probe", response_model=ProbeResponse)
    async def probe_url(req: ProbeRequest):
        try:
            return await service.probe(req.url)
        except RequestFailure as error:
            deadline = error.retry_at or service.clock()
            status = 429 if error.code in ("ADMISSION_LIMITED", "UPSTREAM_RATE_LIMITED") else 401 if error.code == "AUTH_REQUIRED" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "retry_at": error.retry_at},
                                headers={"Retry-After": str(max(0, math.ceil(deadline - service.clock())))}) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"Failed to extract video info: {error}") from error
        except Exception:
            logger.exception("Unexpected error during probing")
            raise HTTPException(status_code=500, detail="Internal server error during video probing")

    return router
