"""Verify elaboration semantics and reject unsupported HDL before information is lost."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/frontend"


def compile_fixture(filename: str, directory: Path, *, top=None, **options) -> dict:
    if top is None:
        top = "alias_passthrough" if filename == "alias.v" else Path(filename).stem
    return lower(Project(sources=[FIXTURES / filename], top=top, **options), directory)


def ports(design: dict) -> dict[str, dict]:
    return {port["name"]: port for port in design["ports"]}


def parameter_int(value) -> int:
    if isinstance(value, int):
        return value
    if value.startswith(("0x", "0b")):
        return int(value, 0)
    return int(value, 2) if set(value) <= {"0", "1"} else int(value)


def evaluate_combinational(design: dict, inputs: dict[str, int]) -> dict[str, int]:
    """Evaluate fixture operations using net bits and Yosys cell semantics.

    Results are independent of generated cell names, order and operation count.
    """
    signals: dict[int, int] = {}

    def read(bits):
        return sum((signals[bit] if isinstance(bit, int) else int(bit)) << index
                   for index, bit in enumerate(bits))

    def drive(bits, value):
        for index, bit in enumerate(bits):
            actual = (value >> index) & 1
            if isinstance(bit, int):
                if bit in signals:
                    assert signals[bit] == actual, "Conflicting drivers in supposedly valid normalized IR"
                signals[bit] = actual
            else:
                assert bit in ("0", "1") and int(bit) == actual

    named_ports = ports(design)
    assert set(inputs) == {name for name, port in named_ports.items() if port["direction"] == "input"}
    for name, value in inputs.items():
        drive(named_ports[name]["bits"], value)

    pending = list(design["cells"])
    while pending:
        ready = [cell for cell in pending if all(
            isinstance(bit, str) or bit in signals
            for pin, bits in cell["ports"].items() if cell["port_directions"][pin] == "input"
            for bit in bits
        )]
        assert ready, "Combinational IR contains a cycle or an undriven cell input"
        for cell in ready:
            connections = cell["ports"]
            values = {pin: read(bits) for pin, bits in connections.items()
                      if cell["port_directions"][pin] == "input"}
            output = connections["Y"]
            mask = (1 << len(output)) - 1

            def operand(name):
                value = values[name]
                signed = parameter_int(cell["parameters"].get(name + "_SIGNED", 0))
                width = len(connections[name])
                return value - (1 << width) if signed and value & (1 << (width - 1)) else value

            kind = cell["type"]
            if kind == "$and":
                result = operand("A") & operand("B")
            elif kind == "$or":
                result = operand("A") | operand("B")
            elif kind == "$xor":
                result = operand("A") ^ operand("B")
            elif kind == "$not":
                result = ~operand("A")
            elif kind == "$pos":
                result = operand("A")
            elif kind == "$add":
                result = operand("A") + operand("B")
            elif kind == "$sub":
                result = operand("A") - operand("B")
            elif kind == "$eq":
                result = int(operand("A") == operand("B"))
            elif kind == "$ne":
                result = int(operand("A") != operand("B"))
            elif kind == "$lt":
                assert cell["parameters"]["A_SIGNED"] == cell["parameters"]["B_SIGNED"]
                result = int(operand("A") < operand("B"))
            elif kind == "$logic_not":
                result = int(not values["A"])
            elif kind == "$logic_and":
                result = int(bool(values["A"]) and bool(values["B"]))
            elif kind == "$logic_or":
                result = int(bool(values["A"]) or bool(values["B"]))
            elif kind in {"$reduce_or", "$reduce_bool"}:
                result = int(bool(values["A"]))
            elif kind == "$mux":
                result = values["B"] if values["S"] else values["A"]
            elif kind == "$pmux":
                selected = [index for index in range(len(connections["S"])) if values["S"] & (1 << index)]
                assert len(selected) <= 1, "Fixture drove an ambiguous priority-free mux selection"
                result = (values["B"] >> (selected[0] * len(output))) & mask if selected else values["A"]
            else:
                raise AssertionError(f"Independent fixture evaluator does not cover {kind}")
            drive(output, result & mask)
            pending.remove(cell)

    return {name: read(port["bits"]) for name, port in named_ports.items() if port["direction"] == "output"}


def assert_design_contract(design: dict):
    assert design["schema_version"] == 1
    assert isinstance(design["top"], str) and design["top"]
    assert isinstance(design["origins"], list) and design["origins"]
    for origin in design["origins"]:
        assert isinstance(origin["module"], str) and origin["module"]
        assert Path(origin["path"]).is_file()
        assert origin["line"] >= 1 and origin["column"] >= 1
    assert isinstance(design["tools"], dict) and design["tools"]
    for vector in [*design["ports"], *design["nets"]]:
        assert 1 <= len(vector["bits"]) <= 64
        assert isinstance(vector["offset"], int)
        assert isinstance(vector["upto"], bool)
        assert isinstance(vector["signed"], bool)
        assert all(type(bit) is int or bit in ("0", "1") for bit in vector["bits"])
    for cell in design["cells"]:
        assert isinstance(cell["source"], str) and cell["source"]
        assert set(cell["ports"]) == set(cell["port_directions"])


def test_alias_preserves_native_bit_identity_and_discovers_single_top(tmp_path):
    design = lower(Project(sources=[FIXTURES / "alias.v"], top=None), tmp_path)
    assert_design_contract(design)
    assert design["top"] == "alias_passthrough"
    assert ports(design)["a"]["bits"] == ports(design)["y"]["bits"]
    assert evaluate_combinational(design, {"a": 1}) == {"y": 1}


def test_scalar_logic_truth_table(tmp_path):
    design = compile_fixture("scalar_logic.v", tmp_path)
    assert_design_contract(design)
    for a in range(2):
        for b in range(2):
            assert evaluate_combinational(design, {"a": a, "b": b}) == {"y": a & b}


def test_bus_offsets_and_slice_aliases_are_preserved(tmp_path):
    design = compile_fixture("bus_alias.v", tmp_path)
    assert_design_contract(design)
    p = ports(design)
    assert p["a"]["offset"] == 8
    assert p["a"]["upto"] is False
    assert p["a"]["bits"] == p["y"]["bits"]
    assert p["low"]["bits"] == p["a"]["bits"][:4]
    assert evaluate_combinational(design, {"a": 0xD6}) == {"y": 0xD6, "low": 6}


def test_constant_vectors_are_lsb_first(tmp_path):
    design = compile_fixture("constants.v", tmp_path)
    assert ports(design)["y"]["bits"] == ["1", "1", "0", "0", "0", "1", "0", "1"]
    assert evaluate_combinational(design, {}) == {"y": 0xA3}


def test_supported_64_bit_constant_is_not_truncated_or_signed(tmp_path):
    design = compile_fixture("constant64.v", tmp_path)
    assert_design_contract(design)
    assert len(ports(design)["y"]["bits"]) == 64
    assert ports(design)["y"]["signed"] is False
    assert evaluate_combinational(design, {}) == {"y": 0xFEDCBA9876543210}


@pytest.mark.parametrize("filename", ["mux.v", "always_if.v"])
def test_conditional_selection_preserves_branch_polarity(tmp_path, filename):
    design = compile_fixture(filename, tmp_path)
    assert_design_contract(design)
    assert all("latch" not in cell["type"].lower() for cell in design["cells"])
    for selection in range(2):
        assert evaluate_combinational(design, {"a": 3, "b": 12, "select_b": selection}) == {
            "y": 12 if selection else 3
        }


def test_complete_case_statement_retains_default_branch(tmp_path):
    design = compile_fixture("always_case.v", tmp_path)
    for mode, result in enumerate((5, 12, 3, 3)):
        assert evaluate_combinational(design, {"a": 5, "b": 12, "mode": mode}) == {"y": result}


def test_multiple_source_files_resolve_child_module(tmp_path):
    project = Project(sources=[FIXTURES / "project_top.v", FIXTURES / "leaf.v"], top="project_top")
    design = lower(project, tmp_path)
    assert_design_contract(design)
    assert all(cell["type"] != "leaf" for cell in design["cells"])
    for a in range(2):
        for b in range(2):
            assert evaluate_combinational(design, {"a": a, "b": b}) == {"y": a ^ b}


def test_includes_defines_and_paths_containing_spaces(tmp_path):
    source_directory = tmp_path / "source folder with spaces"
    include_directory = source_directory / "include folder"
    include_directory.mkdir(parents=True)
    source = source_directory / "include_top.v"
    shutil.copyfile(FIXTURES / "include_top.v", source)
    shutil.copyfile(FIXTURES / "includes/frontend_config.vh", include_directory / "frontend_config.vh")
    project = Project(sources=[source], top="include_top", include_dirs=[include_directory],
                      defines={"MASK": "4'b1010"})
    design = lower(project, tmp_path / "work folder")
    assert len(ports(design)["a"]["bits"]) == 4
    assert evaluate_combinational(design, {"a": 3}) == {"y": 9}


@pytest.mark.parametrize("width,bias,literal", [(6, 21, "6'h15"), (8, 255, "8'hff")])
def test_constant_parameter_overrides_affect_width_and_arithmetic(tmp_path, width, bias, literal):
    design = compile_fixture("parameterized.v", tmp_path, parameters={"WIDTH": width, "BIAS": literal})
    assert_design_contract(design)
    assert len(ports(design)["a"]["bits"]) == width
    assert len(ports(design)["y"]["bits"]) == width
    mask = (1 << width) - 1
    for a in (0, 1, mask // 2, mask):
        assert evaluate_combinational(design, {"a": a}) == {"y": (a + bias) & mask}


def test_unicode_source_file_and_directory_names(tmp_path):
    directory = tmp_path / "hdl_ışık_π"
    directory.mkdir()
    source = directory / "modül.v"
    shutil.copyfile(FIXTURES / "scalar_logic.v", source)
    project = Project(sources=[source], top="scalar_logic")
    if os.name == "nt":
        with pytest.raises(FrontendError) as failure:
            lower(project, tmp_path / "unicode_work")
        assert failure.value.code == "UNSUPPORTED_PATH"
        assert "ASCII" in failure.value.message
        assert Path(failure.value.path) == source
        return
    design = lower(project, tmp_path / "unicode_work")
    assert_design_contract(design)
    assert evaluate_combinational(design, {"a": 1, "b": 0}) == {"y": 0}
    assert evaluate_combinational(design, {"a": 1, "b": 1}) == {"y": 1}


@pytest.mark.skipif(os.name != "nt", reason="The current native Windows tools require ASCII paths")
def test_unicode_include_directory_has_actionable_path_diagnostic(tmp_path):
    directory = tmp_path / "include_\u0131\u015f\u0131k"
    directory.mkdir()
    shutil.copyfile(FIXTURES / "includes/frontend_config.vh", directory / "frontend_config.vh")
    project = Project(sources=[FIXTURES / "include_top.v"], top="include_top",
                      include_dirs=[directory], defines={"MASK": "4'b1010"})
    with pytest.raises(FrontendError) as failure:
        lower(project, tmp_path / "work")
    assert failure.value.code == "UNSUPPORTED_PATH"
    assert "ASCII" in failure.value.message
    assert Path(failure.value.path) == directory


@pytest.mark.skipif(os.name != "nt", reason="The current native Windows tools require ASCII paths")
def test_unicode_work_directory_has_actionable_path_diagnostic(tmp_path):
    directory = tmp_path / "work_\u0131\u015f\u0131k"
    project = Project(sources=[FIXTURES / "scalar_logic.v"], top="scalar_logic")
    with pytest.raises(FrontendError) as failure:
        lower(project, directory)
    assert failure.value.code == "UNSUPPORTED_PATH"
    assert "ASCII" in failure.value.message
    assert Path(failure.value.path) == directory


def test_macros_are_shared_in_explicit_source_order(tmp_path):
    definitions, consumer = FIXTURES / "macro_definitions.v", FIXTURES / "macro_consumer.v"
    design = lower(Project(sources=[definitions, consumer], top="macro_consumer"), tmp_path / "ordered")
    assert evaluate_combinational(design, {"a": 3}) == {"y": 6}
    with pytest.raises(FrontendError) as error:
        lower(Project(sources=[consumer, definitions], top="macro_consumer"), tmp_path / "reversed")
    assert error.value.code == "SLANG_ERROR"


def test_inspection_and_yosys_use_the_same_synthesis_profile(tmp_path):
    design = compile_fixture("synthesis_profile.v", tmp_path)
    assert evaluate_combinational(design, {"a": 0}) == {"y": 1}
    assert evaluate_combinational(design, {"a": 1}) == {"y": 0}


def test_finite_generate_loop_is_elaborated(tmp_path):
    design = compile_fixture("generated.v", tmp_path)
    for enable in range(2):
        for a in range(16):
            assert evaluate_combinational(design, {"a": a, "enable": enable}) == {"y": a if enable else 0}


def test_explicit_top_selection_changes_design(tmp_path):
    for top, result in (("invert_top", 0), ("passthrough_top", 1)):
        design = compile_fixture("two_tops.v", tmp_path / top, top=top)
        assert design["top"] == top
        assert evaluate_combinational(design, {"a": 1}) == {"y": result}


def test_ascending_bus_uses_rightmost_hdl_index_as_lsb(tmp_path):
    design = compile_fixture("ascending.v", tmp_path)
    assert_design_contract(design)
    p = ports(design)
    assert p["a"]["upto"] is True and p["a"]["offset"] == 0
    assert p["y"]["upto"] is False
    assert p["y"]["bits"] == p["a"]["bits"]
    assert p["lsb"]["bits"] == p["a"]["bits"][:1]
    assert p["msb"]["bits"] == p["a"]["bits"][-1:]
    assert evaluate_combinational(design, {"a": 0x80}) == {"y": 0x80, "lsb": 0, "msb": 1}
    assert evaluate_combinational(design, {"a": 0x01}) == {"y": 0x01, "lsb": 1, "msb": 0}


def test_signed_ports_and_sign_extension_survive_lowering(tmp_path):
    design = compile_fixture("signed_add.v", tmp_path)
    assert_design_contract(design)
    p = ports(design)
    assert all(p[name]["signed"] is True for name in ("a", "b", "y"))
    assert len(p["y"]["bits"]) == 5
    for a, b in ((-8, 1), (-1, -2), (7, 7), (-8, -8)):
        assert evaluate_combinational(design, {"a": a & 15, "b": b & 15}) == {"y": (a + b) & 31}


def test_wire_declaration_assignment_is_continuous_not_initialization(tmp_path):
    design = compile_fixture("wire_assignment.v", tmp_path)
    assert ports(design)["a"]["bits"] == ports(design)["y"]["bits"]


def test_positive_edge_register_is_preserved_for_later_mapping(tmp_path):
    design = compile_fixture("register.v", tmp_path)
    assert_design_contract(design)
    p = ports(design)
    registers = [cell for cell in design["cells"] if cell["type"] == "$dff"]
    assert len(registers) == 1
    register = registers[0]
    assert register["ports"]["D"] == p["d"]["bits"]
    assert register["ports"]["Q"] == p["q"]["bits"]
    assert register["ports"]["CLK"] == p["clk"]["bits"]
    assert parameter_int(register["parameters"]["CLK_POLARITY"]) == 1


def test_normalized_design_is_independent_of_work_directory(tmp_path):
    project = Project(sources=[FIXTURES / "scalar_logic.v"], top="scalar_logic")
    first_directory, second_directory = tmp_path / "first", tmp_path / "second"
    first = lower(project, first_directory)
    second = lower(project, second_directory)
    assert first == second
    encoded = json.dumps(first, sort_keys=True)
    assert str(first_directory) not in encoded and first_directory.as_posix() not in encoded
    assert str(second_directory) not in encoded and second_directory.as_posix() not in encoded


@pytest.mark.parametrize("filename,expected_code", [
    ("malformed.v", "SLANG_ERROR"),
    ("missing_include.v", "SLANG_ERROR"),
    ("delay.v", "UNSUPPORTED_TIMING"),
    ("initial_unused.v", "UNSUPPORTED_INITIALIZATION"),
    ("declaration_initialization.v", "UNSUPPORTED_INITIALIZATION"),
    ("unresolved.v", "SLANG_ERROR"),
    ("multiple_drivers.v", "MULTIPLE_DRIVERS"),
    ("undriven.v", "UNDRIVEN_NET"),
    ("latch.v", "UNSUPPORTED_LATCH"),
    ("width65.v", "UNSUPPORTED_WIDTH"),
    ("four_state_unused.v", "UNSUPPORTED_FOUR_STATE"),
    ("memory.v", "UNSUPPORTED_MEMORY"),
    # Yosys folds a self-inverter into an undriven value before its check pass.
    ("combinational_cycle.v", "UNDRIVEN_NET"),
    ("input_dependent_cycle.v", "COMBINATIONAL_CYCLE"),
])
def test_unsupported_or_invalid_source_reports_structured_error(tmp_path, filename, expected_code):
    with pytest.raises(FrontendError) as raised:
        compile_fixture(filename, tmp_path)
    error = raised.value
    assert isinstance(error.code, str) and error.code
    assert isinstance(error.message, str) and error.message
    assert error.code == expected_code
    if filename == "malformed.v":
        assert Path(error.path).name == filename
        assert error.line == 2
        assert isinstance(error.column, int) and error.column >= 1


@pytest.mark.parametrize("text,code,line", [
    ("module located(input [3:0] a, input [2:0] i, output y);\n  assign y = a[i];\nendmodule\n", "UNSUPPORTED_CELL", 2),
    ("module located(input [64:0] a,\n  output y);\n  assign y = ^a;\nendmodule\n", "UNSUPPORTED_WIDTH", 1),
    ("module located({a, b}, y);\n  input a, b;\n  output y;\n  assign y = a;\nendmodule\n", "YOSYS_ERROR", 1),
])
@pytest.mark.parametrize("hierarchical", [False, True])
def test_rejections_after_inspection_keep_a_source_location(tmp_path, text, code, line, hierarchical):
    from verilog2digital.frontend.hierarchy import lower_hierarchy
    source = tmp_path / "located.v"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(FrontendError) as raised:
        (lower_hierarchy if hierarchical else lower)(Project([source], top="located"), tmp_path / "work")
    assert raised.value.code == code
    assert Path(raised.value.path) == source.resolve() and raised.value.line == line
    if code == "UNSUPPORTED_CELL":
        assert "$shiftx" in raised.value.message and raised.value.column >= 1


@pytest.mark.parametrize("guard", ["`ifndef __slang__", "`ifndef __slang_major__", "`ifdef __slang__\n`else"])
@pytest.mark.parametrize("hierarchical", [False, True])
def test_code_hidden_by_the_inspections_own_macros_is_still_inspected(tmp_path, guard, hierarchical):
    # Yosys and Icarus never define slang's macros, so the inspection must not skip code they compile.
    from verilog2digital.frontend.hierarchy import lower_hierarchy
    source = tmp_path / "hidden.v"
    source.write_text(f"module hidden(input a, output y);\n  assign y = a;\n{guard}\n  initial $dumpfile(\"hidden.vcd\");\n"
                      "`endif\nendmodule\n", encoding="utf-8")
    with pytest.raises(FrontendError) as raised:
        (lower_hierarchy if hierarchical else lower)(Project([source], top="hidden"), tmp_path / "work")
    assert raised.value.code == "UNSUPPORTED_INITIALIZATION" and raised.value.line == guard.count("\n") + 4


def test_backend_memory_backstop_is_located():
    # Inspection rejects source arrays first; a backend memory must still be located.
    from verilog2digital.frontend.yosys import _normalize_unchecked
    raw = {"modules": {"top": {"memories": {"m": {"attributes": {"src": "C:/hdl/top.v:3.13-3.14"}, "width": 8, "size": 4}}}}}
    with pytest.raises(FrontendError) as raised:
        _normalize_unchecked(raw, "top", {}, Project([]))
    assert raised.value.code == "UNSUPPORTED_MEMORY"
    assert (Path(raised.value.path), raised.value.line, raised.value.column) == (Path("C:/hdl/top.v"), 3, 13)


def test_yosys_source_spans_become_structured_locations():
    from verilog2digital.frontend.errors import source_location
    assert source_location("C:/hdl/top.v:12.3-12.20") == {"path": str(Path("C:/hdl/top.v")), "line": 12, "column": 3}
    assert source_location("a:b/top.v:7|other.v:1.1-1.2") == {"path": str(Path("a:b/top.v")), "line": 7}
    assert source_location("") == source_location(None) == source_location("top.v") == {}


def test_parameter_overrides_are_normalized_or_rejected_before_any_tool():
    from verilog2digital.frontend.yosys import _parameter
    assert [_parameter(value) for value in ("+5", " 0_7 ", 2147483647, "8'hF_0", "4'b1010")] == [
        "5", "7", "2147483647", "8'hF_0", "4'b1010"]
    for value, code in (("-1", "INVALID_PARAMETER"), (-3, "INVALID_PARAMETER"), ("8'shF0", "INVALID_PARAMETER"),
                        ("4'Sd3", "INVALID_PARAMETER"), ("2147483648", "PARAMETER_LIMIT"), ("8'h_F", "INVALID_PARAMETER"),
                        ("4'hx", "INVALID_PARAMETER"), ("65'h0", "PARAMETER_LIMIT"), ("1+1", "INVALID_PARAMETER")):
        with pytest.raises(FrontendError) as raised:
            _parameter(value)
        assert raised.value.code == code, value


@pytest.mark.parametrize("value,code", [("+5", None), ("-1", "INVALID_PARAMETER"), ("4'sd1", "INVALID_PARAMETER")])
def test_signed_parameter_forms_never_reach_yosys_with_changed_meaning(tmp_path, value, code):
    # Yosys rejected +5/-1 and silently made an untyped 4'sd1 override unsigned.
    source = tmp_path / "signed_parameter.v"
    source.write_text("module signed_parameter #(parameter K = 1)(input signed [3:0] a, output y);\n"
                      "  assign y = a < K;\nendmodule\n", encoding="utf-8")
    project = Project([source], top="signed_parameter", parameters={"K": value})
    if code is not None:
        with pytest.raises(FrontendError) as raised:
            lower(project, tmp_path / "work")
        assert raised.value.code == code
        return
    design = lower(project, tmp_path / "work")
    assert evaluate_combinational(design, {"a": 15}) == {"y": 1}
    assert evaluate_combinational(design, {"a": 5}) == {"y": 0}


def test_missing_top_is_rejected(tmp_path):
    with pytest.raises(FrontendError) as raised:
        compile_fixture("alias.v", tmp_path, top="missing_top")
    assert raised.value.code == "UNKNOWN_TOP" and "missing_top" in raised.value.message


def test_multiple_unrelated_roots_require_top_selection(tmp_path):
    with pytest.raises(FrontendError) as raised:
        lower(Project(sources=[FIXTURES / "two_tops.v"], top=None), tmp_path)
    assert raised.value.code == "AMBIGUOUS_TOP" and raised.value.message
