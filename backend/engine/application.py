import re
import secrets
from urllib.parse import urlsplit

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse

from backend.app import create_app
from backend.engine.execution import ProcessDownloadExecutor
from backend.runtime import RuntimeConfig


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
    """Authenticate before exposing the three narrow spike operations."""

    allowed = {("GET", "/health"), ("GET", "/spike/status"), ("POST", "/spike/fixture")}

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
        credentials = [value for key, value in headers if key.lower() == b"authorization"]
        hosts = [value for key, value in headers if key.lower() == b"host"]
        error = None
        if len(credentials) != 1 or not secrets.compare_digest(credentials[0], self.credential):
            error = JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
        elif hosts != [self.host] or any(key.lower() == b"origin" for key, _ in headers):
            error = JSONResponse({"code": "ORIGIN_REJECTED"}, status_code=403)
        elif (scope["method"], scope["path"]) not in self.allowed:
            error = JSONResponse({"code": "UNSUPPORTED_OPERATION"}, status_code=404)
        if error:
            return await error(scope, receive, send)
        if scope["method"] == "POST":
            # Enforce the limit even when Content-Length is absent/chunked.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 4096:
                    return await JSONResponse({"code": "MESSAGE_TOO_LARGE"}, status_code=413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            async def replay():
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            receive = replay
        return await self.app(scope, receive, send)


def create_engine_app(runtime: RuntimeConfig, *, token: str, port: int, components: dict,
                      start_queue: bool = True):
    """Reuse P01 state/queue while keeping the legacy API behind an allowlist.

    The spike is fixture-only. The versioned public contract remains P04 work.
    Resolving paths or creating this factory does not execute component checks.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not 1 <= port <= 65535:
        raise ValueError("An app-generated credential and bound port are required")
    executor = ProcessDownloadExecutor(components=dict(components), fixture_only=True)
    app = create_app(runtime, start_queue=start_queue, download_executor=executor)

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
        )
        return {"task_id": task_id}

    return EngineAccess(app, token, port)
