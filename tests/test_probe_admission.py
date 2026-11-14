"""Coalesced probes remain bounded and joined across cancellation/shutdown."""

import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import OfflineTestCase


class ProbeAdmissionTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_injected_lazy_metadata_is_materialized_in_its_joined_thread(self):
        threads = []
        def entries():
            threads.append(threading.get_ident())
            yield {"id": "one", "title": "One", "url": "https://fixture.invalid/one"}
        def probe(*_):
            threads.append(threading.get_ident())
            return {"_type": "playlist", "title": "Fixture", "entries": entries()}
        app = self.app(probe_runner=probe)
        async with app.router.lifespan_context(app):
            await app.state.probes.probe("https://fixture.invalid/list")
        self.assertEqual(threads[0], threads[1])

    async def test_lazy_metadata_is_materialized_before_the_budget_scope_closes(self):
        from backend.format_probe import ProbeResponse, _run_probe
        closed = []
        def entries():
            self.assertEqual(closed, [], "Lazy metadata may issue requests and must retain probe authority")
            yield {"id": "one", "title": "One", "url": "https://fixture.invalid/one"}
        class Extractor:
            def __init__(self, *_):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def extract_info(self, *_args, **_kwargs):
                return {"_type": "playlist", "title": "Fixture", "entries": entries()}
        gate = SimpleNamespace(check=lambda: None, close=lambda: closed.append(True))
        with patch("backend.format_probe.yt_dlp.YoutubeDL", Extractor), \
                patch("backend.services.request_network.install_request_budget", return_value=gate):
            result = _run_probe("https://fixture.invalid/list", self.runtime(), request_context={})
        self.assertIsInstance(result, ProbeResponse, "Probe metadata must be resolved while its request scope is owned")
        self.assertEqual(closed, [True])

    async def test_identical_inflight_probes_share_one_runner(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def probe(url, runtime):
            calls.append(url)
            started.set()
            release.wait(3)
            return {"title": "Fixture", "formats": []}
        app = self.app(probe_runner=probe)
        async with app.router.lifespan_context(app):
            one = asyncio.create_task(app.state.probes.probe("https://fixture.invalid/media"))
            two = asyncio.create_task(app.state.probes.probe("https://fixture.invalid/media"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.gather(one, two)
            self.assertEqual(calls, ["https://fixture.invalid/media"])

    async def test_repeated_probe_cancellation_waits_for_runner_exit(self):
        started, release, exited = threading.Event(), threading.Event(), threading.Event()
        def probe(url, runtime):
            started.set()
            release.wait(3)
            exited.set()
            return {"title": "Fixture", "formats": []}
        app = self.app(probe_runner=probe)
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app.state.probes.probe("https://fixture.invalid/media"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Probe cancellation may not detach its running thread")
                self.assertFalse(exited.is_set())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(exited.is_set())

    async def test_success_cache_is_bounded_even_when_no_entry_has_expired(self):
        app = self.app(probe_runner=lambda *_: {"title": "Fixture", "formats": []}, clock=lambda: 1000)
        async with app.router.lifespan_context(app):
            service = app.state.probes
            for index in range(130):
                await service.probe(f"https://fixture.invalid/{index}")
            self.assertLessEqual(len(service.cache), 128, "Fresh probe results must not grow without a cache bound")
