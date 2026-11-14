"""Read-only Linux/Windows identities. An inaccessible process is never assumed dead."""

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID

from backend.runtime import RuntimeOwnershipError


@lru_cache(maxsize=1)
def host_identity():
    if sys.platform == "linux":
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        machine = Path("/etc/machine-id").read_text().strip() or boot
    elif sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            machine = winreg.QueryValueEx(key, "MachineGuid")[0]
        # Windows process creation is an absolute FILETIME, rather than boot-relative ticks.
        boot = ""
    else:
        raise RuntimeOwnershipError("Runtime ownership currently supports Linux and Windows")
    return hashlib.sha256((sys.platform + ":" + machine).encode()).hexdigest(), boot


def linux_process(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = value[value.rindex(")") + 2:].split()
    return {"started": int(fields[19]), "group": int(fields[2]), "session": int(fields[3]), "state": fields[0]}


@lru_cache(maxsize=1)
def windows_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessTimes": ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def windows_started(pid):
    kernel = windows_kernel()
    handle = kernel.OpenProcess(0x101000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: this PID does not exist.
            return None
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:
            return None
        if state != 258:
            raise ctypes.WinError(ctypes.get_last_error())
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started: int
    platform: str
    host: str
    boot: str

    @classmethod
    def capture(cls, pid=None):
        pid = os.getpid() if pid is None else pid
        host, boot = host_identity()
        if sys.platform == "linux":
            process = linux_process(pid)
            started = process["started"] if process and process["state"] not in {"Z", "X"} else None
        else:
            started = windows_started(pid)
        return None if started is None else cls(pid, started, sys.platform, host, boot)

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, str) or len(value) > 2048:
            raise RuntimeOwnershipError("Invalid persisted process identity")
        try:
            data = json.loads(value)
            if not isinstance(data, dict) or data.keys() != {"pid", "started", "platform", "host", "boot"}:
                raise ValueError("unexpected identity fields")
            if (type(data["pid"]) is not int or not 0 < data["pid"] <= 0xFFFFFFFF
                    or type(data["started"]) is not int or data["started"] <= 0
                    or data["platform"] not in {"linux", "win32"}
                    or not isinstance(data["host"], str) or len(data["host"]) != 64
                    or any(char not in "0123456789abcdef" for char in data["host"])
                    or not isinstance(data["boot"], str) or len(data["boot"]) > 64):
                raise ValueError("invalid identity values")
            if ((data["platform"] == "linux" and str(UUID(data["boot"])) != data["boot"])
                    or (data["platform"] == "win32" and data["boot"] != "")):
                raise ValueError("invalid boot identity")
            return cls(**data)
        except (TypeError, ValueError) as error:
            raise RuntimeOwnershipError("Invalid persisted process identity") from error

    def is_local(self):
        return self.platform == sys.platform and (self.host, self.boot) == host_identity()

    def is_running(self):
        return self.is_local() and self.capture(self.pid) == self

    def worker_tree_is_running(self):
        if not self.is_local():
            return False
        current = self.capture(self.pid)
        if current is not None:
            # A PID cannot be reused while its old Linux session/group still exists.
            return current == self
        if sys.platform != "linux":
            # The owner holds a non-inheritable Windows kill-on-close Job Object.
            return False
        for path in Path("/proc").iterdir():
            if path.name.isdigit():
                process = linux_process(int(path.name))
                if (process and process["state"] not in {"Z", "X"}
                        and process["group"] == self.pid and process["session"] == self.pid):
                    return True
        return False
