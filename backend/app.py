"""App factory. Startup owns mutations; factory/import owns no processes or files."""

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import SettingsStore, create_config_router
from backend.db.migrations import SCHEMA_VERSION, schema_version
from backend.downloader import run_download
from backend.format_probe import ProbeService, create_probe_router
from backend.queue_manager import ConnectionManager, QueueManager, create_download_router, open_folder
from backend.runtime import RuntimeConfig
from backend.engine.execution import join_owned_task
from backend.engine.runtime_owner import RuntimeOwner

logger = logging.getLogger("vidgrab.app")


def create_app(
    runtime: RuntimeConfig | None = None, *, start_queue: bool = True,
    probe_runner=None, download_runner=run_download, clock=time.time, open_folder=open_folder,
    download_executor=None,
) -> FastAPI:
    runtime = RuntimeConfig.from_env() if runtime is None else runtime
    runtime.validate()
    settings = SettingsStore(runtime)
    connections = ConnectionManager()
    owner = RuntimeOwner(runtime, clock)
    queue = QueueManager(runtime, settings, connections, download_runner, clock, open_folder, download_executor, owner=owner)
    probes = ProbeService(runtime, runner=probe_runner, clock=clock, owner=owner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owner.lock()
        app.state.ready = False
        try:
            await settings.initialize()
            await owner.start()
            await queue.initialize()
            await probes.initialize()
            if start_queue:
                queue.start()
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            async def cleanup():
                try:
                    if owner.active:
                        await probes.stop()
                        await queue.stop()
                except BaseException:
                    await owner.stop(release=False)
                    raise
                else:
                    await owner.stop()
            await join_owned_task(asyncio.create_task(cleanup()))

    app = FastAPI(
        title="VidGrab Backend API",
        description="Local VidGrab engine; runtime state is owned by this application",
        version="1.0.0", lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.owner = owner
    app.state.settings = settings
    app.state.queue = queue
    app.state.connections = connections
    app.state.probes = probes
    app.state.ready = False

    async def ownership_failed():
        app.state.ready = False
        await probes.stop()
        await queue.stop()

    owner.on_failure = ownership_failed
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension://.*|moz-extension://.*",
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    async def require_ready(request: Request):
        if not request.app.state.ready:
            raise HTTPException(status_code=503, detail="Engine lifespan has not initialized runtime state")

    for router in (create_config_router(settings), create_probe_router(probes), create_download_router(queue)):
        app.include_router(router, dependencies=[Depends(require_ready)])

    @app.websocket("/ws/progress")
    async def websocket_endpoint(websocket: WebSocket):
        if not app.state.ready:
            await websocket.close(code=1013)
            return
        await connections.connect(websocket)
        try:
            await websocket.send_json({"event": "init_state", "data": list(queue.in_memory_progress.values())})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket error")
        finally:
            connections.disconnect(websocket)

    @app.get("/health")
    async def health():
        if not app.state.ready:
            return JSONResponse(status_code=503, content={"status": "not_ready", "schema_version": None})
        try:
            current_schema = await schema_version(runtime.db_path)
        except Exception:
            logger.exception("Runtime database is unavailable")
            return JSONResponse(status_code=503, content={"status": "database_unavailable", "schema_version": None})
        if current_schema != SCHEMA_VERSION:
            return JSONResponse(status_code=503, content={"status": "incompatible_schema", "schema_version": current_schema})
        return {"status": "ready", "schema_version": current_schema}

    @app.get("/status")
    async def status():
        # Preserve legacy fields while keeping idle health entirely local.
        return {
            "status": "online", "yt_dlp_version": version("yt-dlp"),
            "yt_dlp_latest": None, "update_available": False,
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
        }

    @app.post("/update-ytdlp")
    async def trigger_update_ytdlp():
        raise HTTPException(
            status_code=409,
            detail="Install a newer VidGrab release to update the media engine",
        )

    @app.get("/")
    async def root():
        return {"message": "Welcome to VidGrab Backend API. The service is running."}

    return app
