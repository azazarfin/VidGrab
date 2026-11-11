"""Owned async tasks must remain joined through repeated cancellation."""

import asyncio
import unittest
from unittest.mock import patch

from backend.engine.execution import ProcessDownloadExecutor
from tests.support import OfflineTestCase


class ControlledProcess:
    """A process boundary whose teardown is explicitly gated by the test."""

    def __init__(self):
        self.stdin = self
        self.stdout = self
        self.closed = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.release = asyncio.Event()
        self.joined = False
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass

    async def readline(self):
        await asyncio.Event().wait()

    def close(self):
        self.closed.set()

    async def communicate(self):
        self.cleanup_started.set()
        await self.release.wait()
        self.joined = True
        return b"", b""


class ProcessExecutionTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_does_not_detach_process_cleanup(self):
        process = ControlledProcess()

        async def launch(*args, **kwargs):
            return process

        with patch("asyncio.create_subprocess_exec", launch):
            execution = asyncio.create_task(ProcessDownloadExecutor().run(
                runtime=self.runtime(), task_id="fixture", progress_callback=lambda *_: None,
            ))
            try:
                while not process.writes:
                    await asyncio.sleep(0)
                execution.cancel()
                await asyncio.wait_for(process.cleanup_started.wait(), 1)
                execution.cancel()
                await asyncio.sleep(0)
                self.assertFalse(execution.done(), "A second cancellation must not detach process-tree cleanup")
                self.assertFalse(process.joined)
            finally:
                process.release.set()
                await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(process.joined)
        self.assertTrue(process.closed.is_set())
        self.assertTrue(execution.cancelled())

    async def test_cancel_during_launch_still_closes_and_joins_the_process(self):
        process = ControlledProcess()
        launching = asyncio.Event()
        release_launch = asyncio.Event()

        async def launch(*args, **kwargs):
            launching.set()
            await release_launch.wait()
            return process

        with patch("asyncio.create_subprocess_exec", launch):
            execution = asyncio.create_task(ProcessDownloadExecutor().run(
                runtime=self.runtime(), task_id="fixture", progress_callback=lambda *_: None,
            ))
            try:
                await launching.wait()
                execution.cancel()
                await asyncio.sleep(0)
                execution.cancel()
                await asyncio.sleep(0)
                self.assertFalse(execution.done(), "The owner must be joined even when launch is interrupted twice")
                release_launch.set()
                try:
                    await asyncio.wait_for(process.cleanup_started.wait(), 1)
                except TimeoutError:
                    self.fail("Interrupted launch detached the owner instead of joining it")
                self.assertFalse(execution.done())
            finally:
                release_launch.set()
                process.release.set()
                await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(process.joined)
        self.assertTrue(process.closed.is_set())
        self.assertEqual(process.writes, [], "An interrupted launch must not bootstrap a download")
        self.assertTrue(execution.cancelled())
