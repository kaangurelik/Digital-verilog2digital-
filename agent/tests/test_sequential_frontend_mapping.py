"""Ordinary-case state machines retain source behavior after pmux lowering."""
import json
import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.yosys import lower
from verilog2digital.mapping import map_sequential
from verilog2digital.validation.reference import simulate_sequential_reference
from verilog2digital.validation.sequential import sequential_stimulus_plan
from test_scalar_mapping import simulate_mapping


SOURCE = """module case_fsm(input clk, input rst, input step,
  output reg [1:0] state, output reg [3:0] code);
  always @(posedge clk) begin
    if (rst) state <= 2'd2;
    else if (step) begin
      case (state)
        2'd0: state <= 2'd1;
        2'd1: state <= 2'd3;
        2'd2: state <= 2'd0;
        default: state <= 2'd2;
      endcase
    end
  end
  always @* begin
    case (state)
      2'd0: code = 4'd5;
      2'd1: code = 4'd9;
      2'd2: code = 4'd14;
      default: code = 4'd3;
    endcase
  end
endmodule
"""


def test_case_fsm_pmux_lowering_matches_original_icarus_and_native_registers(tmp_path):
    source = tmp_path / "case_fsm.v"
    source.write_text(SOURCE, encoding="utf-8")
    project = Project([source], top="case_fsm")
    design = lower(project, tmp_path / "lower")
    elaborated = json.loads((tmp_path / "lower/elaborated.json").read_text(encoding="utf-8"))
    assert any(cell["type"] == "$pmux" for cell in elaborated["modules"]["case_fsm"]["cells"].values())
    assert not any(cell["type"] == "$pmux" for cell in design["cells"])
    assert any(cell["type"] == "$dff" for cell in design["cells"])
    rows = [{"values": {"rst": rst, "step": step}} for rst, step in
            [(0, 0), (0, 1), (0, 1), (0, 1), (0, 1), (1, 0), (1, 1), (0, 0), (0, 1)]]
    prefix = [{"values": {"rst": 1, "step": 0}}]
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": prefix}, "cycles": rows}
    plan = sequential_stimulus_plan(design, trace=trace)
    reference = simulate_sequential_reference(project, design, tmp_path / "reference", plan["trace"])
    table = {0: 5, 1: 9, 2: 14, 3: 3}
    transitions = {0: 1, 1: 3, 2: 0, 3: 2}
    expected = []
    state = 2
    for cycle, row in enumerate(rows):
        values = row["values"]
        expected.append({"cycle": cycle, "phase": "before_rise", "values": {"state": state, "code": table[state]}})
        if values["rst"]:
            state = 2
        elif values["step"]:
            state = transitions[state]
        for phase in ("after_rise", "after_fall"):
            expected.append({"cycle": cycle, "phase": phase, "values": {"state": state, "code": table[state]}})
    assert reference["samples"] == expected
    vectors = [{**row["values"], "clk": clock} for row in prefix + rows for clock in (0, 1, 0)]
    with Adapter() as adapter:
        mapped = map_sequential(design, adapter)
        native = simulate_mapping(mapped, adapter, tmp_path, vectors)
    assert native[len(prefix)*3:] == [sample["values"] for sample in expected]


def test_clocked_constant_load_remains_state_and_requires_an_initial_edge(tmp_path):
    source = tmp_path / "constant_register.v"
    source.write_text("module constant_register(input clk, output reg [3:0] q); "
                      "always @(posedge clk) q <= 4'd9; endmodule\n", encoding="utf-8")
    project = Project([source], top="constant_register")
    design = lower(project, tmp_path / "lower")
    assert [cell["type"] for cell in design["cells"]] == ["$dff"]
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": []}, "cycles": [{"values": {}}]}
    with pytest.raises(FrontendError) as error:
        sequential_stimulus_plan(design, trace=trace)
    assert error.value.code == "SEQUENTIAL_TRACE"
    assert "initialization" in error.value.message
    trace["initialization"]["cycles"] = [{"values": {}}]
    plan = sequential_stimulus_plan(design, trace=trace)
    reference = simulate_sequential_reference(project, design, tmp_path / "reference", plan["trace"])
    assert [sample["values"] for sample in reference["samples"]] == [{"q": 9}] * 3
