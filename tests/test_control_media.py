"""Ordinary control requests retain durable actions and volatile credentials."""

import asyncio
import inspect
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.support import OfflineTestCase


URL = "https://fixture.invalid/media.mp4"
COOKIES = [{"domain": ".fixture.invalid", "path": "/", "name": "session",
            "value": "control-secret", "secure": True, "expirationDate": 1900000000}]


class ControlMediaTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.engine import create_engine_app
        self.now = 1800000000
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        self.addCleanup(self.assertEqual, self.loop_errors, [])
        self.probe_calls = []

        def probe(url, runtime, *, auth_context=None):
            self.probe_calls.append((url, auth_context))
            return {"title": "Fixture", "duration": 12, "formats": [
                {"format_id": "one", "ext": "mp4", "resolution": "720p"}]}

        self.engine = create_engine_app(
            self.runtime(), token="a" * 64, port=43210,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            media_mode="ordinary", probe_runner=probe, clock=lambda: self.now, start_queue=False,
        )
        self.application = self.engine.app
        await self.enterAsyncContext(self.application.router.lifespan_context(self.application))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.engine), base_url="http://127.0.0.1:43210",
            headers={"Authorization": "Bearer " + "a" * 64},
        ))
        self.number = 0

    async def send(self, operation, params=None, *, key=None):
        self.number += 1
        request = {"protocol_version": 1, "request_id": f"media-{self.number}",
                   "operation": operation, "params": params or {}}
        if key is not None:
            request["idempotency_key"] = key
        return await self.client.post("/v1/requests", json=request)

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        return response.json()["data"]

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], code)
        self.assertEqual(result["request_id"], result["error"]["request_id"])
        self.assertNotIn("control-secret", response.text)
        return result["error"]

    def rows(self, sql, values=()):
        with closing(sqlite3.connect(self.application.state.runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.application.state.runtime.db_path)) as db, db:
            db.execute(sql, values)

    def revision(self, job):
        return self.rows("SELECT revision FROM downloads WHERE id=?", (job,))[0][0]

    async def job(self, *, waiting=False):
        job = self.ok(await self.send("downloads.enqueue", {"url": URL}, key=f"job-{self.number}"))["job_id"]
        if waiting:
            self.execute("UPDATE downloads SET status='waiting_for_auth', auth_required=1 WHERE id=?", (job,))
            self.application.state.queue.in_memory_progress[job]["status"] = "waiting_for_auth"
        return job

    async def authentication(self, *, key="auth-one", job=None, url=URL, **extra):
        params = {"instance_id": self.application.state.owner.id, "url": url, "cookies": COOKIES, **extra}
        if job is not None:
            params["job_id"] = job
        return params, self.ok(await self.send("auth.create", params, key=key))

    async def test_probe_accepts_ordinary_media_and_reuses_coalesced_metadata(self):
        one = self.ok(await self.send("probes.create", {"url": URL}))
        two = self.ok(await self.send("probes.create", {"url": URL}))
        self.assertEqual(one["url"], URL)
        self.assertEqual(one["formats"][0]["format_id"], "one")
        self.assertEqual(one["title"], two["title"])
        self.assertEqual(len(self.probe_calls), 1)

    async def test_explicit_fresh_public_probe_bypasses_success_cache(self):
        self.ok(await self.send("probes.create", {"url": URL}))
        self.ok(await self.send("probes.create", {"url": URL, "fresh": True}))
        self.assertEqual(len(self.probe_calls), 2)
        self.ok(await self.send("probes.create", {"url": URL}))
        self.assertEqual(len(self.probe_calls), 2)

    async def test_expired_format_probe_creates_no_job_or_enqueue_receipt(self):
        probe = self.ok(await self.send("probes.create", {"url": URL}))
        self.now += 301
        self.error(await self.send("downloads.enqueue", {"url": URL, "format_id": "one", "probe_id": probe["probe_id"]},
                                   key="expired-format"), 410, "PROBE_EXPIRED")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM enqueue_receipts"), [(0,)])

    async def test_format_probe_receipt_replay_precedes_expiry_and_does_not_wake_execution(self):
        probe = self.ok(await self.send("probes.create", {"url": URL}))
        params = {"url": URL, "format_id": "one", "probe_id": probe["probe_id"]}
        first = self.ok(await self.send("downloads.enqueue", params, key="format-replay"))
        self.now += 301
        self.application.state.queue.wakeup_event.clear()
        repeated = self.ok(await self.send("downloads.enqueue", params, key="format-replay"))
        self.assertEqual(first, repeated)
        self.assertFalse(self.application.state.queue.wakeup_event.is_set())
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(1,)])

    async def test_format_probe_is_bound_to_literal_url_and_credential_identity(self):
        probe = self.ok(await self.send("probes.create", {"url": URL}))
        self.error(await self.send("downloads.enqueue", {"url": URL + "?new-signature=1", "format_id": "one",
                         "probe_id": probe["probe_id"]}, key="wrong-probe-url"), 403, "PROBE_CONTEXT_SCOPE")
        _, context = await self.authentication()
        self.error(await self.send("downloads.enqueue", {"url": URL, "format_id": "one", "probe_id": probe["probe_id"],
                         "auth_ref": context["reference"]}, key="wrong-probe-auth"), 403, "PROBE_CONTEXT_SCOPE")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(0,)])

    async def test_probe_display_metadata_preserves_unicode_without_changing_filename_policy(self):
        title = 'বাংলা "title" / display'
        self.application.state.probes.runner = lambda *_: {"title": title, "uploader": "বাংলা author", "formats": []}
        result = self.ok(await self.send("probes.create", {"url": URL}))
        self.assertEqual(result["title"], title)
        self.assertEqual(result["uploader"], "বাংলা author")

    async def test_header_context_without_cookies_is_shared_by_probe_and_execution(self):
        params = {"instance_id": self.application.state.owner.id, "url": URL, "cookies": [],
                  "referer": "https://player.fixture.invalid/watch?source=one", "user_agent": "Fixture Browser"}
        context = self.ok(await self.send("auth.create", params, key="header-context"))
        self.ok(await self.send("probes.create", {"url": URL, "auth_ref": context["reference"]}))
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": context["reference"]}, key="header-job"))["job_id"]
        self.assertEqual(self.probe_calls[0][1]["referer"], params["referer"])
        queue = self.application.state.queue
        observed = {}
        def download(**options):
            observed.update(options)
            output = Path(options["download_dir"]) / "header-media.mp4"
            output.write_bytes(b"fixture media")
            return {"filepath": str(output)}
        from backend.engine.execution import ThreadDownloadExecutor
        queue.download_executor = ThreadDownloadExecutor(download)
        await queue._run_task(job, URL, "best", await self.application.state.settings.get_all(), {})
        self.assertEqual(observed["referer"], params["referer"])
        self.assertIsNone(observed["cookies_file_path"])
        persisted = self.rows("SELECT download_options FROM downloads WHERE id=?", (job,))[0][0]
        self.assertNotIn(params["referer"], persisted)
        self.assertNotIn(params["user_agent"], persisted)

    async def test_header_context_missing_after_restart_waits_for_explicit_refresh(self):
        params = {"instance_id": self.application.state.owner.id, "url": URL, "cookies": [],
                  "referer": "https://player.fixture.invalid/watch"}
        reference = self.ok(await self.send("auth.create", params, key="headers-before"))["reference"]
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": reference}, key="header-recovery"))["job_id"]
        queue = self.application.state.queue
        queue.task_headers.clear()
        self.assertIsNone(await queue._claim(job))
        self.assertEqual(self.rows("SELECT status,error_code,attempt_count FROM downloads WHERE id=?", (job,)),
                         [("waiting_for_auth", "BROWSER_CONTEXT_REQUIRED", 0)])
        fresh = self.ok(await self.send("auth.create", {**params, "job_id": job}, key="headers-after"))["reference"]
        result = self.ok(await self.send("downloads.authenticate", {"job_id": job,
                         "expected_revision": self.revision(job), "auth_ref": fresh}, key="refresh-headers"))
        self.assertEqual(result["accepted_status"], "queued")
        self.assertEqual(queue.task_headers[job]["referer"], params["referer"])

    async def test_header_only_context_cannot_bypass_cookie_authentication(self):
        params, context = await self.authentication()
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": context["reference"]}, key="cookie-protected"))["job_id"]
        self.execute("UPDATE downloads SET status='waiting_for_auth',error_code='AUTH_REQUIRED' WHERE id=?", (job,))
        headers = self.ok(await self.send("auth.create", {"instance_id": self.application.state.owner.id,
                     "url": URL, "job_id": job, "cookies": [], "referer": URL}, key="empty-fresh"))
        self.error(await self.send("downloads.authenticate", {"job_id": job, "expected_revision": self.revision(job),
                   "auth_ref": headers["reference"]}, key="refuse-empty"), 401, "AUTH_REQUIRED")
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("waiting_for_auth",)])

    async def test_owned_header_worker_auth_failure_reports_cookie_requirement(self):
        from backend.engine.execution import ThreadDownloadExecutor
        from backend.services.resume import FreshAuthRequired

        def fail(**_):
            raise FreshAuthRequired()

        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(fail)
        _, context = await self.authentication(cookies=[], referer=URL)
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": context["reference"]}, key="header-auth-failure"))["job_id"]
        await queue._run_task(job, URL, "best", await self.application.state.settings.get_all(), {})
        self.assertEqual(self.rows("SELECT status,error_code,retry_count FROM downloads WHERE id=?", (job,)),
                         [("waiting_for_auth", "AUTH_REQUIRED", 0)])
        _, fresh = await self.authentication(job=job, key="header-after-403", cookies=[], referer=URL)
        self.error(await self.send("downloads.authenticate", {"job_id": job, "expected_revision": self.revision(job),
                   "auth_ref": fresh["reference"]}, key="refuse-header-after-403"), 401, "AUTH_REQUIRED")

    async def test_cookie_requirement_survives_cancellation_after_header_worker_auth_failure(self):
        from backend.engine.execution import ThreadDownloadExecutor
        from backend.services.resume import FreshAuthRequired

        def fail(**_):
            raise FreshAuthRequired()

        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(fail)
        _, context = await self.authentication(cookies=[], referer=URL)
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": context["reference"]}, key="header-auth-cancel"))["job_id"]
        await queue._run_task(job, URL, "best", await self.application.state.settings.get_all(), {})
        await queue.cancel(job)
        _, fresh = await self.authentication(job=job, key="header-after-cancel", cookies=[], referer=URL)
        self.error(await self.send("downloads.action", {"job_id": job, "expected_revision": self.revision(job),
                   "action": "retry", "auth_ref": fresh["reference"]}, key="refuse-header-after-cancel"), 401, "AUTH_REQUIRED")
        self.assertEqual(self.rows("SELECT status,attempt_count FROM downloads WHERE id=?", (job,)), [("cancelled", 1)])

    async def test_browser_supplied_parent_referrer_is_never_reconstructed_after_recovery(self):
        from backend.engine.execution import ThreadDownloadExecutor
        parent_url = "https://fixture.invalid/playlist"
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(lambda **_: {"playlist": {"entries": [
            {"position": 1, "url": URL, "referer": parent_url}]}})
        _, context = await self.authentication(url=parent_url, cookies=[], referer=parent_url)
        parent = self.ok(await self.send("downloads.enqueue", {"url": parent_url, "options": {"is_playlist": True},
                         "auth_ref": context["reference"]}, key="matching-browser-parent"))["job_id"]
        await queue._run_task(parent, parent_url, "best", await self.application.state.settings.get_all(), {})
        child = self.rows("SELECT id FROM downloads WHERE parent_id=?", (parent,))[0][0]
        queue.task_headers.clear()
        await queue.initialize()
        self.assertEqual(self.rows("SELECT status,error_code,attempt_count FROM downloads WHERE id=?", (child,)),
                         [("waiting_for_auth", "BROWSER_CONTEXT_REQUIRED", 0)])
        self.assertIsNone(await queue._claim(child))
        self.assertNotIn(child, queue.task_headers)

    async def test_playlist_header_context_does_not_treat_zero_port_as_the_default_origin(self):
        from backend.engine.execution import ThreadDownloadExecutor
        parent_url = "https://fixture.invalid/playlist"
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(lambda **_: {"playlist": {"entries": [
            {"position": 1, "url": "https://fixture.invalid:0/child"}]}})
        _, context = await self.authentication(url=parent_url, cookies=[], referer=parent_url)
        parent = self.ok(await self.send("downloads.enqueue", {"url": parent_url, "options": {"is_playlist": True},
                         "auth_ref": context["reference"]}, key="zero-port-playlist"))["job_id"]
        await queue._run_task(parent, parent_url, "best", await self.application.state.settings.get_all(), {})
        child = self.rows("SELECT id FROM downloads WHERE parent_id=?", (parent,))[0][0]
        self.assertNotIn(child, queue.task_headers)
        self.assertIsNone(await queue._claim(child))
        self.assertEqual(self.rows("SELECT error_code FROM downloads WHERE id=?", (child,)), [("BROWSER_CONTEXT_REQUIRED",)])

    async def test_header_playlist_expansion_and_retry_keep_context_volatile_and_origin_scoped(self):
        from backend.engine.execution import ThreadDownloadExecutor
        parent_url = "https://fixture.invalid/playlist"
        source = "https://player.fixture.invalid/private?token=playlist-header-secret"
        entries = [{"position": 1, "url": "https://fixture.invalid/one"},
                   {"position": 2, "url": "https://fixture.invalid:8443/two"}]
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(lambda **_: {"playlist": {"entries": entries}})
        _, context = await self.authentication(url=parent_url, cookies=[], referer=source)
        parent = self.ok(await self.send("downloads.enqueue", {"url": parent_url,
            "options": {"is_playlist": True}, "auth_ref": context["reference"]}, key="header-playlist"))["job_id"]
        await queue._run_task(parent, parent_url, "best", await self.application.state.settings.get_all(), {})
        children = self.rows("SELECT id,url,download_options FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,))
        self.assertEqual(len(children), 2)
        same, foreign = [child[0] for child in children]
        self.assertEqual(queue.task_headers.get(same, {}).get("referer"), source)
        self.assertNotIn(foreign, queue.task_headers)
        self.assertNotIn(parent, queue.task_headers)
        self.assertFalse(any("playlist-header-secret" in child[2] for child in children))
        self.assertTrue(all(json.loads(child[2])["browser_context_requires_cookies"] is False for child in children))
        self.assertIsNone(await queue._claim(foreign))
        self.assertEqual(self.rows("SELECT status,error_code FROM downloads WHERE id=?", (foreign,)),
                         [("waiting_for_auth", "BROWSER_CONTEXT_REQUIRED")])
        await queue.cancel(parent)
        _, fresh = await self.authentication(key="fresh-playlist-headers", url=parent_url, job=parent, cookies=[], referer=source + "-fresh")
        params = {"job_id": parent, "expected_revision": self.revision(parent), "action": "retry", "auth_ref": fresh["reference"]}
        accepted = self.ok(await self.send("downloads.action", params, key="retry-playlist-headers"))
        self.assertEqual(queue.task_headers.get(same, {}).get("referer"), source + "-fresh")
        self.assertNotIn(foreign, queue.task_headers)
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE parent_id=? ORDER BY playlist_index", (parent,)),
                         [("queued",), ("waiting_for_auth",)])
        queue.task_headers.clear()
        queue.wakeup_event.clear()
        self.now += 301
        self.assertEqual(self.ok(await self.send("downloads.action", params, key="retry-playlist-headers")), accepted)
        self.assertEqual(queue.task_headers, {})
        self.assertFalse(queue.wakeup_event.is_set(), "Receipt replay must not reattach or execute expired credentials")

    async def test_extracted_playlist_referrer_is_volatile_and_restart_requires_refresh(self):
        from backend.engine.execution import ThreadDownloadExecutor
        source = "https://fixture.invalid/private?token=extracted-header-secret"
        entries = [{"position": 1, "url": URL, "referer": source}]
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(lambda **_: {"playlist": {"entries": entries}})
        parent = self.ok(await self.send("downloads.enqueue", {"url": URL, "options": {"is_playlist": True}}, key="extracted-headers"))["job_id"]
        await queue._run_task(parent, URL, "best", await self.application.state.settings.get_all(), {})
        child, options, required = self.rows("SELECT id,download_options,auth_required FROM downloads WHERE parent_id=?", (parent,))[0]
        self.assertNotIn("extracted-header-secret", options)
        self.assertTrue(required)
        self.assertIs(json.loads(options)["browser_context_requires_cookies"], False)
        self.assertEqual(queue.task_headers.get(child, {}).get("referer"), source)
        queue.task_headers.clear()
        self.assertIsNone(await queue._claim(child))
        self.assertEqual(self.rows("SELECT error_code FROM downloads WHERE id=?", (child,)), [("BROWSER_CONTEXT_REQUIRED",)])
        _, context = await self.authentication(job=child, key="refresh-extracted-headers", cookies=[], referer=source)
        self.ok(await self.send("downloads.authenticate", {"job_id": child, "expected_revision": self.revision(child),
            "auth_ref": context["reference"]}, key="authenticate-extracted-child"))
        self.assertEqual(queue.task_headers[child]["referer"], source)

    async def test_playlist_cookie_context_does_not_cross_into_a_matching_cookie_subdomain(self):
        from backend.engine.execution import ThreadDownloadExecutor
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(lambda **_: {"playlist": {"entries": [
            {"position": 1, "url": "https://cdn.fixture.invalid/child.mp4"}]}})
        _, context = await self.authentication()
        parent = self.ok(await self.send("downloads.enqueue", {"url": URL, "options": {"is_playlist": True},
            "auth_ref": context["reference"]}, key="cookie-origin-playlist"))["job_id"]
        await queue._run_task(parent, URL, "best", await self.application.state.settings.get_all(), {})
        child = self.rows("SELECT id FROM downloads WHERE parent_id=?", (parent,))[0][0]
        self.assertNotIn(child, queue.task_cookies)
        self.assertNotIn(child, queue.task_headers)
        self.assertIsNone(await queue._claim(child))
        self.assertEqual(self.rows("SELECT error_code FROM downloads WHERE id=?", (child,)), [("AUTH_REQUIRED",)])

    async def test_completely_empty_context_is_rejected_and_not_retained(self):
        self.error(await self.send("auth.create", {"instance_id": self.application.state.owner.id,
                   "url": URL, "cookies": []}, key="empty-context"), 422, "BAD_REQUEST")
        self.assertEqual(self.application.state.control.credentials.contexts, {})

    async def test_header_retry_missing_context_reports_the_source_refresh_action(self):
        _, context = await self.authentication(cookies=[], referer="https://player.fixture.invalid/")
        job = self.ok(await self.send("downloads.enqueue", {"url": URL, "auth_ref": context["reference"]}, key="header-only-retry"))["job_id"]
        await self.application.state.queue.cancel(job)
        error = self.error(await self.send("downloads.action", {"job_id": job, "expected_revision": self.revision(job),
            "action": "retry"}, key="header-without-context"), 401, "BROWSER_CONTEXT_REQUIRED")
        self.assertIn("source", error["message"])

    async def test_attempt_error_redacts_diagnostics_without_changing_the_job_source(self):
        from backend.engine.execution import ThreadDownloadExecutor
        source = URL + "?signature=diagnostic-private-query"
        queue = self.application.state.queue
        def fail(**_):
            raise ValueError("Rejected " + source + "\nCookie: diagnostic-private-cookie")
        queue.download_executor = ThreadDownloadExecutor(fail)
        job = self.ok(await self.send("downloads.enqueue", {"url": source}, key="private-diagnostics"))["job_id"]
        with self.assertLogs("vidgrab.queue_manager", level="WARNING") as captured:
            await queue._run_task(job, source, "best", await self.application.state.settings.get_all(), {})
        persisted_url, message = self.rows("SELECT url,error_message FROM downloads WHERE id=?", (job,))[0]
        self.assertEqual(persisted_url, source)
        for secret in ("diagnostic-private-query", "diagnostic-private-cookie"):
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, "\n".join(captured.output))

    async def test_job_pages_retain_the_exact_source_for_scoped_authentication(self):
        capabilities = self.ok(await self.send("capabilities.get"))
        self.assertIn("downloads.source", capabilities["features"])
        url = "https://fixture.invalid/মিডিয়া.mp4?token=literal%2Bvalue"
        job = self.ok(await self.send("downloads.enqueue", {"url": url}, key="literal-source"))["job_id"]
        for operation in ("downloads.list", "events.read"):
            with self.subTest(operation=operation):
                result = self.ok(await self.send(operation))
                page = result["snapshot"] if operation == "events.read" else result
                self.assertEqual(page["jobs"][0]["id"], job)
                self.assertEqual(page["jobs"][0].get("url"), url)

    async def test_probe_reports_bounded_format_pages(self):
        self.application.state.probes.runner = lambda *_: {"title": "Many", "formats": [
            {"format_id": str(index), "ext": "mp4"} for index in range(100)]}
        result = self.ok(await self.send("probes.create", {"url": URL, "format_limit": 3}))
        self.assertEqual(len(result["formats"]), 3)
        self.assertEqual(result["format_count"], 100)
        self.assertTrue(result["formats_truncated"])

    async def test_probe_errors_preserve_upstream_deadline_without_secrets(self):
        from backend.services.retry_policy import RequestFailure
        deadline = self.now + 600
        def rate_limited(*_, **kwargs):
            raise RequestFailure("UPSTREAM_RATE_LIMITED", retry_at=deadline)
        self.application.state.probes.runner = rate_limited
        error = self.error(await self.send("probes.create", {"url": URL}), 429, "RATE_LIMITED")
        self.assertEqual(error["retry_at"], deadline)
        self.assertEqual(error["provenance"], "upstream")

    async def test_probe_admission_failure_is_distinct_from_upstream_throttling(self):
        from backend.services.retry_policy import RequestFailure
        def limited(*_, **kwargs):
            raise RequestFailure("ADMISSION_LIMITED", retry_at=self.now + 1, consumes_retry=False)
        self.application.state.probes.runner = limited
        error = self.error(await self.send("probes.create", {"url": URL}), 429, "CAPACITY_EXCEEDED")
        self.assertEqual(error["provenance"], "local")
        self.assertEqual(error["retry_at"], self.now + 1)

    async def test_probe_rejects_unsafe_schemes_inline_credentials_and_arbitrary_options(self):
        for params in ({"url": "file:///private/media"}, {"url": "ftp://fixture.invalid/media"},
                       {"url": "https://user:password@fixture.invalid/media"},
                       {"url": URL, "cookies": COOKIES}, {"url": URL, "headers": {"Authorization": "secret"}},
                       {"url": URL, "format_limit": True}):
            with self.subTest(params=list(params)):
                self.error(await self.send("probes.create", params), 422, "BAD_REQUEST")
        self.assertEqual(self.probe_calls, [])

    async def test_authentication_is_volatile_and_replay_does_not_extend_its_deadline(self):
        params, context = await self.authentication(ttl_seconds=60)
        self.now += 20
        replay = self.ok(await self.send("auth.create", params, key="auth-one"))
        self.assertEqual(replay, context)
        with closing(sqlite3.connect(self.application.state.runtime.db_path)) as db:
            persisted = "\n".join(db.iterdump())
        self.assertNotIn("control-secret", persisted)
        self.assertNotIn(context["reference"], persisted)
        self.assertEqual(list(self.application.state.runtime.data_dir.rglob("cookies-*")), [])

    async def test_changed_credential_payload_cannot_replace_a_keyed_context(self):
        params, _ = await self.authentication()
        changed = {**params, "cookies": [{**COOKIES[0], "value": "replacement-secret"}]}
        self.error(await self.send("auth.create", changed, key="auth-one"), 409, "IDEMPOTENCY_CONFLICT")

    async def test_expired_context_and_key_cannot_be_revived(self):
        params, context = await self.authentication(ttl_seconds=30)
        self.now += 31
        self.error(await self.send("auth.create", params, key="auth-one"), 410, "AUTH_CONTEXT_EXPIRED")
        self.error(await self.send("probes.create", {"url": URL, "auth_ref": context["reference"]}),
                   410, "AUTH_CONTEXT_EXPIRED")
        _, fresh = await self.authentication(key="auth-two")
        self.assertNotEqual(fresh["reference"], context["reference"])

    async def test_old_engine_instance_cannot_install_credentials(self):
        self.error(await self.send("auth.create", {"instance_id": "previous-owner", "url": URL, "cookies": COOKIES},
                                   key="old-owner"), 410, "AUTH_CONTEXT_EXPIRED")

    async def test_missing_expired_wrong_domain_and_injected_cookie_rows_are_rejected(self):
        for cookies in ([], [{**COOKIES[0], "expirationDate": 1}], [{**COOKIES[0], "domain": ".other.invalid"}],
                        [{**COOKIES[0], "value": "control-secret\nforged-row"}],
                        [{**COOKIES[0], "secure": "true"}]):
            with self.subTest(cookies=bool(cookies)):
                self.error(await self.send("auth.create", {"instance_id": self.application.state.owner.id,
                    "url": URL, "cookies": cookies}, key=f"invalid-{self.number}"), 422, "BAD_REQUEST")

    async def test_headers_are_bounded_and_reject_row_injection_and_embedded_credentials(self):
        for extra in ({"user_agent": "Agent\r\nCookie: injected"},
                      {"referer": "https://user:secret@fixture.invalid/"},
                      {"headers": {"Authorization": "secret"}}):
            self.error(await self.send("auth.create", {"instance_id": self.application.state.owner.id,
                "url": URL, "cookies": COOKIES, **extra}, key=f"headers-{self.number}"), 422, "BAD_REQUEST")

    async def test_probe_credential_scope_is_exact_and_cache_does_not_cross_contexts(self):
        _, context = await self.authentication(user_agent="Fixture Agent", referer="https://fixture.invalid/page")
        self.ok(await self.send("probes.create", {"url": URL}))
        self.ok(await self.send("probes.create", {"url": URL, "auth_ref": context["reference"]}))
        self.assertEqual(len(self.probe_calls), 2)
        auth = self.probe_calls[-1][1]
        self.assertEqual(auth["cookies"], COOKIES)
        self.assertEqual(auth["user_agent"], "Fixture Agent")
        self.error(await self.send("probes.create", {"url": URL + "?different", "auth_ref": context["reference"]}),
                   403, "AUTH_CONTEXT_SCOPE")

    async def test_allowlisted_download_options_are_admitted_and_arbitrary_options_are_rejected(self):
        result = self.ok(await self.send("downloads.enqueue", {"url": URL, "options": {
            "download_subtitles": True, "subtitle_langs": ["en"], "embed_subtitles": True,
            "embed_thumbnail": True}}, key="media-options"))
        options = json.loads(self.rows("SELECT download_options FROM downloads WHERE id=?", (result["job_id"],))[0][0])
        self.assertTrue(options["download_subtitles"])
        self.assertEqual(options["subtitle_langs"], ["en"])
        for unsupported in ("extra_args", "output_template", "download_dir", "external_downloader", "cookies"):
            self.error(await self.send("downloads.enqueue", {"url": URL, "options": {unsupported: "blocked"}},
                                       key=f"unsupported-{unsupported}"), 422, "BAD_REQUEST")

    async def test_authenticated_enqueue_replay_ignores_expired_context_and_does_not_reinstall_cookies(self):
        _, context = await self.authentication(ttl_seconds=30)
        params = {"url": URL, "auth_ref": context["reference"]}
        result = self.ok(await self.send("downloads.enqueue", params, key="authenticated-enqueue"))
        job = result["job_id"]
        queue = self.application.state.queue
        self.assertEqual(queue.task_cookies[job], COOKIES)
        queue.task_cookies.pop(job)
        queue.wakeup_event.clear()
        self.now += 31
        self.assertEqual(self.ok(await self.send("downloads.enqueue", params, key="authenticated-enqueue")), result)
        self.assertNotIn(job, queue.task_cookies)
        self.assertFalse(queue.wakeup_event.is_set())
        self.assertNotIn(context["reference"], self.rows("SELECT download_options FROM downloads WHERE id=?", (job,))[0][0])

    async def test_invalid_credential_reference_creates_no_download_or_receipt(self):
        self.error(await self.send("downloads.enqueue", {"url": URL, "auth_ref": "missing"}, key="missing-context"),
                   410, "AUTH_CONTEXT_EXPIRED")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM enqueue_receipts"), [(0,)])

    async def test_authentication_refresh_replay_cannot_revive_a_later_cancellation(self):
        job = await self.job(waiting=True)
        _, context = await self.authentication(job=job, ttl_seconds=30)
        params = {"job_id": job, "expected_revision": self.revision(job), "auth_ref": context["reference"]}
        accepted = self.ok(await self.send("downloads.authenticate", params, key="fresh-auth"))
        self.assertEqual(accepted["accepted_status"], "queued")
        await self.application.state.queue.cancel(job)
        self.now += 31
        queue = self.application.state.queue
        queue.wakeup_event.clear()
        replay = self.ok(await self.send("downloads.authenticate", params, key="fresh-auth"))
        self.assertEqual(replay, accepted)
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("cancelled",)])
        self.assertNotIn(job, queue.task_cookies)
        self.assertFalse(queue.wakeup_event.is_set())

    async def test_stale_authentication_refresh_cannot_attach_credentials_after_cancellation(self):
        job = await self.job(waiting=True)
        _, context = await self.authentication(job=job)
        revision = self.revision(job)
        await self.application.state.queue.cancel(job)
        self.error(await self.send("downloads.authenticate", {"job_id": job, "expected_revision": revision,
            "auth_ref": context["reference"]}, key="stale-auth"), 409, "REVISION_CONFLICT")
        self.assertNotIn(job, self.application.state.queue.task_cookies)

    async def test_job_scoped_credentials_cannot_refresh_another_job(self):
        one, two = await self.job(waiting=True), await self.job(waiting=True)
        _, context = await self.authentication(job=one)
        self.error(await self.send("downloads.authenticate", {"job_id": two, "expected_revision": self.revision(two),
            "auth_ref": context["reference"]}, key="wrong-job"), 403, "AUTH_CONTEXT_SCOPE")
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (two,)), [("waiting_for_auth",)])

    async def test_keyed_retry_does_not_repeat_after_the_job_fails_again(self):
        job = await self.job()
        self.execute("UPDATE downloads SET status='failed' WHERE id=?", (job,))
        params = {"job_id": job, "expected_revision": self.revision(job), "action": "retry"}
        accepted = self.ok(await self.send("downloads.action", params, key="retry-once"))
        self.execute("UPDATE downloads SET status='failed' WHERE id=?", (job,))
        queue = self.application.state.queue
        queue.wakeup_event.clear()
        self.assertEqual(self.ok(await self.send("downloads.action", params, key="retry-once")), accepted)
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("failed",)])
        self.assertFalse(queue.wakeup_event.is_set())

    async def test_changed_action_key_conflicts_without_mutating_current_state(self):
        job = await self.job()
        params = {"job_id": job, "expected_revision": self.revision(job), "action": "cancel"}
        self.ok(await self.send("downloads.action", params, key="one-action"))
        self.error(await self.send("downloads.action", {**params, "action": "retry"}, key="one-action"),
                   409, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("cancelled",)])

    async def test_actions_validate_revision_state_and_missing_jobs(self):
        job = await self.job()
        self.error(await self.send("downloads.action", {"job_id": job, "expected_revision": self.revision(job),
            "action": "retry"}, key="active-retry"), 409, "STATE_CONFLICT")
        self.error(await self.send("downloads.action", {"job_id": "missing", "expected_revision": 1,
            "action": "cancel"}, key="missing-action"), 404, "NOT_FOUND")
        self.error(await self.send("downloads.action", {"job_id": job, "expected_revision": True,
            "action": "cancel"}, key="bad-revision"), 422, "BAD_REQUEST")

    async def test_refresh_delivers_joined_attempt_owned_cookies_and_volatile_headers(self):
        from backend.engine.execution import ThreadDownloadExecutor
        observed = {}
        def download(**options):
            observed.update(options)
            self.assertIn("control-secret", Path(options["cookies_file_path"]).read_text())
            output = Path(options["download_dir"]) / "complete.mp4"
            output.write_bytes(b"media")
            return {"filepath": str(output)}
        queue = self.application.state.queue
        queue.download_executor = ThreadDownloadExecutor(download)
        job = await self.job(waiting=True)
        _, context = await self.authentication(job=job, user_agent="Fixture Agent", referer="https://fixture.invalid/page")
        self.ok(await self.send("downloads.authenticate", {"job_id": job, "expected_revision": self.revision(job),
            "auth_ref": context["reference"]}, key="owned-refresh"))
        await queue._run_task(job, URL, "best", await self.application.state.settings.get_all(), {})
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("complete",)])
        self.assertEqual(observed["user_agent"], "Fixture Agent")
        self.assertEqual(observed["referer"], "https://fixture.invalid/page")
        self.assertFalse(Path(observed["cookies_file_path"]).exists())
        self.assertNotIn(job, queue.task_cookies)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
        durable = self.rows("SELECT download_options FROM downloads WHERE id=?", (job,))[0][0]
        self.assertNotIn("control-secret", durable)
        self.assertNotIn("https://fixture.invalid/page", durable)

    async def test_retry_commit_notification_remains_joined_through_repeated_cancellation(self):
        from backend.api.models import Request
        job = await self.job()
        self.execute("UPDATE downloads SET status='failed' WHERE id=?", (job,))
        request = Request.model_validate({"protocol_version": 1, "request_id": "joined-action",
            "operation": "downloads.action", "idempotency_key": "joined-action",
            "params": {"job_id": job, "expected_revision": self.revision(job), "action": "retry"}})
        started, release = asyncio.Event(), asyncio.Event()
        broadcasts = []
        async def broadcast(message):
            broadcasts.append(message["event"])
            started.set()
            await release.wait()
        with patch.object(self.application.state.connections, "broadcast", broadcast):
            task = asyncio.create_task(self.application.state.control.dispatch(request))
            try:
                done, _ = await asyncio.wait({task, asyncio.create_task(started.wait())}, timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    self.fail(f"The action did not reach its notification boundary: {task.exception()}")
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(len(broadcasts), 1)
            replay = await self.application.state.control.dispatch(request)
            self.assertEqual(replay["accepted_status"], "queued")
            self.assertEqual(len(broadcasts), 1)

    async def test_explicit_ordinary_engine_mode_preserves_fixture_diagnostics(self):
        from backend.engine.application import create_engine_app
        self.assertIn("media_mode", inspect.signature(create_engine_app).parameters,
                      "Ordinary execution must be an explicit engine boundary")
        ordinary = create_engine_app(self.runtime("ordinary"), token="b" * 64, port=43211,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            media_mode="ordinary", start_queue=False)
        self.assertFalse(ordinary.app.state.queue.download_executor.fixture_only)
        fixture = create_engine_app(self.runtime("fixture"), token="c" * 64, port=43212,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            start_queue=False)
        self.assertTrue(fixture.app.state.queue.download_executor.fixture_only)

    async def test_real_probe_confines_cookie_and_referer_headers_across_origins(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from backend.format_probe import _run_probe
        initial, redirected = [], []
        class Media(BaseHTTPRequestHandler):
            def do_GET(self):
                redirected.append(dict(self.headers))
                body = b"controlled media"
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        media = ThreadingHTTPServer(("127.0.0.1", 0), Media)
        destination = f"http://127.0.0.1:{media.server_port}/media.mp4"
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                initial.append(dict(self.headers))
                self.send_response(302)
                self.send_header("Location", destination)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *_):
                pass
        first = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        servers = [first, media]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            url = f"http://127.0.0.1:{first.server_port}/media.mp4"
            params = {"instance_id": self.application.state.owner.id, "url": url,
                "cookies": [{**COOKIES[0], "domain": "127.0.0.1", "secure": False}],
                "referer": f"http://127.0.0.1:{first.server_port}/private-page?token=referrer-secret"}
            context = self.ok(await self.send("auth.create", params, key="wire-scope"))
            self.application.state.probes.runner = _run_probe
            self.ok(await self.send("probes.create", {"url": url, "auth_ref": context["reference"]}))
            self.assertTrue(initial)
            self.assertTrue(redirected)
            self.assertTrue(any("control-secret" in headers.get("Cookie", "") for headers in initial))
            self.assertTrue(any("referrer-secret" in headers.get("Referer", "") for headers in initial))
            self.assertFalse(any("control-secret" in headers.get("Cookie", "") for headers in redirected),
                             "Browser cookies must stay inside their selected origin")
            self.assertFalse(any("referrer-secret" in headers.get("Referer", "") for headers in redirected),
                             "A supplied page referrer must not cross into another origin")
            self.assertEqual(self.rows("SELECT COUNT(*) FROM request_leases"), [(0,)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM request_probes"), [(0,)])
        finally:
            for server in servers:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            for thread in threads:
                await asyncio.to_thread(thread.join, 2)
                self.assertFalse(thread.is_alive())

    async def test_ordinary_control_keeps_legacy_routes_and_websocket_auth_hidden(self):
        self.assertEqual((await self.client.post("/downloads", json={"url": URL, "cookies": COOKIES})).status_code, 404)
        self.assertEqual((await self.client.post("/probe", json={"url": URL})).status_code, 404)
        message = {"protocol_version": 1, "request_id": "unauthorized-auth", "operation": "auth.create",
                   "idempotency_key": "denied", "params": {"url": URL, "cookies": COOKIES}}
        denied = await self.client.post("/v1/requests", json=message, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(denied.status_code, 401)

    async def test_rest_media_adapters_share_authentication_revisions_and_action_receipts(self):
        probe = self.ok(await self.client.post("/v1/probes", json={"url": URL}, headers={"X-Request-ID": "rest-probe"}))
        self.assertEqual(probe["url"], URL)
        job = await self.job(waiting=True)
        context = self.ok(await self.client.post("/v1/auth/contexts", json={"instance_id": self.application.state.owner.id,
            "url": URL, "job_id": job, "cookies": COOKIES}, headers={"Idempotency-Key": "rest-context"}))
        accepted = self.ok(await self.client.post(f"/v1/downloads/{job}/authentication",
            json={"expected_revision": self.revision(job), "auth_ref": context["reference"]},
            headers={"Idempotency-Key": "rest-authentication"}))
        self.assertEqual(accepted["accepted_status"], "queued")
        cancelled = self.ok(await self.client.post(f"/v1/downloads/{job}/actions",
            json={"expected_revision": self.revision(job), "action": "cancel"},
            headers={"Idempotency-Key": "rest-cancel"}))
        self.assertEqual(cancelled["action"], "cancel")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM control_action_receipts"), [(2,)])

    async def test_rest_path_and_body_job_conflict_is_rejected_before_mutation(self):
        one, two = await self.job(), await self.job()
        self.error(await self.client.post(f"/v1/downloads/{one}/actions", json={"job_id": two,
            "expected_revision": self.revision(one), "action": "cancel"}, headers={"Idempotency-Key": "ambiguous-job"}),
            422, "BAD_REQUEST")
        self.assertEqual(self.rows("SELECT status FROM downloads ORDER BY id"), [("queued",), ("queued",)])

    async def test_probe_materializes_numeric_resolution_and_bounds_full_metadata(self):
        self.application.state.probes.runner = lambda *_: {"title": "Numeric", "formats": [
            {"format_id": "1080", "ext": "mp4", "height": 1080}]}
        result = self.ok(await self.send("probes.create", {"url": URL}))
        self.assertEqual(result["formats"][0]["resolution"], "1080")

    async def test_credential_capacity_is_bounded_without_replacing_live_contexts(self):
        with patch("backend.services.credentials.MAX_CONTEXTS", 2):
            one, context = await self.authentication(key="bounded-one")
            await self.authentication(key="bounded-two")
            self.error(await self.send("auth.create", one, key="bounded-three"), 409, "CAPACITY_EXCEEDED")
            self.assertEqual(self.ok(await self.send("auth.create", one, key="bounded-one")), context)

    async def test_action_receipt_failure_rolls_back_job_and_does_not_attach_credentials(self):
        job = await self.job(waiting=True)
        _, context = await self.authentication(job=job)
        queue = self.application.state.queue
        queue.wakeup_event.clear()
        original = queue.control_actions.record
        async def fail_after_receipt(*args):
            await original(*args)
            raise sqlite3.OperationalError("injected action commit failure")
        with patch.object(queue.control_actions, "record", fail_after_receipt):
            self.error(await self.send("downloads.authenticate", {"job_id": job, "expected_revision": self.revision(job),
                "auth_ref": context["reference"]}, key="rollback-auth"), 500, "INTERNAL_ERROR")
        self.assertEqual(self.rows("SELECT status FROM downloads WHERE id=?", (job,)), [("waiting_for_auth",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM control_action_receipts"), [(0,)])
        self.assertNotIn(job, queue.task_cookies)
        self.assertNotIn(job, queue.task_headers)
        self.assertFalse(queue.wakeup_event.is_set())

    async def test_owner_fence_precedes_probe_context_creation_and_download_actions(self):
        job = await self.job()
        self.execute("UPDATE runtime_owners SET released_at='outside-owner' WHERE id=?", (self.application.state.owner.id,))
        try:
            for operation, params, key in (("probes.create", {"url": URL}, None),
                ("auth.create", {"instance_id": self.application.state.owner.id, "url": URL, "cookies": COOKIES}, "fenced-context"),
                ("downloads.action", {"job_id": job, "expected_revision": self.revision(job), "action": "cancel"}, "fenced-action")):
                self.error(await self.send(operation, params, key=key), 503, "ENGINE_UNAVAILABLE")
            self.assertEqual(self.probe_calls, [])
            self.assertEqual(self.application.state.control.credentials.contexts, {})
            self.assertEqual(self.rows("SELECT COUNT(*) FROM control_action_receipts"), [(0,)])
        finally:
            self.execute("UPDATE runtime_owners SET released_at=NULL WHERE id=?", (self.application.state.owner.id,))

    async def test_spike_fixture_keeps_its_per_job_network_guard_in_ordinary_mode(self):
        response = await self.client.post("/spike/fixture", json={"url": "http://127.0.0.1:8080/fixture.wav"})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()["task_id"]
        options = json.loads(self.rows("SELECT download_options FROM downloads WHERE id=?", (job,))[0][0])
        self.assertIs(options.get("fixture_only"), True)

    def isolated_engine(self, runtime):
        from backend.engine.application import create_engine_app
        return create_engine_app(runtime, token="b" * 64, port=43211,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            media_mode="ordinary", start_queue=False, clock=lambda: self.now)

    async def isolated_request(self, engine, operation, params, key):
        from backend.api.models import Request
        return await engine.app.state.control.dispatch(Request.model_validate({"protocol_version": 1,
            "request_id": "isolated", "operation": operation, "params": params, "idempotency_key": key}))

    async def test_schema_nine_upgrade_replays_original_enqueue_and_settings_receipts(self):
        import hashlib
        from backend.db.migrations import SCHEMA_VERSION
        runtime = self.runtime("schema-nine")
        first = self.isolated_engine(runtime)
        old_params = {"url": URL, "format_id": None, "title": None, "thumbnail_url": None,
                      "audio_only": False, "audio_format": "mp3"}
        async with first.app.router.lifespan_context(first.app):
            job = await first.app.state.queue.enqueue(URL, "best",
                idempotency_key="v1." + hashlib.sha256(b"old-enqueue").hexdigest(),
                idempotency_intent={"protocol_version": 1, "operation": "downloads.enqueue", "params": old_params})
            settings_params = {"expected_revision": 0, "patch": {"default_format": "old-format"}}
            original = await self.isolated_request(first, "settings.patch", settings_params, "old-settings")
            later = await self.isolated_request(first, "settings.patch",
                {"expected_revision": original["revision"], "patch": {"default_format": "new-format"}}, "later-settings")
            with closing(sqlite3.connect(runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='complete' WHERE id=?", (job,))
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP TABLE control_action_receipts")
            db.execute("PRAGMA user_version=9")
        second = self.isolated_engine(runtime)
        async with second.app.router.lifespan_context(second.app):
            self.assertEqual(await self.isolated_request(second, "downloads.enqueue", {"url": URL}, "old-enqueue"),
                             {"job_id": job})
            self.assertEqual(await self.isolated_request(second, "settings.patch", settings_params, "old-settings"), original)
            self.assertEqual(await second.app.state.control.events.settings(), later)
            with closing(sqlite3.connect(runtime.db_path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT id,status,format_selected FROM downloads").fetchall(), [(job, "complete", "best")])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM control_action_receipts").fetchone()[0], 0)
        backups = list(runtime.backups_dir.glob("schema-v9-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM enqueue_receipts").fetchone()[0], 1)

    async def test_schema_nine_failed_upgrade_rolls_back_table_and_preserves_bytes(self):
        from backend.config import init_db
        from backend.db import migrations
        runtime = self.runtime("rollback-nine")
        await init_db(runtime)
        with closing(sqlite3.connect(runtime.db_path)) as db, db:
            db.execute("DROP TABLE control_action_receipts")
            db.execute("PRAGMA user_version=9")
        before = runtime.db_path.read_bytes()
        original = migrations._migration_ten
        async def fail_after_table(db):
            await original(db)
            raise sqlite3.OperationalError("injected schema-ten failure")
        with patch.object(migrations, "_migration_ten", fail_after_table):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected schema-ten failure"):
                await init_db(runtime)
        self.assertEqual(runtime.db_path.read_bytes(), before)
        with closing(sqlite3.connect(runtime.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='control_action_receipts'").fetchall(), [])

    async def test_authentication_and_cancel_replay_survive_actual_owner_restart(self):
        from backend.services.control_errors import ControlError
        runtime = self.runtime("action-restart")
        first = self.isolated_engine(runtime)
        async with first.app.router.lifespan_context(first.app):
            job = (await self.isolated_request(first, "downloads.enqueue", {"url": URL}, "restart-job"))["job_id"]
            with closing(sqlite3.connect(runtime.db_path)) as db, db:
                db.execute("UPDATE downloads SET status='waiting_for_auth',auth_required=1 WHERE id=?", (job,))
                revision = db.execute("SELECT revision FROM downloads WHERE id=?", (job,)).fetchone()[0]
            context = await self.isolated_request(first, "auth.create",
                {"instance_id": first.app.state.owner.id, "url": URL, "job_id": job, "cookies": COOKIES}, "restart-context")
            authentication = {"job_id": job, "expected_revision": revision, "auth_ref": context["reference"]}
            accepted = await self.isolated_request(first, "downloads.authenticate", authentication, "restart-authentication")
            cancel = {"job_id": job, "expected_revision": accepted["accepted_revision"], "action": "cancel"}
            cancelled = await self.isolated_request(first, "downloads.action", cancel, "restart-cancel")
            self.assertEqual(cancelled["accepted_status"], "cancelling")
            old_owner = first.app.state.owner.id
        self.assertEqual(first.app.state.control.credentials.contexts, {})
        second = self.isolated_engine(runtime)
        async with second.app.router.lifespan_context(second.app):
            self.assertNotEqual(second.app.state.owner.id, old_owner)
            second.app.state.queue.wakeup_event.clear()
            with patch.object(second.app.state.connections, "broadcast") as broadcast:
                self.assertEqual(await self.isolated_request(second, "downloads.authenticate", authentication, "restart-authentication"), accepted)
                self.assertEqual(await self.isolated_request(second, "downloads.action", cancel, "restart-cancel"), cancelled)
                broadcast.assert_not_called()
            self.assertFalse(second.app.state.queue.wakeup_event.is_set())
            self.assertEqual(second.app.state.queue.task_cookies, {})
            self.assertEqual(second.app.state.queue.task_headers, {})
            with self.assertRaises(ControlError) as error:
                second.app.state.control.credentials.resolve(context["reference"], URL, job)
            self.assertEqual(error.exception.code, "AUTH_CONTEXT_EXPIRED")
            with closing(sqlite3.connect(runtime.db_path)) as db:
                self.assertEqual(db.execute("SELECT status FROM downloads WHERE id=?", (job,)).fetchone()[0], "cancelled")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM control_action_receipts").fetchone()[0], 2)
