"""Freshness retains every recorded artifact binding across one memory retry."""
from copy import deepcopy

import pytest

from verilog2digital import verification
from verilog2digital.jobs.publication import atomic_document, read_document
from verilog2digital.llm import artifacts
from test_refinement_workflow import model_result, NativeModel


def recovery_result(model_result):
    directory, _, _ = model_result
    path = directory / "manifest.json"
    manifest = read_document(path)
    refinement = manifest["refinement"]
    previous = deepcopy(refinement["runtime"])
    previous["profile"]["context_tokens"] = 4096
    refinement["retired_runtimes"] = [previous]
    refinement["runtime"] = {"profile": {"backend": "cpu", "context_tokens": 2048},
                             "state": "unloaded", "process": {"closed": True, "active_processes": 0}}
    atomic_document(path, manifest)
    return path, manifest


def test_pre_artifact_replacement_timeout_preserves_accepted_old_binding(model_result):
    recovery_result(model_result)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert result["valid"]
    assert result["checks"]["model_runtime"]["bindings"] == 1
    assert result["recorded_behavior"]["valid"] and not result["behavior_replayed"]


@pytest.mark.parametrize("file", [1, 2])
def test_changed_retired_model_or_runtime_file_fails_full_freshness(model_result, file):
    recovery_result(model_result)
    model_result[file].write_bytes(b"changed retired artifact")
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_CHANGED"


@pytest.mark.parametrize("family,field", [("model", "sha256"), ("runtime", "server_sha256")])
def test_changed_retired_binding_is_verified_even_when_active_binding_matches(model_result, family, field):
    path, manifest = recovery_result(model_result)
    retired = manifest["refinement"]["retired_runtimes"][0]
    manifest["refinement"]["runtime"] = deepcopy(retired)
    retired[family][field] = "b" * 64
    atomic_document(path, manifest)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_CHANGED"


def test_exact_bindings_are_hashed_once_despite_profile_and_metrics_differences(model_result, monkeypatch):
    path, manifest = recovery_result(model_result)
    retired = manifest["refinement"]["retired_runtimes"][0]
    active = deepcopy(retired)
    active["profile"]["context_tokens"] = 2048
    active["requests"] = 1
    manifest["refinement"]["runtime"] = active
    atomic_document(path, manifest)
    original = artifacts.verify_installation
    calls = []
    def verify(model_id, backend):
        calls.append((model_id, backend))
        return original(model_id, backend)
    monkeypatch.setattr(artifacts, "verify_installation", verify)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert result["valid"] and result["checks"]["model_runtime"]["bindings"] == 1
    assert calls == [("qwen3-4b-q4_k_m", "cpu")]


@pytest.mark.parametrize("backend", ["cpu", "cuda"])
def test_all_distinct_model_runtime_backend_bindings_are_checked(model_result, monkeypatch, backend):
    path, manifest = recovery_result(model_result)
    retired = manifest["refinement"]["retired_runtimes"][0]
    active = deepcopy(retired)
    if backend == "cpu":
        active["model"]["id"] = "qwen3-4b-q5_k_m"
        active["model"]["sha256"] = "c" * 64
    else:
        active["profile"]["backend"] = "cuda"
        active["runtime"]["server_sha256"] = "d" * 64
    manifest["refinement"]["runtime"] = active
    atomic_document(path, manifest)
    records = [active, retired]
    calls = []
    def verify(model_id, selected_backend):
        calls.append((model_id, selected_backend))
        row = next(record for record in records
                   if record["model"]["id"] == model_id and record["profile"]["backend"] == selected_backend)
        return {"model": row["model"], "runtime": row["runtime"]}
    monkeypatch.setattr(artifacts, "verify_installation", verify)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert result["valid"] and result["checks"]["model_runtime"]["bindings"] == 2
    assert len(calls) == 2 and len(set(calls)) == 2


@pytest.mark.parametrize("retired", [None, {}, "unused", [None], [{}], [[], {}]])
def test_corrupt_or_unbounded_retired_records_fail_before_model_hashing(model_result, monkeypatch, retired):
    path, manifest = recovery_result(model_result)
    manifest["refinement"]["retired_runtimes"] = retired
    atomic_document(path, manifest)
    monkeypatch.setattr(artifacts, "verify_installation", lambda *a, **k: pytest.fail("Malformed binding reached hashing"))
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_BINDING"


@pytest.mark.parametrize("change", ["model", "runtime", "profile", "null-model", "empty-runtime", "invalid-backend",
                                    "invalid-backend-type", "mismatched-model"])
def test_partial_retired_binding_cannot_hide_behind_complete_active_record(model_result, change):
    path, manifest = recovery_result(model_result)
    retired = manifest["refinement"]["retired_runtimes"][0]
    manifest["refinement"]["runtime"] = deepcopy(retired)
    if change.startswith("null-"):
        retired["model"] = None
    elif change == "empty-runtime":
        retired["runtime"] = {}
    elif change == "invalid-backend":
        retired["profile"]["backend"] = "unknown"
    elif change == "invalid-backend-type":
        retired["profile"]["backend"] = []
    elif change == "mismatched-model":
        retired["profile"]["model_id"] = "qwen3-4b-q5_k_m"
    else:
        retired.pop(change)
    atomic_document(path, manifest)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_BINDING"


def test_accepted_improvement_requires_a_complete_binding_in_either_record(model_result):
    path, manifest = recovery_result(model_result)
    manifest["refinement"]["retired_runtimes"] = [{"profile": {"backend": "cpu"}}]
    atomic_document(path, manifest)
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_BINDING"


def test_artifacts_only_does_not_require_retired_model_files(model_result, monkeypatch):
    recovery_result(model_result)
    model_result[1].unlink()
    model_result[2].unlink()
    monkeypatch.setattr(artifacts, "verify_installation", lambda *a, **k: pytest.fail("Artifact-only model hash"))
    result = verification.validate_output(model_result[0], artifacts_only=True, adapter=NativeModel())
    assert result["valid"] and "model_runtime" not in result["checks"]
