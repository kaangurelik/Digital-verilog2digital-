"""Freshness checks bind artifacts, original inputs and the recorded runtime."""
from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs.publication import atomic_document, digest, output_lease, publish_revision, read_document
from verilog2digital import verification


FINGERPRINT = {"schema_version": 1, "sha256": "a" * 64}


class NativeModel:
    def __init__(self, action=None, valid=True):
        self.action, self.valid, self.calls = action, valid, []

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if self.action:
            self.action(params)
        return {"model": {"valid": self.valid, "diagnostics": [] if self.valid else [
            {"code": "MULTIPLE_DRIVERS", "message": "Two output pins drive this net"}]}}


@pytest.fixture
def saved_result(tmp_path, monkeypatch):
    source = tmp_path / "kaynak_π.v"
    header = tmp_path / "başlık.vh"
    source.write_text('`include "başlık.vh"\nmodule top(input a,output y); assign y=a; endmodule\n', encoding="utf-8")
    header.write_text("// included dependency\n", encoding="utf-8")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    payloads = {"circuit": ("circuit.dig", b"native model placeholder"), "sidecar": ("circuit.dig.ids.json", b"{}"),
                "preview": ("preview.png", b"png placeholder"), "svg": ("preview.svg", b"<svg/>"),
                "validation": ("validation.json", b'{"valid":true,"behavior":{"valid":true,"input_vectors":2}}')}
    artifacts = {}
    for name, (filename, payload) in payloads.items():
        path = candidate / filename
        path.write_bytes(payload)
        artifacts[name] = {"path": filename, "sha256": digest(path)}
    manifest = {"schema_version": 1, "state": "ready", "top": "top", "profile": "scalar-v1",
                "configuration": {"sources": [str(source)], "include_dirs": [], "defines": {}, "parameters": {}},
                "artifacts": artifacts}
    atomic_document(candidate / "manifest.json", manifest)
    hashes = {str(path): digest(path) for path in (source, header)}
    output = tmp_path / "çıktı_Ω"
    with output_lease(output, replace=False) as lease:
        result = publish_revision(candidate, output, "b" * 32, expected_current_sha256=lease["current_sha256"],
                                  replace=False, job_metadata={"runtime_fingerprint": deepcopy(FINGERPRINT)},
                                  original_hashes=hashes, checkpoint=lambda: None)
    monkeypatch.setattr(verification, "runtime_fingerprint", lambda: deepcopy(FINGERPRINT))
    return {"output": output, "source": source, "header": header, "candidate": candidate, **result}


def test_fresh_result_reports_recorded_behavior_without_claiming_replay(saved_result):
    native = NativeModel()
    result = verification.validate_output(saved_result["output"], adapter=native)
    assert result["valid"] and result["state"] == "ready"
    assert set(result["checks"]) == {"artifacts", "sources", "runtime", "native_model"}
    assert result["recorded_behavior"] == {"valid": True, "input_vectors": 2}
    assert result["behavior_replayed"] is False
    assert native.calls[0][0] == "circuit.inspect"
    assert Path(native.calls[0][1]["path"]) == Path(saved_result["circuit"])


@pytest.mark.parametrize("file", ["source", "header"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_changed_original_or_include_fails_freshness(saved_result, file, change):
    path = saved_result[file]
    path.write_text("changed input", encoding="utf-8") if change == "edit" else path.unlink()
    result = verification.validate_output(saved_result["output"], adapter=NativeModel())
    assert not result["valid"] and result["checks"]["artifacts"]["valid"]
    assert not result["checks"]["sources"]["valid"]
    assert result["checks"]["sources"]["diagnostics"][0]["code"] == "SOURCE_CHANGED"


def test_changed_runtime_is_separate_from_artifact_validity(saved_result, monkeypatch):
    monkeypatch.setattr(verification, "runtime_fingerprint", lambda: {"sha256": "c" * 64})
    result = verification.validate_output(saved_result["output"], adapter=NativeModel())
    assert not result["valid"] and result["checks"]["sources"]["valid"]
    assert result["checks"]["runtime"]["diagnostics"][0]["code"] == "TOOL_CHANGED"


@pytest.mark.parametrize("artifact", ["circuit", "sidecar", "preview", "svg", "validation"])
def test_changed_artifact_fails_before_native_reload(saved_result, artifact):
    Path(saved_result[artifact]).write_bytes(b"changed artifact")
    native = NativeModel()
    result = verification.validate_output(saved_result["output"], adapter=native)
    assert not result["valid"] and not native.calls
    assert result["checks"]["artifacts"]["diagnostics"][0]["code"] == "ARTIFACT_CHANGED"


def test_selected_manifest_hash_cannot_be_silently_rebound(saved_result):
    path = Path(saved_result["manifest"])
    manifest = read_document(path)
    manifest["top"] = "different"
    atomic_document(path, manifest)
    result = verification.validate_output(saved_result["output"], adapter=NativeModel())
    assert not result["valid"]
    assert result["checks"]["artifacts"]["diagnostics"][0]["code"] == "ARTIFACT_CHANGED"


@pytest.mark.parametrize("malformed", [None, [], 1, "manifest"])
def test_malformed_manifest_root_has_structured_failure(tmp_path, malformed):
    atomic_document(tmp_path / "manifest.json", malformed)
    result = verification.validate_output(tmp_path, adapter=NativeModel())
    assert result["state"] == "failed" and not result["valid"]
    assert result["checks"]["artifacts"]["diagnostics"][0]["code"] == "INVALID_CANDIDATE"


@pytest.mark.parametrize("job", [None, [], {"runtime_fingerprint": []}, {"runtime_fingerprint": 1}])
def test_malformed_runtime_binding_has_structured_failure(saved_result, job):
    directory = saved_result["candidate"]
    manifest = read_document(directory / "manifest.json")
    manifest.update(schema_version=2, job=job, sources={str(saved_result["source"]): digest(saved_result["source"])})
    atomic_document(directory / "manifest.json", manifest)
    result = verification.validate_output(directory, adapter=NativeModel())
    assert not result["valid"] and result["state"] == "failed"
    assert result["diagnostics"][0]["code"]


@pytest.mark.parametrize("artifact", ["circuit", "validation"])
def test_artifact_mutation_during_native_check_cannot_report_ready(saved_result, artifact):
    def mutate(_):
        Path(saved_result[artifact]).write_bytes(b"changed while native inspection was running")
    result = verification.validate_output(saved_result["output"], adapter=NativeModel(mutate))
    assert not result["valid"] and result["state"] == "failed"
    assert any(item["code"] == "ARTIFACT_CHANGED" for item in result["diagnostics"])


def test_invalid_native_model_is_reported_without_rewriting_behavior_coverage(saved_result):
    result = verification.validate_output(saved_result["output"], adapter=NativeModel(valid=False))
    assert not result["valid"]
    assert result["diagnostics"] == [{"check": "native_model", "code": "MULTIPLE_DRIVERS", "message": "Two output pins drive this net"}]
    assert result["recorded_behavior"]["valid"] is True and result["behavior_replayed"] is False


def test_native_reload_error_returns_structured_failure(saved_result):
    def fail(_):
        raise FrontendError("NATIVE_RELOAD", "The native file could not be reopened")
    result = verification.validate_output(saved_result["output"], adapter=NativeModel(fail))
    assert not result["valid"]
    assert result["checks"]["native_model"]["diagnostics"][0]["code"] == "NATIVE_RELOAD"


def test_artifacts_only_relocates_without_original_sources_or_runtime(saved_result, tmp_path, monkeypatch):
    relocated = tmp_path / "再配置"
    shutil.copytree(saved_result["output"], relocated)
    saved_result["source"].unlink()
    saved_result["header"].unlink()
    def forbidden():
        pytest.fail("Portable validation queried the source installation runtime")
    monkeypatch.setattr(verification, "runtime_fingerprint", forbidden)
    native = NativeModel()
    result = verification.validate_output(relocated / "current.json", artifacts_only=True, adapter=native)
    assert result["valid"] and result["scope"] == "artifacts_and_native_model"
    assert set(result["checks"]) == {"artifacts", "native_model"}
    assert Path(native.calls[0][1]["path"]).is_relative_to(relocated)
    assert not result["behavior_replayed"]
