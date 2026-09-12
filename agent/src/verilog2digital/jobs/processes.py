"""Own each worker process tree through a noninheritable Windows Job Object.

JOB_LIST assigns ownership at creation; closing the handle terminates the tree.
Limits apply to committed memory. Peak counters can include denied allocations,
so a peak above the cap alone does not prove a limit violation.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import subprocess
import threading
import time


class ProcessError(RuntimeError):
    def __init__(self, code: str, message: str, *, winerror: int | None = None):
        super().__init__(message)
        self.code, self.winerror = code, winerror

    def as_dict(self):
        result = {"code": self.code, "message": str(self)}
        if self.winerror is not None:
            result["winerror"] = self.winerror
        return result


@dataclass(frozen=True)
class ProcessLimits:
    job_memory_bytes: int = 6 * 1024**3
    process_memory_bytes: int | None = None
    active_process_limit: int = 32

    def __post_init__(self):
        if type(self.job_memory_bytes) is not int or not 64 * 1024**2 <= self.job_memory_bytes <= 1024**4:
            raise ProcessError("PROCESS_LIMITS", "Job committed-memory limit must be an integer from 64 MiB through 1 TiB")
        if self.process_memory_bytes is not None and (type(self.process_memory_bytes) is not int
                or not 32 * 1024**2 <= self.process_memory_bytes <= self.job_memory_bytes):
            raise ProcessError("PROCESS_LIMITS", "Per-process committed-memory limit must be from 32 MiB through the job limit")
        if type(self.active_process_limit) is not int or not 1 <= self.active_process_limit <= 256:
            raise ProcessError("PROCESS_LIMITS", "Active process limit must be an integer from 1 through 256")


class _Security(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR),
                ("title", wintypes.LPWSTR), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD), ("x_chars", wintypes.DWORD),
                ("y_chars", wintypes.DWORD), ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reserved_count", wintypes.WORD), ("reserved_bytes", ctypes.c_void_p),
                ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IOCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("user_time", "kernel_time", "period_user_time", "period_kernel_time")] + [
        (name, wintypes.DWORD) for name in ("page_faults", "total_processes", "active_processes", "terminated_processes")]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in ("physical", "available_physical", "commit_limit", "available_commit",
                                               "virtual", "available_virtual", "extended_virtual")]


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise ProcessError("PROCESS_PLATFORM", "Managed process ownership currently requires Windows 10 or newer")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, pointer, dword, size = wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t
        signatures = {
            "CreateJobObjectW": ([pointer, wintypes.LPCWSTR], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, pointer, dword], wintypes.BOOL),
            "QueryInformationJobObject": ([handle, ctypes.c_int, pointer, dword, pointer], wintypes.BOOL),
            "TerminateJobObject": ([handle, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([handle], wintypes.BOOL),
            "CreatePipe": ([ctypes.POINTER(handle), ctypes.POINTER(handle), ctypes.POINTER(_Security), dword], wintypes.BOOL),
            "SetHandleInformation": ([handle, dword, dword], wintypes.BOOL),
            "CreateFileW": ([wintypes.LPCWSTR, dword, dword, ctypes.POINTER(_Security), dword, dword, handle], handle),
            "GetCurrentProcess": ([], handle),
            "DuplicateHandle": ([handle, handle, handle, ctypes.POINTER(handle), dword, wintypes.BOOL, dword], wintypes.BOOL),
            "InitializeProcThreadAttributeList": ([pointer, dword, dword, ctypes.POINTER(size)], wintypes.BOOL),
            "UpdateProcThreadAttribute": ([pointer, dword, size, pointer, size, pointer, pointer], wintypes.BOOL),
            "DeleteProcThreadAttributeList": ([pointer], None),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, pointer, pointer, wintypes.BOOL, dword,
                                pointer, wintypes.LPCWSTR, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
            "ResumeThread": ([handle], dword),
            "IsProcessInJob": ([handle, handle, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "WaitForSingleObject": ([handle, dword], dword),
            "GetExitCodeProcess": ([handle, ctypes.POINTER(dword)], wintypes.BOOL),
            "GlobalMemoryStatusEx": ([ctypes.POINTER(_MemoryStatus)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def error(operation, code="PROCESS_WINDOWS"):
        number = ctypes.get_last_error()
        return ProcessError(code, f"{operation}: {ctypes.FormatError(number).strip()}", winerror=number)

    def check(self, result, operation, code="PROCESS_WINDOWS"):
        if not result:
            raise self.error(operation, code)
        return result

    def close(self, handle):
        if handle:
            self.kernel.CloseHandle(handle)


def memory_status() -> dict:
    """Read current physical/commit availability; this does not reserve memory."""
    windows = _Windows()
    status = _MemoryStatus()
    status.length = ctypes.sizeof(status)
    windows.check(windows.kernel.GlobalMemoryStatusEx(ctypes.byref(status)), "Read system memory availability")
    return {"total_physical_bytes": status.physical, "available_physical_bytes": status.available_physical,
            "commit_limit_bytes": status.commit_limit, "available_commit_bytes": status.available_commit,
            "memory_load_percent": status.load}


def _launch_values(argv, cwd, env, stderr_path, limits):
    if not isinstance(limits, ProcessLimits):
        raise ProcessError("PROCESS_LIMITS", "limits must be ProcessLimits")
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
        raise ProcessError("PROCESS_ARGUMENTS", "argv must contain strings without NUL characters")
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise ProcessError("PROCESS_ARGUMENTS", "The worker executable must be an absolute path")
    command = subprocess.list2cmdline(list(argv))
    try:
        command_length = len(command.encode("utf-16-le")) // 2
    except UnicodeError as error:
        raise ProcessError("PROCESS_ARGUMENTS", "Worker arguments contain invalid Unicode") from error
    if command_length >= 32767:
        raise ProcessError("PROCESS_ARGUMENTS", "The Windows command line exceeds its length limit")
    if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or not key or "\0" in key + value or "=" in key[1:] for key, value in env.items()):
        raise ProcessError("PROCESS_ENVIRONMENT", "Environment names/values must be valid strings")
    if len({key.casefold() for key in env}) != len(env):
        raise ProcessError("PROCESS_ENVIRONMENT", "Windows environment names must be case-insensitively unique")
    environment = "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].casefold())) + "\0\0"
    try:
        directory = Path(cwd).resolve(strict=True)
        log = Path(stderr_path).absolute()
    except (OSError, TypeError, ValueError) as error:
        raise ProcessError("PROCESS_DIRECTORY", f"Invalid worker cwd or log path: {error}",
                           winerror=getattr(error, "winerror", None)) from error
    if not directory.is_dir():
        raise ProcessError("PROCESS_DIRECTORY", "Worker cwd must be an existing directory")
    if not log.parent.is_dir():
        raise ProcessError("PROCESS_DIRECTORY", "Worker log directory must already exist")
    return command, environment, directory, log


class ManagedWorker:
    """An exact owned Windows process tree; never locates or kills by image name."""
    def __init__(self):
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._exit_code = None
        self._last_metrics = {}
        self._job = self._process = None
        self.stdout = None

    @classmethod
    def start(cls, argv: list[str], *, cwd, env: dict[str, str], stderr_path,
              limits: ProcessLimits = ProcessLimits()) -> "ManagedWorker":
        command, environment, directory, log = _launch_values(argv, cwd, env, stderr_path, limits)
        worker = cls()
        worker.args, worker.limits = tuple(argv), limits
        worker._windows = windows = _Windows()
        kernel = windows.kernel
        security = _Security(ctypes.sizeof(_Security), None, True)
        read_pipe, write_pipe, error_pipe = wintypes.HANDLE(), wintypes.HANDLE(), wintypes.HANDLE()
        input_handle = None
        process = _ProcessInfo()
        attributes = None
        attributes_initialized = False
        stderr = None
        try:
            worker._job = windows.check(kernel.CreateJobObjectW(None, None), "Create worker job", "PROCESS_ISOLATION")
            native_limits = _ExtendedLimits()
            # No breakaway flags: tools and their descendants inherit membership.
            native_limits.basic.flags = 0x2000 | 0x200 | 0x400 | 0x8
            native_limits.basic.active_processes = limits.active_process_limit
            native_limits.job_memory = limits.job_memory_bytes
            if limits.process_memory_bytes is not None:
                native_limits.basic.flags |= 0x100
                native_limits.process_memory = limits.process_memory_bytes
            windows.check(kernel.SetInformationJobObject(worker._job, 9, ctypes.byref(native_limits), ctypes.sizeof(native_limits)),
                          "Set worker resource limits", "PROCESS_ISOLATION")
            windows.check(kernel.CreatePipe(ctypes.byref(read_pipe), ctypes.byref(write_pipe), ctypes.byref(security), 0), "Create worker output pipe")
            windows.check(kernel.SetHandleInformation(read_pipe, 1, 0), "Protect parent output pipe")
            input_handle = kernel.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(security), 3, 0x80, None)
            if input_handle == ctypes.c_void_p(-1).value:
                input_handle = None
                raise windows.error("Open worker null input")
            import msvcrt
            stderr = log.open("ab", buffering=0)
            owner = kernel.GetCurrentProcess()
            windows.check(kernel.DuplicateHandle(owner, msvcrt.get_osfhandle(stderr.fileno()), owner,
                                                ctypes.byref(error_pipe), 0, True, 2), "Create worker error handle")
            size = ctypes.c_size_t()
            if kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size)) or ctypes.get_last_error() != 122:
                raise windows.error("Size worker startup attributes", "PROCESS_ISOLATION")
            attributes = ctypes.create_string_buffer(size.value)
            windows.check(kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)), "Initialize worker startup attributes")
            attributes_initialized = True
            jobs = (wintypes.HANDLE * 1)(worker._job)
            handles = (wintypes.HANDLE * 3)(input_handle, write_pipe.value, error_pipe.value)
            windows.check(kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None),
                          "Assign worker at creation", "PROCESS_ISOLATION")
            windows.check(kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None),
                          "Restrict inherited worker handles")
            startup = _StartupInfoEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100 | 0x1  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
            startup.startup.show = 0
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = input_handle, write_pipe.value, error_pipe.value
            startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
            # Suspended setup is safe: JOB_LIST associates ownership atomically.
            flags = 0x08000000 | 0x400 | 0x80000 | 0x4
            command_buffer = ctypes.create_unicode_buffer(command)
            environment_buffer = ctypes.create_unicode_buffer(environment)
            windows.check(kernel.CreateProcessW(argv[0], command_buffer, None, None, True, flags,
                          environment_buffer, str(directory), ctypes.byref(startup), ctypes.byref(process)),
                          "Start isolated worker", "PROCESS_START")
            worker._process, worker.pid = process.process, process.pid
            member = wintypes.BOOL()
            windows.check(kernel.IsProcessInJob(worker._process, worker._job, ctypes.byref(member)), "Verify worker ownership")
            if not member.value:
                raise ProcessError("PROCESS_ISOLATION", "Worker was not assigned to its own job")
            fd = msvcrt.open_osfhandle(read_pipe.value, os.O_RDONLY | os.O_BINARY)
            read_pipe.value = None  # fd now owns this handle.
            try:
                worker.stdout = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            if kernel.ResumeThread(process.thread) == 0xFFFFFFFF:
                raise windows.error("Resume owned worker", "PROCESS_START")
            return worker
        except BaseException as error:
            if worker._job:
                kernel.TerminateJobObject(worker._job, 1)
            if worker._process:
                kernel.WaitForSingleObject(worker._process, 5000)
            windows.close(worker._process)
            windows.close(worker._job)
            worker._process = worker._job = None
            worker._closed = True
            if worker.stdout is not None:
                worker.stdout.close()
            if isinstance(error, OSError):
                raise ProcessError("PROCESS_IO", f"Cannot prepare worker streams: {error}",
                                   winerror=getattr(error, "winerror", None)) from error
            raise
        finally:
            windows.close(process.thread)
            windows.close(read_pipe.value)
            windows.close(write_pipe.value)
            windows.close(error_pipe.value)
            windows.close(input_handle)
            if stderr is not None:
                stderr.close()
            if attributes_initialized:
                kernel.DeleteProcThreadAttributeList(attributes)

    def poll(self) -> int | None:
        with self._lock:
            if self._exit_code is not None or self._closed:
                return self._exit_code
            result = self._windows.kernel.WaitForSingleObject(self._process, 0)
            if result == 258:
                return None
            if result != 0:
                raise self._windows.error("Read worker completion")
            code = wintypes.DWORD()
            self._windows.check(self._windows.kernel.GetExitCodeProcess(self._process, ctypes.byref(code)), "Read worker exit code")
            self._exit_code = code.value
            return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Worker wait timeout must be finite and nonnegative")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.poll()
            if result is not None:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic())))

    def terminate_tree(self, exit_code: int = 1223) -> None:
        if type(exit_code) is not int or not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("Termination exit code must be an unsigned 32-bit integer")
        with self._lock:
            if not self._closed:
                self._windows.check(self._windows.kernel.TerminateJobObject(self._job, exit_code), "Terminate owned worker tree")

    def metrics(self) -> dict:
        with self._lock:
            if self._closed:
                return dict(self._last_metrics)
            limits, accounting = _ExtendedLimits(), _Accounting()
            self._windows.check(self._windows.kernel.QueryInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None),
                                "Read worker memory accounting")
            self._windows.check(self._windows.kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None),
                                "Read worker process accounting")
            self._last_metrics = {
                "memory_metric": "committed_bytes", "job_memory_limit_bytes": limits.job_memory,
                "memory_counter_source": "Windows.JobObjectExtendedLimitInformation",
                "peak_may_include_denied_commits": True,
                "process_memory_limit_bytes": limits.process_memory if self.limits.process_memory_bytes is not None else None,
                "peak_job_memory_bytes": limits.peak_job_memory, "peak_process_memory_bytes": limits.peak_process_memory,
                "active_processes": accounting.active_processes, "total_processes": accounting.total_processes,
                "root_exit_code": self.poll(), "closed": False,
            }
            return dict(self._last_metrics)

    def close(self) -> None:
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
        failure = None
        try:
            self.terminate_tree()
            deadline = time.monotonic() + 5
            while self.metrics()["active_processes"]:
                if time.monotonic() >= deadline:
                    raise ProcessError("PROCESS_CLEANUP_TIMEOUT", "Owned worker processes did not terminate within five seconds")
                time.sleep(0.05)
            self.poll()
            self.metrics()
        except BaseException as error:
            failure = error
        finally:
            with self._lock:
                self._windows.close(self._job)
                self._windows.close(self._process)
                self._job = self._process = None
                self._closed = True
                self._last_metrics["closed"] = True
            if self.stdout is not None:
                self.stdout.close()
        if failure is not None:
            raise failure

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
