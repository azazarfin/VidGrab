"""Read-only process identities for opt-in Linux/Windows worker checks."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    started: int
    name: str

    def is_running(self):
        if sys.platform == "win32":
            return windows_identity(self.pid) == self.started
        current = linux_identity(self.pid)
        return current is not None and current.started == self.started


def linux_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        end = stat.rindex(")")
        fields = stat[end + 2:].split()
        return ProcessIdentity(pid, int(fields[1]), int(fields[19]), stat[stat.index("(") + 1:end])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
               ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
               ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
               ("name", wintypes.WCHAR * 260)]


@lru_cache(maxsize=1)
def windows_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessTimes": ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def windows_identity(pid):
    kernel = windows_kernel()
    handle = kernel.OpenProcess(0x101000, False, pid)  # synchronize + query limited information
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def snapshot():
    if sys.platform == "linux":
        return [identity for path in Path("/proc").iterdir() if path.name.isdigit()
                if (identity := linux_identity(int(path.name))) is not None]
    if sys.platform != "win32":
        raise RuntimeError("Worker checks currently support Linux and Windows")
    kernel = windows_kernel()
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            started = windows_identity(entry.pid) if entry.pid else None
            if started is not None:
                processes.append(ProcessIdentity(entry.pid, entry.parent, started, entry.name))
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return processes


def owned_children(previous=()):
    processes = snapshot()
    descendants = [process for process in processes if process.parent == os.getpid()
                   and (process.pid, process.started) not in previous]
    owned = {process.pid for process in descendants}
    while True:
        added = [process for process in processes if process.parent in owned and process.pid not in owned]
        if not added:
            return descendants
        descendants.extend(added)
        owned.update(process.pid for process in added)
