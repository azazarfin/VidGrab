import os
import logging
import shlex
from pathlib import Path
from typing import Any, Dict, Callable, Optional, List
import yt_dlp

logger = logging.getLogger("vidgrab.downloader")

def parse_and_sanitize_args(args_str: str) -> dict:
    if not args_str or not args_str.strip():
        return {}
    try:
        args = shlex.split(args_str)
    except Exception as e:
        logger.warning(f"Failed to split custom args: {e}")
        return {}
        
    disallowed_flags = {
        '--exec',
        '--exec-before-dl',
        '--external-downloader',
        '--downloader',
        '--downloader-args',
        '--ffmpeg-location',
    }
    
    sanitized_args = []
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        arg_lower = arg.lower()
        is_disallowed = False
        for flag in disallowed_flags:
            if arg_lower == flag:
                is_disallowed = True
                skip_next = True
                break
            elif arg_lower.startswith(flag + '='):
                is_disallowed = True
                break
        if not is_disallowed:
            sanitized_args.append(arg)
            
    if not sanitized_args:
        return {}
        
    try:
        res = yt_dlp.parse_options(sanitized_args)
        ydl_opts = res[-1]
        default_opts = yt_dlp.parse_options([])[-1]
        custom_opts = {}
        for k, v in ydl_opts.items():
            if k in default_opts and default_opts[k] == v:
                continue
            k_lower = k.lower()
            if any(x in k_lower for x in ['exec', 'downloader', 'location', 'cookie', 'path', 'outtmpl']):
                continue
            custom_opts[k] = v
        return custom_opts
    except Exception as e:
        logger.warning(f"Failed to parse/sanitize custom arguments: {e}")
        return {}

def make_progress_hook(
    task_id: str, 
    callback: Callable[[str, Dict[str, Any]], None],
    is_cancelled_cb: Optional[Callable[[], bool]] = None
) -> Callable[[Dict[str, Any]], None]:
    """Generates a progress hook tailored for a specific download task."""
    
    def hook(d: Dict[str, Any]):
        if is_cancelled_cb and is_cancelled_cb():
            raise Exception("Download cancelled by user")
            
        status = d.get('status')
        payload = {}
        
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            
            # Calculate percentage
            progress = (downloaded / total * 100) if total > 0 else 0
            
            payload = {
                "status": "downloading",
                "progress": round(progress, 2),
                "speed": d.get('speed', 0.0),
                "eta": d.get('eta'),
                "file_size": total
            }
            callback(task_id, payload)
            
        elif status == 'finished':
            # Finished downloading the file, moving to post-processing
            payload = {
                "status": "postprocessing",
                "progress": 100.0,
                "speed": 0.0,
                "eta": 0,
                "file_path": d.get('filename')
            }
            callback(task_id, payload)
            
    return hook

def run_download(
    task_id: str,
    url: str,
    format_id: str,
    download_dir: str,
    filename_template: str,
    progress_callback: Callable[[str, Dict[str, Any]], None],
    speed_limit: int = 0,
    cookies_file_path: str = None,
    is_cancelled_cb: Optional[Callable[[], bool]] = None,
    audio_only: bool = False,
    audio_format: str = "mp3",
    download_subtitles: bool = False,
    subtitle_langs: Optional[List[str]] = None,
    subtitle_format: str = "srt",
    embed_subtitles: bool = False,
    embed_thumbnail: bool = False,
    playlist_items: Optional[str] = None,
    is_playlist: bool = False,
    sponsorblock_enabled: bool = False,
    sponsorblock_categories: Optional[List[str]] = None,
    sponsorblock_mode: str = "remove",
    postprocess_codec: str = "none",
    postprocess_normalize_audio: bool = False,
    custom_args_str: str = "",
    extra_args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Synchronous function to execute the yt-dlp download. Run inside a thread."""
    
    # Expand and ensure download directory exists
    out_dir = Path(download_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    
    outtmpl = str(out_dir / filename_template)
    
    postprocessors = []
    
    # If audio-only, force bestaudio and add extraction postprocessor
    if audio_only:
        format_id = "bestaudio/best"
        postprocessors.append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_format,
            'preferredquality': '320' if audio_format == 'mp3' else '0',
        })
        
    ydl_opts = {
        'format': format_id,
        'outtmpl': outtmpl,
        'progress_hooks': [make_progress_hook(task_id, progress_callback, is_cancelled_cb)],
        'quiet': True,
        'no_warnings': True,
    }
    
    # Merge custom user arguments
    custom_opts = parse_and_sanitize_args(custom_args_str)
    for k, v in custom_opts.items():
        if isinstance(v, dict) and k in ydl_opts and isinstance(ydl_opts[k], dict):
            ydl_opts[k].update(v)
        else:
            ydl_opts[k] = v
            
    # Merge per-request extra arguments
    if extra_args:
        for k, v in extra_args.items():
            k_lower = k.lower()
            if any(x in k_lower for x in ['exec', 'downloader', 'location', 'cookie', 'path', 'outtmpl']):
                continue
            if isinstance(v, dict) and k in ydl_opts and isinstance(ydl_opts[k], dict):
                ydl_opts[k].update(v)
            else:
                ydl_opts[k] = v
    
    if speed_limit > 0:
        ydl_opts['ratelimit'] = speed_limit
        
    if cookies_file_path and os.path.exists(cookies_file_path):
        ydl_opts['cookiefile'] = cookies_file_path

    # SponsorBlock configuration
    if sponsorblock_enabled and sponsorblock_categories:
        if sponsorblock_mode == "mark":
            ydl_opts['sponsorblock_mark'] = sponsorblock_categories
        else:
            ydl_opts['sponsorblock_remove'] = sponsorblock_categories

    # Video container conversion / re-encoding
    if postprocess_codec and postprocess_codec != "none":
        ydl_opts['recodevideo'] = postprocess_codec

    # Audio normalization via ffmpeg loudnorm filter
    if postprocess_normalize_audio:
        if 'postprocessor_args' not in ydl_opts:
            ydl_opts['postprocessor_args'] = {}
        ydl_opts['postprocessor_args']['ffmpeg'] = ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        
    # Subtitle download and embed configuration
    if download_subtitles:
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitlesformat'] = subtitle_format
        if subtitle_langs:
            ydl_opts['subtitleslangs'] = subtitle_langs
        else:
            ydl_opts['subtitleslangs'] = ['all']
            
        if embed_subtitles:
            postprocessors.append({
                'key': 'FFmpegEmbedSubtitle',
                'already_have_subtitle': False,
            })
            
    # Thumbnail embedding
    if embed_thumbnail:
        ydl_opts['writethumbnail'] = True
        postprocessors.append({
            'key': 'EmbedThumbnail',
            'already_have_thumbnail': False,
        })
        
    if postprocessors:
        ydl_opts['postprocessors'] = postprocessors
        
    # Playlist configuration
    if is_playlist or playlist_items:
        ydl_opts['noplaylist'] = False
        if playlist_items:
            ydl_opts['playlist_items'] = playlist_items
    else:
        ydl_opts['noplaylist'] = True

    logger.info(f"Starting download for task {task_id} with format {format_id} to {outtmpl}")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp returns the final file path in the info dict or we can derive it
        # If it merged video and audio, requested_downloads contains the files
        # Let's get the final file path
        filepath = info.get('_filename') or ydl.prepare_filename(info)
        
        # If merged/postprocessed, the actual final filename might differ slightly 
        # (e.g. extension changed). We check requested_downloads.
        if info.get('requested_downloads'):
            filepath = info['requested_downloads'][0].get('_filename') or filepath
            
        if audio_only:
            filepath = str(Path(filepath).with_suffix(f".{audio_format}"))
            
        return {
            "filepath": str(filepath),
            "title": info.get("title"),
            "thumbnail_url": info.get("thumbnail")
        }
