"""Fault injection changes only one owned launch and never certifies a fallback."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from verilog2digital.jobs.processes import ProcessLimits

SPEC = importlib.util.spec_from_file_location("benchmark_recovery", Path(__file__).resolve().parents[2] / "scripts/benchmark_recovery.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class Worker:
    def __init__(self):
        self.closed = False
    def metrics(self):
        return {"active_processes": 0 if self.closed else 1, "closed": self.closed}
    def close(self):
        self.closed = True


def test_only_first_owned_server_cap_changes_and_original_launcher_is_restored(tmp_path):
    received = []
    class Launcher:
        @staticmethod
        def start(argv, **kwargs):
            received.append((argv, kwargs))
            return Worker()
    runtime = SimpleNamespace(ManagedWorker=Launcher)
    limits = ProcessLimits(job_memory_bytes=6 * 1024**3, active_process_limit=4)
    options = {"limits": limits, "stderr_path": tmp_path / "server.log", "env": {"original": "value"}}
    with benchmark.allocation_fault(runtime, 2 * 1024**3) as launches:
        first = runtime.ManagedWorker.start(["C:/owned/llama-server.exe", "--ctx-size", "4096"], **options)
        first.close()
        second = runtime.ManagedWorker.start(["C:/owned/llama-server.exe", "--ctx-size", "2048"], **options)
        second.close()
    assert runtime.ManagedWorker is Launcher
    assert [item[1]["limits"].job_memory_bytes for item in received] == [2 * 1024**3, 6 * 1024**3]
    assert received[1][1]["limits"] is limits
    assert all(item[1]["env"] == {"original": "value"} for item in received)
    assert [row["context_tokens"] for row in launches] == [4096, 2048]
    assert launches[1]["prior_processes"] == [{"active_processes": 0, "closed": True}]
    assert all(row["before_harness_cleanup"]["closed"] for row in launches)


def test_live_prior_process_blocks_replacement_and_cleanup_is_not_hidden(tmp_path):
    original = SimpleNamespace(start=lambda *args, **kwargs: Worker())
    runtime = SimpleNamespace(ManagedWorker=original)
    kwargs = {"limits": ProcessLimits(), "stderr_path": tmp_path / "server.log"}
    with benchmark.allocation_fault(runtime, 2 * 1024**3) as launches:
        runtime.ManagedWorker.start(["llama-server.exe", "--ctx-size", "4096"], **kwargs)
        with pytest.raises(RuntimeError, match="prior model Job closed"):
            runtime.ManagedWorker.start(["llama-server.exe", "--ctx-size", "2048"], **kwargs)
    assert runtime.ManagedWorker is original
    assert launches[0]["before_harness_cleanup"] == {"active_processes": 1, "closed": False}
    assert launches[0]["closed"] == {"active_processes": 0, "closed": True}


def valid_report():
    profile = {"model_id": "qwen3-4b-q4_k_m", "backend": "cuda", "gpu_index": 0,
               "gpu_layers": 37, "host_memory_bytes": 6 * 1024**3, "kv_type": "q8_0", "context_tokens": 4096}
    closed = {"active_processes": 0, "closed": True}
    return {"valid": True, "identical_baseline": True, "validation": {"valid": True, "behavior": {"valid": True}},
        "accepted_electrical_regressions": 0, "real_model_executed": True,
        "refinement": {"canvases": [{"attempts": [{"diagnostic": {"code": "MODEL_ALLOCATION"},
            "allocation_evidence": {"source": "owned_server_stderr", "signature": "std_bad_alloc"}}]}],
            "memory_recovery": {"after_attempt": 1, "reason": "MODEL_ALLOCATION", "from": profile,
                                "to": {**profile, "context_tokens": 2048}},
            "retired_runtimes": [{"process": closed}],
            "runtime": {"requests": 1, "profile": {"context_tokens": 2048}, "state": "unloaded", "process": closed}}}


def test_recovery_report_requires_actual_smaller_inference_and_cleanup():
    report = valid_report()
    launches = [{"context_tokens": context, "before_harness_cleanup": {"active_processes": 0, "closed": True},
                 "configured_job_memory_bytes": 6 * 1024**3, "applied_job_memory_bytes": cap * 1024**3}
                for context, cap in ((4096, 2), (2048, 6))]
    assert all(benchmark.recovery_checks(report, launches).values())
    for mutate, expected in [
        (lambda r: r.update(real_model_executed=False), "real_smaller_profile_generation"),
        (lambda r: r["refinement"]["canvases"][0]["attempts"][0]["diagnostic"].update(code="MODEL_EXIT"), "actual_typed_native_denial"),
        (lambda r: r["refinement"]["memory_recovery"]["to"].update(gpu_layers=0), "unchanged_model_and_gpu"),
        (lambda r: r["validation"]["behavior"].update(valid=False), "original_hdl_behavior_valid"),
        (lambda r: r["refinement"]["retired_runtimes"][0]["process"].update(closed=False), "retired_runtime_closed"),
    ]:
        changed = deepcopy(report)
        mutate(changed)
        assert not benchmark.recovery_checks(changed, launches)[expected]
    launches[0]["before_harness_cleanup"] = {"active_processes": 1, "closed": False}
    assert not benchmark.recovery_checks(report, launches)["production_cleanup_before_harness"]


@pytest.mark.parametrize("cap", [0, 63, 3073, True, 2048.0])
def test_invalid_fault_caps_cannot_start_any_owned_server(cap):
    with pytest.raises(ValueError):
        benchmark.validate_fault_limit(cap)


def test_cli_uses_specialized_worker_only_for_faulted_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "runtime_fingerprint", lambda: {"sha256": "f" * 64})
    monkeypatch.setattr(benchmark, "verify_artifact_bindings", lambda result: 6)
    seen = []
    def run(request, limits, *, worker_entrypoint=None):
        seen.append((request, limits, worker_entrypoint))
        return {"valid": True, "wall_seconds": 0}
    monkeypatch.setattr(benchmark.benchmark, "run_owned", run)
    assert benchmark.main(["--output", str(tmp_path / ".runs/recovery")]) == 0
    assert [row[0]["strategy"] for row in seen] == ["baseline", "real4b"]
    assert seen[0][2] is None and seen[1][2] == Path(benchmark.__file__)
    assert all(row[0]["options"]["max_attempts"] == 3 and row[0]["options"]["budget_seconds"] == 60 for row in seen)
    assert seen[1][0]["fault_memory_mib"] == 2048
    assert all(row[1].memory_bytes == 6 * 1024**3 for row in seen)


@pytest.mark.parametrize("changed", ["circuit", "source"])
def test_artifact_audit_rejects_later_circuit_or_original_source_changes(tmp_path, changed):
    conversion = tmp_path / "conversion"
    conversion.mkdir()
    circuit, sidecar = conversion / "circuit.dig", conversion / "circuit.dig.ids.json"
    source = tmp_path / "source.v"
    for file, content in ((circuit, "<circuit/>"), (sidecar, "{}"), (source, "module source; endmodule")):
        file.write_text(content, encoding="utf-8")
    manifest = {"artifacts": {key: {"path": file.name, "sha256": benchmark.digest(file)}
                             for key, file in (("circuit", circuit), ("sidecar", sidecar))},
                "sources": {str(source): benchmark.digest(source)}}
    manifest_path = conversion / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = {"conversion": {"directory": str(conversion), "manifest": str(manifest_path),
                            "circuit": str(circuit), "sidecar": str(sidecar)},
              "selected": {"circuit_sha256": benchmark.digest(circuit), "sidecar_sha256": benchmark.digest(sidecar)}}
    assert benchmark.verify_artifact_bindings(result) == 2
    target = circuit if changed == "circuit" else source
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        benchmark.verify_artifact_bindings(result)
