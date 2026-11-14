import os
import logging
import shlex
from pathlib import Path
from typing import Any, Dict, Callable, Optional, List
import yt_dlp
from backend.sanitizer import sanitize_text
from backend.runtime import OutputPathError, RuntimeConfig
from backend.services.artifacts import is_link, result_artifacts, validate_workspace
from backend.services.artifact_postprocessing import CaptureArtifactMovesPP, RecordFinalArtifactsPP
from backend.services.publication import record_result

logger = logging.getLogger("vidgrab.downloader")

class YtDlpLogger:
    def debug(self, msg):
        # Filter progress updates to avoid flooding system logs
        if not (msg.startswith('[download]') or msg.startswith('[SponsorBlock]')):
            logger.debug(msg)

    def info(self, msg):
        logger.info(msg)

    def warning(self, msg):
        logger.warning(msg)

    def error(self, msg):
        logger.error(msg)

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
            # Do not switch to postprocessing here because another stream might still be downloading
            pass
            
    return hook

def make_postprocessor_hook(
    task_id: str, 
    callback: Callable[[str, Dict[str, Any]], None],
    workspace: Optional[Path] = None
) -> Callable[[Dict[str, Any]], None]:
    """Generates a postprocessor hook to accurately report when post-processing starts."""
    
    def hook(d: Dict[str, Any]):
        status = d.get('status')
        source = (d.get('info_dict') or {}).get('filepath')
        if workspace is not None and status == 'started' and source and d.get('postprocessor') != 'RecordFinalArtifacts':
            path = Path(source)
            publications = (d.get('info_dict') or {}).get('__vidgrab_artifact_moves') or []
            published_move = (d.get('postprocessor') == 'MoveFiles' and publications
                              and publications[0].get('path') == source
                              and all(record.get('version') == 2 and record.get('state') == 'complete' for record in publications))
            if not published_move and (not path.resolve().is_relative_to(workspace) or (path.exists() and is_link(path))):
                raise OutputPathError("Post-processing input is outside this attempt's owned workspace")
        if status in ('started', 'processing'):
            payload = {
                "status": "postprocessing",
                "progress": 100.0,
                "speed": 0.0,
                "eta": 0
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
    runtime: RuntimeConfig,
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
    download_threads: int = 4,
    gpu_accelerated_conversion: bool = False,
    extra_args: Optional[Dict[str, Any]] = None,
    user_agent: Optional[str] = None,
    components: Optional[Dict[str, str]] = None,
    attempt_dir: Optional[str] = None,
    resume_attempt: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Synchronous yt-dlp attempt, normally run inside an owned worker process."""
    
    # Recheck at execution time, including paths restored from a legacy queue.
    out_dir, outtmpl = runtime.validate_output(download_dir, filename_template)
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = validate_workspace(runtime, attempt_dir) if attempt_dir is not None else None
    artifacts = []
    
    postprocessors = []
    
    # If audio-only, force bestaudio and add extraction postprocessor
    if audio_only:
        format_id = "bestaudio/best"
        postprocessors.append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_format,
            'preferredquality': '320' if audio_format == 'mp3' else '0',
        })
    else:
        # For video downloads on YouTube, dynamically inject m3u8 fallback to prevent HTTP 403 Forbidden
        if url and any(domain in url for domain in ['youtube.com', 'youtu.be']):
            if 'protocol^=m3u8' not in format_id and 'best' in format_id:
                new_fmt = format_id.replace('bestvideo', 'bestvideo[protocol^=m3u8]').replace('bestaudio', 'bestaudio[protocol^=m3u8]').replace('/best', '/best[protocol^=m3u8]')
                format_id = f"{new_fmt}/{format_id}"
        
    ydl_opts = {
        'format': format_id,
        'outtmpl': outtmpl,
        'restrictfilenames': True,
        'progress_hooks': [make_progress_hook(task_id, progress_callback, is_cancelled_cb)],
        'postprocessor_hooks': [make_postprocessor_hook(task_id, progress_callback)],
        'merge_output_format': 'mkv',
        'noresume': True,
        'quiet': False,
        'no_warnings': False,
        'logger': YtDlpLogger(),
        'js_runtimes': {'deno': {'path': None}, 'node': {'path': None}},
        'remote_components': [],
    }
    
    if user_agent:
        ydl_opts['http_headers'] = {'User-Agent': user_agent}
    
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
    
    # Enforce the injected cache/component policy after merging legacy options.
    ydl_opts['remote_components'] = []
    ydl_opts['noresume'] = True
    ydl_opts['continuedl'] = False
    ydl_opts['cachedir'] = str(runtime.temp_dir / 'yt-dlp-cache')
    if components is not None:
        # App-owned paths win over legacy custom options and host PATH discovery.
        ydl_opts['ffmpeg_location'] = str(Path(components['ffmpeg']).parent)
        ydl_opts['js_runtimes'] = {'deno': {'path': components['deno']}}

    if download_threads > 1:
        ydl_opts['concurrent_fragment_downloads'] = download_threads

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

    # Initialize postprocessor args
    if 'postprocessor_args' not in ydl_opts:
        ydl_opts['postprocessor_args'] = {}

    # Video container conversion / re-encoding
    if postprocess_codec and postprocess_codec != "none":
        # yt-dlp expects 'recode_video' for the CLI alias but in API we can just append a postprocessor
        # or use 'recodevideo' which is mapped to FFmpegVideoConvertor.
        # Actually, adding the postprocessor explicitly gives more control.
        # We append FFmpegVideoConvertor directly to postprocessors if we want GPU accel.
        if 'postprocessors' not in ydl_opts:
            ydl_opts['postprocessors'] = []
            
        ydl_opts['postprocessors'].append({
            'key': 'FFmpegVideoConvertor', 
            'preferedformat': postprocess_codec
        })
        
        if gpu_accelerated_conversion and postprocess_codec == "mp4":
            # Force re-encode with NVENC for web-playable MP4
            ydl_opts['postprocessor_args']['videoconvertor'] = [
                '-c:v', 'h264_nvenc', '-preset', 'p6', '-cq', '24', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k'
            ]

    # Audio normalization via ffmpeg loudnorm filter
    if postprocess_normalize_audio:
        # Apply loudnorm filter only to postprocessors that re-encode audio to avoid breaking merger stream copying.
        # For merger, we copy video but re-encode audio to aac to allow applying the loudnorm filter.
        # Ensure we don't overwrite the videoconvertor args if GPU accel already set them.
        ydl_opts['postprocessor_args']['merger'] = ['-c:v', 'copy', '-c:a', 'aac', '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        ydl_opts['postprocessor_args']['extractaudio'] = ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        
        if 'videoconvertor' in ydl_opts['postprocessor_args']:
            ydl_opts['postprocessor_args']['videoconvertor'].extend(['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11'])
        else:
            ydl_opts['postprocessor_args']['videoconvertor'] = ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        
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

    # Internal staging and callbacks win over legacy custom options.
    ydl_opts['progress_hooks'] = [make_progress_hook(task_id, progress_callback, is_cancelled_cb)]
    ydl_opts['postprocessor_hooks'] = [make_postprocessor_hook(task_id, progress_callback, workspace)]
    if workspace is not None:
        temporary_template = str(workspace / Path(outtmpl).name)
        ydl_opts['outtmpl'] = {'default': outtmpl, **{kind: temporary_template for kind in
                              ('temp', 'thumbnail', 'description', 'infojson')}}
        ydl_opts['overwrites'] = False

    logger.info(f"Starting download for task {task_id} with format {format_id} to {outtmpl}")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if is_playlist or playlist_items:
            from backend.services.playlists import extract_playlist
            # Raw extraction neither downloads media nor runs playlist postprocessors.
            return extract_playlist(ydl, url, playlist_items, is_cancelled_cb or (lambda: False))
        if workspace is not None and hasattr(ydl, "process_ie_result"):
            from backend.services.playlists import enforce_single_item
            enforce_single_item(ydl)
        if resume_attempt is not None:
            from backend.services.resume import install_http_resume
            from types import SimpleNamespace
            if workspace is None or set(resume_attempt) != {"id", "job_id", "number", "owner_id"}:
                raise ValueError("Verified resume requires an explicit owned attempt")
            install_http_resume(ydl, runtime, SimpleNamespace(**resume_attempt, workspace=workspace))
        ydl.add_post_processor(CaptureArtifactMovesPP(runtime, workspace), when='post_process')
        ydl.add_post_processor(RecordFinalArtifactsPP(runtime, artifacts, workspace), when='after_move')
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise Exception("Failed to extract video information or download file.")
        
        # Check if download had errors (e.g. 403 Forbidden under ignoreerrors)
        if getattr(ydl, '_download_retcode', 0) != 0:
            raise Exception(f"Download failed with exit code {ydl._download_retcode}. Check server logs.")
        if not artifacts:
            if workspace is not None:
                raise RuntimeError("yt-dlp did not report completed post-processing outputs")
            # Explicit diagnostic adapters may return an authoritative info record directly.
            artifacts = result_artifacts({'filepath': info.get('filepath')})
        result = {
            "filepath": artifacts[0]['path'],
            "artifacts": artifacts,
            "title": sanitize_text(info.get("title")),
            "thumbnail_url": info.get("thumbnail")
        }
        if workspace is not None:
            record_result(runtime, workspace, result)
        return result
