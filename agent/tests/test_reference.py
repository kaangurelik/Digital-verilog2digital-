"""Known truth tables exercise an oracle that compiles the original HDL."""
from __future__ import annotations

import hashlib
from itertools import product
import json
from pathlib import Path

import pytest

from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.validation.reference import ReferenceError, simulate_reference
from verilog2digital.validation.stimuli import stimulus_plan
import verilog2digital.validation.reference as reference


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/scalar"

CASES = {
    "buffer": (["a"], lambda v: {"y": v["a"]}),
    "inverter": (["a"], lambda v: {"y": 1 - v["a"]}),
    "and_gate": (["a", "b"], lambda v: {"y": v["a"] & v["b"]}),
    "or_gate": (["a", "b"], lambda v: {"y": v["a"] | v["b"]}),
    "xor_gate": (["a", "b"], lambda v: {"y": v["a"] ^ v["b"]}),
    "nand_gate": (["a", "b"], lambda v: {"y": 1 - (v["a"] & v["b"])}),
    "nor_gate": (["a", "b"], lambda v: {"y": 1 - (v["a"] | v["b"])}),
    "xnor_gate": (["a", "b"], lambda v: {"y": 1 - (v["a"] ^ v["b"])}),
    "fanout": (["a", "b"], lambda v: {"y": v["a"] & v["b"], "z": v["a"] ^ v["b"], "direct": v["a"]}),
    "half_adder": (["a", "b"], lambda v: {"sum": (v["a"] + v["b"]) % 2, "carry": (v["a"] + v["b"]) // 2}),
    "full_adder": (["a", "b", "carry_in"], lambda v: {
        "sum": sum(v.values()) % 2, "carry_out": sum(v.values()) // 2}),
    "scalar_mux": (["a", "b", "select_b"], lambda v: {"y": v["b"] if v["select_b"] else v["a"]}),
    "constant_outputs": ([], lambda v: {"zero": 0, "one": 1}),
    "unused_input": (["a", "unused"], lambda v: {"y": 1 - v["a"]}),
    "repeated_operand": (["a"], lambda v: {"zero": 0, "direct": v["a"]}),
    "nested_mux": (["a", "b", "c", "outer_select", "inner_select"], lambda v: {
        "y": (v["a"] if v["inner_select"] else v["b"]) if v["outer_select"] else v["c"]}),
    "logical_cases": (["a", "b", "c"], lambda v: {
        "both": int(bool(v["a"]) and bool(v["b"])),
        "either": int(bool(v["a"]) or bool(v["c"])), "inverted": int(not v["b"])}),
}


def vectors(names):
    return [dict(zip(names, values)) for values in product((0, 1), repeat=len(names))]


def fixture_project(name, tmp_path):
    project = Project(sources=[FIXTURES / f"{name}.v"], top=name)
    return project, lower(project, tmp_path / "lower")


def source_project(tmp_path, source, top="top"):
    path = tmp_path / "input.v"
    path.write_text(source, encoding="utf-8")
    project = Project(sources=[path], top=top)
    return project, lower(project, tmp_path / "lower")


@pytest.mark.parametrize("name", CASES)
def test_original_verilog_matches_known_exhaustive_truth_tables(tmp_path, name):
    names, expected = CASES[name]
    stimuli = vectors(names)
    project, design = fixture_project(name, tmp_path)
    result = simulate_reference(project, design, tmp_path / "reference", stimuli)
    assert result["samples"] == [{"values": expected(values)} for values in stimuli]
    assert "Icarus Verilog" in result["tools"]["iverilog"]
    assert "Icarus Verilog runtime" in result["tools"]["vvp"]
    assert result["assumptions"]["profile"] == "combinational_scalar"
    assert all(Path(path).is_file() for path in result["artifacts"].values())
    manifest = json.loads(Path(result["artifacts"]["provenance"]).read_text(encoding="utf-8"))
    source = project.sources[0]
    assert manifest["sources"] == [{"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
    assert str(source) in manifest["commands"]["compile"]
    assert "-g2005" in manifest["commands"]["compile"]
    assert manifest["stimuli"] == stimuli


def test_source_order_macros_profile_parameters_and_paths_with_spaces(tmp_path):
    source_directory = tmp_path / "original source files"
    includes = source_directory / "included files"
    includes.mkdir(parents=True)
    (includes / "config.vh").write_text("`define FROM_HEADER 1'b1\n", encoding="utf-8")
    definitions = source_directory / "definitions.v"
    definitions.write_text("`define EARLIER 1'b1\n", encoding="utf-8")
    source = source_directory / "configured.v"
    source.write_text('''`include "config.vh"
`ifdef SYNTHESIS
`ifdef YOSYS
module configured #(parameter MASK=8'h00)(input a,b,output y);
  assign y=a^b^MASK[0]^`FROM_HEADER^`EARLIER^`USER_BIT;
endmodule
`else
module configured; initial $display("wrong YOSYS profile"); endmodule
`endif
`else
module configured; initial $display("wrong SYNTHESIS profile"); endmodule
`endif
''', encoding="utf-8")
    project = Project(sources=[definitions, source], top="configured", include_dirs=[includes],
                      defines={"USER_BIT": "1'b0"}, parameters={"MASK": "8'hff"})
    design = lower(project, tmp_path / "lower")
    stimuli = vectors(["a", "b"])
    result = simulate_reference(project, design, tmp_path / "reference work", stimuli)
    assert result["samples"] == [{"values": {"y": 1 ^ v["a"] ^ v["b"]}} for v in stimuli]
    manifest = json.loads(Path(result["artifacts"]["provenance"]).read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["sources"]] == [str(definitions), str(source)]
    assert manifest["defines"] == {"SYNTHESIS": "1", "YOSYS": "1", "USER_BIT": "1'b0"}
    assert manifest["parameters"] == {"MASK": "8'hff"}


def test_empty_text_macro_is_still_defined_in_every_frontend(tmp_path):
    source = tmp_path / "empty_define.v"
    source.write_text('''module empty_define(input a,output y,defined);
`ifdef EMPTY
  assign y = `EMPTY a;
`ifdef SYNTHESIS
`ifdef YOSYS
  assign defined = 1'b1;
`else
  assign defined = 1'b0;
`endif
`else
  assign defined = 1'b0;
`endif
`else
  assign y = ~a;
  assign defined = 1'b0;
`endif
endmodule
''', encoding="utf-8")
    project = Project(sources=[source], top="empty_define", defines={"EMPTY": ""})
    design = lower(project, tmp_path / "lower")
    by_name = {port["name"]: port for port in design["ports"]}
    assert by_name["y"]["bits"] == by_name["a"]["bits"]
    assert by_name["defined"]["bits"] == ["1"]
    result = simulate_reference(project, design, tmp_path / "reference", [{"a": 0}, {"a": 1}])
    assert result["samples"] == [{"values": {"y": 0, "defined": 1}}, {"values": {"y": 1, "defined": 1}}]


def test_escaped_port_identifiers_cannot_become_testbench_syntax(tmp_path):
    project, design = source_project(tmp_path, r'''
module top(input \a.b , input \select[0] , output \end );
  assign \end = \a.b ^ \select[0] ;
endmodule
''')
    stimuli = vectors(["a.b", "select[0]"])
    result = simulate_reference(project, design, tmp_path / "reference", stimuli)
    assert result["samples"] == [{"values": {"end": v["a.b"] ^ v["select[0]"]}} for v in stimuli]


def test_helper_module_name_does_not_collide_with_original_modules(tmp_path):
    source = "module top(input a,output y); assign y=a; endmodule\n"
    project, design = source_project(tmp_path, source)
    first = simulate_reference(project, design, tmp_path / "first", [{"a": 1}])
    manifest = json.loads(Path(first["artifacts"]["provenance"]).read_text(encoding="utf-8"))
    collision = manifest["testbench_module"]
    project.sources[0].write_text(source + f"module {collision}; endmodule\n", encoding="utf-8")
    design = lower(project, tmp_path / "lower_again")
    second = simulate_reference(project, design, tmp_path / "second", [{"a": 1}])
    manifest = json.loads(Path(second["artifacts"]["provenance"]).read_text(encoding="utf-8"))
    assert manifest["testbench_module"] != collision
    assert second["samples"] == [{"values": {"y": 1}}]


def test_reference_outputs_come_from_original_sources_not_normalized_cells(tmp_path):
    project, design = source_project(tmp_path, "module top(input a,output y); assign y=a; endmodule")
    # Keep the valid direct-wire IR, but change the original source. The reference
    # must independently simulate the file it claims to have compiled.
    project.sources[0].write_text("module top(input a,output y); assign y=~a; endmodule", encoding="utf-8")
    result = simulate_reference(project, design, tmp_path / "reference", [{"a": 0}, {"a": 1}])
    assert result["samples"] == [{"values": {"y": 1}}, {"values": {"y": 0}}]


@pytest.mark.parametrize("stimuli", [[], [{}], [{"a": 0}], [{"a": 0, "b": 1, "extra": 0}],
                                     [{"a": True, "b": 0}], [{"a": "0", "b": 0}],
                                     [{"a": -1, "b": 0}], [{"a": 2, "b": 0}],
                                     [{"a": 0, "b": 0}] * 4097])
def test_vectors_must_be_complete_binary_integers_and_bounded(tmp_path, stimuli):
    project, design = fixture_project("and_gate", tmp_path)
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", stimuli)
    assert failure.value.code == "REFERENCE_STIMULI"
    assert not (tmp_path / "reference").exists()


def test_maximum_vector_count_preserves_order_and_duplicates(tmp_path):
    project, design = fixture_project("inverter", tmp_path)
    stimuli = [{"a": (index // 2) % 2} for index in range(4096)]
    result = simulate_reference(project, design, tmp_path / "reference", stimuli)
    assert result["samples"] == [{"values": {"y": 1 - v["a"]}} for v in stimuli]


@pytest.mark.parametrize("source,stimuli", [
    ("module top(input a); endmodule", [{"a": 0}]),
    ("module top(input clk,a,output reg y); always @(posedge clk) y<=a; endmodule", [{"clk": 0, "a": 0}]),
])
def test_combinational_profile_requires_outputs_and_rejects_storage(tmp_path, source, stimuli):
    project, design = source_project(tmp_path, source)
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", stimuli)
    assert failure.value.code == "REFERENCE_PROFILE"


def test_different_project_configuration_cannot_be_claimed_as_same_reference(tmp_path):
    project, design = fixture_project("buffer", tmp_path)
    project.defines["CHANGED"] = "1"
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{"a": 0}])
    assert failure.value.code == "REFERENCE_CONFIGURATION"


@pytest.mark.parametrize("value", ["x", "z"])
def test_unknown_native_outputs_are_rejected_not_coerced(tmp_path, value):
    project, design = source_project(tmp_path, "module top(output y); assign y=1'b0; endmodule")
    project.sources[0].write_text(f"module top(output y); assign y=1'b{value}; endmodule", encoding="utf-8")
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{}])
    assert failure.value.code == "REFERENCE_UNKNOWN"
    assert Path(failure.value.path).is_file()


def test_native_compile_error_is_actionable(tmp_path):
    project, design = source_project(tmp_path, "module top(input a,output y); assign y=a; endmodule")
    project.sources[0].write_text("module top(input a,output y); assign y=; endmodule", encoding="utf-8")
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{"a": 0}])
    assert failure.value.code == "REFERENCE_COMPILE"
    assert "syntax error" in failure.value.message
    assert Path(failure.value.path).is_file()


def test_premature_simulation_finish_is_not_a_pass(tmp_path):
    project, design = source_project(tmp_path, "module top(input a,output y); assign y=a; endmodule")
    project.sources[0].write_text("module top(input a,output y); assign y=a; initial $finish; endmodule", encoding="utf-8")
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{"a": 0}])
    assert failure.value.code == "REFERENCE_INCOMPLETE"


def test_nonterminating_delta_loop_is_killed_with_a_timeout(tmp_path, monkeypatch):
    project, design = source_project(tmp_path, "module top(input a,output y); assign y=a; endmodule")
    project.sources[0].write_text("module top(input a,output y); assign y=a; initial forever #0; endmodule", encoding="utf-8")
    monkeypatch.setattr(reference, "SIMULATION_TIMEOUT", 1)
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{"a": 0}])
    assert failure.value.code == "REFERENCE_TIMEOUT"
    assert Path(failure.value.path).is_file()


DATAPATH_FIXTURES = FIXTURES.parent / "datapath"
MASK64 = (1 << 64) - 1


def signed_pattern(value, width):
    return value - (1 << width) if value & (1 << (width - 1)) else value


def comparisons(a, b):
    return {"less": int(a < b), "less_equal": int(a <= b), "greater": int(a > b),
            "greater_equal": int(a >= b), "equal": int(a == b), "unequal": int(a != b)}


def alu4_result(values):
    a, b, operation = values["a"], values["b"], values["operation"]
    value = (a + b, a - b, a ^ b, a & b)[operation] & 15
    return {"y": value, "is_zero": int(value == 0)}


DATAPATH_CASES = {
    "bus_permutations": ({"a": 8}, lambda v: {
        "reversed": int(f'{v["a"]:08b}'[::-1], 2),
        "swapped": ((v["a"] & 15) << 4) | (v["a"] >> 4),
        "repeated": (v["a"] & 3) * 0x55, "mixed": 0x80 | ((v["a"] & 3) << 4) | (v["a"] >> 4)}),
    "ascending_bus": ({"a": 8}, lambda v: {"y": v["a"],
        "swapped": ((v["a"] & 15) << 4) | (v["a"] >> 4),
        "lsb": v["a"] & 1, "msb": v["a"] >> 7}),
    "add_sub4": ({"a": 4, "b": 4}, lambda v: {"sum": v["a"] + v["b"], "difference": (v["a"] - v["b"]) & 15}),
    "add_sub8": ({"a": 8, "b": 8}, lambda v: {"sum": (v["a"] + v["b"]) & 255, "difference": (v["a"] - v["b"]) & 255}),
    "signed_math8": ({"a": 8, "b": 8}, lambda v: {
        "sum": (signed_pattern(v["a"], 8) + signed_pattern(v["b"], 8)) & 511,
        "difference": (signed_pattern(v["a"], 8) - signed_pattern(v["b"], 8)) & 511}),
    "comparisons4": ({"a": 4, "b": 4}, lambda v: {**comparisons(v["a"], v["b"]),
        "signed_less": int(signed_pattern(v["a"], 4) < signed_pattern(v["b"], 4)),
        "signed_greater_equal": int(signed_pattern(v["a"], 4) >= signed_pattern(v["b"], 4))}),
    "signed_compare8": ({"a": 8, "b": 8}, lambda v: comparisons(signed_pattern(v["a"], 8), signed_pattern(v["b"], 8))),
    "unsigned_compare8": ({"a": 8, "b": 8}, lambda v: comparisons(v["a"], v["b"])),
    "mixed_compare8": ({"a": 8, "b": 8}, lambda v: {"less": int(v["a"] < v["b"]), "equal": int(v["a"] == v["b"])}),
    "mux8": ({"a": 8, "b": 8, "select_b": 1}, lambda v: {"y": v["b"] if v["select_b"] else v["a"]}),
    "reductions8": ({"a": 8}, lambda v: {"all_set": int(v["a"] == 255), "any_set": int(v["a"] != 0),
        "parity": v["a"].bit_count() % 2, "nand_bits": int(v["a"] != 255), "nor_bits": int(v["a"] == 0),
        "xnor_bits": 1 - (v["a"].bit_count() % 2), "is_zero": int(v["a"] == 0), "inverted": v["a"] ^ 255}),
    "shifts8": ({"a": 8, "amount": 4}, lambda v: {
        "left": (v["a"] << v["amount"]) & 255, "right": v["a"] >> v["amount"],
        "arithmetic": (signed_pattern(v["a"], 8) >> v["amount"]) & 255,
        "signed_left": (v["a"] << v["amount"]) & 255}),
    "alu4": ({"a": 4, "b": 4, "operation": 2}, alu4_result),
    "arithmetic64": ({"a": 64, "b": 64}, lambda v: {
        "sum": (v["a"] + v["b"]) & MASK64, "difference": (v["a"] - v["b"]) & MASK64,
        "signed_less": int(signed_pattern(v["a"], 64) < signed_pattern(v["b"], 64)),
        "unsigned_less": int(v["a"] < v["b"])}),
    "shifts64": ({"a": 64, "amount": 7}, lambda v: {
        "left": (v["a"] << v["amount"]) & MASK64, "right": v["a"] >> v["amount"],
        "arithmetic": (signed_pattern(v["a"], 64) >> v["amount"]) & MASK64}),
    "constants64": ({}, lambda v: {"high_and_low": (1 << 63) | 1, "ones": MASK64, "zero": 0}),
    "bus_repeats64": ({"a": 8}, lambda v: {"repeated": v["a"] * 0x0101010101010101}),
}


@pytest.mark.parametrize("name", DATAPATH_CASES)
def test_datapath_reference_matches_independent_integer_bit_arithmetic(tmp_path, name):
    widths, expected = DATAPATH_CASES[name]
    project = Project(sources=[DATAPATH_FIXTURES / f"{name}.v"], top=name)
    design = lower(project, tmp_path / "lower")
    assert {port["name"]: len(port["bits"]) for port in design["ports"] if port["direction"] == "input"} == widths
    interface = {"inputs": [{"port": port, "instance_id": f"input_{index}", "width": width}
                             for index, (port, width) in enumerate(widths.items())]}
    plan = stimulus_plan(interface, seed=12345, random_count=64)
    result = simulate_reference(project, design, tmp_path / "reference", plan["stimuli"])
    assert result["samples"] == [{"values": expected(vector)} for vector in plan["stimuli"]]
    assert result["assumptions"]["profile"] == "combinational_datapath"
    assert result["assumptions"]["input_widths"] == widths
    assert all(type(value) is int for sample in result["samples"] for value in sample["values"].values())
    if sum(widths.values()) <= 12:
        assert plan["assumptions"]["exhaustive"] is True
        assert len(plan["stimuli"]) == 1 << sum(widths.values())
    else:
        assert plan["assumptions"]["exhaustive"] is False


def test_reference_preserves_a_corruption_in_bit_63(tmp_path):
    project, design = source_project(tmp_path, "module top(input [63:0]a,output [63:0]y); assign y=a; endmodule")
    project.sources[0].write_text("module top(input [63:0]a,output [63:0]y); assign y=a^64'h8000000000000000; endmodule", encoding="utf-8")
    inputs = [{"a": 0}, {"a": 1 << 63}, {"a": MASK64}]
    result = simulate_reference(project, design, tmp_path / "reference", inputs)
    assert result["samples"] == [{"values": {"y": value["a"] ^ (1 << 63)}} for value in inputs]


@pytest.mark.parametrize("value", [-1, 1 << 64, True])
def test_64_bit_reference_inputs_must_fit_without_implicit_truncation(tmp_path, value):
    project, design = source_project(tmp_path, "module top(input [63:0]a,output [63:0]y); assign y=a; endmodule")
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{"a": value}])
    assert failure.value.code == "REFERENCE_STIMULI"


@pytest.mark.parametrize("unknown", ["x", "z"])
def test_partial_unknown_wide_outputs_are_rejected(tmp_path, unknown):
    project, design = source_project(tmp_path, "module top(output [63:0]y); assign y=64'b0; endmodule")
    project.sources[0].write_text(f"module top(output [63:0]y); assign y=64'h80000000000000{unknown}1; endmodule", encoding="utf-8")
    with pytest.raises(ReferenceError) as failure:
        simulate_reference(project, design, tmp_path / "reference", [{}])
    assert failure.value.code == "REFERENCE_UNKNOWN"
