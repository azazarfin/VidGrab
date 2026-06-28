import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.config import router as config_router, init_db
from backend.format_probe import router as probe_router
from backend.queue_manager import router as downloads_router, queue_manager, ws_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("vidgrab.main")

# Cache for /status PyPI version lookups
_version_cache = {"data": None, "ts": 0}
_VERSION_CACHE_TTL = 600  # 10 minutes

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the database and start the queue manager worker
    logger.info("Starting up VidGrab Backend...")
    await init_db()
    queue_manager.start()

    # Check and run auto-update for yt-dlp if enabled
    try:
        from backend.config import get_all_config
        from backend.updater import check_ytdlp_version, update_ytdlp
        config = await get_all_config()
        if config.get("auto_update_ytdlp", True):
            logger.info("Auto-update for yt-dlp is enabled. Checking for updates...")
            version_info = await check_ytdlp_version()
            if version_info.get("update_available"):
                logger.info(f"New yt-dlp version {version_info['latest']} available. Auto-updating...")
                await update_ytdlp()
            else:
                logger.info("yt-dlp is already up to date.")
    except Exception as e:
        logger.error(f"Failed during yt-dlp auto-update check: {e}")

    yield
    # Shutdown: Clean up resources and stop the queue manager worker
    logger.info("Shutting down VidGrab Backend...")
    await queue_manager.stop()

app = FastAPI(
    title="VidGrab Backend API",
    description="Local backend service for VidGrab Browser Extension powered by yt-dlp",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension://.*|moz-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(config_router)
app.include_router(probe_router)
app.include_router(downloads_router)

@app.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send current in-memory progress of all tasks to the newly connected client
        await websocket.send_json({
            "event": "init_state",
            "data": list(queue_manager.in_memory_progress.values())
        })
        # Keep connection open and listen for messages if needed (e.g. ping/pong)
        while True:
            data = await websocket.receive_text()
            # We can handle client messages here if necessary
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)

@app.get("/status")
async def status():
    """Health check endpoint."""
    import time, shutil
    global _version_cache
    now = time.time()
    if _version_cache["data"] is None or now - _version_cache["ts"] > _VERSION_CACHE_TTL:
        from backend.updater import check_ytdlp_version
        _version_cache["data"] = await check_ytdlp_version()
        _version_cache["ts"] = now
    version_info = _version_cache["data"]
    ffmpeg_available = shutil.which("ffmpeg") is not None
    return {
        "status": "online",
        "yt_dlp_version": version_info["current"],
        "yt_dlp_latest": version_info["latest"],
        "update_available": version_info["update_available"],
        "ffmpeg_available": ffmpeg_available
    }

@app.post("/update-ytdlp")
async def trigger_update_ytdlp():
    """Triggers the upgrade of yt-dlp via pip."""
    from backend.updater import update_ytdlp
    res = await update_ytdlp()
    return res

@app.get("/")
async def root():
    return {"message": "Welcome to VidGrab Backend API. The service is running."}
