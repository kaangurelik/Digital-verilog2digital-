"""Original-HDL clock traces with independent register-transition expectations."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.validation.reference import ReferenceError, _samples, simulate_sequential_reference
from verilog2digital.validation.sequential import PHASES, SequentialError, sequential_stimulus_plan


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/sequential"
POSITIVE_CASES = ("d_register", "enabled_register", "counter", "shift_register", "fsm",
                  "simultaneous", "enable_before_reset", "active_low_reset", "wide_register64", "constant_register")


def fixture(name, directory):
    project = Project(sources=[FIXTURES / f"{name}.v"], top=name)
    return project, lower(project, directory / "lower")


def transition(name, state, values):
    if name == "constant_register":
        return 0
    if name == "d_register":
        return values["d"]
    if name in {"enabled_register", "wide_register64"}:
        return values["d"] if values["en"] else state
    if name == "counter":
        return 9 if values["rst"] else None if state is None else (state + values["en"]) % 16
    if name == "shift_register":
        return (*state[1:], values["d"])
    if name == "fsm":
        if values["rst"]:
            return 2
        if state == 0:
            return 1 if values["go"] else 0
        if state == 1:
            return 2 if values["go"] else 0 if values["back"] else 1
        if state == 2:
            return 1 if values["back"] else 2
        return 0
    if name == "simultaneous":
        return (0xa5, 0x3c) if values["rst"] else (state[1], state[0])
    if name == "enable_before_reset":
        return (0x55 if values["rst"] else values["d"]) if values["en"] else state
    if name == "active_low_reset":
        return values["d"] if values["rst_n"] else 0xa6
    raise AssertionError(name)


def observed(name, state):
    if name == "counter":
        return {"count": state}
    if name == "fsm":
        return {"state": state, "busy": int(state != 0)}
    if name == "simultaneous":
        return {"a": state[0], "b": state[1]}
    if name == "shift_register":
        assert None not in state
        return {"q": sum(bit << (3 - index) for index, bit in enumerate(state))}
    return {"q": state}


def expected_samples(name, trace):
    state = (None,) * 4 if name == "shift_register" else (None, None) if name == "simultaneous" else None
    for row in trace["initialization"]["cycles"]:
        state = transition(name, state, row["values"])
    result = []
    for index, row in enumerate(trace["cycles"]):
        result.append({"cycle": index, "phase": "before_rise", "values": observed(name, state)})
        state = transition(name, state, row["values"])
        for phase in ("after_rise", "after_fall"):
            result.append({"cycle": index, "phase": phase, "values": observed(name, state)})
    return result


@pytest.mark.parametrize("name", POSITIVE_CASES)
def test_original_sequential_hdl_matches_independent_cycle_transitions(name, tmp_path):
    project, design = fixture(name, tmp_path)
    plan = sequential_stimulus_plan(design, seed=27, random_count=12)
    result = simulate_sequential_reference(project, design, tmp_path / "reference", plan["trace"])
    assert result["samples"] == expected_samples(name, plan["trace"])
    assert result["assumptions"]["profile"] == "synchronous_sequential"
    proof = result["assumptions"]["initialization"]
    assert proof["valid"] and proof["known_register_bits"] == proof["register_bits"]
    assert proof["initial_state"] == "all_retained_register_bits_unknown"
    source = Path(result["artifacts"]["testbench"]).read_text(encoding="utf-8")
    assert "force " not in source and "release " not in source and "v2d_dut." not in source
    manifest = json.loads(Path(result["artifacts"]["provenance"]).read_text(encoding="utf-8"))
    assert manifest["trace"] == plan["trace"]
    assert "stimuli" not in manifest
    assert manifest["commands"]["compile"][-2] == str(project.sources[0])


def test_explicit_nonzero_load_enable_hold_and_repeat_edges(tmp_path):
    project, design = fixture("enabled_register", tmp_path)
    rows = [{"en": 0, "d": 0x11}, {"en": 1, "d": 0x80}, {"en": 1, "d": 0x80}, {"en": 0, "d": 0xff}]
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": [{"values": {"en": 1, "d": 0xa5}}]},
             "cycles": [{"values": values} for values in rows]}
    plan = sequential_stimulus_plan(design, trace=trace)
    result = simulate_sequential_reference(project, design, tmp_path / "reference", plan["trace"])
    assert result["samples"] == expected_samples("enabled_register", trace)
    assert [sample["values"]["q"] for sample in result["samples"]] == [0xa5, 0xa5, 0xa5, 0xa5, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80]


@pytest.mark.parametrize("name", ["bare_counter", "hidden_uninitialized"])
def test_uninitialized_state_cannot_hide_behind_known_outputs_or_native_zero(name, tmp_path):
    project, design = fixture(name, tmp_path)
    with pytest.raises(SequentialError) as error:
        sequential_stimulus_plan(design, random_count=0)
    assert error.value.code == "UNINITIALIZED_STATE"
    assert "default state" in error.value.message
    inputs = {port["name"]: 0 for port in design["ports"] if port["direction"] == "input" and port["name"] != "clk"}
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": [{"values": inputs}]}, "cycles": [{"values": inputs}]}
    with pytest.raises(SequentialError, match="does not establish"):
        simulate_sequential_reference(project, design, tmp_path / "reference", trace)
    assert not (tmp_path / "reference").exists()


def test_sequential_reference_preserves_macro_parameter_order_and_escaped_ports(tmp_path):
    directory = tmp_path / "source files"
    directory.mkdir()
    (directory / "defs.v").write_text("`define EARLIER 8'h6a\n", encoding="utf-8")
    source = directory / "source.v"
    source.write_text("""`ifdef SYNTHESIS
`ifdef YOSYS
module configured #(parameter MASK=8'h00)(input clk,rst,input [7:0] \\input.data ,output reg [7:0] \\output.data );
always @(posedge clk) if (rst) \\output.data <= `EARLIER ^ MASK;
else \\output.data <= \\input.data ;
endmodule
`endif
`endif
""", encoding="utf-8")
    project = Project(sources=[directory / "defs.v", source], top="configured", parameters={"MASK": "8'hff"})
    design = lower(project, tmp_path / "lower")
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": [{"values": {"rst": 1, "input.data": 0}}]},
             "cycles": [{"values": {"rst": 0, "input.data": 0x80}}]}
    result = simulate_sequential_reference(project, design, tmp_path / "reference", trace)
    assert [sample["values"] for sample in result["samples"]] == [{"output.data": 0x95}, {"output.data": 0x80}, {"output.data": 0x80}]


@pytest.mark.parametrize("mutation", ["phase", "cycle", "missing", "unknown", "duplicate"])
def test_sequential_output_parser_rejects_missing_wrong_order_or_unknown_samples(tmp_path, mutation):
    rows = [f"MARK 0 {phase} 01" for phase in PHASES]
    if mutation == "phase": rows[0] = "MARK 0 after_rise 01"
    if mutation == "cycle": rows[1] = "MARK 1 after_rise 01"
    if mutation == "missing": rows.pop(1)
    if mutation == "unknown": rows[1] = "MARK 0 after_rise xx"
    if mutation == "duplicate": rows.insert(1, rows[0])
    log = tmp_path / "samples.log"
    log.write_text("\n".join([*rows, "MARK DONE 3"]), encoding="utf-8")
    with pytest.raises(ReferenceError):
        _samples(log, "MARK", {"q": 8}, 3, sequential=True)
