"""Only fenced, witnessed HTTP prefixes may cross an attempt boundary."""

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.support import OfflineTestCase

NOW = "2026-11-14T00:00:00+00:00"


class ResumeTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.selected_runtime = self.runtime("resume বাংলা")
        self.application = self.app(self.selected_runtime)
        context = self.application.router.lifespan_context(self.application)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.store = self.application.state.queue.attempts

    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.resume"),
                             "Resume needs an explicit ownership and validator gate")
        from backend.services import resume
        return resume

    def query(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db:
            return db.execute(sql, values).fetchall()

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db, db:
            db.execute(sql, values)

    async def attempts(self):
        from backend.services.artifacts import prepare_workspace
        job = await self.application.state.queue.enqueue("https://fixture.invalid/media?token=secret", "best")
        first = await self.store.claim(job, NOW)
        prepare_workspace(self.selected_runtime, first)
        return first

    async def partial(self):
        service = self.service()
        first = await self.attempts()
        source = service.source_validator("https://fixture.invalid/media?token=secret", "audio", '"secret-etag"', 20)
        path = first.workspace / "fixture.wav.part"
        path.write_bytes(b"verified")
        service.checkpoint(self.selected_runtime, first, path, source, 8, hashlib.sha256(b"verified").hexdigest())
        await self.store.finish(first.job_id, "queued", NOW, attempt_id=first.id)
        second = await self.store.claim(first.job_id, NOW)
        from backend.services.artifacts import prepare_workspace
        prepare_workspace(self.selected_runtime, second)
        return service, first, second, path, source

    async def test_verified_prefix_copies_to_new_attempt_without_mutating_prior_file(self):
        service, first, second, path, source = await self.partial()
        target = second.workspace / path.name
        result = service.restore(self.selected_runtime, second, target, source)
        self.assertEqual(result, 8)
        self.assertEqual(target.read_bytes(), b"verified")
        self.assertEqual(path.read_bytes(), b"verified")
        self.assertNotEqual(target.stat().st_ino, path.stat().st_ino)
        records = self.query("SELECT record FROM attempt_partials")
        self.assertTrue(records)
        self.assertNotIn("secret", str(records), "Raw URL and ETag values must not persist")

    async def test_uncommitted_tail_is_retained_but_never_copied(self):
        service, first, second, path, source = await self.partial()
        with path.open("ab") as file:
            file.write(b"uncommitted")
        target = second.workspace / path.name
        self.assertEqual(service.restore(self.selected_runtime, second, target, source), 8)
        self.assertEqual(target.read_bytes(), b"verified")
        self.assertEqual(path.read_bytes(), b"verifieduncommitted")

    async def test_changed_validators_restart_without_adopting_the_old_partial(self):
        service, first, second, path, source = await self.partial()
        for changed in (service.source_validator("https://fixture.invalid/media?token=secret", "audio", '"changed"', 20),
                        service.source_validator("https://fixture.invalid/media?token=secret", "audio", '"secret-etag"', 21),
                        service.source_validator("https://fixture.invalid/other", "audio", '"secret-etag"', 20)):
            target = second.workspace / path.name
            self.assertEqual(service.restore(self.selected_runtime, second, target, changed), 0)
            self.assertFalse(target.exists())
        self.assertEqual(path.read_bytes(), b"verified")

    async def test_modified_or_replaced_prefix_is_refused_and_preserved(self):
        service, first, second, path, source = await self.partial()
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "prefix|partial"):
            service.restore(self.selected_runtime, second, second.workspace / path.name, source)
        self.assertEqual(path.read_bytes(), b"modified")
        replacement = path.with_suffix(".foreign")
        replacement.write_bytes(b"verified")
        os.replace(replacement, path)
        with self.assertRaisesRegex(ValueError, "identity|partial"):
            service.restore(self.selected_runtime, second, second.workspace / path.name, source)
        self.assertEqual(path.read_bytes(), b"verified")

    async def test_extra_hardlink_and_existing_destination_are_refused(self):
        service, first, second, path, source = await self.partial()
        link = self.root / "foreign-link"
        os.link(path, link)
        with self.assertRaisesRegex(ValueError, "link|partial"):
            service.restore(self.selected_runtime, second, second.workspace / path.name, source)
        link.unlink()
        target = second.workspace / path.name
        target.write_bytes(b"foreign")
        with self.assertRaises(FileExistsError):
            service.restore(self.selected_runtime, second, target, source)
        self.assertEqual(target.read_bytes(), b"foreign")

    async def test_live_previous_worker_fences_reads(self):
        service, first, second, path, source = await self.partial()
        from backend.engine.process_identity import ProcessIdentity
        self.execute("UPDATE download_attempts SET worker_identity=? WHERE id=?",
                     (ProcessIdentity.capture().to_json(), first.id))
        with patch("builtins.open", side_effect=AssertionError("No file read before owner fencing")):
            with self.assertRaisesRegex(Exception, "live.*worker"):
                service.restore(self.selected_runtime, second, second.workspace / path.name, source)

    async def test_retired_current_owner_cannot_read_or_checkpoint(self):
        service, first, second, path, source = await self.partial()
        self.execute("UPDATE runtime_owners SET released_at=1 WHERE id=?", (second.owner_id,))
        try:
            with self.assertRaisesRegex(Exception, "owner|attempt"):
                service.restore(self.selected_runtime, second, second.workspace / path.name, source)
        finally:
            self.execute("UPDATE runtime_owners SET released_at=NULL WHERE id=?", (second.owner_id,))

    async def test_executing_worker_registration_is_fenced_by_the_private_owner_and_current_attempt(self):
        from backend.db import ownership
        from backend.engine.process_identity import ProcessIdentity
        self.assertTrue(callable(getattr(ownership, "register_executing_worker", None)),
                        "The executing interpreter needs a durable startup barrier")
        first = await self.attempts()
        actual = ProcessIdentity.capture()
        ownership.register_worker(self.selected_runtime, first.id, first.owner_id, first.job_id,
                                  str(first.workspace), actual, actual)
        ownership.register_executing_worker(self.selected_runtime, first.id, first.owner_id, actual, actual)
        self.execute("UPDATE downloads SET status='cancelling' WHERE id=?", (first.job_id,))
        with self.assertRaisesRegex(RuntimeError, "owner|attempt|execution"):
            ownership.register_executing_worker(self.selected_runtime, first.id, first.owner_id, actual, actual)
        self.execute("UPDATE download_attempts SET worker_owner_identity=NULL,worker_identity=NULL WHERE id=?", (first.id,))
        self.execute("UPDATE downloads SET status='downloading' WHERE id=?", (first.job_id,))

    async def test_unowned_partials_and_unsafe_target_paths_are_never_adopted(self):
        service = self.service()
        first = await self.attempts()
        path = first.workspace / "fixture.wav.part"
        path.write_bytes(b"unowned")
        source = service.source_validator("https://fixture.invalid/media", "audio", '"etag"', 20)
        with self.assertRaises(FileExistsError):
            service.restore(self.selected_runtime, first, path, source)
        with self.assertRaisesRegex(ValueError, "workspace|path|partial"):
            service.restore(self.selected_runtime, first, self.root / "foreign.part", source)
        self.assertEqual(path.read_bytes(), b"unowned")

    async def test_checkpoint_refuses_a_workspace_belonging_to_a_different_attempt_id(self):
        service, first, second, path, source = await self.partial()
        from dataclasses import replace
        foreign = replace(second, workspace=first.workspace)
        self.execute("UPDATE download_attempts SET workspace=? WHERE id=?", (str(first.workspace), second.id))
        try:
            with self.assertRaisesRegex(ValueError, "workspace|identity"):
                service.checkpoint(self.selected_runtime, foreign, path, source, 8, hashlib.sha256(b"verified").hexdigest())
        finally:
            self.execute("UPDATE download_attempts SET workspace=? WHERE id=?", (str(second.workspace), second.id))

    async def test_weak_missing_oversized_or_invalid_validators_disable_resume(self):
        service = self.service()
        for etag, size in ((None, 20), ('W/"weak"', 20), ('"x"', None), ('"x"', True),
                           ('"x"', -1), ('"' + "x" * 4096 + '"', 20), ('"bad\nvalue"', 20)):
            self.assertIsNone(service.source_validator("https://fixture.invalid/media", "audio", etag, size))

    async def test_schema_five_upgrade_retains_history_without_inventing_authentication(self):
        from backend.config import init_db
        selected = self.runtime("schema-five")
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db, db:
            db.execute("DROP TABLE attempt_partials")
            db.execute("ALTER TABLE downloads DROP COLUMN auth_required")
            db.execute("PRAGMA user_version=5")
            db.execute("INSERT INTO downloads (id,url,status,created_at) VALUES ('history','fixture','complete',?)", (NOW,))
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT status,auth_required,attempt_count FROM downloads").fetchall(), [("complete", 0, 0)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempt_partials").fetchone()[0], 0)
        self.assertEqual(len(list(selected.backups_dir.glob("*.sqlite3"))), 1)

    async def test_interrupted_schema_six_migration_rolls_back_column_table_and_version(self):
        from backend.config import init_db
        selected = self.runtime("schema-six-rollback")
        await init_db(selected)
        with closing(sqlite3.connect(selected.db_path)) as db, db:
            db.execute("DROP TABLE attempt_partials")
            db.execute("ALTER TABLE downloads DROP COLUMN auth_required")
            db.execute("PRAGMA user_version=5")
            db.execute("DELETE FROM config WHERE key='max_retries'")
            db.execute("""CREATE TRIGGER interrupt_resume BEFORE INSERT ON config WHEN NEW.key='max_retries'
                BEGIN SELECT RAISE(ABORT,'resume migration interrupted'); END""")
        before = selected.db_path.read_bytes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "resume migration interrupted"):
            await init_db(selected)
        self.assertEqual(selected.db_path.read_bytes(), before)

    async def test_checkpoint_size_and_digest_are_bounded(self):
        service = self.service()
        first = await self.attempts()
        path = first.workspace / "fixture.wav.part"
        path.write_bytes(b"verified")
        source = service.source_validator("https://fixture.invalid/media", "audio", '"etag"', 20)
        for size, digest in ((9, hashlib.sha256(b"verified").hexdigest()), (True, "0" * 64), (8, "secret")):
            with self.assertRaises(ValueError):
                service.checkpoint(self.selected_runtime, first, path, source, size, digest)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials"), [(0,)])

    async def test_prefix_hashing_does_not_hold_the_database_writer_lock(self):
        service, first, second, path, source = await self.partial()
        original = service._prefix
        def hashing(*args, **kwargs):
            with closing(sqlite3.connect(self.selected_runtime.db_path, timeout=0.05)) as db, db:
                db.execute("UPDATE config SET value=value WHERE key='max_retries'")
            return original(*args, **kwargs)
        with patch.object(service, "_prefix", side_effect=hashing):
            self.assertEqual(service.restore(self.selected_runtime, second, second.workspace / path.name, source), 8)

    async def test_complete_witnessed_prefix_verifies_source_without_requesting_an_unsatisfiable_range(self):
        service, first, second, path, source = await self.partial()
        self.execute("UPDATE attempt_partials SET record=json_set(record, '$.source.length', 8)")
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        requests = []
        def open_response(request):
            requests.append(dict(request.headers))
            self.assertNotIn("Range", request.headers)
            return Response(io.BytesIO(b"verified"), request.url,
                            {"ETag": '"secret-etag"', "Content-Length": "8"}, status=200)
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = open_response
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, second)
            self.assertTrue(fd.real_download(str(second.workspace / "fixture.wav"),
                            {"url": "https://fixture.invalid/media?token=secret", "format_id": "audio"}))
        self.assertEqual(len(requests), 1)
        self.assertEqual((second.workspace / "fixture.wav").read_bytes(), b"verified")

    async def test_direct_http_resume_requires_matching_range_response_before_appending(self):
        service, first, second, path, source = await self.partial()
        self.assertTrue(hasattr(service, "VerifiedHttpFD"), "Direct HTTP transfer must verify every Range response")
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        requests = []
        def open_response(request):
            requests.append(dict(request.headers))
            offset = 8 if request.headers.get("Range") else 0
            return Response(io.BytesIO(b"verified" + b" data suffix" if not offset else b" data suffix"),
                            request.url, {"ETag": '"secret-etag"', "Content-Length": str(20-offset),
                                          **({"Content-Range": "bytes 8-19/20"} if offset else {})},
                            status=206 if offset else 200)
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = open_response
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, second)
            self.assertTrue(fd.real_download(str(second.workspace / "fixture.wav"),
                            {"url": "https://fixture.invalid/media?token=secret", "format_id": "audio"}))
        self.assertEqual(requests[-1]["Range"], "bytes=8-")
        self.assertEqual(requests[-1]["If-Range"], '"secret-etag"')
        self.assertEqual((second.workspace / "fixture.wav").read_bytes(), b"verified data suffix")
        self.assertEqual(path.read_bytes(), b"verified")

    async def test_range_downgrade_cannot_overwrite_or_append_the_verified_prefix(self):
        service, first, second, path, source = await self.partial()
        self.assertTrue(hasattr(service, "VerifiedHttpFD"), "Range refusal must preserve partials")
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        def open_response(request):
            return Response(io.BytesIO(b"changed source bytes"), request.url,
                            {"ETag": '"secret-etag"', "Content-Length": "20"}, status=200)
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = open_response
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, second)
            with self.assertRaisesRegex(ValueError, "Range|source"):
                fd.real_download(str(second.workspace / "fixture.wav"),
                                 {"url": "https://fixture.invalid/media?token=secret", "format_id": "audio"})
        self.assertEqual((second.workspace / "fixture.wav.part").read_bytes(), b"verified")
        self.assertEqual(path.read_bytes(), b"verified")

    async def test_compressed_fresh_response_uses_decoded_bytes_without_a_resume_witness(self):
        service = self.service()
        first = await self.attempts()
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = lambda request: Response(io.BytesIO(b"decoded body"), request.url,
                {"ETag": '"etag"', "Content-Length": "2", "Content-Encoding": "gzip"}, status=200)
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, first)
            self.assertTrue(fd.real_download(str(first.workspace / "fixture.wav"), {"url": "https://fixture.invalid/media"}))
        self.assertEqual((first.workspace / "fixture.wav").read_bytes(), b"decoded body")
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempt_partials"), [(0,)])

    async def test_unsolicited_partial_http_response_cannot_be_published_as_complete_media(self):
        service = self.service()
        first = await self.attempts()
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = lambda request: Response(io.BytesIO(b"fragment"), request.url,
                {"Content-Length": "8", "Content-Range": "bytes 12-19/20"}, status=206)
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, first)
            with self.assertRaisesRegex(ValueError, "HTTP|response"):
                fd.real_download(str(first.workspace / "fixture.wav"), {"url": "https://fixture.invalid/media"})
        self.assertFalse((first.workspace / "fixture.wav").exists())

    async def test_http_authentication_error_is_typed_without_persisting_secrets(self):
        service = self.service()
        first = await self.attempts()
        self.assertTrue(hasattr(service, "VerifiedHttpFD"), "Expired credentials need a typed refresh transition")
        import io
        import yt_dlp
        from yt_dlp.networking import Response
        from yt_dlp.networking.exceptions import HTTPError
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            ydl.urlopen = lambda request: (_ for _ in ()).throw(HTTPError(
                Response(io.BytesIO(b"secret"), request.url, {}, status=401)))
            fd = service.VerifiedHttpFD(ydl, ydl.params, self.selected_runtime, first)
            with self.assertRaisesRegex(service.FreshAuthRequired, "authentication"):
                fd.real_download(str(first.workspace / "fixture.wav"),
                                 {"url": "https://fixture.invalid/media?token=secret", "format_id": "audio"})

    async def test_extractor_wrapped_http_auth_failure_is_classified_without_message_matching(self):
        service = self.service()
        self.assertTrue(hasattr(service, "requires_fresh_auth"), "Extraction needs the same HTTP authentication classification")
        import io
        from yt_dlp.networking import Response
        from yt_dlp.networking.exceptions import HTTPError
        from yt_dlp.utils import DownloadError, ExtractorError
        denied = HTTPError(Response(io.BytesIO(), "https://fixture.invalid/media", {}, status=403))
        nested = ExtractorError("private token", cause=denied)
        error = DownloadError("private token", exc_info=(type(nested), nested, None))
        self.assertTrue(service.requires_fresh_auth(error))
        self.assertFalse(service.requires_fresh_auth(RuntimeError("HTTP Error 403: token")))
