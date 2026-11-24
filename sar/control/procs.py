"""Child processes that never outlive the launcher.

On Windows the launcher puts itself in a job object with "kill on job close":
every process it starts inherits the job, and when the launcher exits for any
reason (normal end, Ctrl+C, crash, killed from outside) the operating system
ends the whole job. Elsewhere, an atexit hook terminates the children.
"""
from __future__ import annotations

import atexit
import subprocess
import sys
from pathlib import Path

_job_handle = None  # kept for the life of the process; closing it kills the job


def _join_kill_on_close_job() -> bool:
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return _job_handle is not None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        return False
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        return False
    _job_handle = job
    return True


class ProcessGroup:
    def __init__(self) -> None:
        self.contained = _join_kill_on_close_job()
        self.procs: list[subprocess.Popen] = []
        atexit.register(self.stop_all)

    def spawn(self, args: list[str], cwd: Path, log_path: Path) -> subprocess.Popen:
        log = open(log_path, "a", encoding="utf-8")
        p = subprocess.Popen(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def stop_all(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
