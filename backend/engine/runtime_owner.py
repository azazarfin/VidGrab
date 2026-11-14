"""A lifespan-owned engine lock plus a durable identity and renewable lease."""

import asyncio
import ctypes
from ctypes import wintypes
import logging
import os
import stat
import sys
import time
from uuid import uuid4

from backend.db.ownership import OwnerStore
from backend.engine.execution import join_owned_task
from backend.engine.process_identity import ProcessIdentity
from backend.runtime import RuntimeOwnershipError

logger = logging.getLogger("vidgrab.owner")
HEARTBEAT_SECONDS = 10.0


def open_windows_lock(path):
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0xC0000000, 3, None, 4, 0x00200080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    # OPEN_REPARSE_POINT opens the link itself; the fstat below refuses it. No delete sharing.
    return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)


class RuntimeOwner:
    def __init__(self, runtime, clock=time.time):
        self.runtime = runtime
        self.clock = clock
        self.id = None
        self.identity = None
        self.lease_expires_at = None
        self.fd = None
        self.active = False
        self.starting = False
        self.failure = None
        self.heartbeat = None
        self.on_failure = None
        self.store = OwnerStore(runtime)

    @property
    def healthy(self):
        return self.active and self.failure is None

    def lock(self):
        if self.fd is not None:
            raise RuntimeOwnershipError("Runtime owner is already in use")
        self.runtime.validate()
        for path in (self.runtime.data_dir, *self.runtime.data_dir.parents):
            try:
                attributes = path.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(attributes.st_mode)
                    or getattr(attributes, "st_file_attributes", 0) & 0x400):
                raise RuntimeOwnershipError("Runtime owner directory has an unsafe link/reparse point")
        self.runtime.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.runtime.data_dir / "engine-owner.lock"
        try:
            try:
                attributes = path.lstat()
                if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                        or getattr(attributes, "st_file_attributes", 0) & 0x400):
                    raise RuntimeOwnershipError("Runtime owner lock must be a regular file without links/reparse points")
            except FileNotFoundError:
                pass
            fd = (open_windows_lock(path) if sys.platform == "win32" else
                  os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600))
            self.fd = fd
            attributes = os.fstat(fd)
            if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                    or getattr(attributes, "st_file_attributes", 0) & 0x400):
                raise RuntimeOwnershipError("Runtime owner lock is an unsafe file")
            os.set_inheritable(fd, False)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if attributes.st_size == 0:
                os.write(fd, b"\0")
            self.identity = ProcessIdentity.capture()
        except BaseException as error:
            self.close_lock()
            if isinstance(error, OSError):
                raise RuntimeOwnershipError("Runtime is already in use or its owner lock cannot be verified") from error
            raise

    def require_lock(self):
        if self.fd is None or self.identity is None or self.identity.pid != os.getpid():
            raise RuntimeOwnershipError("Runtime has no current engine owner lock")

    async def start(self):
        if self.active or self.starting:
            raise RuntimeOwnershipError("Runtime owner is already active")
        if self.fd is None:
            self.lock()
        self.require_lock()
        self.starting = True
        self.id = str(uuid4())
        acquisition = asyncio.create_task(self.store.acquire(self, self.clock()))
        try:
            await join_owned_task(acquisition)
        finally:
            self.starting = False
            if not acquisition.cancelled() and acquisition.exception() is None:
                self.lease_expires_at = acquisition.result()
                self.active = True
        self.failure = None
        self.heartbeat = asyncio.create_task(self._heartbeat())

    async def renew(self):
        self.lease_expires_at = await self.store.renew(self, self.clock())

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await self.renew()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failure = error
            logger.exception("Runtime owner lease renewal failed")
            if self.on_failure:
                await self.on_failure()

    async def stop(self, *, release=True):
        try:
            if self.heartbeat:
                self.heartbeat.cancel()
                try:
                    await self.heartbeat
                except asyncio.CancelledError:
                    pass
                self.heartbeat = None
            if self.active and release:
                await self.store.release(self, self.clock())
        finally:
            self.active = False
            self.close_lock()

    def close_lock(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
