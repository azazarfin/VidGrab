"""Async execution adapters. Only explicitly injected callables use threads."""

import asyncio
import json
import os
import signal
import sys
import tempfile

from backend.engine.worker_owner import BOOTSTRAP_LIMIT, MESSAGE_LIMIT


async def join_owned_task(task):
    """Defer caller cancellation until an owned task has exited and been retrieved."""
    cancelled = False
    while not task.done():
        try:
            # wait() does not propagate cancellation to the task or abandon an
            # exception-bearing shield future when its caller is interrupted.
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def entry_command(module, frozen_flag):
    if getattr(sys, "frozen", False):
        return [sys.executable, frozen_flag]
    return [sys.executable, "-B", "-m", module]


class ThreadDownloadExecutor:
    """Diagnostic/test boundary that joins its injected synchronous callable."""

    def __init__(self, runner):
        self.runner = runner

    async def run(self, **options):
        execution = asyncio.create_task(asyncio.to_thread(self.runner, **options))
        return await join_owned_task(execution)


class ProcessDownloadExecutor:
    def __init__(self, *, components=None, fixture_only=False):
        self.components = components
        self.fixture_only = fixture_only

    async def run(self, **options):
        runtime = options.pop("runtime")
        callback = options.pop("progress_callback")
        options.pop("is_cancelled_cb", None)
        bootstrap = {"options": options, "components": self.components, "fixture_only": self.fixture_only,
                     "runtime": {key: str(getattr(runtime, key)) for key in
                                 ("data_dir", "download_dir", "logs_dir", "temp_dir")}}
        encoded = (json.dumps(bootstrap, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > BOOTSTRAP_LIMIT:
            raise ValueError("Worker bootstrap exceeds its framing limit")
        owner = entry_command("backend.engine.worker_owner", "--worker-owner")
        worker = entry_command("backend.engine.worker", "--worker")
        process = None
        worker_pid = None
        with tempfile.TemporaryFile() as log:
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *owner, "--", *worker, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=log, limit=MESSAGE_LIMIT,
                close_fds=True, creationflags=0x08000000 if sys.platform == "win32" else 0,
            ))
            try:
                process = await join_owned_task(launch)
                process.stdin.write(encoded)
                await process.stdin.drain()
                result = None
                while line := await process.stdout.readline():
                    if len(line) > MESSAGE_LIMIT:
                        raise ValueError("Worker response exceeds its framing limit")
                    message = json.loads(line)
                    if message.get("type") == "started":
                        worker_pid = message["pid"]
                    elif message.get("type") == "progress":
                        callback(options["task_id"], message["data"])
                    elif message.get("type") == "result":
                        result = message["data"]
                    elif message.get("type") == "error":
                        raise RuntimeError(message.get("message", "Worker failed"))
                    else:
                        raise ValueError("Invalid worker response")
                if await process.wait() != 0 or result is None:
                    raise RuntimeError("Worker owner exited without a successful result")
                return result
            finally:
                if process is None:
                    process = launch.result()
                process.stdin.close()
                cleanup = asyncio.create_task(self._join(process, worker_pid))
                await join_owned_task(cleanup)

    async def _join(self, process, worker_pid):
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            if sys.platform == "linux" and worker_pid:
                try:
                    os.killpg(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=3)
