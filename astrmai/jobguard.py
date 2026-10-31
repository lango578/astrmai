"""Windows job object so MaiBot's out-of-process plugin runners cannot outlive the host.

atexit only runs on a clean exit. A crash, or taskkill /F, skips it and leaves runner_main
children holding model handles and the plugin DB. Putting the host process itself into a
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE job makes the kernel do the reaping: when the host dies the
OS closes its handles, the last job handle goes away, and every member process is terminated.

The rule that follows from this design: never close the job handle from inside. Membership
includes our own process, so closing it deliberately would kill us.
"""

import ctypes
import sys
from typing import Any

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _LargeInteger(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LargeInteger),
        ("PerJobUserTimeLimit", _LargeInteger),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobGuard:
    """Holds one job handle. mode is 'self' (host + all descendants), 'pids' (hand-picked) or 'none'."""

    def __init__(self) -> None:
        self.mode = "none"
        self.detail = ""
        self.assigned_pids: list[int] = []
        self._handle: Any = None

    @property
    def armed(self) -> bool:
        return self.mode in ("self", "pids")

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "detail": self.detail,
            "assigned_pids": list(self.assigned_pids),
            "inherited_children": self.mode == "self",
        }

    def arm(self) -> "JobGuard":
        """Create the job, set kill-on-close, and put the current process inside it."""

        if sys.platform != "win32":
            self.detail = f"非 Windows（{sys.platform}），作业对象不可用，只能靠 atexit 回收"
            return self
        if self._handle is not None:
            return self

        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1  # INVALID_HANDLE_VALUE as an unsigned pointer
        if not handle or int(handle) == invalid:
            self.detail = f"CreateJobObjectW 失败 (WinError {ctypes.GetLastError()})"
            return self

        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        size = ctypes.sizeof(info)
        if not kernel32.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), size
        ):
            self.detail = f"SetInformationJobObject 失败 (WinError {ctypes.GetLastError()})"
            kernel32.CloseHandle(handle)
            return self

        current = kernel32.GetCurrentProcess()
        if not kernel32.AssignProcessToJobObject(handle, current):
            # A process already inside a non-nestable job cannot join a second one.
            self.detail = (
                f"本机进程已在别的作业对象里，AssignProcessToJobObject 失败 "
                f"(WinError {ctypes.GetLastError()})；退化为逐个认领 runner PID"
            )
            self.mode = "pids"
            self._handle = handle
            return self

        self._handle = handle
        self.mode = "self"
        self.detail = "宿主进程已进入 kill-on-close 作业，之后派生的子进程都会继承"
        return self

    def absorb(self, pid: int) -> bool:
        """Adopt an already-running process. Only meaningful in 'pids' mode."""

        if self.mode == "self":
            return True
        if self.mode != "pids" or self._handle is None:
            return False
        kernel32 = _kernel32()
        for access in (
            PROCESS_TERMINATE | PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION,
            PROCESS_TERMINATE | PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
        ):
            proc = kernel32.OpenProcess(access, False, int(pid))
            if proc:
                break
        else:
            return False
        try:
            ok = bool(kernel32.AssignProcessToJobObject(self._handle, proc))
        finally:
            kernel32.CloseHandle(proc)
        if ok:
            self.assigned_pids.append(int(pid))
        return ok

    def is_member(self, pid: int | None = None) -> bool | None:
        """Check membership. None means the check itself could not run."""

        if self._handle is None or sys.platform != "win32":
            return None
        kernel32 = _kernel32()
        if pid is None:
            proc = kernel32.GetCurrentProcess()
            own = False
        else:
            proc = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION, False, int(pid)
            )
            own = True
            if not proc:
                return None
        result = ctypes.c_bool(False)
        try:
            ok = kernel32.IsProcessInJob(proc, self._handle, ctypes.byref(result))
        finally:
            if own:
                kernel32.CloseHandle(proc)
        return bool(result.value) if ok else None

    def describe(self) -> str:
        if self.mode == "self":
            return "作业对象=host（宿主进程死亡即连坐所有子进程，含 taskkill /F）"
        if self.mode == "pids":
            return f"作业对象=点名 {self.assigned_pids}；{self.detail}"
        return f"作业对象=不可用；{self.detail}"


_LIB: Any = None


def _kernel32() -> Any:
    global _LIB
    if _LIB is None:
        lib = ctypes.windll.kernel32
        lib.CreateJobObjectW.restype = ctypes.c_void_p
        lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        lib.SetInformationJobObject.restype = ctypes.c_bool
        lib.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        lib.AssignProcessToJobObject.restype = ctypes.c_bool
        lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.OpenProcess.restype = ctypes.c_void_p
        lib.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        lib.GetCurrentProcess.restype = ctypes.c_void_p
        lib.GetCurrentProcess.argtypes = []
        lib.CloseHandle.restype = ctypes.c_bool
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        lib.IsProcessInJob.restype = ctypes.c_bool
        lib.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool)]
        lib.WaitForSingleObject.restype = ctypes.c_ulong
        lib.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.TerminateProcess.restype = ctypes.c_bool
        lib.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        lib.GetExitCodeProcess.restype = ctypes.c_bool
        lib.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        _LIB = lib
    return _LIB


def arm() -> JobGuard:
    """Create and self-assign the guard for the calling process."""

    return JobGuard().arm()


ERROR_ACCESS_DENIED = 5
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259


def process_alive(pid: int) -> bool:
    """Liveness probe that does not disturb the process.

    os.kill(pid, 0) is NOT safe on Windows: CPython opens the handle with PROCESS_TERMINATE and
    calls TerminateProcess(handle, 0), so an 'is it alive' check actually kills it.
    WaitForSingleObject is also unusable here -- a handle opened without SYNCHRONIZE returns
    WAIT_FAILED/ERROR_ACCESS_DENIED for other users' processes, which reads as 'dead'.
    GetExitCodeProcess needs only QUERY_LIMITED_INFORMATION and says 259 while running.
    """

    if sys.platform != "win32":
        import os

        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ctypes.GetLastError() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 探测失败不等于进程没了，宁可当作还活着继续回收
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate_process(pid: int) -> bool:
    """Force-terminate a stray child. Returns True if it is gone afterwards."""

    if sys.platform != "win32":
        import os
        import signal

        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return not process_alive(pid)
        return not process_alive(pid)

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return not process_alive(pid)
    try:
        if not kernel32.TerminateProcess(handle, 1):
            return False
        kernel32.WaitForSingleObject(handle, 5000)
    finally:
        kernel32.CloseHandle(handle)
    return not process_alive(pid)

