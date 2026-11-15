"""Isolated acceptance checks for the versioned control/settings seam."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import OfflineTestCase


class ProtocolTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        from backend.engine import create_engine_app
        self.runtime_config = self.runtime()
        self.engine = create_engine_app(
            self.runtime_config, token="a" * 64, port=43210,
            components={"ffmpeg": "/bundled/ffmpeg", "ffprobe": "/bundled/ffprobe", "deno": "/bundled/deno"},
            start_queue=False,
        )
        self.client = self.enterContext(TestClient(
            self.engine, base_url="http://127.0.0.1:43210",
            headers={"Authorization": "Bearer " + "a" * 64},
        ))
        self.request_number = 0

    def send(self, operation, params=None, *, key=None, request_id=None, version=1):
        self.request_number += 1
        request = {"protocol_version": version, "request_id": request_id or f"request-{self.request_number}",
                   "operation": operation, "params": params or {}}
        if key is not None:
            request["idempotency_key"] = key
        return self.client.post("/v1/requests", json=request)

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIs(result.get("ok"), True, result)
        self.assertEqual(result["protocol_version"], 1)
        return result["data"]

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        result = response.json()
        self.assertIs(result.get("ok"), False, result)
        self.assertEqual(result["error"]["code"], code)
        self.assertEqual(result["error"]["request_id"], result["request_id"])
        self.assertIs(result["error"]["retryable"], False)
        return result["error"]

    def settings(self):
        return self.ok(self.send("settings.get"))

    def patch_settings(self, revision, patch, key="settings-one"):
        return self.send("settings.patch", {"expected_revision": revision, "patch": patch}, key=key)

    def rows(self, sql, parameters=()):
        with closing(sqlite3.connect(self.runtime_config.db_path)) as database:
            return database.execute(sql, parameters).fetchall()

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.runtime_config.db_path)) as database, database:
            database.execute(sql, parameters)

    def test_capabilities_negotiate_version_and_explicit_fixture_limits(self):
        capabilities = self.ok(self.send("capabilities.get", request_id="desktop-client"))
        self.assertEqual((capabilities["protocol_min"], capabilities["protocol_max"]), (1, 1))
        self.assertIs(capabilities["fixture_only"], True)
        self.assertIs(capabilities["paired_loopback"], False)
        self.assertEqual(capabilities["max_message_bytes"], 65536)
        self.assertIn("settings.revisions", capabilities["features"])
        self.assertIn("events.snapshots", capabilities["features"])

    def test_incompatible_versions_report_an_upgrade_without_mutating_state(self):
        error = self.error(self.send("settings.patch", {"expected_revision": 0, "patch": {"max_concurrent": 8}},
                                    key="version-two", version=2, request_id="old-extension"),
                           409, "APP_INCOMPATIBLE")
        self.assertIn("Update", error["message"])
        self.assertEqual(error["request_id"], "old-extension")
        self.assertEqual(self.settings()["revision"], 0)

    def test_authentication_and_origin_policy_run_before_versioned_routing(self):
        message = {"protocol_version": 1, "request_id": "denied", "operation": "settings.get", "params": {}}
        for headers, status in (({"Authorization": "Bearer wrong"}, 401),
                                ({"Origin": "chrome-extension://unpaired"}, 403),
                                ({"Host": "attacker.invalid"}, 403)):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.client.post("/v1/requests", json=message, headers=headers).status_code, status)

    def test_strict_envelope_and_settings_validation_have_typed_safe_errors(self):
        for params in ({"expected_revision": True, "patch": {}},
                       {"expected_revision": "0", "patch": {}},
                       {"expected_revision": 0, "patch": {"speed_limit": None}},
                       {"expected_revision": 0, "patch": {"max_concurrent": 11}},
                       {"expected_revision": 0, "patch": {"cookies": "must-not-echo-this"}}):
            with self.subTest(params=params):
                response = self.send("settings.patch", params, key="invalid")
                self.error(response, 422, "BAD_REQUEST")
                self.assertNotIn("must-not-echo-this", response.text)
        self.error(self.send("settings.patch", {"expected_revision": 0, "patch": {}}), 422, "BAD_REQUEST")
        self.error(self.send("settings.get", {"unknown": 1}), 422, "BAD_REQUEST")
        self.error(self.send("process.execute", {"command": "secret"}), 404, "UNSUPPORTED_OPERATION")
        self.assertEqual(self.settings()["revision"], 0)

    def test_two_clients_observe_partial_settings_and_stale_conflicts(self):
        desktop, extension = self.settings(), self.settings()
        changed = self.ok(self.patch_settings(desktop["revision"], {"max_concurrent": 6, "download_threads": 2}))
        self.assertEqual(changed["revision"], desktop["revision"] + 1)
        current = self.settings()
        self.assertEqual(current, changed)
        self.assertEqual(current["values"]["default_quality"], desktop["values"]["default_quality"])
        conflict = self.error(self.patch_settings(extension["revision"], {"max_retries": 1}, "stale-extension"),
                              409, "REVISION_CONFLICT")
        self.assertEqual(conflict["details"]["settings"], current)
        self.assertEqual(self.settings(), current)

    def test_concurrent_same_revision_has_exactly_one_winner(self):
        revision = self.settings()["revision"]
        def update(index):
            return self.client.post("/v1/requests", json={
                "protocol_version": 1, "request_id": f"concurrent-{index}", "operation": "settings.patch",
                "idempotency_key": f"concurrent-{index}",
                "params": {"expected_revision": revision, "patch": {"max_concurrent": index + 4}},
            })
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(update, [0, 1]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(self.settings()["revision"], revision + 1)

    def test_keyed_settings_replay_survives_later_changes_and_does_not_reapply(self):
        original = self.ok(self.patch_settings(0, {"max_concurrent": 6}, "lost-settings-reply"))
        current = self.ok(self.patch_settings(original["revision"], {"max_concurrent": 7}, "later-settings"))
        replay = self.ok(self.patch_settings(0, {"max_concurrent": 6}, "lost-settings-reply"))
        self.assertEqual(replay, original)
        self.assertEqual(self.settings(), current)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM settings_receipts"), [(2,)])
        self.error(self.patch_settings(0, {"max_concurrent": 8}, "lost-settings-reply"), 409, "IDEMPOTENCY_CONFLICT")

    def test_replayed_settings_do_not_wake_download_execution(self):
        self.ok(self.patch_settings(0, {"max_concurrent": 6}, "settings-wakeup"))
        queue = self.engine.app.state.queue
        queue.wakeup_event.clear()
        self.ok(self.patch_settings(0, {"max_concurrent": 6}, "settings-wakeup"))
        self.assertFalse(queue.wakeup_event.is_set(), "A settings replay must not wake execution")

    def test_stale_output_patch_does_not_create_a_directory(self):
        current = self.ok(self.patch_settings(0, {"max_concurrent": 4}))
        candidate = self.root / "stale-directory"
        self.error(self.patch_settings(0, {"download_dir": str(candidate)}, "stale-path"), 409, "REVISION_CONFLICT")
        self.assertFalse(candidate.exists())
        self.assertEqual(self.settings(), current)

    def test_noop_patch_keeps_revision_cursor_and_missing_directory(self):
        before = self.settings()
        snapshot = self.ok(self.send("events.read"))
        self.runtime_config.download_dir.rmdir()
        self.assertEqual(self.ok(self.patch_settings(before["revision"], {}, "empty")), before)
        self.assertEqual(self.ok(self.patch_settings(before["revision"], {"max_concurrent": 3}, "unchanged")), before)
        self.assertFalse(self.runtime_config.download_dir.exists())
        after = self.ok(self.send("events.read"))
        self.assertEqual(after["cursor"], snapshot["cursor"])

    def test_legacy_patch_invalidates_versioned_revision_in_the_same_store(self):
        before = self.settings()
        legacy = TestClient(self.engine.app)
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.patch("/config", json={"max_retries": 1}).status_code, 200)
        current = self.settings()
        self.assertEqual(current["revision"], before["revision"] + 1)
        self.assertEqual(current["values"]["max_retries"], 1)
        self.error(self.patch_settings(before["revision"], {"max_concurrent": 5}), 409, "REVISION_CONFLICT")

    def test_retired_owner_cannot_patch_settings_or_write_receipts(self):
        before = self.settings()
        owner_id = self.engine.app.state.owner.id
        self.execute("UPDATE runtime_owners SET released_at=1 WHERE id=?", (owner_id,))
        try:
            self.error(self.patch_settings(before["revision"], {"max_concurrent": 6}), 503, "ENGINE_UNAVAILABLE")
            self.assertEqual(self.rows("SELECT settings_revision FROM protocol_state"), [(before["revision"],)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM settings_receipts"), [(0,)])
        finally:
            self.execute("UPDATE runtime_owners SET released_at=NULL WHERE id=?", (owner_id,))

    def test_receipt_write_failure_rolls_back_settings_revision_and_event(self):
        before = self.settings()
        cursor = self.ok(self.send("events.read"))["cursor"]
        self.execute("CREATE TRIGGER refuse_settings_receipt BEFORE INSERT ON settings_receipts "
                     "BEGIN SELECT RAISE(ABORT, 'private receipt failure'); END")
        response = self.patch_settings(before["revision"], {"max_concurrent": 6})
        self.error(response, 500, "INTERNAL_ERROR")
        self.assertNotIn("private receipt failure", response.text)
        self.assertEqual(self.settings(), before)
        self.assertEqual(self.ok(self.send("events.read"))["cursor"], cursor)

    def test_response_budget_failure_rolls_back_a_settings_mutation(self):
        self.execute("UPDATE config SET value=? WHERE key='custom_ytdlp_args'", (json.dumps("a" * 64000),))
        candidate = self.root / "oversized-response-directory"
        self.error(self.patch_settings(0, {"max_concurrent": 6, "download_dir": str(candidate)}, "large-settings"),
                   413, "MESSAGE_TOO_LARGE")
        self.assertFalse(candidate.exists(), "A refused settings reply must not create an output directory")
        self.assertEqual(self.rows("SELECT value FROM config WHERE key='max_concurrent'"), [("3",)])
        self.assertEqual(self.rows("SELECT settings_revision FROM protocol_state"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM settings_receipts"), [(0,)])

    def test_versioned_numeric_settings_use_exact_browser_integers(self):
        self.error(self.patch_settings(0, {"speed_limit": 2**53}, "unsafe-integer"), 422, "BAD_REQUEST")
        self.assertEqual(self.settings()["revision"], 0)

    def test_wide_legacy_integer_has_an_actionable_repair_without_partial_writes(self):
        self.execute("UPDATE config SET value=? WHERE key='speed_limit'", (json.dumps(2**53),))
        error = self.error(self.send("settings.get"), 409, "UNSUPPORTED_OPTION")
        self.assertEqual(error["details"]["settings_revision"], 0)
        self.error(self.patch_settings(0, {"max_concurrent": 6}, "unsafe-result"), 409, "UNSUPPORTED_OPTION")
        self.assertEqual(self.rows("SELECT value FROM config WHERE key='max_concurrent'"), [("3",)])
        fixed = self.ok(self.patch_settings(0, {"speed_limit": 0}, "repair-integer"))
        self.assertEqual(fixed["revision"], 1)
        self.assertEqual(fixed["values"]["speed_limit"], 0)

    def test_new_contract_refuses_legacy_executable_arguments(self):
        before = self.settings()
        self.error(self.patch_settings(before["revision"], {"custom_ytdlp_args": "--exec secret"}),
                   422, "UNSUPPORTED_OPTION")
        self.assertEqual(self.settings(), before)

    def test_keyed_enqueue_and_reconnect_do_not_duplicate_jobs(self):
        initial = self.ok(self.send("events.read"))
        params = {"url": "http://127.0.0.1:8080/fixture.wav", "title": "contract fixture"}
        first = self.ok(self.send("downloads.enqueue", params, key="lost-download-reply"))
        replay = self.ok(self.send("downloads.enqueue", params, key="lost-download-reply"))
        self.assertEqual(first["job_id"], replay["job_id"])
        jobs = self.ok(self.send("downloads.list"))
        self.assertEqual([job["id"] for job in jobs["jobs"]], [first["job_id"]])
        self.assertEqual(jobs["jobs"][0]["revision"], 1)
        events = self.ok(self.send("events.read", {"instance_id": initial["instance_id"], "cursor": initial["cursor"]}))
        self.assertIs(events["resnapshot"], False)
        self.assertEqual([event["entity_id"] for event in events["events"]], [first["job_id"]])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(1,)])

    def test_enqueue_replay_keeps_first_app_defaults_after_settings_change(self):
        params = {"url": "http://127.0.0.1:8080/fixture.wav"}
        first = self.ok(self.send("downloads.enqueue", params, key="default-intent"))
        original_format = self.rows("SELECT format_selected FROM downloads WHERE id=?", (first["job_id"],))[0][0]
        self.ok(self.patch_settings(0, {"default_format": "worst"}, "change-default"))
        replay = self.ok(self.send("downloads.enqueue", params, key="default-intent"))
        self.assertEqual(replay, first)
        self.assertEqual(self.rows("SELECT format_selected FROM downloads WHERE id=?", (first["job_id"],)), [(original_format,)])

    def test_invalid_envelope_keeps_a_valid_request_id_without_echoing_input(self):
        response = self.client.post("/v1/requests", json={"protocol_version": "1", "request_id": "bad-envelope",
                                   "operation": "settings.get", "params": {}, "private": "secret-input"})
        error = self.error(response, 422, "BAD_REQUEST")
        self.assertEqual(error["request_id"], "bad-envelope")
        self.assertNotIn("secret-input", response.text)

    def test_v1_pre_route_errors_have_the_typed_error_envelope(self):
        response = self.client.post("/v1/requests", json={}, headers={"Authorization": "Bearer wrong"})
        self.error(response, 401, "UNAUTHORIZED")

    def test_oversized_versioned_request_is_refused_before_admission(self):
        response = self.client.post("/v1/requests", content=b"x" * 65537)
        self.error(response, 413, "MESSAGE_TOO_LARGE")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(0,)])

    def test_corrupt_server_result_is_an_internal_error(self):
        self.execute("INSERT INTO downloads(id,url,status,progress,created_at) VALUES ('bad-result','https://fixture.invalid/media','complete',101,'old')")
        self.error(self.send("downloads.list"), 500, "INTERNAL_ERROR")

    def test_rest_numeric_inputs_are_bounded_before_integer_conversion(self):
        response = self.client.get("/v1/settings", headers={"X-Protocol-Version": "9" * 5000})
        self.error(response, 422, "BAD_REQUEST")
        response = self.client.get("/v1/events?cursor=" + "9" * 5000)
        self.error(response, 422, "BAD_REQUEST")

    def test_deep_json_is_a_bounded_request_error(self):
        response = self.client.post("/v1/requests", content=b"[" * 1500 + b"0" + b"]" * 1500)
        self.error(response, 422, "BAD_REQUEST")

    def test_packaged_versioned_enqueue_keeps_fixture_and_option_allowlists(self):
        for params in ({"url": "https://fixture.invalid/media"},
                       {"url": "http://localhost:8080/fixture.wav"},
                       {"url": "http://127.0.0.1:8080/fixture.wav", "extra_args": {"exec": "unsafe"}},
                       {"url": "http://127.0.0.1:8080/fixture.wav", "cookies": [{"value": "private-cookie"}]}):
            with self.subTest(params=params):
                response = self.send("downloads.enqueue", params, key="refused-download")
                self.assertIn(response.status_code, [422, 409])
                self.assertIs(response.json().get("ok"), False)
                self.assertNotIn("private-cookie", response.text)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM downloads"), [(0,)])

    def test_snapshot_pagination_and_counts_include_every_unfinished_job(self):
        with closing(sqlite3.connect(self.runtime_config.db_path)) as database, database:
            database.executemany("INSERT INTO downloads (id,url,title,status,created_at) VALUES (?,?,?,?,?)",
                                 [(f"job-{index:03}", "https://fixture.invalid/media", "fixture",
                                   "queued" if index == 50 else "complete", str(index)) for index in range(51)])
        first = self.ok(self.send("downloads.list", {"limit": 25}))
        second = self.ok(self.send("downloads.list", {"limit": 25, "after": first["next_after"]}))
        third = self.ok(self.send("downloads.list", {"limit": 25, "after": second["next_after"]}))
        self.assertEqual(len(first["jobs"]) + len(second["jobs"]) + len(third["jobs"]), 51)
        self.assertEqual(first["counts"], {"total": 51, "unfinished": 1})
        self.assertIsNone(third["next_after"])
        self.execute("UPDATE downloads SET file_path=?", (str(self.root.joinpath(*["p" * 200] * 15, "media.mp3")),))
        snapshot = self.ok(self.send("events.read", {"limit": 1}))["snapshot"]
        self.assertEqual(len(snapshot["jobs"]), 1, "Reconnect snapshots must honor the requested page limit")
        self.assertEqual(snapshot["counts"], first["counts"])
        self.assertIsNotNone(snapshot["next_after"])

    def test_durable_job_changes_and_deletions_have_revisions_without_sensitive_payloads(self):
        job = self.ok(self.send("downloads.enqueue", {"url": "http://127.0.0.1:8080/fixture.wav"}, key="event-job"))["job_id"]
        before = self.ok(self.send("events.read"))
        literal = str(self.root / "existing বাংলা directory" / "chosen.mp3")
        self.execute("UPDATE downloads SET progress=100,status='complete',file_path=? WHERE id=?", (literal, job))
        current = self.ok(self.send("downloads.list"))["jobs"][0]
        self.assertEqual(current["file_path"], literal)
        self.assertEqual(current["revision"], 2)
        changes = self.ok(self.send("events.read", {"instance_id": before["instance_id"], "cursor": before["cursor"]}))
        self.assertEqual(changes["events"][0]["revision"], 2)
        self.assertNotIn("fixture.wav", json.dumps(changes["events"]))
        self.execute("DELETE FROM downloads WHERE id=?", (job,))
        deleted = self.ok(self.send("events.read", {"instance_id": changes["instance_id"], "cursor": changes["cursor"]}))
        self.assertEqual(deleted["events"][0]["kind"], "deleted")

    def test_event_gaps_foreign_instances_and_future_cursors_require_consistent_snapshots(self):
        old = self.ok(self.send("events.read"))
        with closing(sqlite3.connect(self.runtime_config.db_path)) as database, database:
            database.executemany("INSERT INTO downloads (id,url,status,created_at) VALUES (?,?,?,?)",
                                 [(f"gap-{index:04}", "https://fixture.invalid/media", "complete", str(index)) for index in range(520)])
        self.assertLessEqual(self.rows("SELECT COUNT(*) FROM protocol_events")[0][0], 512)
        for instance, cursor in ((old["instance_id"], old["cursor"]), ("different-engine", 0),
                                 (old["instance_id"], 999999)):
            with self.subTest(instance=instance, cursor=cursor):
                response = self.ok(self.send("events.read", {"instance_id": instance, "cursor": cursor}))
                self.assertIs(response["resnapshot"], True)
                self.assertEqual(response["snapshot"]["counts"]["total"], 520)
                self.assertEqual(response["snapshot"]["cursor"], response["cursor"])
                self.assertEqual(response["snapshot"]["settings"], self.settings())

    def test_bounded_event_batches_advance_only_through_delivered_events(self):
        initial = self.ok(self.send("events.read"))
        self.ok(self.patch_settings(0, {"max_concurrent": 4}, "event-one"))
        self.ok(self.patch_settings(1, {"max_concurrent": 5}, "event-two"))
        one = self.ok(self.send("events.read", {"instance_id": initial["instance_id"], "cursor": initial["cursor"], "limit": 1}))
        self.assertEqual(len(one["events"]), 1)
        self.assertIs(one["has_more"], True)
        two = self.ok(self.send("events.read", {"instance_id": one["instance_id"], "cursor": one["cursor"], "limit": 1}))
        self.assertEqual(len(two["events"]), 1)
        self.assertIs(two["has_more"], False)
        self.assertGreater(two["cursor"], one["cursor"])

    def test_rest_settings_adapter_uses_the_same_revisions_and_request_ids(self):
        response = self.client.get("/v1/settings", headers={"X-Request-ID": "rest-read"})
        before = self.ok(response)
        self.assertEqual(response.json()["request_id"], "rest-read")
        changed = self.ok(self.client.patch("/v1/settings", headers={"X-Request-ID": "rest-write", "Idempotency-Key": "rest-key"},
                                           json={"expected_revision": before["revision"], "patch": {"max_concurrent": 5}}))
        self.assertEqual(changed, self.settings())

    def test_schema_eight_upgrade_preserves_history_and_creates_no_attempts(self):
        from backend.config import runtime_defaults
        from backend.db import migrations
        import aiosqlite
        other = self.runtime("schema-eight")
        other.prepare()
        async def seed():
            async with aiosqlite.connect(other.db_path) as database:
                for number in range(1, 9):
                    names = ["one", "two", "three", "four", "five", "six", "seven", "eight"]
                    await getattr(migrations, "_migration_" + names[number - 1])(database)
                    await database.execute(f"PRAGMA user_version={number}")
                await database.executemany("INSERT INTO config(key,value) VALUES (?,?)",
                                           [(key, json.dumps(value)) for key, value in runtime_defaults(other).items()])
                await database.execute("INSERT INTO downloads(id,url,status,created_at) VALUES ('legacy','https://fixture.invalid/media','complete','old')")
                await database.commit()
        asyncio.run(seed())
        app = self.app(other)
        with TestClient(app):
            with closing(sqlite3.connect(other.db_path)) as database:
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertEqual(database.execute("SELECT status,revision FROM downloads WHERE id='legacy'").fetchone(), ("complete", 1))
                self.assertEqual(database.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0], 0)
        snapshots = list(other.backups_dir.glob("schema-v8-*.sqlite3"))
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 8)


class ProtocolGenerationTests(OfflineTestCase):
    def test_contract_schema_export_has_no_application_side_effects(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "backend/api/models.py").is_file(), "P04 needs import-safe Pydantic contract models")
        code = '''
import sys
def audit(event,args):
    if event in {'sqlite3.connect','os.mkdir','subprocess.Popen','socket.connect','socket.getaddrinfo'}:
        raise AssertionError('Contract export attempted runtime I/O: '+event)
sys.addaudithook(audit)
from backend.api.models import contract_schema
schema=contract_schema()
assert schema['protocol_version']==1
assert 'settings.patch' in schema['operations']
'''
        result = self.run_import_check(code, root, {})
        self.assertEqual(result.returncode, 0, result.stderr)
