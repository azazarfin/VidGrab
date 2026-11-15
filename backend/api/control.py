"""Thin adapters; the packaged access guard authenticates before these routes."""

import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request as HttpRequest
from pydantic import TypeAdapter, ValidationError
from starlette.responses import JSONResponse

from backend.api.models import Error, Failure, Identifier, MAX_PAYLOAD_BYTES, Request, Success
from backend.runtime import OutputPathError, RuntimeOwnershipError
from backend.services.control_errors import ControlError

logger = logging.getLogger("vidgrab.control")
identifier_adapter = TypeAdapter(Identifier)


def failure(request_id, code, status, message, *, details=None, retryable=False, retry_at=None, provenance="local"):
    result = Failure(request_id=request_id, error=Error(
        code=code, request_id=request_id, message=message, details=details or {},
        retryable=retryable, retry_at=retry_at, provenance=provenance)).model_dump()
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        settings = result["error"]["details"].get("settings", {})
        result["error"]["details"] = {"resnapshot_required": True, "settings_revision": settings.get("revision")}
    return JSONResponse(result, status_code=status)


async def dispatch(service, raw, ready):
    request_id = None
    if isinstance(raw, dict):
        try:
            request_id = identifier_adapter.validate_python(raw.get("request_id"))
        except ValidationError:
            pass
    try:
        request = Request.model_validate(raw)
        request_id = request.request_id
        if not ready:
            raise ControlError("ENGINE_UNAVAILABLE", 503, "The engine is unavailable; reopen VidGrab")
        data = await service.dispatch(request)
        payload = Success(request_id=request_id, data=data).model_dump()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ControlError("MESSAGE_TOO_LARGE", 413, "Request a smaller page to fit the control message limit")
        return JSONResponse(payload)
    except ControlError as error:
        return failure(request_id, error.code, error.status, str(error), details=error.details,
                       retryable=error.retryable, retry_at=error.retry_at, provenance=error.provenance)
    except ValidationError:
        return failure(request_id, "BAD_REQUEST", 422, "The request does not match the supported contract")
    except RuntimeOwnershipError:
        return failure(request_id, "ENGINE_UNAVAILABLE", 503, "The active engine owner could not be verified")
    except OutputPathError:
        return failure(request_id, "BAD_REQUEST", 400, "Choose an output location outside protected application storage")
    except HTTPException as error:
        codes = {409: "IDEMPOTENCY_CONFLICT", 410: "RECEIPT_EXPIRED", 503: "ENGINE_UNAVAILABLE"}
        return failure(request_id, codes.get(error.status_code, "BAD_REQUEST"), error.status_code,
                       "The download request could not be accepted; check its current state")
    except Exception:
        # Exception text may include a signed URL, path or SQLite input; do not
        # copy it to a client or an automatic diagnostic log.
        logger.error("Control operation failed for request %s", request_id)
        return failure(request_id, "INTERNAL_ERROR", 500, "The control operation could not be completed")


def create_control_router(service):
    router = APIRouter(prefix="/v1", tags=["control-v1"])

    async def payload(http):
        try:
            return await http.json()
        except (ValueError, UnicodeError, RecursionError):
            return None

    def integer_literal(value, max_digits):
        return int(value) if len(value) <= max_digits and value.isascii() and value.isdecimal() else value

    @router.post("/requests")
    async def control_request(http: HttpRequest):
        raw = await payload(http)
        return await dispatch(service, raw, http.app.state.ready)

    async def rest(http, operation, params):
        request_id = http.headers.get("x-request-id", str(uuid4()))
        version = http.headers.get("x-protocol-version", "1")
        raw = {"protocol_version": integer_literal(version, 5),
               "request_id": request_id, "operation": operation, "params": params}
        if "idempotency-key" in http.headers:
            raw["idempotency_key"] = http.headers["idempotency-key"]
        return await dispatch(service, raw, http.app.state.ready)

    @router.get("/capabilities")
    async def capabilities(http: HttpRequest):
        return await rest(http, "capabilities.get", {})

    @router.get("/settings")
    async def settings(http: HttpRequest):
        return await rest(http, "settings.get", {})

    @router.patch("/settings")
    async def update_settings(http: HttpRequest):
        params = await payload(http)
        return await rest(http, "settings.patch", params)

    @router.get("/downloads")
    async def downloads(http: HttpRequest):
        params = dict(http.query_params)
        if "limit" in params:
            params["limit"] = integer_literal(params["limit"], 3)
        return await rest(http, "downloads.list", params)

    @router.post("/downloads")
    async def enqueue(http: HttpRequest):
        params = await payload(http)
        return await rest(http, "downloads.enqueue", params)

    @router.get("/events")
    async def events(http: HttpRequest):
        params = dict(http.query_params)
        for name in ("cursor", "limit"):
            if name in params:
                params[name] = integer_literal(params[name], 16)
        return await rest(http, "events.read", params)

    @router.post("/probes")
    async def probe(http: HttpRequest):
        return await rest(http, "probes.create", await payload(http))

    @router.post("/auth/contexts")
    async def authentication_context(http: HttpRequest):
        return await rest(http, "auth.create", await payload(http))

    @router.post("/downloads/{job_id}/actions")
    async def action(job_id: str, http: HttpRequest):
        params = await payload(http)
        if isinstance(params, dict):
            params = None if "job_id" in params and params["job_id"] != job_id else {**params, "job_id": job_id}
        return await rest(http, "downloads.action", params)

    @router.post("/downloads/{job_id}/authentication")
    async def authenticate(job_id: str, http: HttpRequest):
        params = await payload(http)
        if isinstance(params, dict):
            params = None if "job_id" in params and params["job_id"] != job_id else {**params, "job_id": job_id}
        return await rest(http, "downloads.authenticate", params)

    return router
