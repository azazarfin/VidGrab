"""Pydantic is the source of the v1 native/internal control contract."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator

from backend.config import ConfigModel, DEFAULT_CONFIG
from backend.api.limits import MAX_COUNTER, MAX_EVENT_HISTORY, MAX_MESSAGE_BYTES, MAX_PAYLOAD_BYTES, PROTOCOL_VERSION

Identifier = Annotated[str, Field(strict=True, pattern=r"^[!-~]{1,128}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]


def http_url(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Media URLs cannot contain control characters")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("An HTTP(S) URL without embedded credentials is required")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid media port")
    return value


MediaUrl = Annotated[str, Field(min_length=1, max_length=16384), AfterValidator(http_url)]


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


class DownloadOptions(Model):
    download_subtitles: bool = False
    subtitle_langs: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(default=None, max_length=16)
    subtitle_format: Literal["srt", "vtt", "ass", "ttml", "best"] = "srt"
    embed_subtitles: bool = False
    embed_thumbnail: bool = False
    is_playlist: bool = False
    playlist_items: str | None = Field(default=None, max_length=1024)

    @field_validator("subtitle_langs")
    @classmethod
    def language_codes(cls, values):
        if values is not None and any(not all(char.isascii() and (char.isalnum() or char in "-_") for char in value) for value in values):
            raise ValueError("Subtitle languages must be bounded language codes")
        return values


class DownloadParams(Model):
    url: MediaUrl
    format_id: str | None = Field(default=None, max_length=1024)
    title: str | None = Field(default=None, max_length=512)
    thumbnail_url: str | None = Field(default=None, max_length=16384)
    audio_only: bool = False
    audio_format: Literal["mp3", "m4a", "opus", "wav", "flac"] = "mp3"
    options: DownloadOptions = Field(default_factory=DownloadOptions)
    auth_ref: Identifier | None = None


class ProbeParams(Model):
    url: MediaUrl
    auth_ref: Identifier | None = None
    job_id: str | None = Field(default=None, min_length=1, max_length=256)
    format_limit: int = Field(default=50, ge=1, le=100)
    entry_limit: int = Field(default=25, ge=1, le=128)


class BrowserCookie(Model):
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=1024)
    value: str = Field(max_length=16384)
    secure: bool = False
    expirationDate: float | None = Field(default=None, ge=0, le=253402300799, allow_inf_nan=False)

    @field_validator("domain", "path", "name", "value")
    @classmethod
    def cookie_fields(cls, value):
        if any(char in value for char in "\0\r\n\t"):
            raise ValueError("Cookie fields cannot contain row separators")
        return value


class AuthParams(Model):
    instance_id: Identifier
    url: MediaUrl
    job_id: str | None = Field(default=None, min_length=1, max_length=256)
    cookies: list[BrowserCookie] = Field(min_length=1, max_length=128)
    user_agent: str | None = Field(default=None, min_length=1, max_length=1024)
    referer: MediaUrl | None = None
    ttl_seconds: int = Field(default=300, ge=30, le=900)

    @field_validator("user_agent")
    @classmethod
    def header_value(cls, value):
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("User agents cannot contain control characters")
        return value


class AuthReference(Model):
    reference: Identifier
    instance_id: Identifier
    expires_at: float = Field(ge=0, le=253402300799, allow_inf_nan=False)


class ActionParams(Model):
    job_id: str = Field(min_length=1, max_length=256)
    expected_revision: Counter
    action: Literal["cancel", "retry"]
    auth_ref: Identifier | None = None


class AuthenticationParams(Model):
    job_id: str = Field(min_length=1, max_length=256)
    expected_revision: Counter
    auth_ref: Identifier


class ActionResult(Model):
    job_id: str
    action: Literal["cancel", "retry", "authenticate"]
    accepted_status: str
    accepted_revision: Counter


class ProbeFormat(Model):
    format_id: str = Field(max_length=1024)
    ext: str = Field(max_length=32)
    resolution: str | None = Field(default=None, max_length=128)
    filesize: Counter | None = None
    filesize_approx: Counter | None = None
    vcodec: str | None = Field(default=None, max_length=128)
    acodec: str | None = Field(default=None, max_length=128)
    fps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tbr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    container: str | None = Field(default=None, max_length=128)


class ProbeEntry(Model):
    id: str = Field(max_length=256)
    title: str = Field(max_length=512)
    duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    thumbnail: str | None = Field(default=None, max_length=16384)
    url: str = Field(max_length=16384)


class ProbeSubtitle(Model):
    ext: str = Field(max_length=32)
    name: str = Field(max_length=128)


class ProbeResult(Model):
    probe_id: Identifier
    url: MediaUrl
    title: str = Field(max_length=512)
    thumbnail: str | None = Field(default=None, max_length=16384)
    duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    uploader: str | None = Field(default=None, max_length=512)
    is_playlist: bool
    entries: list[ProbeEntry] | None = Field(default=None, max_length=128)
    formats: list[ProbeFormat] | None = Field(default=None, max_length=100)
    subtitles: dict[str, list[ProbeSubtitle]] | None = None
    format_count: Counter
    entry_count: Counter
    formats_truncated: bool
    entries_truncated: bool


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
    instance_id: Identifier
    paired_loopback: Literal[False] = False
    features: list[str]
    component_versions: dict[str, str]


class Job(Model):
    id: str
    url: str
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
    "AUTH_CONTEXT_EXPIRED", "AUTH_CONTEXT_SCOPE", "STATE_CONFLICT", "NOT_FOUND",
    "REQUEST_BUDGET_EXHAUSTED", "NETWORK_ERROR", "HTTP_ERROR",
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
    data: Capabilities | SettingsSnapshot | DownloadPage | Enqueued | EventBatch | ProbeResult | AuthReference | ActionResult


OPERATIONS = {
    "capabilities.get": (Empty, Capabilities, False),
    "settings.get": (Empty, SettingsSnapshot, False),
    "settings.patch": (SettingsUpdate, SettingsSnapshot, True),
    "downloads.list": (DownloadPageParams, DownloadPage, False),
    "downloads.enqueue": (DownloadParams, Enqueued, True),
    "events.read": (EventParams, EventBatch, False),
    "probes.create": (ProbeParams, ProbeResult, False),
    "auth.create": (AuthParams, AuthReference, True),
    "downloads.action": (ActionParams, ActionResult, True),
    "downloads.authenticate": (AuthenticationParams, ActionResult, True),
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
