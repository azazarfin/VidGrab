import asyncio
import logging
import time
import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from uuid import uuid4
from typing import Dict, Any, List, Optional
from fastapi import WebSocket, APIRouter, HTTPException, Request
from pydantic import BaseModel
import aiosqlite

from backend.config import SettingsStore
from backend.runtime import OutputPathError, RuntimeConfig
from backend.downloader import run_download
from backend.cookie_manager import write_cookies_to_netscape_format
from backend.sanitizer import sanitize_text
from backend.engine.execution import ProcessDownloadExecutor, ThreadDownloadExecutor, join_owned_task
from backend.services.job_state import FINISH_FROM, RUNNABLE, TERMINAL, accepts_progress

logger = logging.getLogger("vidgrab.queue_manager")

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

# Queue Manager State
class QueueManager:
    def __init__(self, runtime: RuntimeConfig, settings: SettingsStore, connections: ConnectionManager,
                 download_runner=run_download, clock=time.time, open_folder=None, download_executor=None):
        self.runtime = runtime
        self.settings = settings
        self.connections = connections
        self.download_runner = download_runner
        self.download_executor = download_executor or (
            ProcessDownloadExecutor() if download_runner is run_download else ThreadDownloadExecutor(download_runner)
        )
        self.clock = clock
        self.open_folder = open_folder
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.cancelled_tasks: set = set()
        self.in_memory_progress: Dict[str, Dict[str, Any]] = {}
        self.worker_task: Optional[asyncio.Task] = None
        self.scheduler_task: Optional[asyncio.Task] = None
        self.stop_task: Optional[asyncio.Task] = None
        self.wakeup_event = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task_cookies: Dict[str, List[Dict[str, Any]]] = {}
        self.cancel_locks: Dict[str, asyncio.Lock] = {}
        self.state_locks: Dict[str, asyncio.Lock] = {}
        self.stopping = False

    def _state_lock(self, task_id):
        return self.state_locks.setdefault(task_id, asyncio.Lock())

    def start(self):
        self.stopping = False
        self.stop_task = None
        self.loop = asyncio.get_running_loop()
        self.worker_task = asyncio.create_task(self._worker_loop())
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("Queue manager worker loop and scheduler loop started.")

    async def stop(self):
        if self.stop_task is None:
            self.stop_task = asyncio.create_task(self._stop())
        await join_owned_task(self.stop_task)

    async def _stop(self):
        self.stopping = True
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
        active = dict(self.active_tasks)
        for task_id, task in active.items():
            if not task.done() and task_id not in self.cancelled_tasks:
                task.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)
            # A coroutine cancelled before its first turn cannot run its finally block.
            for task_id in active:
                status = "cancelled" if task_id in self.cancelled_tasks else "queued"
                await self._finish(task_id, status)
        self.active_tasks.clear()
        self.task_cookies.clear()
        logger.info("Queue manager loops and active execution stopped.")

    def is_cancelled(self, task_id: str) -> bool:
        return self.stopping or task_id in self.cancelled_tasks

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
        config = await self.settings.get_all()
        try:
            directory, _ = self.runtime.validate_output(
                download_dir if download_dir is not None else config["download_dir"],
                output_template if output_template is not None else config["filename_template"],
            )
        except OutputPathError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if download_dir is not None:
            download_dir = str(directory)
        task_id = str(uuid4())
        title = sanitize_text(title)
        created_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
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

        async with aiosqlite.connect(self.runtime.db_path) as db:
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
        await self.connections.broadcast({
            "event": "task_scheduled" if scheduled_at else "task_queued",
            "data": self.in_memory_progress[task_id]
        })

        # Wake up worker if queued
        if not scheduled_at:
            self.wakeup_event.set()
        return task_id

    async def cancel(self, task_id: str):
        operation = asyncio.create_task(self._cancel(task_id))
        await join_owned_task(operation)

    async def _cancel(self, task_id: str):
        async with self.cancel_locks.setdefault(task_id, asyncio.Lock()):
            async with self._state_lock(task_id):
                async with aiosqlite.connect(self.runtime.db_path) as db:
                    cursor = await db.execute(
                        "UPDATE downloads SET status='cancelling' WHERE id=? AND status NOT IN ('complete', 'failed', 'cancelled')",
                        (task_id,),
                    )
                    await db.commit()
                    if not cursor.rowcount:
                        return
                self.cancelled_tasks.add(task_id)
                if task_id in self.in_memory_progress:
                    self.in_memory_progress[task_id]["status"] = "cancelling"
            task = self.active_tasks.get(task_id)
            if task and not task.done():
                task.cancel()
                try:
                    await join_owned_task(task)
                except asyncio.CancelledError:
                    if not task.done():
                        raise
            await self._finish(task_id, "cancelled")
            self.task_cookies.pop(task_id, None)
            self.cancelled_tasks.discard(task_id)

    async def _finish(self, task_id, status, **fields):
        fields.update(status=status, speed=0.0, eta=None)
        if status in TERMINAL:
            fields["completed_at"] = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        else:
            fields["completed_at"] = None
        assignments = ", ".join(f"{key}=?" for key in fields)
        expected = tuple(FINISH_FROM[status])
        placeholders = ", ".join("?" for _ in expected)
        message = None
        async with self._state_lock(task_id):
            async with aiosqlite.connect(self.runtime.db_path) as db:
                cursor = await db.execute(
                    f"UPDATE downloads SET {assignments} WHERE id=? AND status IN ({placeholders})",
                    (*fields.values(), task_id, *expected),
                )
                changed = cursor.rowcount
                await db.commit()
            if changed and task_id in self.in_memory_progress:
                self.in_memory_progress[task_id].update(fields)
                message = {"event": f"task_{status}", "data": dict(self.in_memory_progress[task_id])}
        if message:
            await self.connections.broadcast(message)
        return bool(changed)

    def handle_progress(self, task_id: str, payload: Dict[str, Any]):
        """Callback from downloader. Updates in-memory state and broadcasts."""
        if task_id not in self.in_memory_progress or self.stopping or not accepts_progress(self.in_memory_progress[task_id]["status"]):
            return

        self.in_memory_progress[task_id].update(payload)

        # Schedule the broadcast on the running event loop safely from worker thread
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.connections.broadcast({
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
                now_str = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

                async with aiosqlite.connect(self.runtime.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    # Fetch scheduled downloads that are due
                    async with db.execute(
                        "SELECT * FROM downloads WHERE status = ? AND scheduled_at <= ? ORDER BY scheduled_at ASC",
                        ("scheduled", now_str)
                    ) as cursor:
                        rows = await cursor.fetchall()

                for row in rows:
                    task_id = row["id"]
                    async with self._state_lock(task_id):
                        async with aiosqlite.connect(self.runtime.db_path) as db:
                            cursor = await db.execute(
                                "UPDATE downloads SET status='queued', scheduled_at=NULL WHERE id=? AND status='scheduled'",
                                (task_id,),
                            )
                            await db.commit()
                            if not cursor.rowcount:
                                continue
                        if task_id not in self.in_memory_progress:
                            self.in_memory_progress[task_id] = dict(row)
                        self.in_memory_progress[task_id].update(status="queued", scheduled_at=None)
                        message = {"event": "task_queued", "data": dict(self.in_memory_progress[task_id])}
                    self.wakeup_event.set()
                    await self.connections.broadcast(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")

    async def _worker_loop(self):
        # Reset any stuck tasks from previous crash
        async with aiosqlite.connect(self.runtime.db_path) as db:
            await db.execute(
                "UPDATE downloads SET status = ? WHERE status IN (?, ?, ?)",
                ("queued", "downloading", "probing", "postprocessing")
            )
            await db.execute(
                "UPDATE downloads SET status='cancelled', completed_at=? WHERE status='cancelling'",
                (datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),),
            )
            await db.commit()

        while True:
            try:
                config = await self.settings.get_all()
                max_concurrent = config.get("max_concurrent", 3)

                # Clean up finished tasks from active list
                self.active_tasks = {tid: t for tid, t in self.active_tasks.items() if not t.done()}

                if len(self.active_tasks) >= max_concurrent:
                    # Wait for at least one active task to finish
                    await asyncio.wait(self.active_tasks.values(), return_when=asyncio.FIRST_COMPLETED)
                    continue

                # Preserve an enqueue notification arriving during the database read.
                self.wakeup_event.clear()

                # Fetch next queued item
                async with aiosqlite.connect(self.runtime.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        "SELECT * FROM downloads WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                        ("queued",)
                    ) as cursor:
                        row = await cursor.fetchone()

                if not row:
                    # No queued items, wait for wakeup signal
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

                # Atomically claim the task to prevent race conditions
                async with self._state_lock(task_id):
                    async with aiosqlite.connect(self.runtime.db_path) as db2:
                        cursor = await db2.execute(
                            "UPDATE downloads SET status = ? WHERE id = ? AND status = ?",
                            ("downloading", task_id, "queued")
                        )
                        await db2.commit()
                        if not cursor.rowcount:
                            continue
                    if task_id not in self.in_memory_progress:
                        self.in_memory_progress[task_id] = dict(row)
                    self.in_memory_progress[task_id]["status"] = "downloading"
                    message = {"event": "task_started", "data": dict(self.in_memory_progress[task_id])}

                await self.connections.broadcast(message)

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
        cookies_file_path = None
        fields = {}
        status = "failed"
        try:
            # A cancellation may finish while the claim's broadcast is awaiting.
            async with aiosqlite.connect(self.runtime.db_path) as db:
                async with db.execute("SELECT status FROM downloads WHERE id=?", (task_id,)) as cursor:
                    row = await cursor.fetchone()
            if row is None or row[0] not in RUNNABLE or self.is_cancelled(task_id):
                return
            directory, _ = self.runtime.validate_output(download_dir, filename_template)
            download_dir = str(directory)
            cookies = self.task_cookies.pop(task_id, None)
            if cookies:
                cookies_file_path = write_cookies_to_netscape_format(cookies, self.runtime.temp_dir)
            result = await self.download_executor.run(
                runtime=self.runtime, task_id=task_id, url=url, format_id=format_id,
                download_dir=download_dir, filename_template=filename_template,
                progress_callback=self.handle_progress,
                speed_limit=config.get("speed_limit", 0), cookies_file_path=cookies_file_path,
                is_cancelled_cb=lambda: self.is_cancelled(task_id),
                audio_only=options.get("audio_only", False), audio_format=options.get("audio_format", "mp3"),
                download_subtitles=options.get("download_subtitles", False), subtitle_langs=options.get("subtitle_langs"),
                subtitle_format=options.get("subtitle_format", "srt"), embed_subtitles=options.get("embed_subtitles", False),
                embed_thumbnail=options.get("embed_thumbnail", False), playlist_items=options.get("playlist_items"),
                is_playlist=options.get("is_playlist", False), sponsorblock_enabled=config.get("sponsorblock_enabled", False),
                sponsorblock_categories=config.get("sponsorblock_categories", []), sponsorblock_mode=config.get("sponsorblock_mode", "remove"),
                postprocess_codec=config.get("postprocess_codec", "none"), postprocess_normalize_audio=config.get("postprocess_normalize_audio", False),
                custom_args_str=config.get("custom_ytdlp_args", ""), download_threads=config.get("download_threads", 4),
                gpu_accelerated_conversion=config.get("gpu_accelerated_conversion", False), extra_args=options.get("extra_args"),
                user_agent=options.get("user_agent"),
            )
            if self.is_cancelled(task_id):
                raise asyncio.CancelledError
            filepath = result.get("filepath")
            # Preserve the legacy fallback until P03's authoritative artifact records replace it.
            if filepath and not os.path.exists(filepath):
                base, _ = os.path.splitext(filepath)
                filepath = next((base + ext for ext in ('.mp4', '.mkv', '.webm', '.m4a', '.mp3') if os.path.exists(base + ext)), None)
            if not filepath or not os.path.isfile(filepath):
                raise RuntimeError("Final file was not created. Download failed or was aborted by yt-dlp.")
            status = "complete"
            fields = {"file_path": filepath, "progress": 100.0, "error_message": None}
            current = self.in_memory_progress.get(task_id, {})
            if not current.get("title") and result.get("title"):
                fields["title"] = sanitize_text(result["title"])
            if not current.get("thumbnail_url") and result.get("thumbnail_url"):
                fields["thumbnail_url"] = result["thumbnail_url"]
        except asyncio.CancelledError:
            status = "cancelled" if task_id in self.cancelled_tasks else "queued"
            fields = {"error_message": None}
        except Exception as error:
            error_message = sanitize_text(str(error))
            if task_id in self.cancelled_tasks:
                status = "cancelled"
            elif self.stopping:
                status = "queued"
            elif isinstance(error, OutputPathError) or retry_count >= config.get("max_retries", 3):
                status = "failed"
                logger.error("Task %s failed: %s", task_id, error_message)
            else:
                status = "queued"
                retry_count += 1
                logger.warning("Task %s failed, retrying (%s/%s): %s", task_id, retry_count, config.get("max_retries", 3), error_message)
            fields = {"error_message": error_message, "retry_count": retry_count}
        finally:
            # The executor has joined the worker tree before returning or raising cancellation.
            if cookies_file_path:
                try:
                    os.unlink(cookies_file_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.exception("Failed to remove the owned cookie file for task %s", task_id)
        changed = await self._finish(task_id, status, **fields)
        if changed and status == "queued" and not self.stopping:
            self.wakeup_event.set()


def open_folder(parent_dir: str):
    """Platform process boundary; desktop integration can replace this adapter."""
    if sys.platform == "win32":
        os.startfile(parent_dir)
    elif sys.platform == "darwin":
        subprocess.run(["open", parent_dir], check=True)
    else:
        subprocess.run(["xdg-open", parent_dir], check=True)


def create_download_router(queue_manager: QueueManager) -> APIRouter:
    router = APIRouter(prefix="/downloads", tags=["downloads"])
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
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM downloads WHERE status IN (?, ?, ?) ORDER BY completed_at DESC",
                ("complete", "failed", "cancelled")
            ) as cursor:
                rows = await cursor.fetchall()
                return [DownloadTask(**dict(row)) for row in rows]

    @router.delete("/history", response_model=Dict[str, str], tags=["history"])
    async def clear_all_history():
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
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
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
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
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
            async with db.execute("SELECT 1 FROM downloads WHERE id = ?", (task_id,)) as cursor:
                if not await cursor.fetchone():
                    raise HTTPException(status_code=404, detail="Download task not found")

        await queue_manager.cancel(task_id)
        return {"message": f"Download task {task_id} cancelled"}
    @router.get("", response_model=List[DownloadTask])
    async def list_downloads(status: Optional[str] = None):
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
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
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
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
            "created_at": datetime.fromtimestamp(queue_manager.clock(), timezone.utc).isoformat(),
            "download_options": options_json
        }

        # Broadcast task queued event
        await queue_manager.connections.broadcast({
            "event": "task_queued",
            "data": queue_manager.in_memory_progress[task_id]
        })

        # Wake up worker
        queue_manager.wakeup_event.set()
        return {"message": f"Download task {task_id} re-enqueued for retry"}

    @router.post("/{task_id}/open-folder", response_model=Dict[str, Any])
    async def open_task_folder(task_id: str):
        async with aiosqlite.connect(queue_manager.runtime.db_path) as db:
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
                    queue_manager.open_folder(parent_dir)
                    return {"success": True, "message": f"Opened folder: {parent_dir}"}
                except Exception as e:
                    logger.error(f"Failed to open folder {parent_dir}: {e}")
                    raise HTTPException(status_code=500, detail=f"Failed to open containing folder: {str(e)}")

    return router
