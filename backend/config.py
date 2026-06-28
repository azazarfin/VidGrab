import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import aiosqlite

logger = logging.getLogger("vidgrab.config")

DB_PATH = Path(__file__).parent / "vidgrab.db"

DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads" / "VidGrab"),
    "max_concurrent": 3,
    "default_format": "bestvideo+bestaudio/best",
    "default_quality": "1080",
    "filename_template": "%(title)s.%(ext)s",
    "speed_limit": 0,  # 0 means unlimited
    "auto_update_ytdlp": True,
    "sponsorblock_enabled": False,
    "sponsorblock_categories": ["sponsor", "intro", "outro", "selfpromo", "interaction", "music_offtopic"],
    "sponsorblock_mode": "remove",
    "postprocess_codec": "none",
    "postprocess_normalize_audio": False,
    "custom_ytdlp_args": "",
}

class ConfigModel(BaseModel):
    download_dir: str = Field(default=DEFAULT_CONFIG["download_dir"])
    max_concurrent: int = Field(default=DEFAULT_CONFIG["max_concurrent"], ge=1, le=10)
    default_format: str = Field(default=DEFAULT_CONFIG["default_format"])
    default_quality: str = Field(default=DEFAULT_CONFIG["default_quality"])
    filename_template: str = Field(default=DEFAULT_CONFIG["filename_template"])
    speed_limit: int = Field(default=DEFAULT_CONFIG["speed_limit"], ge=0)
    auto_update_ytdlp: bool = Field(default=DEFAULT_CONFIG["auto_update_ytdlp"])
    sponsorblock_enabled: bool = Field(default=DEFAULT_CONFIG["sponsorblock_enabled"])
    sponsorblock_categories: List[str] = Field(default=DEFAULT_CONFIG["sponsorblock_categories"])
    sponsorblock_mode: str = Field(default=DEFAULT_CONFIG["sponsorblock_mode"])
    postprocess_codec: str = Field(default=DEFAULT_CONFIG["postprocess_codec"])
    postprocess_normalize_audio: bool = Field(default=DEFAULT_CONFIG["postprocess_normalize_audio"])
    custom_ytdlp_args: str = Field(default=DEFAULT_CONFIG["custom_ytdlp_args"])

router = APIRouter(prefix="/config", tags=["config"])

async def init_db():
    """Initializes the SQLite database and populates default settings if empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                thumbnail_url TEXT,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                speed REAL DEFAULT 0,
                eta REAL,
                file_size INTEGER,
                file_path TEXT,
                format_selected TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                download_options TEXT,
                scheduled_at TEXT
            )
            """
        )
        await db.commit()

        # Safely migrate existing database by adding download_options if missing
        try:
            await db.execute("ALTER TABLE downloads ADD COLUMN download_options TEXT")
            await db.commit()
        except aiosqlite.OperationalError:
            pass # Already exists

        # Safely migrate existing database by adding scheduled_at if missing
        try:
            await db.execute("ALTER TABLE downloads ADD COLUMN scheduled_at TEXT")
            await db.commit()
        except aiosqlite.OperationalError:
            pass # Already exists

        # Populate defaults if they do not exist
        for key, val in DEFAULT_CONFIG.items():
            cursor = await db.execute("SELECT 1 FROM config WHERE key = ?", (key,))
            if not await cursor.fetchone():
                # Store all values as JSON strings to maintain types
                await db.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(val)),
                )
        await db.commit()
    logger.info("Database initialized successfully.")

async def get_all_config() -> Dict[str, Any]:
    """Retrieve all configuration settings from the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM config") as cursor:
            rows = await cursor.fetchall()
            config = {}
            for row in rows:
                try:
                    config[row[0]] = json.loads(row[1])
                except json.JSONDecodeError:
                    config[row[0]] = row[1]
            return config

async def update_config_db(settings: Dict[str, Any]):
    """Update configuration settings in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        for key, val in settings.items():
            if key in DEFAULT_CONFIG:
                await db.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(val)),
                )
        await db.commit()

@router.get("", response_model=ConfigModel)
async def get_config():
    try:
        config_dict = await get_all_config()
        return ConfigModel(**config_dict)
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("", response_model=ConfigModel)
async def update_config(settings: ConfigModel):
    try:
        # Convert pydantic model to dict, excluding unset/none fields if needed, 
        # but here we update all fields sent.
        settings_dict = settings.model_dump()
        
        # Ensure download directory exists or can be created
        download_path = Path(settings_dict["download_dir"]).expanduser()
        try:
            download_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid or unwritable download directory: {str(e)}"
            )

        await update_config_db(settings_dict)
        updated_config = await get_all_config()
        return ConfigModel(**updated_config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        raise HTTPException(status_code=500, detail=str(e))
