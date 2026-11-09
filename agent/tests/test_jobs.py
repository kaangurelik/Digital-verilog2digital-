"""Durable supervision rejects incomplete workers and preserves committed results."""
from dataclasses import asdict
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.jobs import manager
from verilog2digital.jobs.manager import JobError, JobLimits, cancel_job, job_status, recover_jobs, run_inspection, run_job
from verilog2digital.jobs.store import JobStore, atomic_json, file_lease, read_json
from test_publication import candidate, publish


def request(kind="inspect", output=None):
    return {"schema_version": 2, "job_id": uuid.uuid4().hex, "kind": kind, "cache": False,
            "project": {"sources": ["source.v"], "top": None, "include_dirs": [], "defines": {}, "parameters": {}},
            "options": {}, "limits": asdict(JobLimits()), "output_root": str(output) if output else None}


def test_atomic_job_records_preserve_order_and_terminal_identity(tmp_path):
    raw = request()
    raw["project"]["defines"] = {"SECOND": "`FIRST", "FIRST": "1"}
    store = JobStore.create(tmp_path, raw)
    assert list(read_json(store.directory / "request.json")["project"]["defines"]) == ["SECOND", "FIRST"]
    store.update(state="running", stage="inspecting")
    done = store.update(state="ready", stage="ready", result={"valid": True})
    assert done["event_seq"] == 2 and done["request"] == raw
    assert len(store.events_path.read_text().splitlines()) == 2
    with pytest.raises(FrontendError) as error:
        store.update(stage="failed")
    assert error.value.code == "JOB_TERMINAL"
    assert not cancel_job(store.directory)["accepted"]


def test_interrupted_atomic_json_write_preserves_old_complete_record(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    atomic_json(path, {"old": True})
    attempts = []
    def denied(*_):
        attempts.append(1)
        raise PermissionError("injected replace failure")
    monkeypatch.setattr("verilog2digital.jobs.store.os.replace", denied)
    with pytest.raises(PermissionError):
        atomic_json(path, {"old": False})
    assert read_json(path) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]
    assert len(attempts) == 1  # No Win32 sharing/access code: do not retry.


@contextmanager
def _reader_without_delete_sharing(path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def release():
        nonlocal handle
        if handle is not None:
            assert kernel.CloseHandle(handle)
            handle = None

    try:
        yield release
    finally:
        release()


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic replacement sharing regression")
def test_atomic_json_retries_after_real_reader_releases_delete_denial(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    atomic_json(path, {"old": True})
    old_bytes = path.read_bytes()
    real_replace, attempts, denials = os.replace, [], []
    with _reader_without_delete_sharing(path) as release:
        def replace(source, destination):
            attempts.append((source, destination))
            try:
                return real_replace(source, destination)
            except OSError as error:
                denials.append(error.winerror)
                assert path.read_bytes() == old_bytes
                release()
                raise

        monkeypatch.setattr("verilog2digital.jobs.store.os.replace", replace)
        atomic_json(path, {"old": False, "complete": True})
    assert len(attempts) == 2 and denials[0] in (5, 32)
    assert read_json(path) == {"old": False, "complete": True}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic replacement sharing regression")
def test_permanent_reader_denial_is_bounded_and_preserves_old_json(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    atomic_json(path, {"old": True})
    old_bytes = path.read_bytes()
    real_replace, attempts = os.replace, []

    def replace(source, destination):
        attempts.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr("verilog2digital.jobs.store.os.replace", replace)
    started = time.monotonic()
    with _reader_without_delete_sharing(path):
        with pytest.raises(OSError) as error:
            atomic_json(path, {"old": False})
        assert error.value.winerror in (5, 32)
        assert path.read_bytes() == old_bytes
    assert len(attempts) == 6 and time.monotonic() - started < 2
    assert list(tmp_path.iterdir()) == [path]


def test_cancel_racing_job_completion_reports_terminal_status(tmp_path, monkeypatch):
    store = JobStore.create(tmp_path, request())
    store.update(state="running", stage="inspecting")
    real_write = manager.atomic_json
    def complete_during_request(path, value):
        real_write(path, value)
        if Path(path).name == "cancel.json":
            store.update(state="ready", stage="ready", result={"valid": True})
    monkeypatch.setattr(manager, "atomic_json", complete_during_request)
    assert cancel_job(store.directory) == {"job_id": store.directory.name, "accepted": False,
                                           "reason": "already_terminal", "state": "ready"}


def test_cancel_after_selection_commit_reports_committed_ready_job(tmp_path):
    output = tmp_path / "output"
    raw = request("convert", output)
    store = JobStore.create(tmp_path, raw)
    store.update(state="running", stage="publishing")
    directory, sources = candidate(tmp_path)
    publish(directory, sources, output, job_id=raw["job_id"])
    assert cancel_job(store.directory) == {"job_id": raw["job_id"], "accepted": False,
                                           "reason": "already_committed", "state": "ready"}
    other = JobStore.create(tmp_path, request("convert", output))
    other.update(state="running", stage="mapping")
    assert cancel_job(other.directory)["accepted"] is True


def test_worker_temporary_variables_all_select_owned_job_storage(tmp_path, monkeypatch):
    from verilog2digital.jobs import processes
    store = JobStore.create(tmp_path, request())
    captured = {}
    monkeypatch.setattr(processes.ManagedWorker, "start", classmethod(lambda cls, argv, **options: captured.update(options)))
    manager._launch_worker(store, JobLimits())
    owned = str(store.directory / "tmp")
    assert {name: captured["env"][name] for name in ("TMP", "TEMP", "TMPDIR", "V2D_JOB_TMP")} == dict.fromkeys(
        ("TMP", "TEMP", "TMPDIR", "V2D_JOB_TMP"), owned)


def test_oversized_worker_diagnostic_still_reaches_supervisor_with_its_code(tmp_path, monkeypatch, capsys):
    from verilog2digital.contracts import validate
    from verilog2digital.jobs import worker, workspace
    raw = request()
    directory = tmp_path / raw["job_id"]
    directory.mkdir()
    atomic_json(directory / "request.json", raw)
    def failing_snapshot(project, job_dir):
        raise FrontendError("SLANG_ERROR", "ğ" * 100000, path=str(tmp_path / "source.v"), line=3, column=4)
    monkeypatch.setattr(workspace, "snapshot_project", failing_snapshot)
    monkeypatch.setattr(worker, "_suppress_crash_dialogs", lambda: None)
    assert worker.main(["--request", str(directory / "request.json")]) == 1
    line = capsys.readouterr().out.splitlines()[-1]
    assert len(line.encode("utf-8")) <= 65536
    event = json.loads(line)
    validate("job-event-v1", event)
    diagnostic = event["params"]["diagnostic"]
    assert event["method"] == "job.error" and diagnostic["code"] == "SLANG_ERROR"
    assert diagnostic["line"] == 3 and diagnostic["message"].endswith("[truncated; see worker.stderr.log]")
    assert read_json(directory / "worker-error.json") == {"diagnostic": diagnostic}


def test_worker_notification_bound_matches_supervisor_line_bound(capsys):
    import queue
    from verilog2digital.jobs import worker
    identifier = "a" * 32
    empty = len(json.dumps({"jsonrpc": "2.0", "method": "job.result", "params": {"job_id": identifier, "result_path": ""}}))
    worker._notify(identifier, "job.result", result_path="x" * (65535 - empty))
    line = capsys.readouterr().out.encode("utf-8")
    assert len(line) == 65536
    events = queue.Queue()
    manager._read_events(io.BytesIO(line), events, threading.Event())
    assert events.get_nowait()["params"]["result_path"] == "x" * (65535 - empty)
    with pytest.raises(RuntimeError, match="64 KiB"):
        worker._notify(identifier, "job.result", result_path="x" * (65536 - empty))
    assert capsys.readouterr().out == ""


def test_os_lease_excludes_concurrent_managers_and_recovers_after_close(tmp_path):
    lock = tmp_path / "manager.lock"
    with file_lease(lock):
        with pytest.raises(FrontendError) as error:
            with file_lease(lock):
                pass
        assert error.value.code == "JOB_BUSY"
    with file_lease(lock):
        pass


def test_recovery_never_promotes_partial_output_or_reuses_pid_as_authority(tmp_path):
    store = JobStore.create(tmp_path, request())
    store.update(state="running", stage="mapping", worker_pid=123456)
    recovered = recover_jobs(tmp_path)
    assert recovered[0]["state"] == "interrupted"
    assert recovered[0]["diagnostics"][0]["code"] == "JOB_INTERRUPTED"
    assert recover_jobs(tmp_path) == []


def test_recovery_reconciles_pointer_commit_before_job_record_update(tmp_path):
    output = tmp_path / "output"
    raw = request("convert", output)
    store = JobStore.create(tmp_path, raw)
    store.update(state="running", stage="publishing")
    directory, sources = candidate(tmp_path)
    result = publish(directory, sources, output, job_id=raw["job_id"])
    recovered = recover_jobs(tmp_path)
    assert recovered[0]["state"] == "ready"
    assert recovered[0]["result"]["circuit"] == result["circuit"]
    assert recovered[0]["recovery"] == "committed_current_revision"


class FakeWorker:
    def __init__(self, data=b"", *, exit_code=0, active=0):
        self.stdout = io.BytesIO(data)
        self.pid = 123
        self.exit_code, self.active, self.closed, self.terminated = exit_code, active, False, False

    def poll(self): return self.exit_code
    def metrics(self): return {"active_processes": self.active, "peak_job_memory_bytes": 123456, "root_exit_code": self.exit_code}
    def terminate_tree(self): self.terminated, self.active, self.exit_code = True, 0, 1223
    def close(self): self.closed = True


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "source.v"
    source.write_text("module top(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    monkeypatch.setenv("V2D_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_fingerprint", lambda: {"schema_version": 1, "sha256": "a" * 64})
    return Project([source], top="top")


def install_worker(monkeypatch, *, mode="success", validation=True):
    workers = []
    def launch(store, limits):
        if mode == "crash":
            worker = FakeWorker(exit_code=0xC0000005)
        elif mode == "hang":
            worker = FakeWorker(exit_code=None, active=2)
        else:
            raw = store.read()["request"]
            result = {"schema_version": 2, "kind": raw["kind"], "job_id": store.directory.name,
                      "source_snapshot": {"sources": {}, "binding": {}, "source_map": {}}}
            if raw["kind"] == "inspect":
                result.update(inspection={"top": "top", "top_candidates": ["top"], "diagnostics": []},
                              inspection_path=str(store.directory / "inspection.json"))
            elif raw["kind"] == "validate":
                result.update(validation={"valid": validation}, validation_path=str(store.directory / "validation.json"))
            else:
                directory, sources = candidate(store.directory)
                result["candidate"] = {"directory": str(directory)}
                result["source_snapshot"]["sources"] = sources
            path = store.directory / "worker-result.json"
            atomic_json(path, result)
            event = {"jsonrpc": "2.0", "method": "job.progress", "params": {"job_id": store.directory.name, "stage": "inspecting"}}
            terminal = {"jsonrpc": "2.0", "method": "job.result", "params": {"job_id": store.directory.name, "result_path": str(path)}}
            if mode == "wrong_id": terminal["params"]["job_id"] = "0" * 32
            data = (json.dumps(event) + "\n" + json.dumps(terminal) + "\n").encode()
            if mode == "oversized": data = b"x" * 65538
            if mode == "duplicate": data = b'{"jsonrpc":"2.0","jsonrpc":"2.0","method":"job.progress","params":{}}\n'
            if mode == "await_cancel":
                data = (json.dumps(event) + "\n").encode()
            worker = FakeWorker(data, active=1 if mode in {"leak", "await_cancel"} else 0,
                                exit_code=None if mode == "await_cancel" else 0)
        workers.append(worker)
        return worker
    monkeypatch.setattr(manager, "_launch_worker", launch)
    return workers


def test_inspection_success_has_durable_progress_and_no_publication(project, monkeypatch):
    workers = install_worker(monkeypatch)
    seen = []
    result = run_inspection(project, progress=seen.append)
    status = job_status(Path(result["job_manifest"]).parent)
    assert result["inspection"]["top_candidates"] == ["top"]
    assert status["state"] == "ready" and status["resources"]["metrics"]["active_processes"] == 0
    assert seen[-1]["stage"] == "ready" and workers[0].closed


def test_failed_observers_do_not_reclassify_successful_result(project, monkeypatch):
    install_worker(monkeypatch)
    def broken(event):
        raise RuntimeError("observer broke")
    result = run_inspection(project, progress=broken)
    assert result["state"] == "ready" and result["progress_warnings"]
    assert job_status(Path(result["job_manifest"]).parent)["state"] == "ready"


def test_delayed_native_job_accounting_settles_before_success(project, monkeypatch):
    workers = install_worker(monkeypatch)
    original = manager._launch_worker
    def launch(store, limits):
        worker = original(store, limits)
        end = time.monotonic() + .1
        worker.metrics = lambda: {"active_processes": int(time.monotonic() < end),
                                 "root_exit_code": 0, "peak_job_memory_bytes": 123456}
        return worker
    monkeypatch.setattr(manager, "_launch_worker", launch)
    result = run_inspection(project)
    assert result["state"] == "ready"
    assert not workers[0].terminated and workers[0].closed


@pytest.mark.parametrize("mode,code", [("crash", "WORKER_CRASH"), ("wrong_id", "WORKER_PROTOCOL"),
                                     ("oversized", "WORKER_PROTOCOL"), ("duplicate", "WORKER_PROTOCOL"),
                                     ("leak", "WORKER_PROCESS_LEAK")])
def test_incomplete_or_malformed_workers_never_report_ready(mode, code, project, monkeypatch):
    workers = install_worker(monkeypatch, mode=mode)
    with pytest.raises(JobError) as error:
        run_inspection(project)
    assert error.value.code == code
    assert workers[0].closed and workers[0].active == 0
    assert job_status(Path(error.value.job_manifest).parent)["state"] == "failed"


def test_total_deadline_terminates_owned_helpers_and_keeps_diagnostics(project, monkeypatch, tmp_path):
    workers = install_worker(monkeypatch, mode="hang")
    with pytest.raises(JobError) as error:
        run_job(project, tmp_path / "output", limits=JobLimits(deadline_seconds=.1))
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert workers[0].terminated and workers[0].closed
    assert read_json(tmp_path / "output/failure.json")["diagnostic"]["code"] == "DEADLINE_EXCEEDED"
    assert not (tmp_path / "output/current.json").exists()


def test_pending_cancel_after_worker_progress_prevents_publication(project, monkeypatch, tmp_path):
    install_worker(monkeypatch, mode="await_cancel")
    def cancel(event):
        if event["stage"] == "inspecting":
            assert cancel_job(Path(event["job_manifest"]).parent)["accepted"]
    with pytest.raises(JobError) as error:
        run_job(project, tmp_path / "output", progress=cancel)
    assert error.value.code == "CANCELLED"
    assert job_status(Path(error.value.job_manifest).parent)["state"] == "cancelled"
    assert not (tmp_path / "output/current.json").exists()


def test_blocked_progress_observer_cannot_block_deadline_or_tree_cleanup(project, monkeypatch):
    workers = install_worker(monkeypatch, mode="hang")
    unblock = threading.Event()
    started = time.monotonic()
    try:
        with pytest.raises(JobError) as error:
            run_inspection(project, limits=JobLimits(deadline_seconds=.1), progress=lambda event: unblock.wait(10))
        assert error.value.code == "DEADLINE_EXCEEDED"
        assert time.monotonic() - started < 2
        assert workers[0].terminated and workers[0].closed
    finally:
        unblock.set()


def test_cancellation_requested_before_start_never_launches_worker(project, monkeypatch):
    def no_launch(*_): raise AssertionError("cancelled job started a native process")
    monkeypatch.setattr(manager, "_launch_worker", no_launch)
    event = threading.Event()
    event.set()
    with pytest.raises(JobError) as error:
        run_inspection(project, cancel_event=event)
    assert error.value.code == "CANCELLED"


def test_runtime_change_invalidates_worker_result_before_publication(project, monkeypatch):
    install_worker(monkeypatch)
    values = iter([{"sha256": "a" * 64}, {"sha256": "b" * 64}])
    monkeypatch.setattr(manager, "_fingerprint", lambda: next(values))
    with pytest.raises(JobError) as error:
        run_inspection(project)
    assert error.value.code == "TOOL_CHANGED"


def test_validation_false_is_a_completed_durable_validation_failure(project, monkeypatch):
    install_worker(monkeypatch, validation=False)
    result = manager.run_validation(project.sources[0])
    assert result["state"] == "failed" and not result["validation"]["valid"]
    assert job_status(Path(result["job_manifest"]).parent)["state"] == "failed"


@pytest.mark.parametrize("options", [{"deadline_seconds": True}, {"deadline_seconds": float("inf")},
    {"memory_bytes": 64 * 1024**2}, {"java_heap_bytes": 6 * 1024**3}, {"process_memory_bytes": 512 * 1024**2},
    {"active_process_limit": 0}])
def test_invalid_resource_budgets_are_rejected_before_launch(options):
    with pytest.raises(JobError) as error:
        JobLimits(**options).validate()
    assert error.value.code == "INVALID_LIMITS"
