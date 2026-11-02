import time
import logging
import asyncio
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import yt_dlp

from backend.sanitizer import sanitize_text
from backend.runtime import RuntimeConfig

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

def _run_probe(url: str, runtime: RuntimeConfig) -> Dict[str, Any]:
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise ValueError("Failed to extract video information (returned None)")
            return info
        except Exception as e:
            logger.error(f"yt-dlp extraction failed for {url}: {e}")
            raise ValueError(str(e))

def _parse_probe_result(url: str, info: Dict[str, Any]) -> ProbeResponse:
    is_playlist = info.get('_type') == 'playlist' or 'entries' in info
    
    entries = None
    formats = []
    subtitles = {}

    if is_playlist:
        entries = []
        for entry in info.get('entries', []):
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
            formats.append(FormatInfo(
                format_id=f.get('format_id', ''),
                ext=f.get('ext', ''),
                resolution=f.get('resolution') or f.get('format_note') or f.get('height'),
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
    def __init__(self, runtime: RuntimeConfig, *, runner=None, clock=time.time):
        self.runtime = runtime
        self.runner = _run_probe if runner is None else runner
        self.clock = clock
        self.cache = {}

    async def probe(self, url: str) -> ProbeResponse:
        url = url.strip()
        if not url:
            raise ValueError("URL cannot be empty")
        now = self.clock()
        if url in self.cache:
            timestamp, data = self.cache[url]
            if now - timestamp < CACHE_TTL:
                return data
        info = await asyncio.to_thread(self.runner, url, self.runtime)
        parsed = _parse_probe_result(url, info)
        self.cache[url] = (now, parsed)
        if len(self.cache) > 500:
            expired = [key for key, (timestamp, _) in self.cache.items() if now - timestamp >= CACHE_TTL]
            for key in expired:
                del self.cache[key]
        return parsed


def create_probe_router(service: ProbeService) -> APIRouter:
    router = APIRouter(tags=["probe"])

    @router.post("/probe", response_model=ProbeResponse)
    async def probe_url(req: ProbeRequest):
        try:
            return await service.probe(req.url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"Failed to extract video info: {error}") from error
        except Exception:
            logger.exception("Unexpected error during probing")
            raise HTTPException(status_code=500, detail="Internal server error during video probing")

    return router
