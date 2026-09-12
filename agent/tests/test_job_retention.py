"""Owned terminal-job pruning never deletes live work or published revisions."""
from pathlib import Path
from contextlib import contextmanager
import os

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs import manager
from verilog2digital.jobs.manager import JobError, run_inspection
from verilog2digital.jobs.store import (JobStore, atomic_json, directory_bytes, prune_jobs, read_json,
                                      recover_unlocked)
from test_jobs import request, project, install_worker
from test_publication import candidate, publish


def completed(root, number, *, heavy_bytes=0, state="failed"):
    raw = request()
    raw["job_id"] = f"{number:032x}"
    store = JobStore.create(root, raw)
    store.update(state="running", stage="mapping")
    if state != "running":
        store.update(state=state, stage=state)
    if heavy_bytes:
        work = store.directory / "candidate/work"
        work.mkdir(parents=True)
        (work / "diagnostics.bin").write_bytes(b"x" * heavy_bytes)
    return store


def test_terminal_count_bound_removes_oldest_owned_jobs_only(tmp_path):
    jobs = [completed(tmp_path, index) for index in range(1, 5)]
    active = completed(tmp_path, 5, state="running")
    unrelated = tmp_path / "my-notes"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    output = tmp_path / "revisions"
    output.mkdir()
    (output / "circuit.dig").write_text("published")
    report = prune_jobs(tmp_path, max_jobs=2)
    assert report["removed_jobs"] == [jobs[0].directory.name, jobs[1].directory.name]
    assert active.path.exists() and jobs[-1].path.exists()
    assert (unrelated / "keep.txt").read_text() == "keep"
    assert (output / "circuit.dig").read_text() == "published"


def test_byte_pressure_discards_heavy_candidate_but_keeps_newest_diagnostics(tmp_path):
    first = completed(tmp_path, 1, heavy_bytes=30000)
    second = completed(tmp_path, 2, heavy_bytes=30000)
    report = prune_jobs(tmp_path, max_jobs=16, max_bytes=40000, keep_ids={second.directory.name})
    assert report["retained_bytes"] <= 40000
    assert first.path.exists() and second.path.exists()
    pruned = read_json(first.path)
    assert pruned["state"] == "failed" and pruned["retention"]["heavy_artifacts_removed"] == ["candidate"]
    assert not (first.directory / "candidate").exists()
    assert (second.directory / "candidate/work/diagnostics.bin").exists()


def test_active_work_is_not_pruned_even_when_it_exceeds_retention_budget(tmp_path):
    active = completed(tmp_path, 1, heavy_bytes=30000, state="running")
    report = prune_jobs(tmp_path, max_bytes=1)
    assert active.path.exists() and (active.directory / "candidate").is_dir()
    assert report["removed_jobs"] == [] and report["pruned_jobs"] == []


def test_directory_and_manifest_identity_mismatch_is_not_deletion_authority(tmp_path):
    wrong = completed(tmp_path, 1, heavy_bytes=30000)
    value = read_json(wrong.path)
    value["job_id"] = "f" * 32
    atomic_json(wrong.path, value)
    report = prune_jobs(tmp_path, max_bytes=1)
    assert wrong.path.exists() and (wrong.directory / "candidate").exists()
    assert report["diagnostics"] and not report["removed_jobs"]


def test_redirected_subtree_is_rejected_before_recursive_pruning(tmp_path):
    store = completed(tmp_path, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    try:
        (store.directory / "candidate").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a test directory symlink requires Windows developer privilege")
    report = prune_jobs(tmp_path, max_bytes=1)
    assert report["diagnostics"]
    assert (outside / "keep.txt").read_text() == "keep"
    assert store.path.exists()


def test_live_directory_size_and_file_count_are_bounded(tmp_path):
    (tmp_path / "large.log").write_bytes(b"x" * 101)
    with pytest.raises(FrontendError) as error:
        directory_bytes(tmp_path, limit=100)
    assert error.value.code == "JOB_DISK_LIMIT"
    (tmp_path / "other.log").write_text("x")
    with pytest.raises(FrontendError):
        directory_bytes(tmp_path, max_entries=1)


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive file-sharing regression")
def test_exclusively_locked_temporary_file_still_counts_toward_live_disk_limit(tmp_path):
    import ctypes
    from ctypes import wintypes
    file = tmp_path / "java-perfdata.tmp"
    file.write_bytes(b"x" * 321)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(file), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assert directory_bytes(tmp_path) == 321
        with pytest.raises(FrontendError) as error:
            directory_bytes(tmp_path, limit=320)
        assert error.value.code == "JOB_DISK_LIMIT"
    finally:
        assert kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows live file metadata regression")
@pytest.mark.parametrize("share", [0, 7], ids=["exclusive", "shared"])
def test_live_growth_cannot_hide_behind_cached_directory_metadata(tmp_path, monkeypatch, share):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    path = tmp_path / "growing.tmp"
    handle = kernel.CreateFileW(str(path), 0x40000000, share, None, 1, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        # Windows can keep precisely this pre-growth metadata in fresh directory
        # enumerations too. Retain a real cached entry to make that race reliable.
        with os.scandir(tmp_path) as stream:
            cached_entries = list(stream)
        assert cached_entries[0].stat(follow_symlinks=False).st_size == 0
        real_scandir = os.scandir

        @contextmanager
        def cached_enumeration(directory):
            if Path(directory) == tmp_path:
                yield iter(cached_entries)
            else:
                with real_scandir(directory) as stream:
                    yield stream

        assert os.write(descriptor, b"x" * 65536) == 65536
        assert os.fstat(descriptor).st_size == 65536
        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", cached_enumeration)
            with pytest.raises(FrontendError) as error:
                directory_bytes(tmp_path, limit=1024)
            assert error.value.code == "JOB_DISK_LIMIT"
            assert directory_bytes(tmp_path) == 65536
    finally:
        os.close(descriptor)  # The CRT descriptor owns the Windows file handle.


def test_fresh_metadata_still_rejects_redirected_regular_files(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"keep")
    try:
        (tmp_path / "link.bin").symlink_to(target)
    except OSError:
        pytest.skip("Creating a test file symlink requires Windows developer privilege")
    with pytest.raises(FrontendError) as error:
        directory_bytes(tmp_path)
    assert error.value.code == "JOB_STORAGE_PATH"
    assert target.read_bytes() == b"keep"


def test_disk_budget_failure_terminates_worker_before_accepting_a_result(project, monkeypatch):
    workers = install_worker(monkeypatch, mode="hang")
    def budget(directory, **options):
        if options.get("limit") is not None and any(workers):
            raise FrontendError("JOB_DISK_LIMIT", "injected live disk budget failure")
        return 0
    monkeypatch.setattr(manager, "directory_bytes", budget)
    with pytest.raises(JobError) as error:
        run_inspection(project)
    assert error.value.code == "JOB_DISK_LIMIT"
    assert workers[0].terminated and workers[0].closed


def test_recovery_does_not_mark_tampered_committed_bundle_ready(tmp_path):
    output = tmp_path / "output"
    raw = request("convert", output)
    store = JobStore.create(tmp_path, raw)
    store.update(state="running", stage="publishing")
    directory, sources = candidate(tmp_path)
    result = publish(directory, sources, output, job_id=raw["job_id"])
    Path(result["circuit"]).write_text("changed")
    recovered = recover_unlocked(tmp_path)
    assert recovered[0]["state"] == "interrupted"
    assert recovered[0]["diagnostics"][0]["code"] == "JOB_INTERRUPTED"
