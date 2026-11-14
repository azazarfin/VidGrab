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


class ThreadEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
               ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
               ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]


class WindowsJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
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

    def contains(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, pid)
        self.check(process)
        try:
            assigned = wintypes.BOOL()
            self.check(self.kernel.IsProcessInJob(process, self.handle, ctypes.byref(assigned)))
            return bool(assigned.value)
        finally:
            self.kernel.CloseHandle(process)

    def resume(self, process):
        # subprocess.Popen closes CreateProcessW's initial thread handle. Use
        # supported Win32 APIs to reopen the still-suspended primary thread.
        # The retained Popen process handle prevents reuse of its process ID.
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        threads = []
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        try:
            more = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.process_id == process.pid:
                    threads.append(entry.thread_id)
                more = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(snapshot)
        if len(threads) != 1:
            raise RuntimeError("Suspended worker has no unique primary thread")
        thread = self.kernel.OpenThread(0x0802, False, threads[0])  # query limited + suspend/resume
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if self.kernel.GetProcessIdOfThread(thread) != process.pid:
                raise RuntimeError("Worker thread identity no longer matches its process")
            count = self.kernel.ResumeThread(thread)
            if count == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            if count != 1:
                raise RuntimeError("Worker primary thread has an unexpected suspend count")
        finally:
            self.kernel.CloseHandle(thread)

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
