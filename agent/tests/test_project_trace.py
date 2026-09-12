"""Project trace files are bounded inputs resolved relative to their project."""
from copy import deepcopy
import json

import pytest

from verilog2digital import cli
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs import manager
from verilog2digital.jobs.publication import atomic_document
from verilog2digital.project_file import load_project
from test_workflow_cli import write_project


TRACE = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
         "initialization": {"kind": "input_sequence", "cycles": [{"values": {"a": 0}}]},
         "cycles": [{"values": {"a": 1}}]}


def trace_project(tmp_path, content=None):
    path, value, _, _ = write_project(tmp_path)
    trace = path.parent / "izler_Ω" / "başlangıç.json"
    trace.parent.mkdir()
    trace.write_text(json.dumps(TRACE) if content is None else content, encoding="utf-8")
    value["trace"] = trace.relative_to(path.parent).as_posix()
    atomic_document(path, value)
    return path, trace


def test_project_trace_uses_project_directory_and_unicode_filename(tmp_path, monkeypatch):
    path, _ = trace_project(tmp_path)
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert load_project(path)["options"]["sequential_trace"] == TRACE


@pytest.mark.parametrize("content,code", [("[]", "INVALID_TRACE"), ("null", "INVALID_TRACE"),
    ('"text"', "INVALID_TRACE"), ("{broken", "INVALID_DOCUMENT"),
    ('{"cycles":[],"cycles":[]}', "INVALID_DOCUMENT"), ('{"value":NaN}', "INVALID_DOCUMENT")])
def test_project_trace_rejects_nonobjects_and_ambiguous_json(tmp_path, content, code):
    path, _ = trace_project(tmp_path, content)
    with pytest.raises(FrontendError) as caught:
        load_project(path)
    assert caught.value.code == code


def test_project_trace_has_one_mib_read_bound(tmp_path):
    path, trace = trace_project(tmp_path)
    trace.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(FrontendError) as caught:
        load_project(path)
    assert caught.value.code == "DOCUMENT_LIMIT"


def test_explicit_cli_trace_overrides_loaded_project_trace(tmp_path, monkeypatch, capsys):
    path, _ = trace_project(tmp_path)
    replacement = deepcopy(TRACE)
    replacement["cycles"][0]["values"]["a"] = 0
    override = tmp_path / "override.json"
    atomic_document(override, replacement)
    calls = []
    monkeypatch.setattr(manager, "run_job", lambda *args, **kwargs: calls.append(kwargs) or {"state": "ready"})
    assert cli.main(["convert", "--project", str(path), "--trace", str(override), "--json", "--quiet"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"
    assert calls[0]["options"]["sequential_trace"] == replacement
    assert load_project(path)["options"]["sequential_trace"] == TRACE
