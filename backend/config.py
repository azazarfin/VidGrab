import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.runtime import OutputPathError, RuntimeConfig
from backend.db.settings import SettingsRepository
from backend.engine.execution import join_owned_task

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
    "transliterate_bangla_filenames": True,
}


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

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
    transliterate_bangla_filenames: bool = Field(default=DEFAULT_CONFIG["transliterate_bangla_filenames"])


def runtime_defaults(runtime: RuntimeConfig) -> Dict[str, Any]:
    return {**DEFAULT_CONFIG, "download_dir": str(runtime.download_dir)}


def validate_stored_settings(values: dict) -> ConfigModel:
    # Preserve unknown legacy keys in SQLite until an explicit migration removes them.
    return ConfigModel.model_validate({key: value for key, value in values.items() if key in DEFAULT_CONFIG})


class SettingsStore:
    def __init__(self, runtime: RuntimeConfig, owner=None):
        self.runtime = runtime
        self.owner = owner
        self.repository = SettingsRepository(runtime, owner)
        self.on_change = None

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
        try:
            result = await join_owned_task(asyncio.create_task(self._patch(supplied)))
        except (OSError, OutputPathError) as error:
            detail = str(error) if isinstance(error, OutputPathError) else "Choose a writable download directory"
            raise HTTPException(status_code=400, detail=detail) from error
        return ConfigModel.model_validate(result["values"])

    async def patch_revision(self, supplied, expected_revision, key):
        if self.owner is None:
            from backend.runtime import RuntimeOwnershipError
            raise RuntimeOwnershipError("Versioned settings require the active runtime owner")
        return await join_owned_task(asyncio.create_task(self._patch(supplied, expected_revision=expected_revision, key=key)))

    async def _patch(self, supplied, *, expected_revision=None, key=None):
        result = await self.repository.patch(supplied, self.validate_settings, expected_revision=expected_revision, key=key,
                                             validate_result=self.validate_wire if expected_revision is not None else None)
        if result.changed and self.on_change is not None:
            self.on_change()
        return result.snapshot

    def validate_wire(self, values):
        from backend.api.models import SettingsValues
        from backend.services.control_errors import ControlError
        try:
            return SettingsValues.model_validate(values)
        except ValidationError as error:
            fields = sorted({str(issue["loc"][0]) for issue in error.errors(include_input=False, include_context=False)})
            raise ControlError("UNSUPPORTED_OPTION", 409, "Update the reported legacy settings to values this client can represent exactly",
                               details={"fields": fields}) from error


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
