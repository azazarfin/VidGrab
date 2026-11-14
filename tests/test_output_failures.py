"""Disk-full failures retain owned bytes without reporting success or retrying."""

import asyncio
import errno
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.support import OfflineTestCase


class OutputFailureTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        errors = []
        loop.set_exception_handler(lambda _, context: errors.append(context))

        async def check_loop():
            await asyncio.sleep(0)
            loop.set_exception_handler(previous)
            self.assertEqual(errors, [], "Output recovery must join its owned tasks")

        self.addAsyncCleanup(check_loop)

    def rows(self, runtime, sql):
        with closing(sqlite3.connect(runtime.db_path)) as db:
            return db.execute(sql).fetchall()

    async def disk_full(self, phase, published):
        from backend.services import publication

        runtime = self.runtime()
        calls = []
        observed = {}
        payloads = {"first বাংলা.mkv": ("media", b"first media"),
                    "second.mkv": ("media", b"second media"),
                    "first.en.srt": ("subtitle", b"subtitle"),
                    "first.jpg": ("thumbnail", b"thumbnail")}

        def exhausted(*args, **kwargs):
            observed["injected"] = phase
            raise OSError(errno.ENOSPC, "No space left on device")

        def download(**options):
            calls.append(options["task_id"])
            workspace = Path(options["attempt_dir"])
            observed["workspace"] = workspace
            observed["cookie"] = Path(options["cookies_file_path"])
            self.assertTrue(observed["cookie"].is_file())
            if phase == "download":
                partial = workspace / "first.part"
                partial.write_bytes(b"downloaded prefix")
                exhausted()
            moves = []
            for name, (kind, contents) in payloads.items():
                source = workspace / name
                source.write_bytes(contents)
                moves.append({"source": str(source), "path": str(runtime.download_dir / name), "kind": kind})
            if phase == "journal":
                with patch.object(publication, "append_journal", side_effect=exhausted):
                    publication.plan_publications(runtime, workspace, moves)
                self.fail("The journal write must fail before publication")
            planned = publication.plan_publications(runtime, workspace, moves)
            original_link = os.link

            def link(source, destination, **kwargs):
                if phase == "publication" and Path(destination).name == "second.mkv":
                    exhausted()
                return original_link(source, destination, **kwargs)

            with patch.object(publication.os, "link", link):
                for record in planned:
                    publication.publish_artifact(runtime, workspace, record)
            result = {"filepath": moves[0]["path"], "artifacts": [
                {"path": move["path"], "kind": move["kind"]} for move in moves]}
            with patch.object(publication, "append_journal_result", side_effect=exhausted):
                publication.record_result(runtime, workspace, result)
            self.fail("A failed result footer must not report success")

        app = self.app(runtime, download_runner=download)
        async with app.router.lifespan_context(app):
            unrelated = runtime.download_dir / "first other job.part"
            unrelated.write_bytes(b"unrelated partial")
            job = await app.state.queue.enqueue("https://fixture.invalid/media", "best", cookies=[
                {"domain": "fixture.invalid", "name": "fixture", "value": "ephemeral fixture cookie"}])
            config = await app.state.settings.get_all()
            self.assertGreater(config["max_retries"], 0)
            with self.assertLogs("vidgrab.queue_manager", level="ERROR"):
                await app.state.queue._run_task(job, "https://fixture.invalid/media", "best", config, {})
            self.assertEqual(observed.get("injected"), phase, "The selected ENOSPC boundary must execute")
            expected = [("failed", None, 0, None, 1, None)]
            sql = "SELECT status,file_path,retry_count,retry_at,attempt_count,current_attempt_id FROM downloads"
            self.assertEqual(self.rows(runtime, sql), expected)
            self.assertEqual(self.rows(runtime, "SELECT status FROM download_attempts"), [("failed",)])
            self.assertIsNone(await app.state.queue.attempts.claim(job, app.state.queue._now()))
            self.assertFalse(observed["cookie"].exists(), "Cookie cleanup must follow the joined runner")
            self.assertEqual(self.rows(runtime, "SELECT COUNT(*) FROM attempt_cookie_files"), [(0,)])
            complete = self.rows(runtime, "SELECT path,kind FROM download_artifacts WHERE state='complete' ORDER BY path")
            self.assertEqual(complete, sorted((str(runtime.download_dir / name), payloads[name][0]) for name in published))
            for name, (_, contents) in payloads.items():
                output = runtime.download_dir / name
                self.assertEqual(output.exists(), name in published)
                if name in published:
                    self.assertEqual(output.read_bytes(), contents)
            retained = {path: path.read_bytes() for path in observed["workspace"].rglob("*") if path.is_file()}
            self.assertTrue(retained)
            if phase == "download":
                self.assertEqual((observed["workspace"] / "first.part").read_bytes(), b"downloaded prefix")
            else:
                for name, (_, contents) in payloads.items():
                    self.assertEqual((observed["workspace"] / name).read_bytes(), contents)

        reopened = self.app(runtime, download_runner=download)
        async with reopened.router.lifespan_context(reopened):
            self.assertEqual(self.rows(runtime, sql), expected)
            self.assertIsNone(await reopened.state.queue.attempts.claim(job, reopened.state.queue._now()))
            self.assertEqual(calls, [job], "Restart must not execute a failed disk-full attempt")
            self.assertEqual(unrelated.read_bytes(), b"unrelated partial")
            for path, contents in retained.items():
                self.assertEqual(path.read_bytes(), contents)
            for name in published:
                self.assertEqual((runtime.download_dir / name).read_bytes(), payloads[name][1])

    async def test_download_disk_full_retains_partial_and_stops_retry(self):
        await self.disk_full("download", ())

    async def test_journal_disk_full_publishes_no_outputs(self):
        await self.disk_full("journal", ())

    async def test_disk_full_after_first_output_preserves_partial_publication(self):
        await self.disk_full("publication", ("first বাংলা.mkv",))

    async def test_result_footer_disk_full_cannot_complete_multiple_outputs(self):
        await self.disk_full("footer", ("first বাংলা.mkv", "second.mkv", "first.en.srt", "first.jpg"))
