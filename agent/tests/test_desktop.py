"""Desktop result operations keep real bundle checks around mocked native validation."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from verilog2digital import cli, desktop
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs import manager
from verilog2digital.jobs.publication import atomic_document, bundle_bindings, digest, read_document
from test_publication import candidate


@pytest.fixture
def result(tmp_path, monkeypatch):
    directory, _ = candidate(tmp_path, "sonuç_Ω")
    calls = []
    def validate(path, **kwargs):
        assert kwargs["artifacts_only"] is True
        bundle_bindings(Path(path), read_document(Path(path) / "manifest.json"))
        calls.append((path, kwargs))
        return {"validation": {"valid": True, "checks": {
            "artifacts": {"valid": True}, "native_model": {"valid": True}}},
                "job_manifest": str(tmp_path / "owned-validation-job.json")}
    monkeypatch.setattr(manager, "run_validation", validate)
    return directory, calls


def test_checked_result_returns_bound_metadata_without_replaying_behavior(result):
    directory, calls = result
    checked = desktop.checked_result(directory)
    assert checked["state"] == "ready" and len(calls) == 1
    assert checked["manifest_sha256"] == digest(directory / "manifest.json")
    assert checked["metadata_path"] is None  # Legacy image-only result.
    assert checked["summary"]["native_model_valid"] and checked["summary"]["artifact_binding_valid"]
    assert checked["summary"]["behavior_replayed"] is False


def test_checked_result_surfaces_bounded_recorded_model_failures(result):
    directory, _ = result
    manifest = read_document(directory / "manifest.json")
    missing = {"code": "MODEL_MISSING", "message": "Install the pinned model before enabling local refinement"}
    manifest["refinement"] = {"mode": "local", "accepted": 0, "diagnostics": [missing],
        "canvases": [{"attempts": [{"diagnostic": missing, "response": "private raw response"},
                                 {"diagnostic": {"code": "C" * 100, "message": "M" * 3000}}]}]}
    atomic_document(directory / "manifest.json", manifest)
    summary = desktop.checked_result(directory)["summary"]["refinement"]
    assert summary["accepted"] == 0 and summary["attempted"] == 2
    assert summary["diagnostics"][0] == missing and len(summary["diagnostics"]) == 2
    assert len(summary["diagnostics"][1]["code"]) == 80 and len(summary["diagnostics"][1]["message"]) == 2000
    assert "private raw response" not in str(summary)


def test_checked_result_keeps_model_failure_when_large_behavior_is_condensed(result):
    directory, _ = result
    manifest = read_document(directory / "manifest.json")
    report = {"valid": True, "behavior": {"valid": True, "large_record": "x" * 600000}}
    atomic_document(directory / "validation.json", report)
    manifest["artifacts"]["validation"]["sha256"] = digest(directory / "validation.json")
    manifest["refinement"] = {"diagnostics": [{"code": "MODEL_MISSING", "message": "Install the model"}] * 32}
    atomic_document(directory / "manifest.json", manifest)
    summary = desktop.checked_result(directory)["summary"]
    assert summary["recorded_behavior"]["details_in_validation_artifact"] is True
    assert summary["refinement"]["diagnostics"] == [{"code": "MODEL_MISSING", "message": "Install the model"}]


@pytest.mark.parametrize("refinement", [None, [], {"diagnostics": None}, {"canvases": {}}, {"canvases": [{"attempts": 42}]}])
def test_malformed_refinement_summary_reports_checked_result_error(result, refinement):
    directory, _ = result
    manifest = read_document(directory / "manifest.json")
    manifest["refinement"] = refinement
    atomic_document(directory / "manifest.json", manifest)
    with pytest.raises(FrontendError, match="malformed"):
        desktop.checked_result(directory)


@pytest.mark.parametrize("validation", [None, {}, {"valid": True}, {"valid": True, "checks": {}},
    {"valid": True, "checks": {"artifacts": {"valid": True}, "native_model": {"valid": False}}},
    {"valid": True, "checks": {"artifacts": {"valid": False}, "native_model": {"valid": True}}}])
def test_incomplete_or_failed_native_validation_cannot_open_result(result, monkeypatch, validation):
    monkeypatch.setattr(manager, "run_validation", lambda *a, **k: {"validation": validation})
    with pytest.raises(FrontendError) as caught:
        desktop.checked_result(result[0])
    assert caught.value.code == "INVALID_RESULT"


def test_tampered_bundle_fails_before_native_validation(result):
    directory, calls = result
    (directory / "circuit.dig").write_bytes(b"changed circuit")
    with pytest.raises(FrontendError) as caught:
        desktop.checked_result(directory)
    assert caught.value.code == "ARTIFACT_CHANGED" and calls == []


@pytest.mark.parametrize("target", ["manifest.json", "circuit.dig"])
def test_mutation_during_native_validation_is_detected(result, monkeypatch, target):
    directory, _ = result
    original = manager.run_validation
    def mutate(*args, **kwargs):
        checked = original(*args, **kwargs)
        path = directory / target
        path.write_bytes(path.read_bytes() + b" ")
        return checked
    monkeypatch.setattr(manager, "run_validation", mutate)
    with pytest.raises(FrontendError) as caught:
        desktop.checked_result(directory)
    assert caught.value.code == "ARTIFACT_CHANGED"


def test_export_preserves_complete_hierarchy_package_and_source(result, tmp_path):
    source, _ = result
    manifest = read_document(source / "manifest.json")
    children = {"dependency_0001": ("child.dig", b"native child"),
                "dependency_0002": ("child.dig.ids.json", b"child identities"),
                "package": ("package.json", b'{"root_file":"circuit.dig","modules":[{"type":"child.dig"}]}')}
    for label, (name, data) in children.items():
        (source / name).write_bytes(data)
        manifest["artifacts"][label] = {"path": name, "sha256": digest(source / name)}
    atomic_document(source / "manifest.json", manifest)
    before = {file.name: file.read_bytes() for file in source.iterdir()}
    destination = tmp_path / "dışa aktarım_日本語"
    exported = desktop.export_result(source, destination)
    assert exported["directory"] == str(destination) and exported["files"] == len(before)
    assert {file.name: file.read_bytes() for file in source.iterdir()} == before
    assert {file.name: file.read_bytes() for file in destination.iterdir()} == before
    assert bundle_bindings(destination, read_document(destination / "manifest.json"))


@pytest.mark.parametrize("inside", [False, True])
def test_export_rejects_existing_or_source_descendant_destination_without_modification(result, tmp_path, inside):
    source, _ = result
    destination = source / "inside" if inside else tmp_path / "existing"
    if not inside:
        destination.mkdir()
        (destination / "user-file.txt").write_bytes(b"preserve")
    before = {file.name: file.read_bytes() for file in source.iterdir()}
    with pytest.raises(FrontendError) as caught:
        desktop.export_result(source, destination)
    assert caught.value.code == ("EXPORT_PATH" if inside else "EXPORT_EXISTS")
    assert {file.name: file.read_bytes() for file in source.iterdir()} == before
    if not inside:
        assert (destination / "user-file.txt").read_bytes() == b"preserve"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".v2d-export-*"))


@pytest.mark.parametrize("target", ["circuit.dig", "manifest.json"])
def test_growing_artifact_or_manifest_race_aborts_export_and_cleans_only_owned_temporary(result, tmp_path, monkeypatch, target):
    source, _ = result
    original = desktop.contained_file
    changed = False
    def mutate(directory, name, **kwargs):
        nonlocal changed
        path = original(directory, name, **kwargs)
        if not changed and Path(directory).name.startswith(".v2d-export-"):
            changed = True
            item = source / target
            item.write_bytes(item.read_bytes() + b" " * 100)
        return path
    monkeypatch.setattr(desktop, "contained_file", mutate)
    destination = tmp_path / "export"
    with pytest.raises(FrontendError) as caught:
        desktop.export_result(source, destination)
    assert changed and caught.value.code == "ARTIFACT_CHANGED"
    assert not destination.exists() and not list(tmp_path.glob(".v2d-export-*"))
    assert source.is_dir() and (source / "circuit.dig").is_file()


@pytest.fixture
def launch_tools(tmp_path, monkeypatch):
    root = tmp_path / "tools space_ışık"
    java = root / "jdk/bin/javaw.exe"
    jar = root / ".runs/digital-build/Digital.jar"
    library = root / "src/main/dig/lib"
    for path in (java, jar):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder; never executed")
    library.mkdir(parents=True)
    monkeypatch.setattr(desktop, "repository_root", lambda: root)
    monkeypatch.setattr(desktop, "development_toolchain", lambda _: SimpleNamespace(executable=lambda _: str(java.with_name("java.exe"))))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Launch plan started a process"))
    return root, java, jar, library


def test_digital_returns_relative_circuit_argument_and_unicode_working_directory(result, launch_tools):
    source, _ = result
    _, java, jar, library = launch_tools
    plan = desktop.digital_launch_plan(source)
    assert plan["argv"] == [str(java), "-Ddigital.library.path=" + str(library), "-jar", str(jar), "circuit.dig"]
    assert plan["cwd"] == str(source)
    assert plan["manifest_sha256"] == digest(source / "manifest.json")


def test_non_ascii_circuit_filename_is_rejected_before_java_can_ignore_it(result, launch_tools):
    source, _ = result
    manifest = read_document(source / "manifest.json")
    renamed = "circuit_\u03c0.dig"
    (source / "circuit.dig").rename(source / renamed)
    manifest["artifacts"]["circuit"]["path"] = renamed
    atomic_document(source / "manifest.json", manifest)
    with pytest.raises(FrontendError) as caught:
        desktop.digital_launch_plan(source)
    assert caught.value.code == "DIGITAL_ARGUMENT_PATH"


@pytest.mark.parametrize("target", ["circuit.dig", "manifest.json"])
def test_launch_plan_rechecks_mutation_during_tool_lookup(result, launch_tools, monkeypatch, target):
    source, _ = result
    original = desktop.development_toolchain
    def mutate(root):
        path = source / target
        path.write_bytes(path.read_bytes() + b" ")
        return original(root)
    monkeypatch.setattr(desktop, "development_toolchain", mutate)
    with pytest.raises(FrontendError) as caught:
        desktop.digital_launch_plan(source)
    assert caught.value.code == "ARTIFACT_CHANGED"


def test_missing_digital_runtime_reports_actionable_error(result, launch_tools):
    launch_tools[2].unlink()
    with pytest.raises(FrontendError) as caught:
        desktop.digital_launch_plan(result[0])
    assert caught.value.code == "DIGITAL_MISSING"


@pytest.mark.parametrize("action", ["result", "export"])
def test_desktop_cli_returns_checked_result_envelope_without_inference(result, tmp_path, capsys, action):
    import json
    arguments = ["desktop", action, str(result[0]), "--json", "--quiet"]
    if action == "export":
        arguments += ["-o", str(tmp_path / "cli-export")]
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"
