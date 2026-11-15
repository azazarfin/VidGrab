"""Pydantic is the source of the v1 native/internal control contract."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator

from backend.config import ConfigModel, DEFAULT_CONFIG
from backend.api.limits import MAX_COUNTER, MAX_EVENT_HISTORY, MAX_MESSAGE_BYTES, MAX_PAYLOAD_BYTES, PROTOCOL_VERSION

Identifier = Annotated[str, Field(strict=True, pattern=r"^[!-~]{1,128}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_serialization_defaults_required=True)


class Empty(Model):
    pass


class SettingsValues(ConfigModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    speed_limit: int = Field(default=DEFAULT_CONFIG["speed_limit"], strict=True, ge=0, le=MAX_COUNTER)


class SettingsPatch(SettingsValues):
    pass


class SettingsUpdate(Model):
    expected_revision: Counter
    patch: SettingsPatch


class SettingsSnapshot(Model):
    revision: Counter
    values: SettingsValues


class DownloadParams(Model):
    url: str = Field(min_length=1, max_length=16384)
    format_id: str | None = Field(default=None, max_length=1024)
    title: str | None = Field(default=None, max_length=512)
    thumbnail_url: str | None = Field(default=None, max_length=16384)
    audio_only: bool = False
    audio_format: Literal["mp3", "m4a", "opus", "wav", "flac"] = "mp3"

    @field_validator("url")
    @classmethod
    def http_url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("An HTTP(S) media URL without embedded credentials is required")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid media port")
        return value


class DownloadPageParams(Model):
    limit: int = Field(default=25, ge=1, le=50)
    after: str | None = Field(default=None, max_length=256)


class EventParams(Model):
    instance_id: str | None = Field(default=None, max_length=128)
    cursor: Counter = 0
    limit: int = Field(default=50, ge=1, le=100)


class Capabilities(Model):
    protocol_min: int = Field(default=1, ge=1, le=65535)
    protocol_max: int = Field(default=1, ge=1, le=65535)
    max_message_bytes: Literal[65536] = MAX_MESSAGE_BYTES
    max_payload_bytes: Literal[64512] = MAX_PAYLOAD_BYTES
    fixture_only: bool
    paired_loopback: Literal[False] = False
    features: list[str]
    component_versions: dict[str, str]


class Job(Model):
    id: str
    revision: Counter
    title: str | None
    status: str
    progress: float = Field(ge=0, le=100, allow_inf_nan=False)
    file_path: str | None
    error_code: str | None
    retry_at: float | None = Field(default=None, allow_inf_nan=False)
    parent_id: str | None


class Counts(Model):
    total: Counter
    unfinished: Counter


class DownloadPage(Model):
    jobs: list[Job]
    next_after: str | None
    counts: Counts


class Enqueued(Model):
    job_id: str


class Snapshot(DownloadPage):
    instance_id: str
    cursor: Counter
    settings: SettingsSnapshot


class Event(Model):
    sequence: Counter
    topic: Literal["downloads", "settings"]
    entity_id: str | None
    revision: Counter
    kind: Literal["changed", "deleted"]


class EventBatch(Model):
    instance_id: str
    cursor: Counter
    resnapshot: bool
    has_more: bool
    snapshot: Snapshot | None
    events: list[Event]


class Request(Model):
    protocol_version: int = Field(ge=0, le=65535)
    request_id: Identifier
    operation: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue]
    idempotency_key: Identifier | None = None


ErrorCode = Literal[
    "BAD_REQUEST", "BAD_RESPONSE", "APP_INCOMPATIBLE", "UNAUTHORIZED", "ORIGIN_REJECTED",
    "UNSUPPORTED_OPERATION", "UNSUPPORTED_OPTION", "ENGINE_UNAVAILABLE", "APP_MISSING",
    "APP_UNAVAILABLE", "BROWSER_DISCONNECTED", "BROWSER_CONNECTION_UNAVAILABLE",
    "REQUEST_OUTCOME_UNKNOWN", "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "RECEIPT_EXPIRED",
    "CAPACITY_EXCEEDED", "MESSAGE_TOO_LARGE", "INTERNAL_ERROR", "AUTH_REQUIRED", "RATE_LIMITED",
    "UNSUPPORTED_MEDIA",
]


class Error(Model):
    code: ErrorCode
    message: str
    retryable: bool = False
    retry_at: float | None = Field(default=None, allow_inf_nan=False)
    request_id: Identifier | None
    provenance: Literal["local", "engine", "upstream"] = "local"
    details: dict[str, JsonValue] = Field(default_factory=dict)


class Failure(Model):
    protocol_version: Literal[1] = 1
    request_id: Identifier | None
    ok: Literal[False] = False
    error: Error


class Success(Model):
    protocol_version: Literal[1] = 1
    request_id: Identifier
    ok: Literal[True] = True
    data: Capabilities | SettingsSnapshot | DownloadPage | Enqueued | EventBatch


OPERATIONS = {
    "capabilities.get": (Empty, Capabilities, False),
    "settings.get": (Empty, SettingsSnapshot, False),
    "settings.patch": (SettingsUpdate, SettingsSnapshot, True),
    "downloads.list": (DownloadPageParams, DownloadPage, False),
    "downloads.enqueue": (DownloadParams, Enqueued, True),
    "events.read": (EventParams, EventBatch, False),
}


def contract_schema():
    """Export without importing an app, resolving a runtime or opening SQLite."""
    schema = {
        "protocol_version": PROTOCOL_VERSION,
        "max_message_bytes": MAX_MESSAGE_BYTES,
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "request": Request.model_json_schema(),
        "response": TypeAdapter(Success | Failure).json_schema(mode="serialization"),
        "operations": {
            name: {"params": params.model_json_schema(), "result": result.model_json_schema(mode="serialization"), "mutation": mutation}
            for name, (params, result, mutation) in OPERATIONS.items()
        },
    }
    # Runtime injection supplies the directory; a builder's home is not a wire default.
    def strip_runtime_default(value):
        if isinstance(value, dict):
            if "download_dir" in value.get("properties", {}):
                value["properties"]["download_dir"].pop("default", None)
            for child in value.values():
                strip_runtime_default(child)
        elif isinstance(value, list):
            for child in value:
                strip_runtime_default(child)
    strip_runtime_default(schema)
    return schema
