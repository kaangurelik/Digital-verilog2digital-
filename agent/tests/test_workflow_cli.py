"""Project settings and the isolated public workflow use the same contracts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from verilog2digital.contracts import ContractError
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs.publication import digest, read_document
from verilog2digital.project_file import load_project


ROOT = Path(__file__).resolve().parents[2]


def write_project(tmp_path, *, two_tops=False):
    directory = tmp_path / "proje_ışık_π"
    includes = directory / "başlıklar_日本語"
    includes.mkdir(parents=True)
    header = includes / "işlem.vh"
    header.write_text("`define RESULT (a ^ b)\n", encoding="utf-8")
    source = directory / "modül.v"
    text = '`include "işlem.vh"\nmodule top(input a,b,output y); assign y=`RESULT; endmodule\n'
    if two_tops:
        text += "module second(input c,output z); assign z=~c; endmodule\n"
    source.write_text(text, encoding="utf-8")
    value = {"schema_version": 1, "sources": [source.name], "include_dirs": [includes.name],
             "defines": {"EMPTY": ""}, "parameters": {}, "output": "çıktı_Ω",
             "conversion": {"layout_strategy": "reserved", "seed": 7, "random_count": 0},
             "resources": {"deadline_seconds": 120, "memory_mib": 2048, "java_heap_mib": 256}}
    if not two_tops:
        value["top"] = "top"
    path = directory / "devre.v2d.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path, value, source, header


def run_cli(tmp_path, *arguments, timeout=180, env_overrides=None):
    environment = {**os.environ, "PYTHONNOUSERSITE": "1",
                   "V2D_JOB_ROOT": str(tmp_path / "jobs"), **(env_overrides or {})}
    return subprocess.run([sys.executable, "-X", "faulthandler", str(ROOT / "scripts/v2d.py"), *map(str, arguments)],
                          cwd=ROOT, env=environment, capture_output=True, text=True,
                          encoding="utf-8", errors="strict", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def json_result(process, expected=0):
    assert process.returncode == expected, process.stdout + process.stderr
    assert process.stdout.strip(), process.stderr
    return json.loads(process.stdout)


def test_project_relative_unicode_paths_and_configuration_resolve_from_file(tmp_path, monkeypatch):
    path, value, source, header = write_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = load_project(path)
    assert loaded["path"] == path.resolve()
    assert loaded["project"].sources == [source.resolve()]
    assert loaded["project"].include_dirs == [header.parent.resolve()]
    assert loaded["project"].defines == {"EMPTY": ""}
    assert loaded["options"] == value["conversion"] and loaded["resources"] == value["resources"]
    assert loaded["output"] == path.parent / value["output"]


@pytest.mark.parametrize("patch", [
    {"schema_version": 2}, {"sources": []}, {"sources": ["modül.v", "modül.v"]},
    {"unexpected": "instruction"}, {"top": "top; command"}, {"defines": {"MODE": 1}},
    {"parameters": {"WIDTH": True}}, {"output": "bad\npath"},
    {"conversion": {"spacing": 41}}, {"conversion": {"profile": "invented"}},
    {"conversion": {"random_count": 2049}}, {"conversion": {"shell": "unused"}},
    {"resources": {"deadline_seconds": 0}}, {"resources": {"memory_mib": 0}},
])
def test_project_configuration_rejects_invalid_schema_before_native_work(tmp_path, patch):
    path, value, _, _ = write_project(tmp_path)
    value.update(patch)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ContractError):
        load_project(path)


def test_project_duplicate_json_fields_are_rejected(tmp_path):
    path = tmp_path / "project.json"
    path.write_text('{"schema_version":1,"sources":[],"sources":["top.v"]}', encoding="utf-8")
    with pytest.raises(FrontendError) as error:
        load_project(path)
    assert error.value.code == "INVALID_DOCUMENT"


@pytest.mark.parametrize("missing", ["source", "include"])
def test_project_missing_dependencies_have_actionable_paths(tmp_path, missing):
    path, value, source, header = write_project(tmp_path)
    if missing == "source":
        source.unlink()
    else:
        header.unlink()
        header.parent.rmdir()
    with pytest.raises(FrontendError) as error:
        load_project(path)
    assert error.value.code == {"source": "MISSING_SOURCE", "include": "MISSING_INCLUDE_DIRECTORY"}[missing]
    assert error.value.path == str(source if missing == "source" else header.parent)


def test_cli_doctor_reports_real_tools_and_venv_without_native_import_in_supervisor(tmp_path):
    result = json_result(run_cli(tmp_path, "doctor", "--json", "--quiet"))
    assert result["state"] == "ready"
    assert {"python", "java", "yosys", "iverilog", "vvp", "adapter"} <= set(result["tools"])
    assert all(tool["available"] for tool in result["tools"].values())
    assert Path(result["python_isolation"]["venv"]).resolve() == ROOT / ".venv"
    assert result["python_isolation"]["global_packages"] is False
    assert len(result["fingerprint"]["sha256"]) == 64


def test_cli_doctor_missing_tool_is_incomplete_with_structured_details(tmp_path):
    result = json_result(run_cli(tmp_path, "doctor", "--json", "--quiet",
                                 env_overrides={"V2D_YOSYS": str(tmp_path / "missing-yosys.exe")}), expected=1)
    assert result["state"] == "incomplete" and not result["tools"]["yosys"]["available"]
    assert result["tools"]["yosys"]["error"] and result["fingerprint"] is None


def test_cli_inspect_returns_all_top_candidates_with_original_unicode_origins(tmp_path):
    project, _, source, header = write_project(tmp_path, two_tops=True)
    result = json_result(run_cli(tmp_path, "inspect", "--project", project, "--json", "--quiet"))
    assert result["state"] == "ready"
    inspection = result["inspection"]
    assert inspection["top"] is None and inspection["top_candidates"] == ["second", "top"]
    assert {origin["path"] for origin in inspection["origins"]} == {str(source)}
    assert set(inspection["source_files"]) == {str(source), str(header)}
    status = read_document(Path(result["job_manifest"]))
    assert status["state"] == "ready" and status["kind"] == "inspect"


def test_cli_project_conversion_events_replace_and_validation_preserve_revisions(tmp_path):
    project, settings, source, header = write_project(tmp_path)
    process = run_cli(tmp_path, "convert", "--project", project, "--events")
    assert process.returncode == 0, process.stdout + process.stderr
    events = [json.loads(line) for line in process.stdout.splitlines()]
    assert events[-1]["type"] == "result"
    progress = [event["event"] for event in events if event["type"] == "progress"]
    assert progress and all(event["schema_version"] == 1 for event in events)
    assert [event["sequence"] for event in progress] == sorted(event["sequence"] for event in progress)
    first = events[-1]["result"]
    assert first["state"] == "ready" and Path(first["circuit"]).is_file()
    first_hashes = {key: digest(Path(first[key])) for key in ("circuit", "sidecar", "preview", "manifest", "validation")}
    manifest = read_document(Path(first["manifest"]))
    assert manifest["configuration"]["sources"] == [str(source)]
    assert manifest["configuration"]["include_dirs"] == [str(header.parent)]
    assert manifest["sources"] == {str(path): digest(path) for path in (source, header)}
    report = read_document(Path(first["validation"]))
    assert report["valid"] and report["behavior"]["input_vectors"] == 4
    output = project.parent / settings["output"]
    rejected = json_result(run_cli(tmp_path, "convert", "--project", project, "--json", "--quiet"), expected=1)
    assert rejected["diagnostic"]["code"] == "OUTPUT_EXISTS"
    second = json_result(run_cli(tmp_path, "convert", "--project", project, "--replace", "--json", "--quiet"))
    assert first["revision"] != second["revision"]
    assert {key: digest(Path(first[key])) for key in first_hashes} == first_hashes
    assert read_document(output / "current.json")["job_id"] == second["job_id"]
    selected = (output / "current.json").read_bytes()
    tool_failure = json_result(run_cli(tmp_path, "convert", "--project", project, "--replace", "--json", "--quiet",
                                       env_overrides={"V2D_YOSYS": str(tmp_path / "missing-yosys.exe")}), expected=1)
    assert tool_failure["state"] == "failed" and tool_failure["diagnostic"]["code"]
    assert (output / "current.json").read_bytes() == selected
    assert {key: digest(Path(first[key])) for key in first_hashes} == first_hashes
    valid = json_result(run_cli(tmp_path, "validate", output, "--json", "--quiet"))
    assert valid["validation"]["valid"] and not valid["validation"]["behavior_replayed"]
    header.write_text("// changed original include\n", encoding="utf-8")
    stale = json_result(run_cli(tmp_path, "validate", output, "--json", "--quiet"), expected=1)
    assert stale["state"] == "failed" and not stale["validation"]["checks"]["sources"]["valid"]
    portable = json_result(run_cli(tmp_path, "validate", output, "--artifacts-only", "--json", "--quiet"))
    assert portable["validation"]["valid"]


def test_cli_benchmark_compares_two_valid_small_layouts(tmp_path):
    source = tmp_path / "buffer.v"
    source.write_text("module buffer(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    output = tmp_path / "benchmark"
    result = json_result(run_cli(tmp_path, "benchmark", source, "-o", output, "--repeats", 1,
                                 "--deadline", 120, "--java-heap-mib", 256, "--json", "--quiet"))
    assert result["state"] == "ready" and len(result["runs"]) == 2
    assert [run["strategy"] for run in result["runs"]] == ["auto", "reserved"]
    assert read_document(output / "benchmark.json") == result
    for run in result["runs"]:
        assert run["duration_seconds"] >= 0 and run["metrics"]["wire_length"] >= 0
        assert read_document(Path(run["result"]["validation"]))["valid"]
