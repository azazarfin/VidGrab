import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.runtime import OutputPathError, RuntimeConfig

logger = logging.getLogger("vidgrab.config")
DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads" / "VidGrab"),
    "max_concurrent": 3,
    "default_format": "bestvideo+bestaudio/best",
    "default_quality": "1080",
    "filename_template": "%(title)s.%(ext)s",
    "speed_limit": 0,
    "auto_update_ytdlp": False,  # Legacy preference; startup never runs pip.
    "sponsorblock_enabled": False,
    "sponsorblock_categories": ["sponsor", "intro", "outro", "selfpromo", "interaction", "music_offtopic"],
    "sponsorblock_mode": "remove",
    "postprocess_codec": "none",
    "postprocess_normalize_audio": False,
    "custom_ytdlp_args": "",
    "download_threads": 4,
    "gpu_accelerated_conversion": False,
    "max_retries": 3,
}


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download_dir: str = Field(default=DEFAULT_CONFIG["download_dir"], min_length=1)
    max_concurrent: int = Field(default=DEFAULT_CONFIG["max_concurrent"], strict=True, ge=1, le=10)
    default_format: str = Field(default=DEFAULT_CONFIG["default_format"])
    default_quality: str = Field(default=DEFAULT_CONFIG["default_quality"])
    filename_template: str = Field(default=DEFAULT_CONFIG["filename_template"])
    speed_limit: int = Field(default=DEFAULT_CONFIG["speed_limit"], strict=True, ge=0, le=2**63 - 1)
    auto_update_ytdlp: bool = Field(default=DEFAULT_CONFIG["auto_update_ytdlp"])
    sponsorblock_enabled: bool = Field(default=DEFAULT_CONFIG["sponsorblock_enabled"])
    sponsorblock_categories: List[str] = Field(default=DEFAULT_CONFIG["sponsorblock_categories"])
    sponsorblock_mode: str = Field(default=DEFAULT_CONFIG["sponsorblock_mode"])
    postprocess_codec: str = Field(default=DEFAULT_CONFIG["postprocess_codec"])
    postprocess_normalize_audio: bool = Field(default=DEFAULT_CONFIG["postprocess_normalize_audio"])
    custom_ytdlp_args: str = Field(default=DEFAULT_CONFIG["custom_ytdlp_args"])
    download_threads: int = Field(default=DEFAULT_CONFIG["download_threads"], strict=True, ge=1, le=32)
    gpu_accelerated_conversion: bool = Field(default=DEFAULT_CONFIG["gpu_accelerated_conversion"])
    max_retries: int = Field(default=DEFAULT_CONFIG["max_retries"], strict=True, ge=0, le=10)


def runtime_defaults(runtime: RuntimeConfig) -> Dict[str, Any]:
    return {**DEFAULT_CONFIG, "download_dir": str(runtime.download_dir)}


def validate_stored_settings(values: dict) -> ConfigModel:
    # Preserve unknown legacy keys in SQLite until an explicit migration removes them.
    return ConfigModel.model_validate({key: value for key, value in values.items() if key in DEFAULT_CONFIG})


class SettingsStore:
    def __init__(self, runtime: RuntimeConfig):
        self.runtime = runtime

    async def initialize(self):
        from backend.db.migrations import migrate_database
        await migrate_database(self.runtime, runtime_defaults(self.runtime), self.validate_settings)

    def validate_settings(self, values: dict) -> ConfigModel:
        validated = validate_stored_settings(values)
        self.runtime.validate_output(validated.download_dir, validated.filename_template)
        return validated

    async def get_all(self) -> Dict[str, Any]:
        async with aiosqlite.connect(self.runtime.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute("SELECT key, value FROM config") as cursor:
                return {key: json.loads(value) for key, value in await cursor.fetchall()}

    async def patch(self, settings: dict) -> ConfigModel:
        supplied = ConfigModel.model_validate(settings).model_dump(exclude_unset=True)
        if not supplied:
            return validate_stored_settings(await self.get_all())
        async with aiosqlite.connect(self.runtime.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT key, value FROM config") as cursor:
                    current = {key: json.loads(value) for key, value in await cursor.fetchall()}
                updated = self.validate_settings({**current, **supplied})
                if "download_dir" in supplied:
                    download_path = Path(updated.download_dir).expanduser().resolve()
                    download_path.mkdir(parents=True, exist_ok=True)
                    supplied["download_dir"] = updated.download_dir = str(download_path)
                for key, value in supplied.items():
                    await db.execute(
                        "INSERT INTO config (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)),
                    )
                await db.commit()
            except (OSError, OutputPathError) as error:
                await db.rollback()
                detail = str(error) if isinstance(error, OutputPathError) else "Choose a writable download directory"
                raise HTTPException(status_code=400, detail=detail) from error
            except BaseException:
                await db.rollback()
                raise
        return updated


async def init_db(runtime: RuntimeConfig):
    await SettingsStore(runtime).initialize()


async def get_all_config(runtime: RuntimeConfig) -> Dict[str, Any]:
    return await SettingsStore(runtime).get_all()


async def update_config_db(settings: Dict[str, Any], runtime: RuntimeConfig):
    await SettingsStore(runtime).patch(settings)


def create_config_router(settings: SettingsStore) -> APIRouter:
    router = APIRouter(prefix="/config", tags=["config"])

    @router.get("", response_model=ConfigModel)
    async def get_config():
        return validate_stored_settings(await settings.get_all())

    @router.patch("", response_model=ConfigModel)
    async def update_config(values: ConfigModel):
        return await settings.patch(values.model_dump(exclude_unset=True))

    return router
