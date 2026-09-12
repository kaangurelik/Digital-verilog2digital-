"""Real Win32 ownership checks, using only children created by each test."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from verilog2digital.jobs.processes import ManagedWorker, ProcessError, ProcessLimits, memory_status


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object implementation")
ROOT = Path(__file__).resolve().parents[2]


def environment():
    result = dict(os.environ)
    result["PYTHONPATH"] = str(ROOT / "agent/src")
    result["PYTHONIOENCODING"] = "utf-8"
    return result


def launch(tmp_path, code, *, limits=ProcessLimits(), args=()):
    return ManagedWorker.start([sys.executable, "-u", "-c", code, *args], cwd=tmp_path,
                               env=environment(), stderr_path=tmp_path / "worker.log", limits=limits)


def line(pipe, timeout=10):
    results = queue.Queue()
    def read():
        try:
            results.put(pipe.readline())
        except BaseException as error:
            results.put(error)
    threading.Thread(target=read, daemon=True).start()
    value = results.get(timeout=timeout)
    if isinstance(value, BaseException):
        raise value
    assert value, "Worker closed its protocol pipe before reporting readiness"
    return value.decode("utf-8").strip()


class ProcessHandle:
    """Hold exact test-process identity across exit; never search by image name."""
    def __init__(self, pid, *, terminate=False):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = self.kernel.OpenProcess(0x100000 | int(terminate), False, pid)
        assert self.handle, f"Cannot open test process {pid}: {ctypes.get_last_error()}"

    def exited(self, timeout=0):
        return self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000)) == 0

    def kill(self):
        assert self.kernel.TerminateProcess(self.handle, 137)

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def wait_empty(worker):
    deadline = time.monotonic() + 5
    while worker.metrics()["active_processes"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_hidden_unicode_launch_and_truthful_memory_accounting(tmp_path):
    code = """import ctypes,json,os,sys
print(json.dumps({'console':bool(ctypes.windll.kernel32.GetConsoleWindow()),'argv':sys.argv[1:],'cwd':os.getcwd(),'stdin':sys.stdin.read()}),flush=True)
print('native stderr',file=sys.stderr)
"""
    args = ("ışık π", 'a"b', "trailing\\", "", "a b")
    with launch(tmp_path, code, args=args) as worker:
        actual = json.loads(line(worker.stdout))
        assert actual == {"console": False, "argv": list(args), "cwd": str(tmp_path), "stdin": ""}
        assert worker.wait(10) == 0
        wait_empty(worker)
        metrics = worker.metrics()
        assert metrics["memory_metric"] == "committed_bytes"
        assert 0 < metrics["peak_job_memory_bytes"] < metrics["job_memory_limit_bytes"]
        assert metrics["total_processes"] >= 1
    assert (tmp_path / "worker.log").read_text() == "native stderr\n"
    assert worker.metrics() == {**metrics, "closed": True}
    worker.close()  # Idempotent, with the final metrics preserved.


def test_native_hang_and_grandchild_are_killed_without_touching_unrelated_process(tmp_path):
    sleeper = "import time; time.sleep(60)"
    sentinel = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    code = f"""import ctypes,json,os,subprocess,sys
child=subprocess.Popen([sys.executable,'-c',{sleeper!r}],creationflags=subprocess.CREATE_NO_WINDOW)
print(json.dumps([os.getpid(),child.pid]),flush=True)
ctypes.PyDLL('kernel32').Sleep(60000)
"""
    try:
        with launch(tmp_path, code) as worker:
            parent, child = json.loads(line(worker.stdout))
            with ProcessHandle(parent) as parent_handle, ProcessHandle(child) as child_handle:
                with pytest.raises(subprocess.TimeoutExpired):
                    worker.wait(0.03)
                started = time.monotonic()
                worker.terminate_tree(1223)
                assert parent_handle.exited(5)
                assert child_handle.exited(5)
                worker.wait(5)
                wait_empty(worker)
                assert time.monotonic() - started < 5
                assert sentinel.poll() is None
    finally:
        sentinel.stdin.close()
        sentinel.wait(5)


def test_root_exit_does_not_hide_live_descendant(tmp_path):
    code = """import subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
print(child.pid,flush=True)
"""
    worker = launch(tmp_path, code)
    try:
        with ProcessHandle(int(line(worker.stdout))) as child:
            assert worker.wait(10) == 0
            assert worker.metrics()["active_processes"] > 0
            assert not child.exited()
            worker.close()
            assert child.exited(5)
            assert worker.metrics()["active_processes"] == 0
    finally:
        worker.close()


def test_owner_death_closes_noninherited_job_handle_and_kills_entire_tree(tmp_path):
    child_code = """import ctypes,json,os,subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
print(json.dumps([os.getpid(),child.pid]),flush=True)
ctypes.PyDLL('kernel32').Sleep(60000)
"""
    owner_code = f"""from verilog2digital.jobs.processes import ManagedWorker
import ctypes,json,os,sys
worker=ManagedWorker.start([sys.executable,'-u','-c',{child_code!r}],cwd=os.getcwd(),env=dict(os.environ),stderr_path='owned.log')
print(json.dumps([os.getpid(),json.loads(worker.stdout.readline())]),flush=True)
ctypes.PyDLL('kernel32').Sleep(60000)
"""
    # A second outer test job guarantees cleanup even if an assertion fails.
    # It stays open while checking that death of the *inner* owner kills its job.
    with launch(tmp_path, owner_code) as owner:
        owner_pid, (worker_pid, child_pid) = json.loads(line(owner.stdout))
        # The venv executable can be a redirector. Terminate the actual reported
        # owner, whose job handle must never be inherited by either descendant.
        with ProcessHandle(owner_pid, terminate=True) as native_owner, ProcessHandle(worker_pid) as worker, ProcessHandle(child_pid) as child:
            native_owner.kill()
            assert native_owner.exited(5)
            assert worker.exited(5)
            assert child.exited(5)
        owner.wait(5)


@pytest.mark.parametrize("limits", [ProcessLimits(job_memory_bytes=96 * 1024**2),
    ProcessLimits(job_memory_bytes=256 * 1024**2, process_memory_bytes=64 * 1024**2)])
def test_native_committed_memory_limit_denies_allocation(limits, tmp_path):
    code = """try:
 allocation=bytearray(512*1024**2)
except MemoryError:
 print('allocation denied',flush=True)
else:
 print('allocation unexpectedly succeeded',flush=True)
"""
    with launch(tmp_path, code, limits=limits) as worker:
        assert line(worker.stdout) == "allocation denied"
        assert worker.wait(10) == 0
        wait_empty(worker)
        metrics = worker.metrics()
        # On Windows 11 a denied allocation can be included in the native peak
        # job counter. Do not confuse that accounting high-water mark with RSS
        # or assert that rejected commit requests were successfully allocated.
        assert metrics["peak_job_memory_bytes"] > 0
        if limits.process_memory_bytes:
            assert metrics["peak_process_memory_bytes"] <= limits.process_memory_bytes


def test_aggregate_limit_applies_across_parent_and_child(tmp_path):
    child_code = """try:
 allocation=bytearray(128*1024**2)
except MemoryError:
 print('combined allocation denied',flush=True)
else:
 print('combined allocation succeeded',flush=True)
"""
    code = f"""import subprocess,sys
allocation=bytearray(128*1024**2)
child=subprocess.run([sys.executable,'-u','-c',{child_code!r}],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
sys.stdout.buffer.write(child.stdout)
sys.stderr.buffer.write(child.stderr)
raise SystemExit(child.returncode)
"""
    limits = ProcessLimits(job_memory_bytes=256 * 1024**2)
    with launch(tmp_path, child_code, limits=limits) as single:
        assert line(single.stdout) == "combined allocation succeeded"
        assert single.wait(10) == 0
    with launch(tmp_path, code, limits=limits) as worker:
        assert line(worker.stdout) == "combined allocation denied"
        assert worker.wait(10) == 0
        wait_empty(worker)
        assert worker.metrics()["total_processes"] >= 2


def test_stdout_backpressure_does_not_block_tree_cancellation(tmp_path):
    with launch(tmp_path, "import sys; sys.stdout.buffer.write(b'x'*16_000_000); sys.stdout.flush()") as worker:
        time.sleep(0.1)
        assert worker.poll() is None
        worker.terminate_tree()
        worker.wait(5)
        wait_empty(worker)


def test_nested_supervisor_jobs_preserve_outer_owner_cleanup(tmp_path):
    owner_code = """from verilog2digital.jobs.processes import ManagedWorker
import os,sys,time
child=ManagedWorker.start([sys.executable,'-c','import time; time.sleep(60)'],cwd=os.getcwd(),env=dict(os.environ),stderr_path='nested.log')
print(child.pid,flush=True)
time.sleep(60)
"""
    with launch(tmp_path, owner_code) as outer:
        with ProcessHandle(int(line(outer.stdout))) as nested:
            outer.terminate_tree()
            assert nested.exited(5)
            outer.wait(5)
            wait_empty(outer)


@pytest.mark.parametrize("kwargs", [{"job_memory_bytes": True}, {"job_memory_bytes": 1},
    {"process_memory_bytes": 8 * 1024**3}, {"process_memory_bytes": 1},
    {"active_process_limit": 0}, {"active_process_limit": 257}, {"active_process_limit": False}])
def test_invalid_limits_are_actionable(kwargs):
    with pytest.raises(ProcessError) as failure:
        ProcessLimits(**kwargs)
    assert failure.value.code == "PROCESS_LIMITS"
    assert failure.value.as_dict()["message"]


def test_launch_failure_is_structured_and_does_not_create_worker(tmp_path):
    with pytest.raises(ProcessError) as failure:
        ManagedWorker.start([str(tmp_path / "absent.exe")], cwd=tmp_path, env=environment(),
                            stderr_path=tmp_path / "failure.log")
    assert failure.value.code == "PROCESS_START"
    assert failure.value.winerror == 2


@pytest.mark.parametrize("argv,env", [(["python.exe"], {}), ([sys.executable, "bad\0arg"], {}),
    ([sys.executable], {"PATH": "x", "Path": "y"}), ([sys.executable], {"bad=key": "x"}),
    ([sys.executable], {"key": "bad\0value"})])
def test_invalid_launch_arguments_fail_before_process_creation(tmp_path, argv, env):
    with pytest.raises(ProcessError) as failure:
        ManagedWorker.start(argv, cwd=tmp_path, env=env, stderr_path=tmp_path / "invalid.log")
    assert failure.value.code in {"PROCESS_ARGUMENTS", "PROCESS_ENVIRONMENT"}
    assert not (tmp_path / "invalid.log").exists()


def test_memory_status_reports_available_resources_without_reserving_them():
    status = memory_status()
    assert 0 < status["available_physical_bytes"] <= status["total_physical_bytes"]
    assert 0 < status["available_commit_bytes"] <= status["commit_limit_bytes"]
    assert 0 <= status["memory_load_percent"] <= 100


def test_missing_directory_and_unwritable_log_are_structured(tmp_path):
    for directory, log, code in [(tmp_path / "absent", tmp_path / "error.log", "PROCESS_DIRECTORY"),
                                 (tmp_path, tmp_path, "PROCESS_IO")]:
        with pytest.raises(ProcessError) as failure:
            ManagedWorker.start([sys.executable, "-c", "pass"], cwd=directory,
                                env=environment(), stderr_path=log)
        assert failure.value.code == code


def test_failed_suspended_setup_never_executes_worker_and_releases_handles(tmp_path, monkeypatch):
    from verilog2digital.jobs import processes
    original_windows = processes._Windows

    class FailResume(original_windows):
        def __init__(self):
            super().__init__()
            def fail(_):
                ctypes.set_last_error(5)
                return 0xFFFFFFFF
            self.kernel.ResumeThread = fail

    kernel = original_windows().kernel
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    def count():
        handles = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(handles))
        return handles.value

    monkeypatch.setattr(processes, "_Windows", FailResume)
    before = count()
    for _ in range(5):
        with pytest.raises(ProcessError) as failure:
            launch(tmp_path, "from pathlib import Path; Path('executed').write_text('unsafe')")
        assert failure.value.code == "PROCESS_START"
    assert not (tmp_path / "executed").exists()
    assert count() <= before + 1


def test_concurrent_close_is_idempotent_and_preserves_final_metrics(tmp_path):
    worker = launch(tmp_path, "import time; time.sleep(60)")
    failures = []
    def close():
        try:
            worker.close()
        except BaseException as error:
            failures.append(error)
    threads = [threading.Thread(target=close) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
        assert failures == []
        assert worker.metrics()["active_processes"] == 0
        assert worker.metrics()["closed"] is True
    finally:
        worker.close()
