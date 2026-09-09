"""Source fingerprints cover includes and prevent a changed input being published."""
import hashlib
import json
from pathlib import Path

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.frontend import yosys


def source_project(directory: Path):
    header = directory / "value.vh"
    source = directory / "top.v"
    header.write_text("`define MASK 1'b1\n", encoding="utf-8")
    source.write_text('`include "value.vh"\nmodule top(input a,output y); assign y=a^`MASK; endmodule\n', encoding="utf-8")
    return Project([source], top="top", include_dirs=[directory]), source, header


def test_macro_only_include_bytes_are_part_of_frontend_fingerprint(tmp_path):
    project, source, header = source_project(tmp_path)
    yosys.lower(project, tmp_path / "work")
    inspection = json.loads((tmp_path / "work/source-inspection.json").read_text(encoding="utf-8"))
    assert inspection["input_hashes"] == {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, header)
    }


def test_header_change_during_lowering_rejects_the_stale_design(tmp_path, monkeypatch):
    project, _, header = source_project(tmp_path)
    original_run = yosys.subprocess.run
    def change_after_backend(*args, **kwargs):
        result = original_run(*args, **kwargs)
        header.write_text("`define MASK 1'b0\n", encoding="utf-8")
        return result
    monkeypatch.setattr(yosys.subprocess, "run", change_after_backend)
    with pytest.raises(FrontendError) as failure:
        yosys.lower(project, tmp_path / "work")
    assert failure.value.code == "SOURCE_CHANGED"
    assert not (tmp_path / "work/design.json").exists()
