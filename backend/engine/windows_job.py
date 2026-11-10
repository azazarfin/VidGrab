"""An attempt's non-inheritable, kill-on-close Windows Job Object."""

import ctypes
from ctypes import wintypes
import time


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
               ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
               ("maximum", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
               ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
               ("scheduling", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
               ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters),
               ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
               ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
               ("user", "kernel", "period_user", "period_kernel")] + [
               (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]


class WindowsJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.check(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def check(self, success):
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        # The worker waits for bootstrap before importing or invoking media tools.
        self.check(self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)))

    def stop(self):
        self.check(self.kernel.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 3
        while True:
            accounting = Accounting()
            self.check(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker Job Object still contains active processes")
            time.sleep(0.01)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
