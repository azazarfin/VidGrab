import re
import secrets
import time
from urllib.parse import urlsplit

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse

from backend.app import create_app
from backend.api.control import create_control_router, failure
from backend.api.limits import MAX_PAYLOAD_BYTES
from backend.engine.execution import ProcessDownloadExecutor
from backend.runtime import RuntimeConfig
from backend.services.control import ControlService


class FixtureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(strict=True, max_length=2048)

    @field_validator("url")
    @classmethod
    def local_wav(cls, value):
        parsed = urlsplit(value)
        if not (
            parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port is not None and 1 <= parsed.port <= 65535
            and parsed.path == "/fixture.wav" and not parsed.query and not parsed.fragment
            and parsed.username is None and parsed.password is None
        ):
            raise ValueError("This lifecycle spike accepts only a numeric loopback fixture.wav URL")
        return value


class EngineAccess:
    """Authenticate before exposing the explicit spike and versioned routes."""

    allowed = {("GET", "/health"), ("GET", "/spike/status"), ("POST", "/spike/fixture"),
               ("POST", "/v1/requests"), ("GET", "/v1/capabilities"), ("GET", "/v1/settings"),
               ("PATCH", "/v1/settings"), ("GET", "/v1/downloads"), ("POST", "/v1/downloads"),
               ("DELETE", "/v1/downloads/history"), ("GET", "/v1/diagnostics"), ("GET", "/v1/events"),
               ("POST", "/v1/probes"), ("POST", "/v1/auth/contexts")}

    def __init__(self, app, token, port):
        self.app = app
        self.credential = ("Bearer " + token).encode("ascii")
        self.host = f"127.0.0.1:{port}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        headers = scope.get("headers", [])
        def reject(code, status):
            if scope["path"].startswith("/v1/"):
                return failure(None, code, status, "The authenticated control request was rejected")
            return JSONResponse({"code": code}, status_code=status)
        credentials = [value for key, value in headers if key.lower() == b"authorization"]
        hosts = [value for key, value in headers if key.lower() == b"host"]
        error = None
        if len(credentials) != 1 or not secrets.compare_digest(credentials[0], self.credential):
            error = reject("UNAUTHORIZED", 401)
        elif hosts != [self.host] or any(key.lower() == b"origin" for key, _ in headers):
            error = reject("ORIGIN_REJECTED", 403)
        elif (scope["method"], scope["path"]) not in self.allowed:
            action_path = re.fullmatch(r"/v1/downloads/[^/]{1,256}/(?:actions|authentication)", scope["path"])
            get_job_path = re.fullmatch(r"/v1/downloads/([^/]{1,256})", scope["path"])
            if not (
                (scope["method"] == "POST" and action_path)
                or (scope["method"] == "GET" and get_job_path and get_job_path.group(1) != "history")
            ):
                error = reject("UNSUPPORTED_OPERATION", 404)
        if error:
            return await error(scope, receive, send)
        if scope["method"] in {"POST", "PATCH"}:
            # Enforce the limit even when Content-Length is absent/chunked.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                limit = MAX_PAYLOAD_BYTES if scope["path"].startswith("/v1/") else 4096
                if len(body) > limit:
                    return await reject("MESSAGE_TOO_LARGE", 413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            async def replay():
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            receive = replay
        return await self.app(scope, receive, send)


def create_engine_app(runtime: RuntimeConfig, *, token: str, port: int, components: dict,
                      start_queue: bool = True, component_versions: dict | None = None,
                      media_mode: str = "fixture", probe_runner=None, clock=time.time):
    """Reuse P01 state/queue while keeping the legacy API behind an allowlist.

    Ordinary media requires its explicit mode and the versioned allowlist.
    Legacy routes remain inaccessible through this wrapper.
    Resolving paths or creating this factory does not execute component checks.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not 1 <= port <= 65535:
        raise ValueError("An app-generated credential and bound port are required")
    if media_mode not in {"fixture", "ordinary"}:
        raise ValueError("Choose the explicit fixture or ordinary media mode")
    executor = ProcessDownloadExecutor(components=dict(components), fixture_only=media_mode == "fixture")
    app = create_app(runtime, start_queue=start_queue, download_executor=executor, probe_runner=probe_runner, clock=clock)
    app.state.queue.allow_legacy_options = False
    app.state.probes.components = dict(components)
    service = ControlService(app.state.settings, app.state.queue, app.state.owner,
                             fixture_validator=FixtureRequest.model_validate if media_mode == "fixture" else None,
                             component_versions=component_versions, probes=app.state.probes, components=components)
    app.state.control = service
    app.include_router(create_control_router(service))

    @app.get("/spike/status")
    async def status():
        async with aiosqlite.connect(runtime.db_path) as database:
            database.row_factory = aiosqlite.Row
            async with database.execute(
                "SELECT id, title, status, progress, file_path, error_message FROM downloads "
                "ORDER BY created_at DESC, id DESC LIMIT 50"
            ) as cursor:
                jobs = [dict(row) for row in await cursor.fetchall()]
            async with database.execute(
                "SELECT EXISTS(SELECT 1 FROM downloads "
                "WHERE status NOT IN ('complete', 'failed', 'cancelled'))"
            ) as cursor:
                downloads_active = bool((await cursor.fetchone())[0])
        return {"spike_version": 1, "jobs": jobs, "downloads_active": downloads_active}

    @app.post("/spike/fixture")
    async def fixture(request: FixtureRequest):
        task_id = await app.state.queue.enqueue(
            url=request.url, format_id="best", title="VidGrab test audio",
            audio_only=True, audio_format="mp3",
            output_template="fixture-%(id)s.%(ext)s",
            fixture_only=True,
        )
        return {"task_id": task_id}

    return EngineAccess(app, token, port)
