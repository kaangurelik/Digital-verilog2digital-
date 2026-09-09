"""Feature checks must see constructs that HDL optimization could erase."""
import os
from pathlib import Path

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.inspection import inspect_sources


def inspect(tmp_path: Path, source: str, **options) -> dict:
    path = tmp_path / "input.v"
    path.write_text(source, encoding="utf-8")
    return inspect_sources([path], **options)


@pytest.mark.parametrize("body,code", [
    ("initial begin end", "UNSUPPORTED_INITIALIZATION"),
    ("reg unused=0;", "UNSUPPORTED_INITIALIZATION"),
    ("wire unused=1'bx;", "UNSUPPORTED_FOUR_STATE"),
    ("wire unused=1'bz;", "UNSUPPORTED_FOUR_STATE"),
    ("assign #1 y=a;", "UNSUPPORTED_TIMING"),
    ("always @(a) #1 y=a;", "UNSUPPORTED_TIMING"),
    ("always @(a) y<=#1 a;", "UNSUPPORTED_TIMING"),
    ("always @(a) @(a) y=a;", "UNSUPPORTED_TIMING"),
    ("always @(a) $display(a);", "UNSUPPORTED_SYSTEM_CALL"),
    ("reg [7:0] mem[0:3];", "UNSUPPORTED_MEMORY"),
    ("real r;", "UNSUPPORTED_REAL"),
    ("always @(negedge a) y<=a;", "UNSUPPORTED_CLOCK"),
    ("always @(posedge a or posedge y) y<=a;", "UNSUPPORTED_CLOCK"),
    ("(* blackbox *) wire hidden;", "UNSUPPORTED_BLACKBOX"),
])
def test_unsupported_source_is_rejected_before_elaboration(tmp_path, body, code):
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, f"module top(input a, output reg y);\n{body}\nendmodule\n")
    assert failure.value.code == code
    assert failure.value.path == str(tmp_path / "input.v")
    assert failure.value.line == 2
    assert failure.value.column >= 1


@pytest.mark.parametrize("source", [
    "module top(input a,output y); wire n=a; assign y=n; endmodule",
    "module top(input a,b,output reg y); always @* begin if(a) y=b; else y=0; end endmodule",
    "module top(input a,b,output reg y); always @(a or b) y=a&b; endmodule",
    "module top(input a,output reg y); always @(posedge a) y<=1; endmodule",
    "module top(input a,b,output y); and g(y,a,b); endmodule",
    "module top(input [3:0]a,output [3:0]y); function [3:0]f; input [3:0]x; f=x+1; endfunction assign y=f(a); endmodule",
    "module top #(parameter N=4)(input[N-1:0]a,output[N-1:0]y); genvar i; generate for(i=0;i<N;i=i+1) begin:g assign y[i]=a[i]; end endgenerate endmodule",
])
def test_supported_verilog_constructs_remain_available(tmp_path, source):
    result = inspect(tmp_path, source)
    assert result["top"] == "top"
    assert result["modules"] == ["top"]
    assert result["origins"][0]["path"] == str(tmp_path / "input.v")


def test_shared_preprocessing_includes_defines_and_parameter_overrides(tmp_path):
    includes = tmp_path / "include files"
    includes.mkdir()
    (includes / "sizes.vh").write_text("`define WIDTH 4\n", encoding="utf-8")
    first = tmp_path / "first.v"
    first.write_text('`include "sizes.vh"\n`define OTHER 1\n', encoding="utf-8")
    second = tmp_path / "second.v"
    second.write_text("module top #(parameter N=`WIDTH)(output [N-1:0]y); assign y=`VALUE+`OTHER; endmodule", encoding="utf-8")
    result = inspect_sources([first, second], include_dirs=[includes], defines={"VALUE": "2"}, parameters={"N": 8})
    assert result["top"] == "top"
    assert result["origins"][0]["path"] == str(second)
    assert result["source_files"] == sorted([str(first), str(second), str(includes / "sizes.vh")])


def test_unknown_override_is_rejected_instead_of_silently_ignored(tmp_path):
    with pytest.raises(FrontendError, match="no overridable parameter") as failure:
        inspect(tmp_path, "module top #(parameter N=4)(output [N-1:0]y); assign y=0; endmodule", parameters={"NO_SUCH_PARAMETER": 8})
    assert failure.value.code == "UNKNOWN_PARAMETER"


def test_empty_macro_is_defined_and_expands_to_empty_text(tmp_path):
    source = ("module top(input a,output y);\n`ifdef EMPTY\nassign y=`EMPTY a;\n"
              "`else\nassign y=unknown_signal;\n`endif\nendmodule")
    assert inspect(tmp_path, source, defines={"EMPTY": ""})["top"] == "top"


def test_macro_diagnostic_uses_invocation_coordinates(tmp_path):
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, "`define BAD #1\nmodule top(input a,output y);\nassign `BAD y=a;\nendmodule\n")
    assert failure.value.code == "UNSUPPORTED_TIMING"
    assert failure.value.line == 3


def test_multiple_tops_and_explicit_selection(tmp_path):
    source = "module first(output y); assign y=0; endmodule\nmodule second(output y); assign y=1; endmodule"
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, source)
    assert failure.value.code == "AMBIGUOUS_TOP"
    assert inspect(tmp_path, source, top="second")["top"] == "second"


def test_unknown_top_and_parser_location(tmp_path):
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, "module top; endmodule", top="missing")
    assert failure.value.code == "UNKNOWN_TOP"
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, "module top(input a, output y);\nassign y = ;\nendmodule")
    assert failure.value.code == "SLANG_ERROR"
    assert failure.value.line == 2
    assert failure.value.path == str(tmp_path / "input.v")


@pytest.mark.parametrize("sensitivity,body,code", [
    ("@(a)", "y=b;", "INCOMPLETE_SENSITIVITY"),
    ("@(a[0])", "y=a[1];", "INCOMPLETE_SENSITIVITY"),
    ("@(a[0] or a[1])", "y=&a;", "INCOMPLETE_SENSITIVITY"),
    ("@(a&b)", "y=a|b;", "UNSUPPORTED_SENSITIVITY"),
    ("@(a[b])", "y=a[b];", "UNSUPPORTED_SENSITIVITY"),
])
def test_incomplete_or_expression_sensitivity_is_not_silently_synthesized(tmp_path, sensitivity, body, code):
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, f"module top(input [3:0]a,input b,output reg y); always {sensitivity} {body} endmodule")
    assert failure.value.code == code


def test_constant_parameter_is_not_a_required_event(tmp_path):
    assert inspect(tmp_path, "module top #(parameter P=1)(input a,output reg y); always @(a) y=a&P; endmodule")["top"] == "top"


def test_range_sensitivity_uses_native_storage_offsets(tmp_path):
    assert inspect(tmp_path, "module top(input [7:4]a,output reg y); always @(a[7:6]) y=a[7]; endmodule")["top"] == "top"


@pytest.mark.parametrize("sensitivity", ["@(a)", "@*"])
def test_function_global_reads_cannot_escape_the_sensitivity_check(tmp_path, sensitivity):
    source = f"module top(input a,b,output reg y); function f(input x); f=x&b; endfunction always {sensitivity} y=f(a); endmodule"
    with pytest.raises(FrontendError) as failure:
        inspect(tmp_path, source)
    assert failure.value.code == "INCOMPLETE_SENSITIVITY"


def test_complete_function_sensitivity_and_pure_function_arguments(tmp_path):
    for source in [
        "module top(input a,b,output reg y); function f(input x); f=x&b; endfunction always @(a or b) y=f(a); endmodule",
        "module top(input a,b,output reg y); function f(input x,z); f=x&z; endfunction always @* y=f(a,b); endmodule",
    ]:
        assert inspect(tmp_path, source)["top"] == "top"


@pytest.mark.parametrize("first_complete", [True, False])
def test_multiple_procedures_preserve_first_sensitivity_snapshot(tmp_path, first_complete):
    blocks = [f"always {'@*' if first_complete or bit else '@(a)'} y[{bit}]=a&b;" for bit in range(32)]
    source = "module top(input a,b,output reg [31:0]y);\n" + "\n".join(blocks) + "\nendmodule"
    if first_complete:
        assert inspect(tmp_path, source)["top"] == "top"
    else:
        with pytest.raises(FrontendError) as failure:
            inspect(tmp_path, source)
        assert failure.value.code == "INCOMPLETE_SENSITIVITY"
        assert failure.value.line == 2


@pytest.mark.skipif(os.name != "nt", reason="The pinned native path limitation is Windows-specific")
@pytest.mark.parametrize("unicode_includes", [False, True])
def test_windows_unicode_paths_fail_before_native_file_access(tmp_path, unicode_includes):
    unicode_path = tmp_path / "hdl_\u0131\u015f\u0131k_\u03c0"
    unicode_path.mkdir()
    source = (tmp_path if unicode_includes else unicode_path) / "top.v"
    source.write_text("module top; endmodule", encoding="utf-8")
    with pytest.raises(FrontendError) as failure:
        inspect_sources([source], include_dirs=[unicode_path] if unicode_includes else [])
    assert failure.value.code == "UNSUPPORTED_PATH"
    assert failure.value.path == str(unicode_path if unicode_includes else source)
    assert "ASCII" in str(failure.value)
