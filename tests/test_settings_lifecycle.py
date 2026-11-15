"""Settings commits and notifications remain owned through cancellation."""

import asyncio
import unittest
from unittest.mock import patch

from tests.support import OfflineTestCase


class SettingsLifecycleTests(OfflineTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        self.addCleanup(self.assertEqual, self.loop_errors, [])
        self.application = self.app()
        await self.enterAsyncContext(self.application.router.lifespan_context(self.application))
        self.settings = self.application.state.settings

    async def test_repeated_cancellation_joins_committed_patch_and_notification(self):
        started, release = asyncio.Event(), asyncio.Event()
        notifications = []
        self.settings.on_change = lambda: notifications.append("changed")
        original = self.settings.repository.patch
        async def gated(*args, **kwargs):
            result = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return result
        with patch.object(self.settings.repository, "patch", gated):
            task = asyncio.create_task(self.settings.patch_revision({"max_concurrent": 6}, 0, "joined-settings"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "A settings commit/notification may not detach")
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(notifications, ["changed"])
        replay = await self.settings.patch_revision({"max_concurrent": 6}, 0, "joined-settings")
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(notifications, ["changed"], "Replay must not notify or wake execution")

    async def test_legacy_and_versioned_changes_share_one_notification_boundary(self):
        notifications = []
        self.settings.on_change = lambda: notifications.append("changed")
        await self.settings.patch({"max_retries": 1})
        await self.settings.patch_revision({"max_concurrent": 5}, 1, "versioned-change")
        await self.settings.patch_revision({"max_concurrent": 5}, 2, "noop-change")
        self.assertEqual(notifications, ["changed", "changed"])
