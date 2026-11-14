"""Static manifest streams reuse only validated whole-fragment prefixes."""

from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import unittest
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError

from tests.support import OfflineTestCase
from tests import test_resume as resume_tests

NOW = resume_tests.NOW


class Stopped(RuntimeError):
    pass


class FragmentResumeTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    asyncSetUp = resume_tests.ResumeTests.asyncSetUp
    query = resume_tests.ResumeTests.query
    execute = resume_tests.ResumeTests.execute
    attempts = resume_tests.ResumeTests.attempts

    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.fragment_resume"),
                             "Fragment reuse needs a bounded manifest and HTTP validator gate")
        from backend.services import fragment_resume
        return fragment_resume

    def fixture(self, protocol="hls"):
        self.protocol = protocol
        self.parts = {"/a": b"alpha", "/b": b"bravo", "/c": b"charlie"}
        self.tags = {name: '"' + name[1:] + '-secret"' for name in self.parts}
        self.manifest = (b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\na\n#EXTINF:1,\nb\n#EXTINF:1,\nc\n#EXT-X-ENDLIST\n"
                         if protocol == "hls" else b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period/></MPD>')
        self.manifest_path = "/list.m3u8" if protocol == "hls" else "/list.mpd"
        self.requests = []
        self.responses = []
        self.denied = None
        self.head_status = 200
        self.change_get = None
        self.info = {"id": "fixture", "format_id": "audio", "ext": "mp4", "url": "https://fixture.invalid" + self.manifest_path,
                     "protocol": "m3u8_native" if protocol == "hls" else "http_dash_segments"}
        if protocol == "dash":
            self.info.update(manifest_url=self.info["url"], fragment_base_url="https://fixture.invalid/",
                             fragments=[{"path": name[1:]} for name in self.parts])

    def response(self, request):
        path = urlsplit(request.url).path
        self.requests.append((request.method, path))
        body = self.manifest if path == self.manifest_path else self.parts[path]
        tag = '"manifest"' if path == self.manifest_path else self.tags[path]
        if self.change_get == path and request.method == "GET":
            tag = '"changed-after-head"'
        status = 403 if self.denied == path else self.head_status if request.method == "HEAD" else 200
        response = Response(io.BytesIO(b"" if request.method == "HEAD" else body), request.url,
                            {"Content-Length": str(len(body)), "ETag": tag}, status=status)
        self.responses.append(response)
        if status >= 400:
            raise HTTPError(response)
        return response

    def download(self, attempt, stop=None):
        service = self.service()
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "continuedl": False}) as ydl:
            ydl.urlopen = self.response
            cls = service.VerifiedHlsFD if self.protocol == "hls" else service.VerifiedDashFD
            fd = cls(ydl, ydl.params, self.selected_runtime, attempt)
            if stop is not None:
                def interrupt(event):
                    if event.get("downloaded_bytes", 0) >= stop and event.get("fragment_index", 0) > 0:
                        raise Stopped("Fixture interruption")
                fd.add_progress_hook(interrupt)
            with patch("yt_dlp.downloader.hls.FFmpegFD.available", return_value=False):
                return fd.real_download(str(attempt.workspace / "fixture.mp4"), dict(self.info))

    async def interrupted(self, protocol="hls"):
        self.fixture(protocol)
        first = await self.attempts()
        with self.assertRaises(Stopped):
            self.download(first, stop=5)
        record = json.loads(self.query("SELECT record FROM attempt_partials WHERE attempt_id=?", (first.id,))[0][0])
        path = first.workspace / record["name"]
        self.assertEqual(record["size"], 5)
        self.assertEqual(path.read_bytes(), b"alpha")
        await self.store.finish(first.job_id, "queued", NOW, attempt_id=first.id)
        second = await self.store.claim(first.job_id, NOW)
        from backend.services.artifacts import prepare_workspace
        prepare_workspace(self.selected_runtime, second)
        self.requests.clear()
        return first, second, path, record

    async def test_hls_rechecks_manifest_and_all_validators_before_skipping_completed_fragments(self):
        first, second, path, record = await self.interrupted()
        self.assertTrue(self.download(second))
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"alphabravocharlie")
        self.assertIn(("GET", self.manifest_path), self.requests)
        self.assertIn(("HEAD", "/a"), self.requests)
        self.assertNotIn(("GET", "/a"), self.requests)
        self.assertEqual(path.read_bytes(), b"alpha")
        self.assertNotIn("secret", str(self.query("SELECT record FROM attempt_partials")))

    async def test_dash_static_manifest_and_ordered_resolved_fragment_plan_are_bound(self):
        first, second, path, record = await self.interrupted("dash")
        self.assertTrue(self.download(second))
        self.assertNotIn(("GET", "/a"), self.requests)
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"alphabravocharlie")
        self.assertEqual(path.read_bytes(), b"alpha")

    async def test_changed_manifest_restarts_without_reading_the_old_file(self):
        first, second, path, record = await self.interrupted()
        self.manifest = self.manifest.replace(b"TARGETDURATION:1", b"TARGETDURATION:2")
        with patch("backend.services.resume._file", wraps=__import__("backend.services.resume", fromlist=["_file"])._file) as reads:
            self.assertTrue(self.download(second))
            self.assertNotIn(path, [call.args[0] for call in reads.call_args_list])
        self.assertIn(("GET", "/a"), self.requests)
        self.assertEqual(path.read_bytes(), b"alpha")

    async def test_changed_fragment_validator_restarts_the_entire_stream(self):
        first, second, path, record = await self.interrupted()
        self.tags["/a"] = '"new-a"'
        self.parts["/a"] = b"DELTA"
        self.assertTrue(self.download(second))
        self.assertIn(("GET", "/a"), self.requests)
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"DELTAbravocharlie")
        self.assertEqual(path.read_bytes(), b"alpha")

    async def test_changed_dash_order_restarts_even_when_the_manifest_bytes_are_identical(self):
        first, second, path, record = await self.interrupted("dash")
        self.info["fragments"].reverse()
        self.assertTrue(self.download(second))
        self.assertIn(("GET", "/a"), self.requests)
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"charliebravoalpha")

    async def test_foreign_fragment_prefix_is_refused_without_modifying_either_attempt(self):
        first, second, path, record = await self.interrupted()
        replacement = path.with_suffix(".foreign")
        replacement.write_bytes(b"alpha")
        os.replace(replacement, path)
        with self.assertRaisesRegex(ValueError, "ownership|Partial|partial"):
            self.download(second)
        self.assertEqual(path.read_bytes(), b"alpha")
        self.assertFalse((second.workspace / "fixture.mp4").exists())

    async def test_uncommitted_fragment_tail_is_retained_and_not_copied(self):
        first, second, path, record = await self.interrupted()
        with path.open("ab") as file:
            file.write(b"uncommitted")
        self.assertTrue(self.download(second))
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"alphabravocharlie")
        self.assertEqual(path.read_bytes(), b"alphauncommitted")

    async def test_a_valid_hash_inside_a_fragment_is_not_a_resume_boundary(self):
        first, second, path, record = await self.interrupted()
        record.update(size=3, sha256=hashlib.sha256(b"alp").hexdigest())
        self.execute("UPDATE attempt_partials SET record=? WHERE attempt_id=?", (json.dumps(record), first.id))
        with self.assertRaisesRegex(ValueError, "boundary|fragment"):
            self.download(second)
        self.assertFalse((second.workspace / "fixture.mp4").exists())
        self.assertEqual(path.read_bytes(), b"alpha")

    async def test_missing_strong_fragment_validator_disables_all_prefix_reuse(self):
        first, second, path, record = await self.interrupted()
        self.tags["/c"] = 'W/"weak"'
        self.assertTrue(self.download(second))
        self.assertIn(("GET", "/a"), self.requests)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials WHERE attempt_id=?", (second.id,)), [(0,)])

    async def test_head_not_supported_downloads_fresh_without_a_witness(self):
        first, second, path, record = await self.interrupted()
        self.head_status = 405
        self.assertTrue(self.download(second))
        self.assertIn(("GET", "/a"), self.requests)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials WHERE attempt_id=?", (second.id,)), [(0,)])

    async def test_fragment_authentication_failure_is_typed_and_retains_the_prefix(self):
        first, second, path, record = await self.interrupted()
        self.denied = "/b"
        from backend.services.resume import FreshAuthRequired
        with self.assertRaises(FreshAuthRequired):
            self.download(second)
        self.assertEqual(path.read_bytes(), b"alpha")
        self.assertFalse((second.workspace / "fixture.mp4").exists())

    async def test_get_validator_change_cannot_append_or_publish(self):
        first, second, path, record = await self.interrupted()
        self.change_get = "/b"
        with self.assertRaisesRegex(ValueError, "validator|source|changed"):
            self.download(second)
        self.assertEqual((second.workspace / "fixture.mp4.part").read_bytes(), b"alpha")
        self.assertFalse((second.workspace / "fixture.mp4").exists())

    async def test_cancellation_wins_before_any_prior_file_read(self):
        first, second, path, record = await self.interrupted()
        self.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (second.job_id,))
        with patch("backend.services.resume._file", side_effect=AssertionError("Read after cancellation")):
            with self.assertRaisesRegex(Exception, "owner|attempt"):
                self.download(second)
        self.assertEqual(self.requests, [])

    async def test_live_previous_worker_fences_fragment_reads(self):
        first, second, path, record = await self.interrupted()
        from backend.engine.process_identity import ProcessIdentity
        self.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?", (ProcessIdentity.capture().to_json(), first.id))
        with patch("backend.services.resume._file", side_effect=AssertionError("Read live worker's prefix")):
            with self.assertRaisesRegex(Exception, "live.*worker"):
                self.download(second)

    async def test_existing_attempt_part_and_final_destinations_are_preserved(self):
        self.fixture()
        first = await self.attempts()
        partial = first.workspace / "fixture.mp4.part"
        partial.write_bytes(b"foreign")
        with self.assertRaisesRegex((ValueError, OSError), "partial|Partial|Existing|existing"):
            self.download(first)
        self.assertEqual(partial.read_bytes(), b"foreign")
        partial.unlink()
        final = first.workspace / "fixture.mp4"
        final.write_bytes(b"foreign-final")
        with self.assertRaisesRegex((ValueError, OSError), "Existing|existing"):
            self.download(first)
        self.assertEqual(final.read_bytes(), b"foreign-final")

    async def test_oversized_manifest_is_bounded_before_parsing_or_prefix_reads(self):
        self.fixture()
        first = await self.attempts()
        self.manifest = b"#EXTM3U\n" + b"x" * (1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "manifest|Manifest|limit"):
            self.download(first)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials"), [(0,)])

    async def test_manifest_authentication_failure_closes_its_response(self):
        self.fixture()
        first = await self.attempts()
        self.denied = self.manifest_path
        from backend.services.resume import FreshAuthRequired
        with self.assertRaises(FreshAuthRequired):
            self.download(first)
        self.assertTrue(all(response.closed for response in self.responses))

    async def test_default_dispatch_uses_verified_hls_without_enabling_legacy_resume(self):
        self.fixture()
        first = await self.attempts()
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "continuedl": False, "noresume": True}) as ydl:
            ydl.urlopen = self.response
            from backend.services.resume import install_http_resume
            install_http_resume(ydl, self.selected_runtime, first)
            with patch("yt_dlp.downloader.hls.FFmpegFD.available", return_value=False):
                success, _ = ydl.dl(str(first.workspace / "fixture.mp4"), dict(self.info))
            self.assertTrue(success)
            self.assertFalse(ydl.params["continuedl"])
        self.assertTrue(self.query("SELECT record FROM attempt_partials"), "The owned HLS dispatcher must use fragment witnesses")

    async def test_unsupported_hls_cannot_adopt_a_foreign_native_fragment_by_filename(self):
        self.fixture()
        first = await self.attempts()
        self.manifest = self.manifest.replace(b"#EXTINF:1,\na", b"#EXTINF:1,\n#EXT-X-BYTERANGE:5@0\na")
        foreign = first.workspace / "fixture.mp4.part-Frag1"
        foreign.write_bytes(b"foreign")
        with self.assertRaisesRegex(ValueError, "Existing|existing|fragment"):
            self.download(first)
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertFalse((first.workspace / "fixture.mp4").exists())

    async def test_non_authentication_http_error_closes_the_rejected_response(self):
        self.fixture()
        first = await self.attempts()
        self.head_status = 500
        with self.assertRaises(HTTPError):
            self.download(first)
        self.assertTrue(all(response.closed for response in self.responses))

    async def test_manifest_declared_length_must_match_its_complete_body(self):
        self.fixture()
        first = await self.attempts()
        response = self.response
        def truncated(request):
            selected = response(request)
            if request.url.endswith(self.manifest_path):
                selected.headers.replace_header("Content-Length", str(len(self.manifest) + 1))
            return selected
        with patch.object(self, "response", side_effect=truncated):
            with self.assertRaisesRegex((RuntimeError, ValueError), "manifest|Manifest|length"):
                self.download(first)
        self.assertFalse((first.workspace / "fixture.mp4").exists())

    async def test_dynamic_dash_stays_fresh_and_creates_no_resume_witness(self):
        self.fixture("dash")
        first = await self.attempts()
        self.manifest = self.manifest.replace(b'type="static"', b'type="dynamic"')
        self.assertTrue(self.download(first))
        self.assertNotIn(("HEAD", "/a"), self.requests)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials"), [(0,)])

    async def test_repeated_interruption_carries_only_whole_fragments_into_distinct_attempts(self):
        first, second, path, record = await self.interrupted()
        with self.assertRaises(Stopped):
            self.download(second, stop=10)
        second_path = second.workspace / "fixture.mp4.part"
        self.assertEqual(second_path.read_bytes(), b"alphabravo")
        await self.store.finish(second.job_id, "queued", NOW, attempt_id=second.id)
        third = await self.store.claim(second.job_id, NOW)
        from backend.services.artifacts import prepare_workspace
        prepare_workspace(self.selected_runtime, third)
        self.requests.clear()
        self.assertTrue(self.download(third))
        self.assertNotIn(("GET", "/a"), self.requests)
        self.assertNotIn(("GET", "/b"), self.requests)
        self.assertEqual((third.workspace / "fixture.mp4").read_bytes(), b"alphabravocharlie")
        self.assertEqual(second_path.read_bytes(), b"alphabravo")
        self.assertEqual(path.read_bytes(), b"alpha")

    async def test_complete_fragment_prefix_does_not_fetch_any_media_again(self):
        self.fixture()
        first = await self.attempts()
        with self.assertRaises(Stopped):
            self.download(first, stop=17)
        await self.store.finish(first.job_id, "queued", NOW, attempt_id=first.id)
        second = await self.store.claim(first.job_id, NOW)
        from backend.services.artifacts import prepare_workspace
        prepare_workspace(self.selected_runtime, second)
        self.requests.clear()
        self.assertTrue(self.download(second))
        self.assertFalse(any(method == "GET" and path in self.parts for method, path in self.requests))
        self.assertEqual((second.workspace / "fixture.mp4").read_bytes(), b"alphabravocharlie")

    async def test_dash_resolved_url_plan_has_an_aggregate_size_bound(self):
        self.fixture("dash")
        first = await self.attempts()
        self.parts = {"/" + str(index) + "x" * 60000: b"alpha" for index in range(20)}
        self.tags = {path: '"tag"' for path in self.parts}
        self.info["fragments"] = [{"url": "https://fixture.invalid" + path} for path in self.parts]
        with self.assertRaisesRegex(ValueError, "plan|Plan|limit"):
            self.download(first)
        self.assertFalse(any(method == "HEAD" for method, path in self.requests))

    async def test_utf16_dash_cannot_hide_a_doctype_declaration(self):
        self.fixture("dash")
        first = await self.attempts()
        self.manifest = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE MPD [<!ENTITY secret "data">]>'
                         '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period/></MPD>').encode("utf-16-le")
        with self.assertRaisesRegex(ValueError, "manifest|Manifest|UTF|unsupported"):
            self.download(first)
        self.assertFalse(any(method == "HEAD" for method, path in self.requests))
