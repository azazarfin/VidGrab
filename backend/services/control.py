"""One control service for REST, native messaging and desktop commands."""

import hashlib

from pydantic import ValidationError

from backend.api.models import Capabilities, OPERATIONS, PROTOCOL_VERSION, Request
from backend.db.events import EventRepository
from backend.services.control_errors import ControlError


class ControlService:
    def __init__(self, settings, queue, owner, *, fixture_validator, component_versions=None):
        self.settings = settings
        self.queue = queue
        self.owner = owner
        self.fixture_validator = fixture_validator
        self.component_versions = component_versions or {}
        self.events = EventRepository(settings.runtime, owner, settings.validate_settings)

    def validate_settings_snapshot(self, snapshot):
        try:
            self.settings.validate_wire(snapshot["values"])
        except ControlError as error:
            error.details["settings_revision"] = snapshot["revision"]
            raise

    async def dispatch(self, request: Request):
        if request.protocol_version != PROTOCOL_VERSION:
            raise ControlError("APP_INCOMPATIBLE", 409, "Update VidGrab and the browser extension to compatible versions",
                               details={"protocol_min": 1, "protocol_max": 1})
        if request.operation not in OPERATIONS:
            raise ControlError("UNSUPPORTED_OPERATION", 404, "This operation is unavailable in this contract")
        params_model, result_model, mutation = OPERATIONS[request.operation]
        if mutation and request.idempotency_key is None:
            raise ControlError("BAD_REQUEST", 422, "A stable idempotency key is required for this mutation")
        try:
            params = params_model.model_validate(request.params)
        except ValidationError as error:
            raise ControlError("BAD_REQUEST", 422, "The parameters do not match this operation") from error
        if request.operation == "capabilities.get":
            result = Capabilities(fixture_only=self.fixture_validator is not None,
                                  features=["settings.revisions", "downloads.idempotency", "events.snapshots", "events.replay"],
                                  component_versions=self.component_versions)
        elif request.operation == "settings.get":
            result = await self.events.settings()
        elif request.operation == "settings.patch":
            patch = params.patch.model_dump(exclude_unset=True)
            if patch.get("custom_ytdlp_args"):
                raise ControlError("UNSUPPORTED_OPTION", 422, "Executable legacy arguments are unavailable through this contract")
            result = await self.settings.patch_revision(patch, params.expected_revision, request.idempotency_key)
        elif request.operation == "downloads.list":
            result = await self.events.downloads(params.limit, params.after)
        elif request.operation == "events.read":
            result = await self.events.events(params.instance_id, params.cursor, params.limit)
        else:
            if self.fixture_validator is not None:
                self.fixture_validator({"url": params.url})
            defaults = await self.settings.get_all()
            if defaults.get("custom_ytdlp_args"):
                raise ControlError("UNSUPPORTED_OPTION", 422, "Clear executable legacy arguments before using this contract")
            intent = {"protocol_version": PROTOCOL_VERSION, "operation": request.operation, "params": params.model_dump()}
            # Legacy and v1 receipts have separate key domains; both retain P03's
            # atomic owner-fenced admission, tombstones and joined cancellation.
            key = "v1." + hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            job_id = await self.queue.enqueue(
                params.url, params.format_id or defaults["default_format"], title=params.title,
                thumbnail_url=params.thumbnail_url, audio_only=params.audio_only, audio_format=params.audio_format,
                idempotency_key=key, idempotency_intent=intent,
            )
            result = {"job_id": job_id}
        if request.operation.startswith("settings."):
            self.validate_settings_snapshot(result)
        elif request.operation == "events.read" and result["snapshot"] is not None:
            self.validate_settings_snapshot(result["snapshot"]["settings"])
        try:
            return result_model.model_validate(result).model_dump()
        except ValidationError as error:
            raise ControlError("INTERNAL_ERROR", 500, "The stored result could not be read safely") from error
