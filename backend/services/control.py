"""One control service for REST, native messaging and desktop commands."""

import hashlib
from uuid import uuid4

from pydantic import ValidationError

from backend.api.models import Capabilities, OPERATIONS, PROTOCOL_VERSION, Request
from backend.db.events import EventRepository, read_transaction
from backend.db.control_actions import ControlAction
from backend.services.control_errors import ControlError
from backend.services.credentials import CredentialStore
from backend.services.retry_policy import RequestFailure, decide, nested_errors


class ControlService:
    def __init__(self, settings, queue, owner, *, fixture_validator, component_versions=None, probes=None):
        self.settings = settings
        self.queue = queue
        self.owner = owner
        self.fixture_validator = fixture_validator
        self.component_versions = component_versions or {}
        self.events = EventRepository(settings.runtime, owner, settings.validate_settings)
        self.probes = probes
        self.credentials = CredentialStore(owner, lambda: queue.clock())

    def close(self):
        self.credentials.close()

    async def _credential_scope(self, params):
        async with read_transaction(self.settings.runtime, self.owner) as db:
            if params.job_id is not None:
                async with db.execute("SELECT url FROM downloads WHERE id=?", (params.job_id,)) as cursor:
                    job = await cursor.fetchone()
                if job is None:
                    raise ControlError("NOT_FOUND", 404, "The selected download no longer exists")
                if job["url"] != params.url:
                    raise ControlError("AUTH_CONTEXT_SCOPE", 403, "The browser context belongs to different media")

    async def _probe(self, params):
        if self.probes is None:
            raise ControlError("UNSUPPORTED_OPERATION", 404, "Media probing is unavailable in this engine")
        if self.fixture_validator is not None:
            self.fixture_validator({"url": params.url})
        await self._credential_scope(params)
        auth = self.credentials.resolve(params.auth_ref, params.url, params.job_id) if params.auth_ref else None
        try:
            result = (await self.probes.probe(params.url, auth_context=auth)).model_dump()
        except Exception as error:
            decision = decide(error, 0, 1, self.queue.clock())
            failure = next((value for value in nested_errors(error) if isinstance(value, RequestFailure)), None)
            deadline = failure.retry_at if failure is not None and failure.retry_at is not None else decision.retry_at
            details = {"request_failure": decision.code}
            if decision.code == "AUTH_REQUIRED":
                raise ControlError("AUTH_REQUIRED", 401, "Share fresh browser authentication to probe this media") from error
            if decision.code == "ADMISSION_LIMITED":
                raise ControlError("CAPACITY_EXCEEDED", 429, "The local probe or request capacity is busy",
                                   retry_at=deadline, retryable=True, details=details) from error
            if decision.code in ("UPSTREAM_RATE_LIMITED", "UPSTREAM_TRANSIENT"):
                raise ControlError("RATE_LIMITED", 429, "The media server requires a later request",
                                   retry_at=deadline, retryable=True, provenance="upstream", details=details) from error
            if decision.code == "REQUEST_BUDGET_EXHAUSTED":
                raise ControlError("REQUEST_BUDGET_EXHAUSTED", 409, "This probe exhausted its finite request budget") from error
            if decision.code == "TRANSPORT_ERROR":
                raise ControlError("NETWORK_ERROR", 502, "The media request could not be completed",
                                   retry_at=deadline, retryable=True, provenance="upstream") from error
            if decision.code == "UPSTREAM_PERMANENT":
                raise ControlError("HTTP_ERROR", 422, "The media server rejected this request", provenance="upstream") from error
            raise ControlError("UNSUPPORTED_MEDIA", 422, "The media could not be probed with the supported request lane") from error
        formats, entries = result["formats"] or [], result["entries"] or []
        result.update(probe_id=str(uuid4()), format_count=len(formats), entry_count=len(entries),
                      formats_truncated=len(formats) > params.format_limit, entries_truncated=len(entries) > params.entry_limit)
        result["formats"] = formats[:params.format_limit] if result["formats"] is not None else None
        result["entries"] = entries[:params.entry_limit] if result["entries"] is not None else None
        result["title"] = result["title"][:512]
        if result["uploader"] is not None:
            result["uploader"] = result["uploader"][:512]
        for entry in result["entries"] or []:
            entry["title"] = entry["title"][:512]
        subtitles = result["subtitles"]
        if subtitles is not None:
            result["subtitles"] = {name: values[:8] for name, values in list(subtitles.items())[:32]}
        return result

    async def _action(self, request, params, action):
        if action == "cancel" and params.auth_ref is not None:
            raise ControlError("BAD_REQUEST", 422, "Cancellation does not accept a browser context")
        accepted = ControlAction.from_request(request, params, action)
        if action == "cancel":
            return await self.queue.cancel(params.job_id, control_action=accepted)
        loader = None
        if params.auth_ref is not None:
            loader = lambda url, job: self.credentials.resolve(params.auth_ref, url, job)
        return await self.queue.retry(params.job_id, expected_status="waiting_for_auth" if action == "authenticate" else None,
                                      control_action=accepted, credential_loader=loader)

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
            result = Capabilities(fixture_only=self.fixture_validator is not None, instance_id=self.owner.id,
                                  features=["settings.revisions", "downloads.idempotency", "downloads.source", "events.snapshots", "events.replay",
                                            "downloads.actions", "downloads.options", "auth.scoped", "probes.http"],
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
        elif request.operation == "probes.create":
            result = await self._probe(params)
        elif request.operation == "auth.create":
            await self._credential_scope(params)
            result = self.credentials.create(params, request.idempotency_key)
        elif request.operation == "downloads.action":
            result = await self._action(request, params, params.action)
        elif request.operation == "downloads.authenticate":
            result = await self._action(request, params, "authenticate")
        else:
            if self.fixture_validator is not None:
                self.fixture_validator({"url": params.url})
            defaults = await self.settings.get_all()
            # Keep the original six-operation enqueue identity when all added
            # fields have defaults, so schema-9 receipts survive this extension.
            semantic = params.model_dump(exclude={"options", "auth_ref"})
            selected_options = params.options.model_dump(exclude_defaults=True)
            if selected_options:
                semantic["options"] = selected_options
            if params.auth_ref is not None:
                semantic["auth_ref"] = params.auth_ref
            intent = {"protocol_version": PROTOCOL_VERSION, "operation": request.operation, "params": semantic}
            def credentials():
                if defaults.get("custom_ytdlp_args"):
                    raise ControlError("UNSUPPORTED_OPTION", 422, "Clear executable legacy arguments before using this contract")
                return self.credentials.resolve(params.auth_ref, params.url) if params.auth_ref else None
            # Legacy and v1 receipts have separate key domains; both retain P03's
            # atomic owner-fenced admission, tombstones and joined cancellation.
            key = "v1." + hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            job_id = await self.queue.enqueue(
                params.url, params.format_id or defaults["default_format"], title=params.title,
                thumbnail_url=params.thumbnail_url, audio_only=params.audio_only, audio_format=params.audio_format,
                idempotency_key=key, idempotency_intent=intent, credential_loader=credentials,
                requires_auth=params.auth_ref is not None, **params.options.model_dump(),
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
