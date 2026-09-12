"""Real owned workers stage Unicode HDL, preserve revisions and release helpers."""
from pathlib import Path
import threading

import pytest

from verilog2digital.frontend.project import Project
from verilog2digital.jobs import manager
from verilog2digital.jobs.manager import JobError, JobLimits, job_status, run_inspection, run_job, run_validation
from verilog2digital.jobs.store import read_json


@pytest.fixture(autouse=True)
def owned_job_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("V2D_JOB_ROOT", str(tmp_path / "jobs"))
    # Concurrent development edits are independent of process/snapshot behavior;
    # test_jobs.py explicitly checks the runtime-change rejection contract.
    fingerprint = manager._fingerprint()
    monkeypatch.setattr(manager, "_fingerprint", lambda: fingerprint)


def test_real_inspection_discovers_ambiguous_tops_in_an_owned_worker(tmp_path):
    source = tmp_path / "seçim alanı Ω.v"
    source.write_text("module first(input a,output y); assign y=a; endmodule\n"
                      "module second(input b,output z); assign z=~b; endmodule\n", encoding="utf-8")
    result = run_inspection(Project([source]))
    assert result["inspection"]["top"] is None
    assert result["inspection"]["top_candidates"] == ["first", "second"]
    assert set(result["inspection"]["source_files"]) == {str(source)}
    status = job_status(Path(result["job_manifest"]).parent)
    assert status["resources"]["metrics"]["active_processes"] == 0
    assert status["source_snapshot"]["sources"].keys() == {str(source)}


def test_real_unicode_conversion_replacement_failure_and_owned_validation(tmp_path):
    inputs = tmp_path / "kaynak alanı Ω"
    inputs.mkdir()
    header = inputs / "tanım Ω.vh"
    header.write_text("`define OP(a) (~(a))\n", encoding="utf-8")
    source = inputs / "devre Ω.v"
    source.write_text('`include "tanım Ω.vh"\nmodule top(input [3:0] a,output [3:0] y); assign y=`OP(a); endmodule\n', encoding="utf-8")
    project = Project([source], top="top")
    output = tmp_path / "çıktı Ω"
    first = run_job(project, output, cache=False, options={"random_count": 0})
    assert first["state"] == "ready" and Path(first["circuit"]).is_file()
    assert Path(first["circuit"]).is_relative_to(output / "revisions")
    assert read_json(Path(first["manifest"]))["configuration"]["sources"] == [str(source)]
    before = (output / "current.json").read_bytes()
    circuit = Path(first["circuit"]).read_bytes()
    header.write_text("`define OP(a) ((a) ^ 4'h8)\n", encoding="utf-8")
    second = run_job(project, output, replace=True, cache=False, options={"random_count": 0})
    assert second["revision"] != first["revision"]
    assert Path(first["circuit"]).read_bytes() == circuit
    selected = (output / "current.json").read_bytes()
    assert selected != before
    source.write_text("module top(\n", encoding="utf-8")
    with pytest.raises(JobError) as error:
        run_job(project, output, replace=True, cache=False)
    assert error.value.code == "SLANG_ERROR"
    assert error.value.path == str(source)
    assert (output / "current.json").read_bytes() == selected
    assert Path(second["circuit"]).is_file() and (output / "failure.json").is_file()
    checked = run_validation(output, artifacts_only=True)
    assert checked["validation"]["valid"]
    assert checked["validation"]["behavior_replayed"] is False
    assert job_status(Path(checked["job_manifest"]).parent)["resources"]["metrics"]["active_processes"] == 0


def test_real_manager_deadline_during_native_worker_start_leaves_no_process_tree(tmp_path):
    source = tmp_path / "simple.v"
    source.write_text("module top(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    with pytest.raises(JobError) as error:
        run_inspection(Project([source]), limits=JobLimits(deadline_seconds=.3))
    assert error.value.code == "DEADLINE_EXCEEDED"
    status = job_status(Path(error.value.job_manifest).parent)
    assert status["state"] == "failed"
    if status["resources"]["metrics"]:
        assert status["resources"]["metrics"]["active_processes"] == 0


def test_real_worker_can_be_cancelled_after_source_snapshot_stage(tmp_path):
    source = tmp_path / "simple.v"
    source.write_text("module top(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    cancelled = threading.Event()
    def observe(event):
        if event["stage"] == "snapshotting_sources":
            cancelled.set()
    with pytest.raises(JobError) as error:
        run_job(Project([source]), tmp_path / "cancelled", cache=False, progress=observe, cancel_event=cancelled)
    assert error.value.code == "CANCELLED"
    status = job_status(Path(error.value.job_manifest).parent)
    assert status["state"] == "cancelled" and status["resources"]["metrics"]["active_processes"] == 0
    assert not (tmp_path / "cancelled/current.json").exists()


def test_real_cache_hit_reuses_bound_candidate_and_header_change_forces_miss(tmp_path):
    source = tmp_path / "önbellek.v"
    header = tmp_path / "tanım.vh"
    header.write_text("`define MASK 4'h1\n", encoding="utf-8")
    source.write_text('`include "tanım.vh"\nmodule top(input [3:0] a,output [3:0] y); assign y=a ^ `MASK; endmodule\n', encoding="utf-8")
    project = Project([source], top="top")
    first = run_job(project, tmp_path / "first", options={"random_count": 0})
    assert first["cache"]["stored"] and not first["cache"]["hit"]
    second = run_job(project, tmp_path / "second", options={"random_count": 0})
    assert second["cache"]["hit"] and second["cache"]["key"] == first["cache"]["key"]
    second_job = Path(second["job_manifest"]).parent
    assert not (second_job / "candidate/work").exists()
    assert Path(first["circuit"]).read_bytes() == Path(second["circuit"]).read_bytes()
    assert read_json(Path(second["validation"]))["valid"]
    assert read_json(Path(second["manifest"]))["job"]["cache"]["hit"]
    assert read_json(Path(second["manifest"]))["configuration"]["sources"] == [str(source)]
    header.write_text("`define MASK 4'h8\n", encoding="utf-8")
    third = run_job(project, tmp_path / "third", options={"random_count": 0})
    assert not third["cache"]["hit"] and third["cache"]["key"] != first["cache"]["key"]
    assert Path(second["circuit"]).read_bytes() != Path(third["circuit"]).read_bytes()
