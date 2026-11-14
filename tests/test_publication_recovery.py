"""Publication recovery must prove ownership before completing or retrying a job."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.support import OfflineTestCase

NOW = "2026-11-14T00:00:00+00:00"
LATER = "2026-11-14T00:01:00+00:00"


class PublicationRecoveryTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))

        async def check_loop():
            await asyncio.sleep(0)
            loop.set_exception_handler(previous)
            self.assertEqual(self.loop_errors, [], "Publication cleanup must retrieve owned task exceptions")

        self.addAsyncCleanup(check_loop)
        self.selected_runtime = self.runtime()
        self.application = self.app(self.selected_runtime)
        context = self.application.router.lifespan_context(self.application)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)

    def store(self):
        return self.application.state.queue.attempts

    def query(self, sql):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db:
            return db.execute(sql).fetchall()

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.selected_runtime.db_path)) as db, db:
            db.execute(sql, values)

    def enqueue(self):
        self.execute("INSERT INTO downloads (id, url, status, format_selected, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("job", "https://fixture.invalid/media", "queued", "best", NOW))
        return "job"

    def publication(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.publication"),
                             "Publication needs durable file ownership and result evidence")
        from backend.services import publication
        return publication

    async def planned(self, names=("finished বাংলা.mp3",)):
        from backend.services.artifacts import prepare_workspace
        attempt = await self.store().claim(self.enqueue(), NOW)
        prepare_workspace(self.selected_runtime, attempt)
        moves = []
        for index, name in enumerate(names):
            source = attempt.workspace / name
            source.write_bytes(b"media" if index == 0 else b"subtitle")
            moves.append({"source": str(source), "path": str(self.selected_runtime.download_dir / name),
                          "kind": "media" if index == 0 else "subtitle"})
        records = self.publication().plan_publications(self.selected_runtime, attempt.workspace, moves)
        return attempt, records

    def publish(self, attempt, records, *, success=False):
        service = self.publication()
        for record in records:
            service.publish_artifact(self.selected_runtime, attempt.workspace, record)
        if success:
            result = {"filepath": records[0]["path"], "artifacts": [
                {"path": record["path"], "kind": record["kind"]} for record in records],
                "title": "A preserved title বাংলা", "thumbnail_url": None}
            service.record_result(self.selected_runtime, attempt.workspace, result)
        return records[0]["path"]

    async def test_interruption_before_publication_requeues_without_touching_staged_bytes(self):
        attempt, records = await self.planned()
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path, attempt_count FROM downloads"), [("queued", None, 1)])
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("interrupted",)])
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")
        self.assertFalse(Path(records[0]["path"]).exists())

    async def test_interruption_after_publication_stops_automatic_retry_and_preserves_owned_files(self):
        attempt, records = await self.planned()
        output = self.publish(attempt, records)
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path, retry_count FROM downloads"), [("failed", None, 0)])
        self.assertIsNone(await self.store().claim("job", LATER))
        self.assertEqual(self.query("SELECT path, state FROM download_artifacts WHERE kind='media'"),
                         [(output, "complete")])
        self.assertEqual(Path(output).read_bytes(), b"media")
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")

    async def test_durable_result_recovers_completion_without_a_second_download(self):
        attempt, records = await self.planned(("finished বাংলা.mp3", "finished বাংলা.en.srt"))
        output = self.publish(attempt, records, success=True)
        await self.store().recover(LATER)
        before = self.query("SELECT status, file_path, progress, completed_at, attempt_count, title, error_message FROM downloads")
        self.assertEqual(before, [("complete", output, 100.0, LATER, 1, "A preserved title বাংলা", None)])
        self.assertEqual(self.query("SELECT status, finished_at FROM download_attempts"), [("complete", LATER)])
        self.assertEqual(self.query("SELECT kind, state FROM download_artifacts WHERE kind!='auxiliary' ORDER BY kind"),
                         [("media", "complete"), ("subtitle", "complete")])
        self.assertEqual(await self.store().recover("2026-11-15T00:00:00+00:00"), [])
        self.assertEqual(self.query("SELECT status, file_path, progress, completed_at, attempt_count, title, error_message FROM downloads"), before)
        self.assertIsNone(await self.store().claim("job", LATER))

    async def test_replaced_destination_with_identical_bytes_is_not_adopted(self):
        attempt, records = await self.planned()
        output = Path(self.publish(attempt, records, success=True))
        output.unlink()
        output.write_bytes(b"media")
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("failed", None)])
        self.assertEqual(self.query("SELECT path FROM download_artifacts WHERE kind='media'"), [])
        self.assertEqual(output.read_bytes(), b"media")
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")

    async def test_foreign_destination_created_after_plan_is_preserved_and_refuses_publication(self):
        attempt, records = await self.planned()
        output = Path(records[0]["path"])
        output.write_bytes(b"foreign")
        with self.assertRaisesRegex(ValueError, "exist|ownership|publish"):
            self.publication().publish_artifact(self.selected_runtime, attempt.workspace, records[0])
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("failed",)])
        self.assertEqual(self.query("SELECT path FROM download_artifacts WHERE kind='media'"), [])
        self.assertEqual(output.read_bytes(), b"foreign")

    async def test_partial_multiple_output_publication_does_not_requeue_into_its_media(self):
        attempt, records = await self.planned(("finished.mp3", "finished.en.srt"))
        self.publish(attempt, records[:1])
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, retry_count FROM downloads"), [("failed", 0)])
        self.assertEqual(self.query("SELECT kind FROM download_artifacts WHERE state='complete'"), [("media",)])
        self.assertEqual(Path(records[1]["source"]).read_bytes(), b"subtitle")
        self.assertFalse(Path(records[1]["path"]).exists())

    async def test_persisted_cancellation_wins_over_durable_success(self):
        attempt, records = await self.planned()
        output = self.publish(attempt, records, success=True)
        self.execute("UPDATE downloads SET status='cancelling' WHERE id='job'")
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("cancelled", None)])
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("cancelled",)])
        self.assertEqual(Path(output).read_bytes(), b"media")

    async def test_recovery_sqlite_failure_leaves_evidence_available_for_the_next_recovery(self):
        attempt, records = await self.planned()
        output = self.publish(attempt, records, success=True)
        self.execute("""CREATE TRIGGER reject_publication_recovery BEFORE UPDATE ON download_attempts
                        WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT, 'terminal write interrupted'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "terminal write interrupted"):
            await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, current_attempt_id FROM downloads"), [("downloading", attempt.id)])
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")
        self.execute("DROP TRIGGER reject_publication_recovery")
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("complete", output)])

    async def test_cancelled_recovery_joins_verification_before_rolling_back_its_transaction(self):
        attempt, records = await self.planned()
        output = self.publish(attempt, records, success=True)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        reconcile = self.publication().reconcile_publication

        def gated(runtime, owned):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return reconcile(runtime, owned)

        with patch("backend.db.jobs.reconcile_publication", side_effect=gated):
            task = asyncio.create_task(self.store().recover(LATER))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done(), "Recovery must retain its transaction until verification exits")
                self.assertEqual(self.query("SELECT status FROM downloads"), [("downloading",)])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(Path(records[0]["witness"]).read_bytes(), b"media")
        self.assertEqual(self.query("SELECT status FROM download_attempts"), [("running",)])
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("complete", output)])

    async def test_truncated_success_frame_preserves_publication_and_stops_retry(self):
        attempt, records = await self.planned()
        self.publish(attempt, records)
        with (attempt.workspace / ".vidgrab" / "artifacts.jsonl").open("ab") as journal:
            journal.write(b'{"event":"result","result":')
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("failed",)])
        self.assertEqual(Path(records[0]["path"]).read_bytes(), b"media")

    async def test_missing_success_output_fails_without_inventing_completion(self):
        attempt, records = await self.planned()
        self.publish(attempt, records, success=True)
        Path(records[0]["path"]).unlink()
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("failed", None)])
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")

    async def test_modified_owned_output_fails_even_when_its_inode_and_size_match(self):
        attempt, records = await self.planned()
        output = Path(self.publish(attempt, records, success=True))
        output.write_bytes(b"other")
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("failed", None)])
        self.assertEqual(self.query("SELECT path FROM download_artifacts WHERE kind='media'"), [])
        self.assertEqual(output.read_bytes(), b"other")

    async def test_successful_recovery_releases_only_its_committed_publication_witness(self):
        attempt, records = await self.planned()
        unrelated = attempt.workspace / "unrelated.part"
        unrelated.write_bytes(b"retained partial")
        output = self.publish(attempt, records, success=True)
        await self.store().recover(LATER)
        self.assertFalse(Path(records[0]["witness"]).exists(), "Committed completion must release its temporary witness")
        self.assertEqual(Path(output).read_bytes(), b"media")
        self.assertEqual(unrelated.read_bytes(), b"retained partial")
        self.assertEqual(self.query("SELECT path FROM download_artifacts WHERE kind='auxiliary'"), [(str(unrelated),)])

    async def test_cleanup_after_commit_preserves_evidence_if_the_destination_was_replaced(self):
        attempt, records = await self.planned()
        output = Path(self.publish(attempt, records, success=True))
        self.assertTrue(callable(getattr(self.publication(), "cleanup_publication", None)),
                        "Publication cleanup must verify ownership after the SQLite commit")
        cleanup = self.publication().cleanup_publication

        def replaced(runtime, owned):
            self.assertEqual(self.query("SELECT status FROM downloads"), [("complete",)])
            output.unlink()
            output.write_bytes(b"foreign")
            return cleanup(runtime, owned)

        with patch("backend.db.jobs.cleanup_publication", side_effect=replaced), self.assertLogs("vidgrab.attempts", level="WARNING"):
            await self.store().recover(LATER)
        self.assertEqual(Path(records[0]["witness"]).read_bytes(), b"media")
        self.assertEqual(output.read_bytes(), b"foreign")

    async def test_link_completed_before_its_journal_append_is_reconciled_as_published(self):
        attempt, records = await self.planned()
        with patch("backend.services.publication.append_journal", side_effect=OSError("post-link crash")):
            with self.assertRaisesRegex(OSError, "post-link crash"):
                self.publish(attempt, records)
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("failed",)])
        self.assertEqual(self.query("SELECT kind, state FROM download_artifacts WHERE kind='media'"), [("media", "complete")])
        self.assertEqual(Path(records[0]["path"]).read_bytes(), b"media")

    async def test_missing_witness_refuses_to_adopt_an_existing_output(self):
        attempt, records = await self.planned()
        self.publish(attempt, records, success=True)
        Path(records[0]["witness"]).unlink()
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status, file_path FROM downloads"), [("failed", None)])
        self.assertEqual(self.query("SELECT path FROM download_artifacts WHERE kind='media'"), [])
        self.assertEqual(Path(records[0]["path"]).read_bytes(), b"media")

    async def test_success_result_cannot_omit_a_published_sidecar(self):
        attempt, records = await self.planned(("finished.mp3", "finished.en.srt"))
        self.publish(attempt, records)
        with self.assertRaisesRegex(ValueError, "full publication journal"):
            self.publication().record_result(self.selected_runtime, attempt.workspace, {"filepath": records[0]["path"]})
        await self.store().recover(LATER)
        self.assertEqual(self.query("SELECT status FROM downloads"), [("failed",)])
        self.assertEqual(Path(records[1]["path"]).read_bytes(), b"subtitle")

    async def test_shared_staged_source_cannot_be_published_as_an_owned_file(self):
        from backend.services.artifacts import prepare_workspace
        attempt = await self.store().claim(self.enqueue(), NOW)
        prepare_workspace(self.selected_runtime, attempt)
        foreign = self.root / "foreign.mp3"
        foreign.write_bytes(b"foreign")
        source = attempt.workspace / "linked.mp3"
        os.link(foreign, source)
        output = self.selected_runtime.download_dir / source.name
        with self.assertRaisesRegex(ValueError, "link|owned"):
            self.publication().plan_publications(self.selected_runtime, attempt.workspace,
                                                [{"source": str(source), "path": str(output), "kind": "media"}])
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertFalse(output.exists())

    async def test_filesystem_link_failure_preserves_all_staged_bytes(self):
        attempt, records = await self.planned()
        with patch("backend.services.publication.os.link", side_effect=OSError("links unsupported")):
            with self.assertRaisesRegex(ValueError, "publish|retained"):
                self.publish(attempt, records)
        self.assertFalse(Path(records[0]["path"]).exists())
        self.assertEqual(Path(records[0]["source"]).read_bytes(), b"media")

    async def test_corrupted_postcommit_journal_preserves_witnesses_without_an_unhandled_cleanup_error(self):
        attempt, records = await self.planned()
        output = self.publish(attempt, records, success=True)
        cleanup = self.publication().cleanup_publication

        def corrupt(runtime, owned):
            self.assertEqual(self.query("SELECT status FROM downloads"), [("complete",)])
            frames = [{"source": records[0]["source"], "path": output, "kind": "media", "state": "complete", "size": 5},
                      {"event": "result", "version": 2, "result": {"filepath": output}}]
            (attempt.workspace / ".vidgrab" / "artifacts.jsonl").write_text(
                "".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")
            return cleanup(runtime, owned)

        with patch("backend.db.jobs.cleanup_publication", side_effect=corrupt), self.assertLogs("vidgrab.attempts", level="WARNING"):
            await self.store().recover(LATER)
        self.assertEqual(Path(records[0]["witness"]).read_bytes(), b"media")
        self.assertEqual(Path(output).read_bytes(), b"media")
