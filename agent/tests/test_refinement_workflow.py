"""Optional model settings and replay use the same isolated conversion workflow."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from verilog2digital import cli, verification
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.jobs import manager
from verilog2digital.jobs.publication import atomic_document, digest, read_document
from verilog2digital.llm import artifacts
from verilog2digital.project_file import load_project


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "proje_ışık_日本語"
    directory.mkdir()
    source = directory / "modül.v"
    source.write_text("module top(input a,b,output y); assign y=a^b; endmodule\n", encoding="utf-8")
    settings = {"schema_version": 1, "sources": [source.name], "top": "top", "output": "çıktı_Ω",
                "conversion": {"layout_strategy": "reserved", "refinement": {"mode": "off"}}}
    path = directory / "project.json"
    atomic_document(path, settings)
    return path, source, settings


def replay(path, responses):
    atomic_document(path, {"schema_version": 1, "responses": responses})
    return path


@pytest.fixture
def captured_jobs(monkeypatch):
    calls = []
    def run(project, output, **kwargs):
        calls.append({"project": project, "output": output, **deepcopy(kwargs)})
        return {"schema_version": 2, "state": "ready"}
    monkeypatch.setattr(manager, "run_job", run)
    return calls


@pytest.mark.parametrize("choice,mode,model", [("off", "off", "qwen3-4b-q4_k_m"),
    ("auto", "local", "qwen3-4b-q4_k_m"), ("4b", "local", "qwen3-4b-q4_k_m"),
    ("14b", "local", "qwen3-14b-q4_k_m")])
def test_cli_llm_choices_are_validated_and_forwarded_before_job(project, captured_jobs, capsys, choice, mode, model):
    path, source, _ = project
    result = cli.main(["convert", "--project", str(path), "--llm", choice, "--refinement-attempts", "2",
                       "--refinement-budget", "40", "--layout-request", "Keep related gates together", "--json", "--quiet"])
    assert result == 0 and json.loads(capsys.readouterr().out)["state"] == "ready"
    call = captured_jobs[0]
    assert call["project"].sources == [source]
    assert call["options"]["refinement"] == {"mode": mode, "model_id": model, "max_attempts": 2,
        "budget_seconds": 40.0, "request": "Keep related gates together", "responses": [],
        "context_tokens": None, "model_memory_mib": 6144, "idle_seconds": 30}


@pytest.mark.parametrize("arguments", [["--refinement-attempts", "0"], ["--refinement-attempts", "9"],
    ["--refinement-budget", "0"], ["--refinement-budget", "301"], ["--refinement-budget", "nan"],
    ["--refinement-budget", "inf"], ["--layout-request", "x" * 501]])
def test_invalid_cli_refinement_limits_do_not_start_a_job(project, captured_jobs, capsys, arguments):
    path, _, _ = project
    code = cli.main(["convert", "--project", str(path), "--llm", "4b", *arguments, "--json", "--quiet"])
    assert code == 1 and not captured_jobs
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "INVALID_REFINEMENT"


@pytest.mark.parametrize("value", [None, [], {"mode": "remote"}, {"mode": "local", "model_id": "arbitrary-path.gguf"},
    {"mode": "off", "responses": ["{}"]}, {"mode": "replay", "responses": [{}]},
    {"mode": "replay", "responses": ["{}"] * 9}, {"mode": "local", "command": "unused"}])
def test_project_refinement_configuration_rejects_unsupported_authority(project, value):
    path, _, settings = project
    settings["conversion"]["refinement"] = value
    atomic_document(path, settings)
    with pytest.raises((FrontendError, ValueError)):
        load_project(path)


def test_project_refinement_defaults_and_explicit_cli_override(project, captured_jobs, capsys):
    path, _, settings = project
    settings["conversion"]["refinement"] = {"mode": "local", "model_id": "qwen3-14b-q4_k_m", "max_attempts": 1}
    atomic_document(path, settings)
    assert load_project(path)["options"]["refinement"]["budget_seconds"] == 120
    assert cli.main(["convert", "--project", str(path), "--llm", "off", "--json", "--quiet"]) == 0
    capsys.readouterr()
    assert captured_jobs[0]["options"]["refinement"]["mode"] == "off"


@pytest.mark.parametrize("mode", ["off", "local"])
@pytest.mark.parametrize("model", ["qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"])
def test_explicit_model_limits_enable_local_and_preserve_project_model(project, captured_jobs, capsys, mode, model):
    path, _, settings = project
    settings["conversion"]["refinement"] = {"mode": mode, "model_id": model}
    atomic_document(path, settings)
    assert cli.main(["convert", "--project", str(path), "--model-context-tokens", "2048",
                     "--model-memory-mib", "11264", "--model-idle-seconds", "15",
                     "--memory-mib", "12288", "--json", "--quiet"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"
    refinement = captured_jobs[0]["options"]["refinement"]
    assert refinement["mode"] == "local" and refinement["model_id"] == model
    assert {key: refinement[key] for key in ("context_tokens", "model_memory_mib", "idle_seconds")} == {
        "context_tokens": 2048, "model_memory_mib": 11264, "idle_seconds": 15}


@pytest.mark.parametrize("mode", ["off", "replay"])
def test_saved_model_limits_do_not_activate_local_model(project, captured_jobs, capsys, mode):
    path, _, settings = project
    settings["conversion"]["refinement"] = {"mode": mode, "context_tokens": 2048,
        "model_memory_mib": 4096, "idle_seconds": 10}
    atomic_document(path, settings)
    assert cli.main(["convert", "--project", str(path), "--json", "--quiet"]) == 0
    capsys.readouterr()
    refinement = captured_jobs[0]["options"]["refinement"]
    assert refinement["mode"] == mode and refinement["context_tokens"] == 2048


@pytest.mark.parametrize("flag", ["--model-context-tokens", "--model-memory-mib", "--model-idle-seconds"])
@pytest.mark.parametrize("selection", ["off", "replay", "saved-replay"])
def test_explicit_model_limits_reject_off_or_replay_before_job(project, captured_jobs, capsys, tmp_path, flag, selection):
    path, _, settings = project
    arguments = [flag, "2048" if flag != "--model-idle-seconds" else "30"]
    if selection == "off":
        arguments += ["--llm", "off"]
    elif selection == "replay":
        # The conflict is diagnosed before attempting to open even a missing replay file.
        arguments += ["--replay-actions", str(tmp_path / "missing.json")]
    else:
        settings["conversion"]["refinement"] = {"mode": "replay", "responses": []}
        atomic_document(path, settings)
    assert cli.main(["convert", "--project", str(path), *arguments, "--json", "--quiet"]) == 1
    assert not captured_jobs
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "REFINEMENT_OPTIONS"


def test_explicit_local_selection_can_override_saved_replay_with_model_limits(project, captured_jobs, capsys):
    path, _, settings = project
    settings["conversion"]["refinement"] = {"mode": "replay", "responses": ["{}"]}
    atomic_document(path, settings)
    assert cli.main(["convert", "--project", str(path), "--llm", "4b", "--model-context-tokens", "2048",
                     "--json", "--quiet"]) == 0
    capsys.readouterr()
    refinement = captured_jobs[0]["options"]["refinement"]
    assert refinement["mode"] == "local" and refinement["responses"] == []
    assert refinement["context_tokens"] == 2048


@pytest.mark.parametrize("flag,value", [("--model-context-tokens", "1023"), ("--model-context-tokens", "8193"),
    ("--model-memory-mib", "63"), ("--model-memory-mib", "12289"),
    ("--model-idle-seconds", "0"), ("--model-idle-seconds", "301")])
def test_invalid_model_limits_fail_before_job(project, captured_jobs, capsys, flag, value):
    path, _, _ = project
    assert cli.main(["convert", "--project", str(path), flag, value, "--json", "--quiet"]) == 1
    assert not captured_jobs
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "INVALID_REFINEMENT"


def test_replay_cli_forwards_raw_responses_without_executing_them(project, captured_jobs, capsys, tmp_path):
    path, _, _ = project
    response = '{"actions":[{"action":"not_allowed"}]}'
    recorded = replay(tmp_path / "replay.json", [response])
    assert cli.main(["convert", "--project", str(path), "--replay-actions", str(recorded), "--json", "--quiet"]) == 0
    capsys.readouterr()
    assert captured_jobs[0]["options"]["refinement"]["mode"] == "replay"
    assert captured_jobs[0]["options"]["refinement"]["responses"] == [response]


@pytest.mark.parametrize("contents", ["null", "[]", '{"schema_version":1,"responses":[],"extra":0}',
    '{"schema_version":true,"responses":[]}', '{"schema_version":1,"responses":[],"responses":[]}',
    '{"schema_version":1,"responses":[{}]}'])
def test_malformed_replay_envelope_fails_before_job(project, captured_jobs, capsys, tmp_path, contents):
    path, _, _ = project
    recorded = tmp_path / "replay.json"
    recorded.write_text(contents, encoding="utf-8")
    assert cli.main(["convert", "--project", str(path), "--replay-actions", str(recorded), "--json", "--quiet"]) == 1
    assert not captured_jobs
    assert json.loads(capsys.readouterr().out)["state"] == "failed"


def test_missing_replay_file_and_conflicting_local_mode_fail_before_job(project, captured_jobs, capsys, tmp_path):
    path, _, _ = project
    missing = tmp_path / "missing.json"
    for flags in ([], ["--llm", "4b"]):
        assert cli.main(["convert", "--project", str(path), "--replay-actions", str(missing), *flags, "--json", "--quiet"]) == 1
        assert not captured_jobs
        result = json.loads(capsys.readouterr().out)
        assert result["diagnostic"]["code"] == ("REFINEMENT_OPTIONS" if flags else "COMMAND_FAILED")


@pytest.mark.parametrize("mode,cache_expected", [("off", True), ("local", False), ("replay", False)])
def test_manager_bypasses_candidate_cache_for_enabled_refinement(monkeypatch, tmp_path, mode, cache_expected):
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return {"state": "ready"}
    monkeypatch.setattr(manager, "_run", run)
    manager.run_job(Project([tmp_path / "unused.v"]), tmp_path / "out",
                    options={"refinement": {"mode": mode}}, cache=True)
    assert captured["cache"] is cache_expected
    assert captured["options"]["refinement"]["mode"] == mode


class NativeModel:
    def call(self, method, params):
        assert method == "circuit.inspect"
        return {"model": {"valid": True, "diagnostics": []}}


@pytest.fixture
def model_result(tmp_path, monkeypatch):
    directory = tmp_path / "result"
    directory.mkdir()
    source = tmp_path / "original.v"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    model, executable = tmp_path / "model.gguf", tmp_path / "llama-server.exe"
    model.write_bytes(b"recorded model bytes")
    executable.write_bytes(b"recorded runtime bytes")
    def inventory(model_id, backend):
        assert model_id == "qwen3-4b-q4_k_m" and backend == "cpu"
        return {"model": {"id": model_id, "sha256": digest(model)}, "runtime": {"server_sha256": digest(executable)}}
    actual = inventory("qwen3-4b-q4_k_m", "cpu")
    contents = {"circuit": ("circuit.dig", b"native placeholder"), "sidecar": ("circuit.dig.ids.json", b"{}"),
                "preview": ("preview.png", b"png"), "svg": ("preview.svg", b"<svg/>"),
                "validation": ("validation.json", b'{"valid":true,"behavior":{"valid":true}}')}
    declared = {}
    for label, (name, data) in contents.items():
        (directory / name).write_bytes(data)
        declared[label] = {"path": name, "sha256": digest(directory / name)}
    manifest = {"schema_version": 2, "state": "ready", "top": "top", "configuration": {"sources": [str(source)]},
                "sources": {str(source): digest(source)},
                "artifacts": declared, "job": {"runtime_fingerprint": {"sha256": "a" * 64}},
                "refinement": {"mode": "local", "accepted": 1, "runtime": {**actual, "profile": {"backend": "cpu"}}}}
    atomic_document(directory / "manifest.json", manifest)
    monkeypatch.setattr(verification, "runtime_fingerprint", lambda: {"sha256": "a" * 64})
    monkeypatch.setattr(artifacts, "verify_installation", inventory)
    return directory, model, executable


def test_full_validation_checks_recorded_model_inventory_without_inference(model_result):
    directory, _, _ = model_result
    result = verification.validate_output(directory, adapter=NativeModel())
    assert result["valid"] and result["checks"]["model_runtime"]["valid"]
    assert result["checks"]["model_runtime"]["scope"] == "pinned_model_and_runtime_files"
    assert result["behavior_replayed"] is False


@pytest.mark.parametrize("file", [1, 2])
def test_changed_model_or_runtime_fails_full_freshness(model_result, file):
    model_result[file].write_bytes(b"modified inventory")
    result = verification.validate_output(model_result[0], adapter=NativeModel())
    assert not result["valid"] and result["checks"]["artifacts"]["valid"]
    assert result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_CHANGED"


def test_artifacts_only_needs_no_installed_model_and_keeps_recorded_behavior(model_result, monkeypatch):
    directory, model, executable = model_result
    model.unlink()
    executable.unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("Artifact-only validation attempted local model inspection")
    monkeypatch.setattr(artifacts, "verify_installation", forbidden)
    result = verification.validate_output(directory, artifacts_only=True, adapter=NativeModel())
    assert result["valid"] and "model_runtime" not in result["checks"]
    assert result["recorded_behavior"]["valid"] and result["behavior_replayed"] is False


def test_accepted_local_refinement_requires_model_binding(model_result):
    directory, _, _ = model_result
    path = directory / "manifest.json"
    manifest = read_document(path)
    manifest["refinement"].pop("runtime")
    atomic_document(path, manifest)
    result = verification.validate_output(directory, adapter=NativeModel())
    assert not result["valid"] and result["checks"]["model_runtime"]["diagnostics"][0]["code"] == "TOOL_BINDING"


@pytest.mark.parametrize("responses,expected", [([], "EOFError"), (["{malformed"], "ACTION_JSON"),
    ([json.dumps({"schema_version": 1, "candidate_id": "0" * 64, "actions": [], "explanation": "stale"})], "ACTION_STALE")])
def test_real_replay_workflow_preserves_valid_unicode_result_without_cache(tmp_path, project, responses, expected):
    path, source, settings = project
    recorded = replay(tmp_path / "replay.json", responses)
    environment = {**os.environ, "PYTHONNOUSERSITE": "1", "V2D_JOB_ROOT": str(tmp_path / "jobs")}
    command = [sys.executable, "-X", "faulthandler", str(ROOT / "scripts/v2d.py"), "convert", "--project", str(path),
               "--replay-actions", str(recorded), "--refinement-attempts", "1", "--refinement-budget", "10", "--json", "--quiet"]
    process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True,
                             encoding="utf-8", errors="strict", timeout=120,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["state"] == "ready" and result["cache"] == {"enabled": False, "hit": False}
    manifest = read_document(Path(result["manifest"]))
    report = read_document(Path(result["validation"]))
    assert report["valid"] and report["behavior"]["valid"] and report["behavior"]["input_vectors"] == 4
    assert manifest["configuration"]["sources"] == [str(source)]
    assert Path(result["directory"]).is_relative_to(path.parent / settings["output"])
    refinement = manifest["refinement"]
    assert refinement["mode"] == "replay" and refinement["accepted"] == 0
    canvas = refinement["canvases"][0]
    assert canvas["baseline_id"] == canvas["selected_id"]
    assert canvas["attempts"][0]["diagnostic"]["code"] == expected
    assert Path(result["baseline_preview"]).is_file()
    status = read_document(Path(result["job_manifest"]))
    assert manifest["job"]["cache"]["enabled"] is False
    assert status["resources"]["metrics"]["active_processes"] == 0
