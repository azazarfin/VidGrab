import asyncio
import logging
import time
import json
import os
import sys
import subprocess
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4
from typing import Dict, Any, List, Optional
from fastapi import WebSocket, APIRouter, HTTPException, Request
from pydantic import BaseModel
import aiosqlite

from backend.config import SettingsStore
from backend.runtime import OutputPathError, RuntimeConfig
from backend.downloader import run_download
from backend.cookie_manager import create_cookie_file
from backend.sanitizer import sanitize_text
from backend.engine.execution import ProcessDownloadExecutor, ThreadDownloadExecutor, join_owned_task
from backend.db.jobs import AttemptStore
from backend.db.enqueue import EnqueueStore
from backend.db.control_actions import ControlActionStore
from backend.db.playlists import PlaylistStore, sync_parent
from backend.services.playlists import PlaylistError, parse_selection, validate_playlist_result
from backend.services.artifacts import completed_artifacts, prepare_workspace
from backend.services.publication import reconcile_publication
from backend.services.job_state import RUNNABLE, TERMINAL, accepts_progress
from backend.services.resume import FreshAuthRequired
from backend.services.auth_context import validate_fresh_cookies
from backend.services.retry_policy import decide
from backend.services.control_errors import ControlError
from backend.db.ownership import require_owner

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
    attempt_count: int = 0
    current_attempt_id: Optional[str] = None
    auth_required: int = 0
    job_kind: str = "media"
    parent_id: Optional[str] = None
    playlist_index: Optional[int] = None
    retry_at: Optional[float] = None
    error_code: Optional[str] = None
    request_count: int = 0

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
                 download_runner=run_download, clock=time.time, open_folder=None, download_executor=None, *, owner):
        self.runtime = runtime
        self.settings = settings
        self.connections = connections
        self.download_runner = download_runner
        self.download_executor = download_executor or (
            ProcessDownloadExecutor() if download_runner is run_download else ThreadDownloadExecutor(download_runner)
        )
        self.clock = clock
        self.owner = owner
        self.open_folder = open_folder
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.attempts = AttemptStore(runtime, owner)
        self.enqueues = EnqueueStore(runtime, owner)
        self.control_actions = ControlActionStore()
        self.active_attempts = {}
        self.cancelled_tasks: set = set()
        self.in_memory_progress: Dict[str, Dict[str, Any]] = {}
        self.worker_task: Optional[asyncio.Task] = None
        self.scheduler_task: Optional[asyncio.Task] = None
        self.stop_task: Optional[asyncio.Task] = None
        self.wakeup_event = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task_cookies: Dict[str, List[Dict[str, Any]]] = {}
        self.task_headers = {}
        self.allow_legacy_options = True
        self.cancel_locks: Dict[str, asyncio.Lock] = {}
        self.state_locks: Dict[str, asyncio.Lock] = {}
        self.stopping = False

    def _state_lock(self, task_id):
        return self.state_locks.setdefault(task_id, asyncio.Lock())

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    async def _claim(self, task_id, row=None):
        async with self._state_lock(task_id):
            async with aiosqlite.connect(self.runtime.db_path) as db:
                async with db.execute("SELECT auth_required, status FROM downloads WHERE id=?", (task_id,)) as cursor:
                    auth = await cursor.fetchone()
            if auth and auth[0] and auth[1] == "queued" and not self.task_cookies.get(task_id):
                await self.attempts.finish(task_id, "waiting_for_auth", self._now(),
                                           error_message="Fresh browser authentication is required")
                if task_id in self.in_memory_progress:
                    self.in_memory_progress[task_id].update(status="waiting_for_auth", error_message="Fresh browser authentication is required")
                return None
            attempt = await self.attempts.claim(task_id, self._now())
            if attempt is None:
                async with aiosqlite.connect(self.runtime.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT * FROM downloads WHERE id=?", (task_id,)) as cursor:
                        current = await cursor.fetchone()
                if current is not None:
                    self.in_memory_progress[task_id] = dict(current)
                    if current["error_code"] == "ATTEMPT_BUDGET_EXHAUSTED":
                        await self.connections.broadcast({"event": "task_failed", "data": dict(current)})
                        await self._refresh_playlist(task_id)
                return None
            self.active_attempts[task_id] = attempt
            if task_id not in self.in_memory_progress and row is not None:
                self.in_memory_progress[task_id] = dict(row)
            if task_id in self.in_memory_progress:
                self.in_memory_progress[task_id].update(
                    status="downloading", attempt_count=attempt.number, current_attempt_id=attempt.id,
                    progress=0.0, speed=0.0, eta=None, file_path=None, error_message=None, completed_at=None,
                    error_code=None, retry_at=None)
            return attempt

    async def _recover(self):
        for row in await self.attempts.recover(self._now()):
            self.in_memory_progress[row["id"]] = row
        self.active_attempts.clear()

    async def initialize(self):
        self.stopping = False
        self.stop_task = None
        await self._recover()

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
                attempt = self.active_attempts.get(task_id)
                if attempt is not None:
                    await self._finish(task_id, status, attempt_id=attempt.id)
        # Include claims whose started broadcast was interrupted before task creation.
        await self._recover()
        self.active_tasks.clear()
        self.task_cookies.clear()
        self.task_headers.clear()
        logger.info("Queue manager loops and active execution stopped.")

    def is_cancelled(self, task_id: str) -> bool:
        return self.stopping or not self.owner.healthy or task_id in self.cancelled_tasks

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
        user_agent: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_intent: Optional[dict] = None,
        credential_loader=None,
        requires_auth=False,
        fixture_only=False,
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
        if fixture_only:
            options["fixture_only"] = True
        options_json = json.dumps(options)
        if is_playlist or playlist_items:
            try:
                parse_selection(playlist_items)
            except PlaylistError as error:
                raise HTTPException(400, str(error)) from error

        record = {
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
            "scheduled_at": scheduled_at,
            "auth_required": int(bool(cookies) or requires_auth),
            "job_kind": "playlist" if is_playlist or playlist_items else "media",
        }
        operation = asyncio.create_task(self._admit(record, cookies, idempotency_key, idempotency_intent, credential_loader))
        return await join_owned_task(operation)

    async def _admit(self, record, cookies, idempotency_key, idempotency_intent=None, credential_loader=None):
        if self.stopping:
            raise HTTPException(503, "Engine is stopping")
        auth = None
        def prepare_credentials():
            nonlocal auth
            auth = credential_loader()
        if credential_loader is not None:
            record, created = await self.enqueues.admit(record, idempotency_key, intent=idempotency_intent,
                                                       before_insert=prepare_credentials)
        elif idempotency_intent is None:
            record, created = await self.enqueues.admit(record, idempotency_key)
        else:
            record, created = await self.enqueues.admit(record, idempotency_key, intent=idempotency_intent)
        task_id = record["id"]
        if not created:
            return task_id
        if auth is not None:
            cookies = auth["cookies"]
            self.task_headers[task_id] = {name: auth[name] for name in ("user_agent", "referer") if auth.get(name)}
            self.task_headers[task_id]["credential_url"] = auth["scope_url"]
        if cookies:
            self.task_cookies[task_id] = cookies
        self.in_memory_progress[task_id] = record

        # Broadcast event
        await self.connections.broadcast({
            "event": "task_scheduled" if record["scheduled_at"] else "task_queued",
            "data": self.in_memory_progress[task_id]
        })

        # Wake up worker if queued
        if not record["scheduled_at"]:
            self.wakeup_event.set()
        return task_id

    async def cancel(self, task_id: str, *, control_action=None):
        operation = asyncio.create_task(self._cancel(task_id, control_action=control_action))
        return await join_owned_task(operation)

    async def _cancel(self, task_id: str, *, control_action=None):
        accepted = None
        async with self.cancel_locks.setdefault(task_id, asyncio.Lock()):
            async with self._state_lock(task_id):
                async with self.attempts.transaction() as db:
                    await require_owner(db, self.owner)
                    if control_action is not None:
                        replay = await self.control_actions.replay(db, control_action)
                        if replay is not None:
                            return replay
                        async with db.execute("SELECT * FROM downloads WHERE id=?", (task_id,)) as cursor:
                            current = await cursor.fetchone()
                        await self.control_actions.validate(db, control_action, current)
                        if current["status"] == "cancelled":
                            return await self.control_actions.record(db, control_action, task_id)
                        if current["status"] in ("complete", "failed"):
                            raise ControlError("STATE_CONFLICT", 409, "This download can no longer be cancelled")
                    cursor = await db.execute(
                        "UPDATE downloads SET status='cancelling' WHERE id=? AND status NOT IN ('complete', 'failed', 'cancelled')",
                        (task_id,),
                    )
                    if not cursor.rowcount:
                        return
                    async with db.execute("SELECT current_attempt_id FROM downloads WHERE id=?", (task_id,)) as cursor:
                        attempt_id = (await cursor.fetchone())[0]
                    await db.execute("""UPDATE downloads SET status='cancelling' WHERE parent_id=?
                        AND status NOT IN ('complete', 'failed', 'cancelled')""", (task_id,))
                    async with db.execute("SELECT id FROM downloads WHERE parent_id=? AND status='cancelling'", (task_id,)) as cursor:
                        children = [row[0] for row in await cursor.fetchall()]
                    if control_action is not None:
                        accepted = await self.control_actions.record(db, control_action, task_id)
                self.cancelled_tasks.add(task_id)
                self.cancelled_tasks.update(children)
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
            if children:
                await asyncio.gather(*(self._cancel(child) for child in children))
            await self._finish(task_id, "cancelled", attempt_id=attempt_id)
            await self._refresh_playlist(task_id)
            self.task_cookies.pop(task_id, None)
            self.task_headers.pop(task_id, None)
            self.cancelled_tasks.discard(task_id)
            return accepted

    async def retry(self, task_id, cookies=None, user_agent=None, *, expected_status=None, control_action=None, credential_loader=None):
        operation = asyncio.create_task(self._retry(task_id, cookies, user_agent, expected_status=expected_status,
                                                    control_action=control_action, credential_loader=credential_loader))
        return await join_owned_task(operation)

    async def _retry(self, task_id, cookies=None, user_agent=None, *, expected_status=None, control_action=None, credential_loader=None):
        messages = []
        accepted = None
        headers = None
        async with self._state_lock(task_id):
            async with self.attempts.transaction() as db:
                await require_owner(db, self.owner)
                if control_action is not None:
                    replay = await self.control_actions.replay(db, control_action)
                    if replay is not None:
                        return replay
                async with db.execute("SELECT * FROM downloads WHERE id=?", (task_id,)) as cursor:
                    row = await cursor.fetchone()
                if control_action is not None:
                    await self.control_actions.validate(db, control_action, row)
                if row is None:
                    raise HTTPException(status_code=404, detail="Download task not found")
                if expected_status is not None and row["status"] != expected_status:
                    if control_action is not None:
                        raise ControlError("STATE_CONFLICT", 409, "This download no longer waits for browser authentication")
                    raise HTTPException(status_code=400, detail="Download no longer waits for browser authentication")
                if row["status"] not in ("failed", "cancelled", "waiting_for_auth"):
                    if control_action is not None:
                        raise ControlError("STATE_CONFLICT", 409, "This download is still active or waiting for its retry deadline")
                    raise HTTPException(status_code=400, detail="Only failed, cancelled or authentication-waiting downloads can be retried")
                if credential_loader is not None:
                    context = credential_loader(row["url"], row["id"])
                    cookies = context["cookies"]
                    headers = {name: context[name] for name in ("user_agent", "referer") if context.get(name)}
                    headers["credential_url"] = context["scope_url"]
                if row["status"] == "waiting_for_auth" or row["auth_required"]:
                    try:
                        validate_fresh_cookies(row["url"], cookies, self.clock())
                    except ValueError as error:
                        if control_action is not None:
                            raise ControlError("AUTH_REQUIRED", 401, "Share fresh browser cookies before retrying this download") from error
                        raise HTTPException(status_code=400, detail=str(error)) from error
                options = json.loads(row["download_options"] or "{}")
                async with db.execute("SELECT * FROM downloads WHERE parent_id=? ORDER BY playlist_index", (task_id,)) as cursor:
                    children = [dict(child) for child in await cursor.fetchall()]
                if children:
                    selected, current = await self._retry_children(db, dict(row), children, cookies, user_agent)
                else:
                    if user_agent:
                        options["user_agent"] = user_agent
                    options_json = json.dumps(options)
                    await db.execute("""UPDATE downloads SET status='queued', error_message=NULL,
                        error_code=NULL, retry_at=NULL, progress=0, speed=0, eta=NULL, completed_at=NULL, download_options=?,
                        auth_required=? WHERE id=?""", (options_json, int(bool(cookies) or row["auth_required"]), task_id))
                    if row["parent_id"] is not None:
                        await sync_parent(db, row["parent_id"], self._now())
                    current = dict(row)
                    current.update(status="queued", error_message=None, progress=0.0, speed=0.0,
                                   eta=None, completed_at=None, error_code=None, retry_at=None, download_options=options_json,
                                   auth_required=int(bool(cookies) or row["auth_required"]))
                if control_action is not None:
                    accepted = await self.control_actions.record(db, control_action, task_id)
                    async with db.execute("SELECT * FROM downloads WHERE id=?", (task_id,)) as cursor:
                        current = dict(await cursor.fetchone())
            if children:
                for child, scoped in selected:
                    if scoped:
                        self.task_cookies[child["id"]] = cookies
                        if headers is not None:
                            self.task_headers[child["id"]] = dict(headers)
                        else:
                            self.task_headers.pop(child["id"], None)
                    else:
                        self.task_cookies.pop(child["id"], None)
                        self.task_headers.pop(child["id"], None)
                    self.cancelled_tasks.discard(child["id"])
                    self.in_memory_progress[child["id"]] = child
                    messages.append({"event": "task_" + child["status"], "data": child})
            elif cookies:
                self.task_cookies[task_id] = cookies
                if headers is not None:
                    self.task_headers[task_id] = dict(headers)
                else:
                    self.task_headers.pop(task_id, None)
            else:
                self.task_cookies.pop(task_id, None)
                self.task_headers.pop(task_id, None)
            self.cancelled_tasks.discard(task_id)
            self.in_memory_progress[task_id] = current
        self.wakeup_event.set()
        for message in messages:
            await self.connections.broadcast(message)
        await self.connections.broadcast({"event": "task_" + current["status"], "data": current})
        await self._refresh_playlist(task_id)
        return accepted

    async def _retry_children(self, db, parent, children, cookies, user_agent):
        if any(child["status"] not in TERMINAL and child["status"] != "waiting_for_auth" for child in children):
            raise HTTPException(400, "Playlist children are still active")
        selected = []
        for child in children:
            if child["status"] == "complete":
                continue
            scoped = False
            if cookies:
                try:
                    validate_fresh_cookies(child["url"], cookies, self.clock())
                    scoped = True
                except ValueError:
                    pass
            status = "waiting_for_auth" if child["auth_required"] and not scoped else "queued"
            options = json.loads(child["download_options"] or "{}")
            if user_agent:
                options["user_agent"] = user_agent
            child.update(status=status, progress=0.0, speed=0.0, eta=None, completed_at=None,
                         error_code=None, retry_at=None,
                         error_message="Fresh browser authentication is required" if status == "waiting_for_auth" else None,
                         download_options=json.dumps(options), auth_required=int(scoped or child["auth_required"]))
            await db.execute("""UPDATE downloads SET status=?, progress=0, speed=0, eta=NULL, completed_at=NULL,
                error_message=?, download_options=?, auth_required=?, error_code=NULL, retry_at=NULL WHERE id=?""",
                (status, child["error_message"], child["download_options"], child["auth_required"], child["id"]))
            selected.append((child, scoped))
        await sync_parent(db, parent["id"], self._now())
        async with db.execute("SELECT * FROM downloads WHERE id=?", (parent["id"],)) as cursor:
            current = dict(await cursor.fetchone())
        return selected, current

    async def _refresh_playlist(self, task_id):
        async with aiosqlite.connect(self.runtime.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, parent_id, job_kind FROM downloads WHERE id=?", (task_id,)) as cursor:
                job = await cursor.fetchone()
            if job is None:
                return
            parent_id = job["parent_id"] or (job["id"] if job["job_kind"] == "playlist" else None)
            if parent_id is None:
                return
            async with db.execute("SELECT * FROM downloads WHERE id=?", (parent_id,)) as cursor:
                parent = dict(await cursor.fetchone())
        self.in_memory_progress[parent_id] = parent
        await self.connections.broadcast({"event": "task_" + parent["status"], "data": parent})

    async def delete_history(self, task_id=None):
        async def remove():
            removed = await PlaylistStore(self.runtime, self.owner).delete_history(task_id)
            for job_id in removed:
                self.in_memory_progress.pop(job_id, None)
                self.task_cookies.pop(job_id, None)
                self.task_headers.pop(job_id, None)
            return removed
        return await join_owned_task(asyncio.create_task(remove()))

    async def _finish(self, task_id, status, *, attempt_id=None, artifacts=(), outcome=None, **fields):
        now = self._now()
        message = None
        async with self._state_lock(task_id):
            changed = await self.attempts.finish(task_id, status, now, attempt_id=attempt_id,
                                                 artifacts=artifacts, outcome=outcome, **fields)
            fields.update(status=status, speed=0.0, eta=None, current_attempt_id=None,
                          completed_at=now if status in TERMINAL else None)
            if status != "retry_wait":
                fields["retry_at"] = None
            if status in ("complete", "cancelled", "queued"):
                fields["error_code"] = None
            if changed:
                async with aiosqlite.connect(self.runtime.db_path) as db:
                    async with db.execute("SELECT request_count FROM downloads WHERE id=?", (task_id,)) as cursor:
                        fields["request_count"] = (await cursor.fetchone())[0]
            attempt = self.active_attempts.get(task_id)
            if changed and attempt is not None and attempt.id == attempt_id:
                self.active_attempts.pop(task_id, None)
            if changed and task_id in self.in_memory_progress:
                self.in_memory_progress[task_id].update(fields)
                message = {"event": f"task_{status}", "data": dict(self.in_memory_progress[task_id])}
        if message:
            await self.connections.broadcast(message)
        if changed:
            await self._refresh_playlist(task_id)
        return bool(changed)

    def handle_progress(self, task_id: str, payload: Dict[str, Any], *, attempt_id=None):
        """Callback from downloader. Updates in-memory state and broadcasts."""
        attempt = self.active_attempts.get(task_id)
        if attempt_id is not None and (attempt is None or attempt.id != attempt_id):
            return
        if task_id not in self.in_memory_progress or self.stopping or not self.owner.healthy or not accepts_progress(self.in_memory_progress[task_id]["status"]):
            return

        self.in_memory_progress[task_id].update(payload)

        # Schedule the broadcast on the running event loop safely from worker thread
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.connections.broadcast({
                    "event": "task_progress",
                    "data": dict(self.in_memory_progress[task_id])
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
        while True:
            try:
                config = await self.settings.get_all()
                for due in await self.attempts.promote_due_retries(self.clock()):
                    self.in_memory_progress[due["id"]] = due
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
                    async with aiosqlite.connect(self.runtime.db_path) as db:
                        async with db.execute("SELECT MIN(retry_at) FROM downloads WHERE status='retry_wait'") as cursor:
                            deadline = (await cursor.fetchone())[0]
                    if deadline is None:
                        await self.wakeup_event.wait()
                    else:
                        try:
                            await asyncio.wait_for(self.wakeup_event.wait(), max(0.01, deadline - self.clock()))
                        except TimeoutError:
                            pass
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

                attempt = await self._claim(task_id, row)
                if attempt is None:
                    continue
                message = {"event": "task_started", "data": dict(self.in_memory_progress[task_id])}

                await self.connections.broadcast(message)

                # Start the download task
                task = asyncio.create_task(self._run_task(task_id, url, format_id, config, options,
                                                          dict(row).get("retry_count", 0), attempt=attempt))
                self.active_tasks[task_id] = task

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in queue worker loop: {e}")
                await asyncio.sleep(5)

    async def _run_task(self, task_id: str, url: str, format_id: str, config: Dict[str, Any], options: Dict[str, Any], retry_count: int = 0, *, attempt=None):
        attempt = attempt or await self._claim(task_id)
        if attempt is None:
            return
        download_dir = options.get("download_dir") or config.get("download_dir")
        filename_template = options.get("output_template") or config.get("filename_template")
        cookies_file_path = None
        fields = {}
        status = "failed"
        outcome = "failed"
        artifacts = []
        workspace_ready = False
        cookie_error = None
        previous_retry_count = retry_count
        expansion = None
        cookies = None
        try:
            # A cancellation may finish while the claim's broadcast is awaiting.
            async with aiosqlite.connect(self.runtime.db_path) as db:
                async with db.execute("SELECT status, current_attempt_id, job_kind FROM downloads WHERE id=?", (task_id,)) as cursor:
                    row = await cursor.fetchone()
            if row is None or row[0] not in RUNNABLE or row[1] != attempt.id or self.is_cancelled(task_id):
                return
            directory, _ = self.runtime.validate_output(download_dir, filename_template)
            download_dir = str(directory)
            prepare_workspace(self.runtime, attempt)
            workspace_ready = True
            cookies = self.task_cookies.pop(task_id, None)
            headers = self.task_headers.pop(task_id, {})
            if cookies:
                creation = asyncio.create_task(asyncio.to_thread(create_cookie_file, cookies, self.runtime, attempt))
                cookies_file_path = await join_owned_task(creation)
            result = await self.download_executor.run(
                runtime=self.runtime, task_id=task_id, url=url, format_id=format_id,
                ownership={"attempt_id": attempt.id, "owner_id": attempt.owner_id},
                download_dir=download_dir, filename_template=filename_template,
                progress_callback=lambda tid, payload: self.handle_progress(tid, payload, attempt_id=attempt.id),
                attempt_dir=str(attempt.workspace),
                resume_attempt={"id": attempt.id, "job_id": task_id, "number": attempt.number, "owner_id": attempt.owner_id},
                speed_limit=config.get("speed_limit", 0), cookies_file_path=cookies_file_path,
                is_cancelled_cb=lambda: self.is_cancelled(task_id),
                audio_only=options.get("audio_only", False), audio_format=options.get("audio_format", "mp3"),
                download_subtitles=options.get("download_subtitles", False), subtitle_langs=options.get("subtitle_langs"),
                subtitle_format=options.get("subtitle_format", "srt"), embed_subtitles=options.get("embed_subtitles", False),
                embed_thumbnail=options.get("embed_thumbnail", False), playlist_items=options.get("playlist_items"),
                is_playlist=row[2] == "playlist", sponsorblock_enabled=config.get("sponsorblock_enabled", False),
                sponsorblock_categories=config.get("sponsorblock_categories", []), sponsorblock_mode=config.get("sponsorblock_mode", "remove"),
                postprocess_codec=config.get("postprocess_codec", "none"), postprocess_normalize_audio=config.get("postprocess_normalize_audio", False),
                custom_args_str=config.get("custom_ytdlp_args", "") if self.allow_legacy_options else "",
                download_threads=config.get("download_threads", 4),
                gpu_accelerated_conversion=config.get("gpu_accelerated_conversion", False),
                extra_args=options.get("extra_args") if self.allow_legacy_options else None,
                user_agent=headers.get("user_agent", options.get("user_agent")), referer=headers.get("referer"),
                credential_url=headers.get("credential_url"),
                fixture_only=options.get("fixture_only", False),
            )
            if self.is_cancelled(task_id):
                raise asyncio.CancelledError
            if row[2] == "playlist":
                expansion = validate_playlist_result(result)
                filepath = None
            else:
                if "playlist" in result:
                    raise PlaylistError("A media child cannot expand another playlist")
                filepath = result.get("filepath")
                artifacts = completed_artifacts(self.runtime, result)
            status = "complete"
            outcome = "complete"
            fields = {"file_path": filepath, "progress": 100.0, "error_message": None}
            current = self.in_memory_progress.get(task_id, {})
            if not current.get("title") and result.get("title"):
                fields["title"] = sanitize_text(result["title"])
            if not current.get("thumbnail_url") and result.get("thumbnail_url"):
                fields["thumbnail_url"] = result["thumbnail_url"]
        except asyncio.CancelledError:
            status = "cancelled" if task_id in self.cancelled_tasks else "queued"
            outcome = "cancelled" if status == "cancelled" else "interrupted"
            fields = {"error_message": None}
        except FreshAuthRequired:
            status = "cancelled" if task_id in self.cancelled_tasks else "waiting_for_auth"
            outcome = "cancelled" if status == "cancelled" else "failed"
            fields = {"error_message": "Fresh browser authentication is required", "auth_required": 1,
                      "retry_count": previous_retry_count}
        except Exception as error:
            error_message = sanitize_text(str(error))
            if task_id in self.cancelled_tasks:
                status = "cancelled"
                outcome = "cancelled"
            elif self.stopping:
                status = "queued"
                outcome = "interrupted"
            else:
                decision = decide(error, retry_count, config.get("max_retries", 3), self.clock())
                status = decision.status
                retry_count = decision.retry_count
                fields.update(retry_at=decision.retry_at, error_code=decision.code)
                if status == "waiting_for_auth":
                    fields["auth_required"] = 1
                logger.log(logging.ERROR if status == "failed" else logging.WARNING,
                           "Task %s execution failed (%s): %s", task_id, decision.code, error_message)
            fields.update(error_message=error_message, retry_count=retry_count)
        finally:
            # The executor has joined the worker tree before returning or raising cancellation.
            cleanup = asyncio.create_task(self.attempts.cleanup_cookies(attempt))
            try:
                await join_owned_task(cleanup)
            except asyncio.CancelledError:
                # Cleanup has exited; a rolled-back record can be recovered again.
                if not cleanup.cancelled():
                    cookie_error = cleanup.exception()
            except (OSError, ValueError) as error:
                cookie_error = error
            if cookie_error is not None:
                if not isinstance(cookie_error, (OSError, ValueError)):
                    raise cookie_error
                logger.error("Retained unverifiable cookie ownership for task %s: %s", task_id, cookie_error)
                status = "cancelled" if task_id in self.cancelled_tasks else "failed"
                outcome = status
                fields = {"error_message": "Cookie ownership could not be verified; files retained",
                          "file_path": None, "retry_count": previous_retry_count}
            if workspace_ready:
                try:
                    reconciliation = asyncio.create_task(asyncio.to_thread(reconcile_publication, self.runtime, attempt))
                    try:
                        recovery = await join_owned_task(reconciliation)
                    except asyncio.CancelledError:
                        # Repeated cancellation cannot detach filesystem verification.
                        recovery = reconciliation.result()
                    recovered = {record["path"]: record for record in recovery.artifacts}
                    if not recovery.error:
                        recovered.update({record["path"]: record for record in artifacts})
                    artifacts = list(recovered.values())
                    if task_id in self.cancelled_tasks:
                        status = outcome = "cancelled"
                        fields = {"error_message": None, "retry_count": previous_retry_count}
                    elif cookie_error is not None:
                        status = outcome = "failed"
                    elif recovery.error:
                        status = outcome = "failed"
                        fields = {"error_message": sanitize_text(recovery.error), "retry_count": previous_retry_count}
                    elif recovery.result is not None:
                        status = outcome = "complete"
                        artifacts = [record for record in artifacts if record["path"] not in recovery.witness_paths]
                        fields = {"file_path": recovery.result["filepath"], "progress": 100.0,
                                  "error_message": None, "retry_count": previous_retry_count}
                        current = self.in_memory_progress.get(task_id, {})
                        for key in ("title", "thumbnail_url"):
                            if not current.get(key) and recovery.result.get(key):
                                fields[key] = sanitize_text(recovery.result[key]) if key == "title" else recovery.result[key]
                except (OSError, ValueError) as error:
                    logger.exception("Failed to record retained artifacts for task %s", task_id)
                    if status != "cancelled":
                        status = outcome = "failed"
                        artifacts = []
                        fields = {"error_message": sanitize_text(str(error)), "retry_count": previous_retry_count}
        if expansion is not None and status == "complete":
            operation = asyncio.create_task(self._expand_playlist(task_id, attempt.id, expansion, cookies))
            await join_owned_task(operation)
            return
        transition = asyncio.create_task(self._finish(task_id, status, attempt_id=attempt.id,
                                                     artifacts=artifacts, outcome=outcome, **fields))
        changed = await join_owned_task(transition)
        if changed and status in ("queued", "retry_wait") and not self.stopping:
            self.wakeup_event.set()

    async def _expand_playlist(self, task_id, attempt_id, result, cookies):
        try:
            children = await self.attempts.expand(task_id, attempt_id, result, self._now())
        except (ValueError, OSError, sqlite3.DatabaseError):
            await self._finish(task_id, "failed", attempt_id=attempt_id,
                               error_message="Playlist expansion could not be committed")
            return
        if children is None:
            return
        self.active_attempts.pop(task_id, None)
        for child in children:
            self.in_memory_progress[child["id"]] = child
            if cookies:
                try:
                    validate_fresh_cookies(child["url"], cookies, self.clock())
                    self.task_cookies[child["id"]] = cookies
                except ValueError:
                    pass
        self.wakeup_event.set()
        await self._refresh_playlist(task_id)
        for child in children:
            await self.connections.broadcast({"event": "task_queued", "data": child})


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
            user_agent=user_agent,
            idempotency_key=request.headers.get("idempotency-key"),
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
        await queue_manager.delete_history()

        return {"message": "Removable download history cleared"}

    @router.delete("/history/{task_id}", response_model=Dict[str, str], tags=["history"])
    async def delete_history_item(task_id: str):
        await queue_manager.delete_history(task_id)
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
        await queue_manager.retry(task_id, req.cookies, request.headers.get("user-agent"))
        return {"message": f"Download task {task_id} re-enqueued for retry"}

    @router.post("/{task_id}/authentication", response_model=Dict[str, str])
    async def refresh_authentication(task_id: str, req: RetryRequest, request: Request):
        await queue_manager.retry(task_id, req.cookies, request.headers.get("user-agent"),
                                  expected_status="waiting_for_auth")
        return {"message": "Fresh browser authentication supplied"}

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
