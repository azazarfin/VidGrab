"""Opt-in P05 probe witnesses through actual owned source/frozen media workers.

Generated clear HLS/DASH audio stays on loopback. The caller/probe runs from
source; --engine selects genuine frozen workers. No installed state is used.
"""

import argparse
import json
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

from backend.api.models import Request
from backend.engine.network_guard import fixture_network_guard
from backend.engine.resources import check_components
from backend.services.control import ControlService
from tests import check_verified_resume as direct
from tests.check_fragment_resume import FragmentServer


class FormatRepairChecks(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = direct.VerifiedResumeChecks.asyncTearDown
    rows = direct.VerifiedResumeChecks.rows

    async def asyncSetUp(self):
        await direct.VerifiedResumeChecks.asyncSetUp(self)
        self.app.state.probes.components = dict(direct.COMPONENTS)
        self.queue.allow_legacy_options = False
        self.control = ControlService(self.app.state.settings, self.queue, self.app.state.owner,
                                      fixture_validator=None, probes=self.app.state.probes)
        self.addCleanup(self.control.close)
        self.runtime.download_dir.mkdir(parents=True, exist_ok=True)
        self.unrelated = self.runtime.download_dir / "unrelated.part"
        self.unrelated.write_bytes(b"preserve this unrelated partial")

    async def request(self, operation, params, *, mutation=False):
        values = {"protocol_version": 1, "request_id": str(uuid4()), "operation": operation, "params": params}
        if mutation:
            values["idempotency_key"] = str(uuid4())
        return await self.control.dispatch(Request.model_validate(values))

    async def selection(self, protocol, *, rotate=True, incompatible=False, headers=False):
        with FragmentServer(protocol) as server:
            server.gated = False
            if protocol == "hls":
                server.parts["/variant.m3u8"] = server.manifest
                server.manifest = (b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\n'
                                   b'variant.m3u8\n')
            params = {"url": server.url}
            received_headers = []
            if headers:
                referer = server.url.rsplit("/", 1)[0] + "/"
                original = server.server.RequestHandlerClass

                class HeaderGuard(original):
                    def respond(self, body):
                        received_headers.append(self.headers.get("Referer"))
                        if self.headers.get("Referer") != referer:
                            self.send_error(403)
                            return
                        super().respond(body)

                server.server.RequestHandlerClass = HeaderGuard
                context = await self.request("auth.create", {"instance_id": self.app.state.owner.id,
                    "url": server.url, "cookies": [], "referer": referer}, mutation=True)
                params["auth_ref"] = context["reference"]
            probe = await self.request("probes.create", params)
            selected = next(row for row in probe["formats"] if row["acodec"] not in (None, "none"))
            if incompatible:
                server.manifest = server.manifest.replace(b'audio/mp4', b'audio/webm')
            elif rotate:
                server.manifest = (server.manifest.replace(b'BANDWIDTH=64000', b'BANDWIDTH=96000')
                                   if protocol == "hls" else server.manifest.replace(b'id="audio"', b'id="fresh-audio"'))
            accepted = await self.request("downloads.enqueue", {**params, "format_id": selected["format_id"],
                                         "probe_id": probe["probe_id"]}, mutation=True)
            job = accepted["job_id"]
            options = json.loads(self.rows("SELECT download_options FROM downloads WHERE id=?", (job,))[0][0])
            self.assertEqual(options["format_intent"]["format_id"], selected["format_id"])
            self.assertNotIn("auth_ref", options)
            self.assertNotIn("referer", options)
            server.requests.clear()
            await self.queue._run_task(job, server.url, selected["format_id"], self.config, options)
            status, path, code, error = self.rows(
                "SELECT status,file_path,error_code,error_message FROM downloads WHERE id=?", (job,))[0]
            if incompatible:
                self.assertEqual((status, path, code), ("failed", None, "FORMAT_UNAVAILABLE"), error)
                self.assertFalse(any(path == "/init.mp4" or path.endswith(".m4s") for _, path in server.requests))
                self.assertEqual(list(self.runtime.download_dir.iterdir()), [self.unrelated])
            else:
                self.assertEqual(status, "complete", error)
                self.assertEqual(server.decoded_hash(path), server.decoded)
                self.assertIn("বাংলা media", str(path))
                self.assertEqual(self.rows("SELECT status FROM download_attempts WHERE download_id=?", (job,)), [("complete",)])
            if headers:
                self.assertGreater(len(received_headers), 2)
                self.assertEqual(set(received_headers), {referer})
                self.assertNotIn(job, self.queue.task_headers)
                self.assertEqual(self.rows("SELECT count(*) FROM attempt_cookie_files"), [(0,)])
            self.assertEqual(self.unrelated.read_bytes(), b"preserve this unrelated partial")

    async def test_hls_rotated_format_id_retains_witnessed_audio_and_decoded_media(self):
        await self.selection("hls")

    async def test_dash_rotated_format_id_retains_witnessed_audio_and_decoded_media(self):
        await self.selection("dash")

    async def test_unchanged_dash_selection_completes_through_the_owned_worker(self):
        await self.selection("dash", rotate=False)

    async def test_changed_dash_container_fails_before_fragments_without_fallback(self):
        await self.selection("dash", incompatible=True)

    async def test_header_only_probe_and_rotated_dash_worker_share_volatile_context(self):
        await self.selection("dash", headers=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Choose a fresh report; existing evidence is preserved")
    direct.COMPONENTS, versions = check_components(args.tools.resolve())
    direct.ENGINE = args.engine.resolve(strict=True) if args.engine else None
    started = time.monotonic()
    sys.addaudithook(fixture_network_guard)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FormatRepairChecks))
    report = {"ok": result.wasSuccessful(), "tests": result.testsRun, "seconds": round(time.monotonic() - started, 3),
              "engine": str(direct.ENGINE) if direct.ENGINE else "source", "versions": versions,
              "scope": "source probes/caller, owned source or frozen workers, generated clear loopback audio",
              "failures": [str(test) + ": " + trace for test, trace in result.failures + result.errors]}
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps({key: report[key] for key in ("ok", "tests", "seconds", "engine")}))
    raise SystemExit(not report["ok"])


if __name__ == "__main__":
    main()
