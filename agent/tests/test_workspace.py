"""ASCII snapshots preserve original HDL preprocessing and Unicode provenance."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.jobs.workspace import inspect_project, restore_error, restore_provenance, snapshot_project, verify_snapshot
from verilog2digital.validation.reference import simulate_reference


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_unicode_source_directory_filename_and_literal_include_are_snapshotted(tmp_path):
    directory = tmp_path / "ışık_π"
    header = write(directory / "dönüş.vh", "`define EXPR (a^b)\n")
    source = write(directory / "modül.v", '`include "dönüş.vh"\nmodule top(input a,b,output y); assign y=`EXPR; endmodule\n')
    project = Project([source], top="top")
    snapshot = snapshot_project(project, tmp_path / "job")
    assert all(str(path).isascii() for path in snapshot["project"].sources)
    assert snapshot["sources"] == {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, header)}
    design = lower(snapshot["project"], tmp_path / "lower")
    assert [cell["type"] for cell in design["cells"]] == ["$xor"]
    restored = restore_provenance(design, snapshot["source_map"])
    assert restored["configuration"]["sources"] == [str(source.resolve())]
    assert restored["configuration"]["include_dirs"] == []
    assert any(origin["path"] == str(source.resolve()) for origin in restored["origins"])
    assert "modül.v" in restored["cells"][0]["source"]
    values = [{"a": a, "b": b} for a, b in ((0, 0), (0, 1), (1, 0), (1, 1))]
    reference = simulate_reference(snapshot["project"], design, tmp_path / "reference", values)
    assert [sample["values"] for sample in reference["samples"]] == [{"y": value["a"] ^ value["b"]} for value in values]


def test_same_header_selects_different_macro_filenames_per_occurrence(tmp_path):
    directory = tmp_path / "özgün"
    write(directory / "bir.vh", "assign y[0]=a;\n")
    write(directory / "iki.vh", "assign y[1]=b;\n")
    dispatch = write(directory / "seç.vh", "`include `HEADER\n")
    source = write(directory / "modül.v", '''module top(input a,b,output [1:0] y);
`define HEADER "bir.vh"
`include "seç.vh"
`undef HEADER
`define HEADER "iki.vh"
`include "seç.vh"
endmodule
''')
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert len([entry for entry in snapshot["binding"]["staged"] if entry["original_path"] == str(dispatch.resolve())]) == 2
    design = lower(snapshot["project"], tmp_path / "lower")
    ports = {port["name"]: port["bits"] for port in design["ports"]}
    assert ports["y"] == [ports["a"][0], ports["b"][0]]


def test_include_resolution_uses_source_relative_then_ordered_directories(tmp_path):
    first, second = tmp_path / "önce", tmp_path / "sonra"
    local = tmp_path / "yerel"
    chosen = write(local / "aynı.vh", "`define EXPR a\n")
    write(first / "aynı.vh", "`define EXPR b\n")
    write(second / "aynı.vh", "`define EXPR 1'b0\n")
    write(first / "başka.vh", "`define SECOND b\n")
    write(second / "başka.vh", "`define SECOND 1'b0\n")
    source = write(local / "top.v", '`include "aynı.vh"\n`include "başka.vh"\nmodule top(input a,b,output [1:0] y); assign y={`SECOND,`EXPR}; endmodule\n')
    snapshot = snapshot_project(Project([source], top="top", include_dirs=[first, second]), tmp_path / "job")
    assert str(chosen.resolve()) in snapshot["sources"]
    assert str((first / "aynı.vh").resolve()) not in snapshot["sources"]
    design = lower(snapshot["project"], tmp_path / "lower")
    ports = {port["name"]: port["bits"] for port in design["ports"]}
    assert ports["y"] == [ports["a"][0], ports["b"][0]]


def test_includes_are_staged_as_yosys_and_icarus_select_them(tmp_path):
    # slang's own macros are undefined for the snapshot, as for every tool, so it stages the includes they read.
    shared = write(tmp_path / "shared.vh", "`define EXPR a\n")
    write(tmp_path / "parser_only.vh", "`define EXPR 1'b0\n")
    source = write(tmp_path / "top.v", '`ifdef __slang__\n`include "parser_only.vh"\n`else\n`include "shared.vh"\n`endif\n'
                                        "module top(input a, output y); assign y = `EXPR; endmodule\n")
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert set(snapshot["sources"]) == {str(source.resolve()), str(shared.resolve())}
    design = lower(snapshot["project"], tmp_path / "lower")
    ports = {port["name"]: port["bits"] for port in design["ports"]}
    assert ports["y"] == ports["a"]


def test_source_order_empty_defines_inactive_missing_includes_and_line_counts(tmp_path):
    first = write(tmp_path / "順序" / "最初.v", '`define READY\n')
    second = write(tmp_path / "順序" / "次.v", '''`ifdef MISSING
`include "存在しない.vh"
`endif
`ifdef READY
module top(output [7:0] y);
assign y=6;
endmodule
`endif
''')
    snapshot = snapshot_project(Project([first, second], top="top", defines={"EMPTY": ""}), tmp_path / "job")
    assert len(snapshot["sources"]) == 2
    assert snapshot["project"].sources[1].read_bytes().count(b"\n") == second.read_bytes().count(b"\n")
    design = lower(snapshot["project"], tmp_path / "lower")
    assert next(port for port in design["ports"] if port["name"] == "y")["bits"] == ["0", "1", "1", "0", "0", "0", "0", "0"]


def test_discovery_returns_structured_candidates_and_conversion_stays_strict(tmp_path):
    source = write(tmp_path / "kaynak.v", "module first(input a,output y); assign y=a; endmodule\nmodule second(input b,output z); assign z=~b; endmodule\n")
    snapshot = snapshot_project(Project([source]), tmp_path / "job")
    result = inspect_project(snapshot["project"])
    assert result["top"] is None
    assert result["top_candidates"] == ["first", "second"]
    with pytest.raises(FrontendError) as error:
        lower(snapshot["project"], tmp_path / "lower")
    assert error.value.code == "AMBIGUOUS_TOP"


def test_discovery_uses_the_same_parameter_literal_profile_as_conversion(tmp_path):
    source = write(tmp_path / "top.v", "module top #(parameter WIDTH=1)(input a,output y); assign y=a; endmodule\n")
    snapshot = snapshot_project(Project([source], parameters={"WIDTH": "1+1"}), tmp_path / "job")
    with pytest.raises(FrontendError) as error:
        inspect_project(snapshot["project"])
    assert error.value.code == "INVALID_PARAMETER"


def test_syntax_error_in_unicode_header_uses_original_path_and_line(tmp_path):
    header = write(tmp_path / "ölçü" / "bozuk.vh", "\nassign y=;\n")
    source = write(header.parent / "top.v", 'module top(output y);\n`include "bozuk.vh"\nendmodule\n')
    with pytest.raises(FrontendError) as error:
        snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert error.value.code == "SLANG_ERROR"
    assert error.value.path == str(header.resolve())
    assert error.value.line == 2


def test_missing_unicode_include_has_actionable_original_location(tmp_path):
    source = write(tmp_path / "özgün.v", '`include "eksik.vh"\nmodule top; endmodule\n')
    with pytest.raises(FrontendError) as error:
        snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert error.value.code == "MISSING_INCLUDE"
    assert error.value.path == str(source.resolve())
    assert error.value.line == 1


def test_angle_include_does_not_silently_change_system_search_semantics(tmp_path):
    source = write(tmp_path / "özgün.v", '`include <header.vh>\nmodule top(output y); assign y=1\'b0; endmodule\n')
    write(tmp_path / "header.vh", "// local candidate\n")
    with pytest.raises(FrontendError) as error:
        snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert error.value.code == "UNSUPPORTED_INCLUDE_FORM"
    assert error.value.path == str(source.resolve()) and error.value.line == 1


def test_provenance_restore_does_not_modify_hdl_labels_or_artifact_paths(tmp_path):
    staged = str(tmp_path / "f0000.v")
    original = str(tmp_path / "özgün.v")
    mapping = {"files": {staged: {"path": original, "columns": [{"line": 2, "start": 10, "end": 20, "new_end": 40}]}}, "configuration": {}}
    data = {"path": str(tmp_path / "candidate.dig"), "attributes": {"Label": staged, "path": staged}, "native_name": staged,
            "parameters": {"name": staged, "path": staged, "source": staged}, "hdl_path": [staged], "hierarchy": staged,
            "source": staged.replace("\\", "/")+":2.42-2.45", "diagnostics": [{"path": staged, "line": 2, "column": 42}]}
    before = deepcopy(data)
    restored = restore_provenance(data, mapping)
    assert data == before
    assert restored["path"] == data["path"]
    assert restored["attributes"] == data["attributes"]
    assert restored["native_name"] == staged and restored["hierarchy"] == staged
    assert restored["parameters"] == data["parameters"] and restored["hdl_path"] == data["hdl_path"]
    assert restored["source"] == original+":2.22-2.25"
    assert restored["diagnostics"][0] == {"path": original, "line": 2, "column": 22}


def test_function_filename_macro_and_absolute_unicode_filename(tmp_path):
    header = write(tmp_path / "日本語" / "入力.vh", "assign y=a;\n")
    source = write(tmp_path / "top.v", 'module top(input a,output y);\n`define PICK(first,second) first\n'
                   '`include `PICK("'+header.as_posix()+'", "missing.vh")\nendmodule\n')
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    design = lower(snapshot["project"], tmp_path / "lower")
    assert design["ports"][0]["bits"] == design["ports"][1]["bits"]
    assert b'`define PICK(first,second) first' in snapshot["project"].sources[0].read_bytes()


def test_guarded_file_macro_self_include_retains_original_path_meaning(tmp_path):
    source = write(tmp_path / "özgün" / "自分.v", '''`ifndef GUARD
`define GUARD
`include `__FILE__
module top(input a,output y); assign y=a; endmodule
`endif
''')
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert len(snapshot["binding"]["staged"]) == 2
    assert len(snapshot["sources"]) == 1
    assert lower(snapshot["project"], tmp_path / "lower")["top"] == "top"


def test_nested_file_macro_and_literal_staged_lookalike_remain_distinct(tmp_path):
    directory = tmp_path / "özgün"
    literal = write(directory / "f0000.v", "`define EXPR a\n")
    source = write(directory / "top.v", '''`ifndef GUARD
`define GUARD
`define CURRENT_FILE `__FILE__
`include `CURRENT_FILE
`define HEADER "f0000.v"
`include `HEADER
module top(input a,output y); assign y=`EXPR; endmodule
`endif
''')
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert len(snapshot["binding"]["staged"]) == 3
    assert str(literal.resolve()) in snapshot["sources"]
    assert lower(snapshot["project"], tmp_path / "lower")["top"] == "top"


def test_line_remapped_file_include_is_not_mistaken_for_physical_self_include(tmp_path):
    write(tmp_path / "logical.vh", "`define EXPR a\n")
    source = write(tmp_path / "özgün.v", '`line 40 "logical.vh" 0\n`include `__FILE__\n'
                   'module top(input a,output y); assign y=`EXPR; endmodule\n')
    with pytest.raises(FrontendError) as error:
        snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert error.value.code == "UNSUPPORTED_SOURCE_LOCATION"
    assert error.value.path == str(source.resolve()) and error.value.line == 2


def test_line_macro_and_comments_are_preserved_byte_for_byte_when_not_includes(tmp_path):
    source = write(tmp_path / "行.v", '// コメント\nmodule top(output [7:0] y);\nassign y=`__LINE__;\nendmodule\n')
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert snapshot["project"].sources[0].read_bytes() == source.read_bytes()
    assert inspect_project(snapshot["project"])["top"] == "top"


@pytest.mark.parametrize("definition,expression", [
    ("", "`__FILE__"),
    ("`define FILE_VALUE `__FILE__\n", "`FILE_VALUE"),
    ('`define STRINGIFY(value) `"value`"\n', "`STRINGIFY(`__FILE__)"),
])
def test_filename_dependent_hdl_values_are_rejected_before_staged_lowering(tmp_path, definition, expression):
    source = write(tmp_path / "özgün_日本語.v", definition + "module top(output [7:0] y);\n"
                   "localparam [255:0] TAG=" + expression + "; assign y=TAG[7:0]; endmodule\n")
    with pytest.raises(FrontendError) as error:
        snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert error.value.code == "UNSUPPORTED_SOURCE_LOCATION"
    assert error.value.path == str(source.resolve()) and error.value.column > 0
    assert error.value.line == (3 if definition else 2)


def test_unexpanded_file_macro_definition_and_filename_text_in_comment_are_unchanged(tmp_path):
    source = write(tmp_path / "özgün.v", "`define UNUSED_FILE `__FILE__\n// `__FILE__ is text\n"
                   "module top(input a,output y); assign y=a; endmodule\n")
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    assert snapshot["project"].sources[0].read_bytes() == source.read_bytes()
    assert inspect_project(snapshot["project"])["top"] == "top"


def test_snapshot_integrity_detects_modified_stage_and_original_edits_do_not_change_snapshot(tmp_path):
    source = write(tmp_path / "入力.v", "module top(input a,output y); assign y=a; endmodule\n")
    snapshot = snapshot_project(Project([source], top="top"), tmp_path / "job")
    verify_snapshot(snapshot)
    staged = snapshot["project"].sources[0]
    original_bytes = staged.read_bytes()
    source.write_text("changed original", encoding="utf-8")
    assert staged.read_bytes() == original_bytes
    verify_snapshot(snapshot)
    staged.write_bytes(original_bytes.replace(b"y=a", b"y=~a"))
    with pytest.raises(FrontendError) as error:
        verify_snapshot(snapshot)
    assert error.value.code == "SOURCE_SNAPSHOT_CHANGED"
