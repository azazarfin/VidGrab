import asyncio
import logging
import time
import json
import os
import sys
import glob
import subprocess
from datetime import datetime, timezone
from uuid import uuid4
from typing import Dict, Any, List, Optional
from fastapi import WebSocket, APIRouter, HTTPException, Request
from pydantic import BaseModel
import aiosqlite

from backend.config import DB_PATH, get_all_config
from backend.downloader import run_download
from backend.cookie_manager import write_cookies_to_netscape_format
from backend.sanitizer import sanitize_text

logger = logging.getLogger("vidgrab.queue_manager")
router = APIRouter(prefix="/downloads", tags=["downloads"])

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    audio_only: Optional[bool] = False
    audio_format: Optional[str] = "mp3"
    download_subtitles: Optional[bool] = False
    subtitle_langs: Optional[List[str]] = None
    subtitle_format: Optional[str] = "srt"
    embed_subtitles: Optional[bool] = False
    embed_thumbnail: Optional[bool] = False
    playlist_items: Optional[str] = None
    is_playlist: Optional[bool] = False
    output_template: Optional[str] = None
    cookies: Optional[List[Dict[str, Any]]] = None
    scheduled_at: Optional[str] = None
    extra_args: Optional[Dict[str, Any]] = None
    duration: Optional[int] = None
    download_dir: Optional[str] = None

class RetryRequest(BaseModel):
    cookies: Optional[List[Dict[str, Any]]] = None

class DownloadTask(BaseModel):
    id: str
    url: str
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    status: str
    progress: float
    speed: float
    eta: Optional[float] = None
    file_size: Optional[int] = None
    file_path: Optional[str] = None
    format_selected: str
    error_message: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    download_options: Optional[str] = None
    scheduled_at: Optional[str] = None

# WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send WS message, removing stale connection: {e}")
                self.disconnect(connection)

ws_manager = ConnectionManager()

# Queue Manager State
class QueueManager:
    def __init__(self):
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.cancelled_tasks: set = set()
        self.in_memory_progress: Dict[str, Dict[str, Any]] = {}
        self.worker_task: Optional[asyncio.Task] = None
        self.scheduler_task: Optional[asyncio.Task] = None
        self.wakeup_event = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task_cookies: Dict[str, List[Dict[str, Any]]] = {}

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.worker_task = asyncio.create_task(self._worker_loop())
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("Queue manager worker loop and scheduler loop started.")

    async def stop(self):
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        logger.info("Queue manager worker loop and scheduler loop stopped.")

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self.cancelled_tasks

    async def enqueue(
        self,
        url: str,
        format_id: str,
        title: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        audio_only: bool = False,
        audio_format: str = "mp3",
        download_subtitles: bool = False,
        subtitle_langs: Optional[List[str]] = None,
        subtitle_format: str = "srt",
        embed_subtitles: bool = False,
        embed_thumbnail: bool = False,
        playlist_items: Optional[str] = None,
        is_playlist: bool = False,
        output_template: Optional[str] = None,
        cookies: Optional[List[Dict[str, Any]]] = None,
        scheduled_at: Optional[str] = None,
        extra_args: Optional[Dict[str, Any]] = None,
        duration: Optional[int] = None,
        download_dir: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> str:
        task_id = str(uuid4())
        title = sanitize_text(title)
        created_at = datetime.now(timezone.utc).isoformat()
        status = "scheduled" if scheduled_at else "queued"
        
        if cookies:
            self.task_cookies[task_id] = cookies

        options = {
            "audio_only": audio_only,
            "audio_format": audio_format,
            "download_subtitles": download_subtitles,
            "subtitle_langs": subtitle_langs,
            "subtitle_format": subtitle_format,
            "embed_subtitles": embed_subtitles,
            "embed_thumbnail": embed_thumbnail,
            "playlist_items": playlist_items,
            "is_playlist": is_playlist,
            "output_template": output_template,
            "extra_args": extra_args,
            "duration": duration,
            "download_dir": download_dir,
            "user_agent": user_agent
        }
        options_json = json.dumps(options)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO downloads (id, url, title, thumbnail_url, status, progress, created_at, format_selected, download_options, scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, url, title, thumbnail_url, status, 0.0, created_at, format_id, options_json, scheduled_at)
            )
            await db.commit()

        self.in_memory_progress[task_id] = {
            "id": task_id,
            "url": url,
            "title": title,
            "thumbnail_url": thumbnail_url,
            "status": status,
            "progress": 0.0,
            "speed": 0.0,
            "eta": None,
            "file_size": None,
            "format_selected": format_id,
            "created_at": created_at,
            "download_options": options_json,
            "scheduled_at": scheduled_at
        }

        # Broadcast event
        await ws_manager.broadcast({
            "event": "task_scheduled" if scheduled_at else "task_queued",
            "data": self.in_memory_progress[task_id]
        })

        # Wake up worker if queued
        if not scheduled_at:
            self.wakeup_event.set()
        return task_id

    async def cancel(self, task_id: str):
        self.cancelled_tasks.add(task_id)
        
        # If running, cancel the asyncio task
        if task_id in self.active_tasks:
            self.active_tasks[task_id].cancel()
            
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE downloads SET status = ?, completed_at = ? WHERE id = ?",
                ("cancelled", datetime.now(timezone.utc).isoformat(), task_id)
            )
            await db.commit()

        if task_id in self.in_memory_progress:
            self.in_memory_progress[task_id]["status"] = "cancelled"
            await ws_manager.broadcast({
                "event": "task_cancelled",
                "data": self.in_memory_progress[task_id]
            })

    def handle_progress(self, task_id: str, payload: Dict[str, Any]):
        """Callback from downloader. Updates in-memory state and broadcasts."""
        if task_id not in self.in_memory_progress:
            return
            
        self.in_memory_progress[task_id].update(payload)
        
        # Schedule the broadcast on the running event loop safely from worker thread
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                ws_manager.broadcast({
                    "event": "task_progress",
                    "data": self.in_memory_progress[task_id]
                }),
                self.loop
            )

    async def _scheduler_loop(self):
        logger.info("Queue manager scheduler loop started.")
        while True:
            try:
                await asyncio.sleep(30)
                now_str = datetime.now(timezone.utc).isoformat()
                
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    # Fetch scheduled downloads that are due
                    async with db.execute(
                        "SELECT * FROM downloads WHERE status = ? AND scheduled_at <= ? ORDER BY scheduled_at ASC",
                        ("scheduled", now_str)
                    ) as cursor:
                        rows = await cursor.fetchall()
                        
                    if rows:
                        logger.info(f"Scheduler found {len(rows)} due scheduled download(s). Enqueueing...")
                        for row in rows:
                            task_id = row["id"]
                            # Update status to queued and clear scheduled_at
                            await db.execute(
                                "UPDATE downloads SET status = ?, scheduled_at = NULL WHERE id = ?",
                                ("queued", task_id)
                            )
                            
                            # Update in-memory and broadcast
                            if task_id not in self.in_memory_progress:
                                self.in_memory_progress[task_id] = dict(row)
                            
                            self.in_memory_progress[task_id]["status"] = "queued"
                            self.in_memory_progress[task_id]["scheduled_at"] = None
                            
                            await ws_manager.broadcast({
                                "event": "task_queued",
                                "data": self.in_memory_progress[task_id]
                            })
                            
                        await db.commit()
                        # Wake up worker
                        self.wakeup_event.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")

    async def _worker_loop(self):
        # Reset any stuck tasks from previous crash
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE downloads SET status = ? WHERE status IN (?, ?)",
                ("queued", "downloading", "probing")
            )
            await db.commit()

        while True:
            try:
                config = await get_all_config()
                max_concurrent = config.get("max_concurrent", 3)
                
                # Clean up finished tasks from active list
                self.active_tasks = {tid: t for tid, t in self.active_tasks.items() if not t.done()}
                
                if len(self.active_tasks) >= max_concurrent:
                    # Wait for at least one active task to finish
                    await asyncio.wait(self.active_tasks.values(), return_when=asyncio.FIRST_COMPLETED)
                    continue
                
                # Fetch next queued item
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        "SELECT * FROM downloads WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                        ("queued",)
                    ) as cursor:
                        row = await cursor.fetchone()
                
                if not row:
                    # No queued items, wait for wakeup signal
                    self.wakeup_event.clear()
                    await self.wakeup_event.wait()
                    continue
                
                task_id = row["id"]
                url = row["url"]
                format_id = row["format_selected"]
                options_str = row["download_options"]
                
                options = {}
                if options_str:
                    try:
                        options = json.loads(options_str)
                    except Exception:
                        pass
                
                # Initialize in-memory progress if not present
                if task_id not in self.in_memory_progress:
                    self.in_memory_progress[task_id] = dict(row)
                
                # Atomically claim the task to prevent race conditions
                async with aiosqlite.connect(DB_PATH) as db2:
                    await db2.execute(
                        "UPDATE downloads SET status = ? WHERE id = ? AND status = ?",
                        ("downloading", task_id, "queued")
                    )
                    await db2.commit()
                    if db2.total_changes == 0:
                        continue  # Another worker iteration already claimed it
                    
                await ws_manager.broadcast({
                    "event": "task_started",
                    "data": self.in_memory_progress[task_id]
                })

                # Start the download task
                task = asyncio.create_task(self._run_task(task_id, url, format_id, config, options, dict(row).get("retry_count", 0)))
                self.active_tasks[task_id] = task
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in queue worker loop: {e}")
                await asyncio.sleep(5)

    async def _run_task(self, task_id: str, url: str, format_id: str, config: Dict[str, Any], options: Dict[str, Any], retry_count: int = 0):
        download_dir = options.get("download_dir") or config.get("download_dir")
        filename_template = options.get("output_template") or config.get("filename_template")
        speed_limit = config.get("speed_limit", 0)

        # Extract SponsorBlock and post-processing options from config
        sponsorblock_enabled = config.get("sponsorblock_enabled", False)
        sponsorblock_categories = config.get("sponsorblock_categories", [])
        sponsorblock_mode = config.get("sponsorblock_mode", "remove")
        postprocess_codec = config.get("postprocess_codec", "none")
        postprocess_normalize_audio = config.get("postprocess_normalize_audio", False)
        custom_ytdlp_args = config.get("custom_ytdlp_args", "")
        download_threads = config.get("download_threads", 4)
        gpu_accelerated_conversion = config.get("gpu_accelerated_conversion", False)
        user_agent = options.get("user_agent")

        # Retrieve and write cookies if present
        cookies = self.task_cookies.pop(task_id, None)
        cookies_file_path = None
        if cookies:
            try:
                cookies_file_path = write_cookies_to_netscape_format(cookies)
            except Exception as e:
                logger.warning(f"Failed to generate Netscape cookies for task {task_id}: {e}")

        try:
            # Run the blocking download in a separate thread
            # Pass a lambda that checks if the task is cancelled
            result = await asyncio.to_thread(
                run_download,
                task_id=task_id,
                url=url,
                format_id=format_id,
                download_dir=download_dir,
                filename_template=filename_template,
                progress_callback=self.handle_progress,
                speed_limit=speed_limit,
                cookies_file_path=cookies_file_path,
                is_cancelled_cb=lambda: self.is_cancelled(task_id),
                audio_only=options.get("audio_only", False),
                audio_format=options.get("audio_format", "mp3"),
                download_subtitles=options.get("download_subtitles", False),
                subtitle_langs=options.get("subtitle_langs"),
                subtitle_format=options.get("subtitle_format", "srt"),
                embed_subtitles=options.get("embed_subtitles", False),
                embed_thumbnail=options.get("embed_thumbnail", False),
                playlist_items=options.get("playlist_items"),
                is_playlist=options.get("is_playlist", False),
                sponsorblock_enabled=sponsorblock_enabled,
                sponsorblock_categories=sponsorblock_categories,
                sponsorblock_mode=sponsorblock_mode,
                postprocess_codec=postprocess_codec,
                postprocess_normalize_audio=postprocess_normalize_audio,
                custom_args_str=custom_ytdlp_args,
                download_threads=download_threads,
                gpu_accelerated_conversion=gpu_accelerated_conversion,
                extra_args=options.get("extra_args"),
                user_agent=user_agent
            )
            
            filepath = sanitize_text(result.get("filepath"))
            extracted_title = sanitize_text(result.get("title"))
            extracted_thumbnail = result.get("thumbnail_url")
            
            if self.is_cancelled(task_id):
                raise Exception("Download cancelled by user")
                
            if not filepath or not os.path.exists(filepath):
                # yt-dlp might have changed the extension during postprocessing
                if filepath:
                    base, ext = os.path.splitext(filepath)
                    found = False
                    for possible_ext in ['.mp4', '.mkv', '.webm', '.m4a', '.mp3']:
                        if os.path.exists(base + possible_ext):
                            filepath = base + possible_ext
                            found = True
                            break
                    if not found:
                        raise Exception(f"Final file was not created. Download failed or was aborted by yt-dlp.")
                else:
                    raise Exception(f"Final file was not created. Download failed or was aborted by yt-dlp.")
            
            # Download completed successfully
            completed_at = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE downloads SET status = ?, file_path = ?, completed_at = ?, progress = 100.0, title = COALESCE(title, ?), thumbnail_url = COALESCE(thumbnail_url, ?) WHERE id = ?",
                    ("complete", filepath, completed_at, extracted_title, extracted_thumbnail, task_id)
                )
                await db.commit()
                
            if task_id in self.in_memory_progress:
                self.in_memory_progress[task_id].update({
                    "status": "complete",
                    "progress": 100.0,
                    "file_path": filepath,
                    "completed_at": completed_at
                })
                # Apply extracted title if missing
                if not self.in_memory_progress[task_id].get("title") and extracted_title:
                    self.in_memory_progress[task_id]["title"] = extracted_title
                if not self.in_memory_progress[task_id].get("thumbnail_url") and extracted_thumbnail:
                    self.in_memory_progress[task_id]["thumbnail_url"] = extracted_thumbnail
                    
                await ws_manager.broadcast({
                    "event": "task_complete",
                    "data": self.in_memory_progress[task_id]
                })
                
        except Exception as e:
            # Handle failure or cancellation
            is_cancelled = self.is_cancelled(task_id)
            error_msg = sanitize_text(str(e))
            completed_at = datetime.now(timezone.utc).isoformat()
            
            max_retries = config.get("max_retries", 3)
            
            if is_cancelled:
                status = "cancelled"
                logger.error(f"Task {task_id} ended with status {status}: {error_msg}")
            elif retry_count < max_retries:
                status = "queued"
                retry_count += 1
                logger.warning(f"Task {task_id} failed, retrying ({retry_count}/{max_retries}). Error: {error_msg}")
            else:
                status = "failed"
                logger.error(f"Task {task_id} ended with status {status}: {error_msg}")
            
            async with aiosqlite.connect(DB_PATH) as db:
                if status == "queued":
                    await db.execute(
                        "UPDATE downloads SET status = ?, error_message = ?, retry_count = ? WHERE id = ?",
                        (status, error_msg, retry_count, task_id)
                    )
                else:
                    await db.execute(
                        "UPDATE downloads SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
                        (status, error_msg, completed_at, task_id)
                    )
                await db.commit()
                
            if task_id in self.in_memory_progress:
                self.in_memory_progress[task_id].update({
                    "status": status,
                    "error_message": error_msg,
                })
                if status != "queued":
                    self.in_memory_progress[task_id]["completed_at"] = completed_at
                
                await ws_manager.broadcast({
                    "event": f"task_{status}",
                    "data": self.in_memory_progress[task_id]
                })
                
            # If we queued it for retry, wake up the worker
            if status == "queued":
                self.wakeup_event.set()
                
            # Clean up orphaned .part files if the task has permanently failed or was cancelled
            if status in ("failed", "cancelled") and download_dir:
                try:
                    title = self.in_memory_progress.get(task_id, {}).get("title")
                    if title:
                        safe_title = title.replace("/", "_").replace("\\", "_")
                        search_pattern = os.path.join(download_dir, f"{safe_title}*")
                        for f in glob.glob(search_pattern):
                            if ".part" in f or ".ytdl" in f:
                                try:
                                    os.remove(f)
                                except OSError:
                                    pass
                except Exception as clean_e:
                    logger.warning(f"Failed to cleanup temp files for task {task_id}: {clean_e}")
                
                
        finally:
            # Clean up tracking sets
            self.cancelled_tasks.discard(task_id)
            
            # Delete temporary cookie file
            if cookies_file_path and os.path.exists(cookies_file_path):
                try:
                    os.unlink(cookies_file_path)
                    logger.info(f"Deleted temporary cookie file: {cookies_file_path}")
                except Exception as ce:
                    logger.warning(f"Failed to delete temporary cookie file {cookies_file_path}: {ce}")

queue_manager = QueueManager()

# REST Endpoints for Downloads
@router.post("", response_model=Dict[str, str])
async def create_download(req: DownloadRequest, request: Request):
    user_agent = request.headers.get("user-agent")
    task_id = await queue_manager.enqueue(
        url=req.url,
        format_id=req.format_id,
        title=req.title,
        thumbnail_url=req.thumbnail_url,
        audio_only=req.audio_only,
        audio_format=req.audio_format,
        download_subtitles=req.download_subtitles,
        subtitle_langs=req.subtitle_langs,
        subtitle_format=req.subtitle_format,
        embed_subtitles=req.embed_subtitles,
        embed_thumbnail=req.embed_thumbnail,
        playlist_items=req.playlist_items,
        is_playlist=req.is_playlist,
        output_template=req.output_template,
        cookies=req.cookies,
        scheduled_at=req.scheduled_at,
        extra_args=req.extra_args,
        duration=req.duration,
        download_dir=req.download_dir,
        user_agent=user_agent
    )
    return {"task_id": task_id}

# History specific endpoints (subset of downloads: complete, failed, cancelled)
@router.get("/history", response_model=List[DownloadTask], tags=["history"])
async def get_history():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM downloads WHERE status IN (?, ?, ?) ORDER BY completed_at DESC",
            ("complete", "failed", "cancelled")
        ) as cursor:
            rows = await cursor.fetchall()
            return [DownloadTask(**dict(row)) for row in rows]

@router.delete("/history", response_model=Dict[str, str], tags=["history"])
async def clear_all_history():
    async with aiosqlite.connect(DB_PATH) as db:
        # Delete only completed, failed, or cancelled tasks from DB
        await db.execute(
            "DELETE FROM downloads WHERE status IN (?, ?, ?)",
            ("complete", "failed", "cancelled")
        )
        await db.commit()
    
    # Remove from in-memory tracking if present
    to_remove = [tid for tid, task in queue_manager.in_memory_progress.items() 
                 if task["status"] in ("complete", "failed", "cancelled")]
    for tid in to_remove:
        queue_manager.in_memory_progress.pop(tid, None)
        
    return {"message": "All download history cleared"}

@router.delete("/history/{task_id}", response_model=Dict[str, str], tags=["history"])
async def delete_history_item(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT status FROM downloads WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="History item not found")
            if row[0] not in ("complete", "failed", "cancelled"):
                raise HTTPException(status_code=400, detail="Cannot delete an active download from history")
            
            await db.execute("DELETE FROM downloads WHERE id = ?", (task_id,))
            await db.commit()

    # Remove from in-memory tracking
    queue_manager.in_memory_progress.pop(task_id, None)
    return {"message": f"History item {task_id} deleted"}

@router.delete("/{task_id}")
async def cancel_download(task_id: str):
    # Verify task exists
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM downloads WHERE id = ?", (task_id,)) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Download task not found")
                
    await queue_manager.cancel(task_id)
    return {"message": f"Download task {task_id} cancelled"}
@router.get("", response_model=List[DownloadTask])
async def list_downloads(status: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            query = "SELECT * FROM downloads WHERE status = ? ORDER BY created_at DESC"
            params = (status,)
        else:
            query = "SELECT * FROM downloads ORDER BY created_at DESC"
            params = ()
            
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [DownloadTask(**dict(row)) for row in rows]

@router.post("/{task_id}/retry", response_model=Dict[str, str])
async def retry_download(task_id: str, req: RetryRequest, request: Request):
    user_agent = request.headers.get("user-agent")
    if req.cookies:
        queue_manager.task_cookies[task_id] = req.cookies
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM downloads WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Download task not found")
            
            task_dict = dict(row)
            if task_dict["status"] not in ("failed", "cancelled"):
                raise HTTPException(status_code=400, detail="Only failed or cancelled downloads can be retried")

            # Extract options and update user_agent if present
            try:
                opts = json.loads(task_dict.get("download_options") or "{}")
                if user_agent:
                    opts["user_agent"] = user_agent
                options_json = json.dumps(opts)
            except Exception:
                options_json = task_dict.get("download_options")

            # Update status back to queued in database
            await db.execute(
                "UPDATE downloads SET status = ?, error_message = NULL, progress = 0.0, speed = 0.0, eta = NULL, download_options = ? WHERE id = ?",
                ("queued", options_json, task_id)
            )
            await db.commit()

    # Update in-memory state
    queue_manager.in_memory_progress[task_id] = {
        "id": task_id,
        "url": task_dict["url"],
        "title": task_dict["title"],
        "thumbnail_url": task_dict["thumbnail_url"],
        "status": "queued",
        "progress": 0.0,
        "speed": 0.0,
        "eta": None,
        "file_size": None,
        "format_selected": task_dict["format_selected"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "download_options": options_json
    }

    # Broadcast task queued event
    await ws_manager.broadcast({
        "event": "task_queued",
        "data": queue_manager.in_memory_progress[task_id]
    })

    # Wake up worker
    queue_manager.wakeup_event.set()
    return {"message": f"Download task {task_id} re-enqueued for retry"}

@router.post("/{task_id}/open-folder", response_model=Dict[str, Any])
async def open_task_folder(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT file_path, status FROM downloads WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Download task not found")
            
            file_path = row["file_path"]
            status = row["status"]
            if status != "complete" or not file_path:
                raise HTTPException(status_code=400, detail="Only completed downloads with a valid file path can have their folder opened")
                
            if not os.path.exists(file_path):
                raise HTTPException(status_code=404, detail=f"File does not exist on disk: {file_path}")
                
            parent_dir = os.path.dirname(file_path)
            try:
                if sys.platform == "win32":
                    os.startfile(parent_dir)
                elif sys.platform == "darwin":
                    subprocess.run(["open", parent_dir], check=True)
                else:
                    subprocess.run(["xdg-open", parent_dir], check=True)
                return {"success": True, "message": f"Opened folder: {parent_dir}"}
            except Exception as e:
                logger.error(f"Failed to open folder {parent_dir}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to open containing folder: {str(e)}")
