"""Initialization proof, full trace contracts, and finite temporal coverage."""
from copy import deepcopy
from itertools import product

import pytest

from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.validation.sequential import (
    MAX_CHECKED_CYCLES, MAX_PREFIX_CYCLES, SequentialError, _Machine, _constant, _evaluate,
    _word, prove_initialization, sequential_stimulus_plan, validate_trace,
)
from test_sequential_reference import fixture


def test_default_prefix_proves_shift_flush_and_never_uses_initial_zero(tmp_path):
    _, design = fixture("shift_register", tmp_path)
    plan = sequential_stimulus_plan(design, random_count=0)
    assert plan["initialization"]["prefix_cycles"] == 4
    assert plan["initialization"]["known_register_bits"] == 4
    trace = deepcopy(plan["trace"])
    trace["initialization"]["cycles"].pop()
    with pytest.raises(SequentialError) as error:
        prove_initialization(design, trace)
    assert error.value.code == "UNINITIALIZED_STATE"


def test_simultaneous_register_snapshot_is_independent_of_cell_order(tmp_path):
    _, design = fixture("simultaneous", tmp_path)
    plan = sequential_stimulus_plan(design, random_count=0)
    first = _Machine(design)
    reversed_design = deepcopy(design)
    reversed_design["cells"].reverse()
    second = _Machine(reversed_design)
    for row in [*plan["trace"]["initialization"]["cycles"], {"values": {"rst": 0}}]:
        first.step(row)
        second.step(row)
    assert first.state == second.state
    for port in design["ports"]:
        if port["direction"] == "output":
            assert first.word(port["bits"], first.state).value == {"a": 0x3c, "b": 0xa5}[port["name"]]


def test_generated_cycles_are_reproducible_bounded_and_temporally_sampled(tmp_path):
    _, design = fixture("wide_register64", tmp_path)
    original = deepcopy(design)
    plan = sequential_stimulus_plan(design, seed=67, random_count=20)
    assert plan == sequential_stimulus_plan(design, seed=67, random_count=20)
    assert design == original
    assert plan["trace"]["cycles"] != sequential_stimulus_plan(design, seed=68, random_count=20)["trace"]["cycles"]
    assert 1 <= len(plan["trace"]["initialization"]["cycles"]) <= MAX_PREFIX_CYCLES
    assert 1 <= len(plan["trace"]["cycles"]) <= MAX_CHECKED_CYCLES
    assert plan["trace"]["cycles"][0] == plan["trace"]["cycles"][1]
    assert any(row["values"]["d"] == 1 << 63 for row in plan["trace"]["cycles"])
    assert any(row["values"]["d"] == (1 << 64) - 1 for row in plan["trace"]["cycles"])
    assert plan["assumptions"]["exhaustive"] is False
    assert plan["assumptions"]["random_cycles"] == 20
    assert plan["assumptions"]["sample_count"] == 3 * len(plan["trace"]["cycles"])


@pytest.mark.parametrize("mutation", ["missing_data", "clock_value", "extra_port", "negative", "overflow", "bool",
                                      "empty_prefix", "empty_cycles", "long_prefix", "long_cycles", "clock_name", "falling", "force"])
def test_malformed_or_ambiguous_trace_is_rejected_before_simulation(tmp_path, mutation):
    _, design = fixture("d_register", tmp_path)
    trace = sequential_stimulus_plan(design, random_count=0)["trace"]
    row = trace["cycles"][0]["values"]
    if mutation == "missing_data": row.clear()
    elif mutation == "clock_value": row["clk"] = 0
    elif mutation == "extra_port": row["typo"] = 0
    elif mutation == "negative": row["d"] = -1
    elif mutation == "overflow": row["d"] = 256
    elif mutation == "bool": row["d"] = False
    elif mutation == "empty_prefix": trace["initialization"]["cycles"] = []
    elif mutation == "empty_cycles": trace["cycles"] = []
    elif mutation == "long_prefix": trace["initialization"]["cycles"] *= MAX_PREFIX_CYCLES + 1
    elif mutation == "long_cycles": trace["cycles"] = [trace["cycles"][0]] * (MAX_CHECKED_CYCLES + 1)
    elif mutation == "clock_name": trace["clock"]["port"] = "d"
    elif mutation == "falling": trace["clock"]["edge"] = "falling"
    elif mutation == "force": trace["initialization"]["kind"] = "force_state"
    with pytest.raises(SequentialError) as error:
        validate_trace(design, trace)
    assert error.value.code == "SEQUENTIAL_TRACE"


@pytest.mark.parametrize("parameters", [{"seed": True}, {"seed": 1.5}, {"random_count": -1}, {"random_count": 513}, {"random_count": True}])
def test_invalid_generator_parameters_are_rejected(tmp_path, parameters):
    _, design = fixture("d_register", tmp_path)
    with pytest.raises(SequentialError) as error:
        sequential_stimulus_plan(design, **parameters)
    assert error.value.code == "SEQUENTIAL_PARAMETERS"


def test_clock_as_data_is_rejected_even_when_directly_connected_to_q(tmp_path):
    source = tmp_path / "clock_data.v"
    source.write_text("module clock_data(input clk, output reg q); always @(posedge clk) q<=clk; endmodule", encoding="utf-8")
    design = lower(Project(sources=[source], top="clock_data"), tmp_path / "lower")
    with pytest.raises(SequentialError) as error:
        sequential_stimulus_plan(design)
    assert error.value.code == "UNSUPPORTED_CLOCK"


@pytest.mark.parametrize("kind", ["$eqx", "$nex"])
def test_case_equality_of_unknown_bits_cannot_manufacture_initialization(kind):
    cell = {"type": kind, "parameters": {"A_SIGNED": 0, "B_SIGNED": 0}, "ports": {"Y": [2]}}
    unknown = _word(0, 0, 2)
    result = _evaluate(cell, {"A": unknown, "B": unknown})
    assert result.known == 0
    known = _evaluate(cell, {"A": _constant(2, 2), "B": _constant(2, 2)})
    assert known.complete and known.value == int(kind == "$eqx")


def test_unknown_case_equality_cannot_initialize_a_register_on_same_edge_as_operand_load(tmp_path):
    source = tmp_path / "case_equality.v"
    source.write_text("""module case_equality(input clk,load,input [1:0] a,b,output reg q);
reg [1:0] x,y;
always @(posedge clk) begin
  if (load) begin x<=a; y<=b; end
  if (x===y) q<=1'b1;
end
endmodule
""", encoding="utf-8")
    design = lower(Project(sources=[source], top="case_equality"), tmp_path / "lower")
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": [{"values": {"load": 1, "a": 2, "b": 2}}]},
             "cycles": [{"values": {"load": 0, "a": 0, "b": 0}}]}
    with pytest.raises(SequentialError) as error:
        prove_initialization(design, trace)
    assert error.value.code == "UNINITIALIZED_STATE"
    trace["initialization"]["cycles"].append({"values": {"load": 0, "a": 0, "b": 0}})
    assert prove_initialization(design, trace)["valid"]


def abstract_words(width):
    for bits in product((None, 0, 1), repeat=width):
        yield _word(sum((bit or 0) << index for index, bit in enumerate(bits)),
                    sum(int(bit is not None) << index for index, bit in enumerate(bits)), width)


def completions(word):
    return [value for value in range(1 << word.width) if value & word.known == word.value]


def test_mux_knowledge_is_sound_for_unknown_selectors_and_partially_equal_branches():
    cell = {"type": "$mux", "parameters": {}, "ports": {"Y": [2, 3, 4]}}
    for a, b, select in product(abstract_words(3), abstract_words(3), abstract_words(1)):
        result = _evaluate(cell, {"A": a, "B": b, "S": select})
        for av, bv, sv in product(completions(a), completions(b), completions(select)):
            expected = bv if sv else av
            assert (expected ^ result.value) & result.known == 0


@pytest.mark.parametrize("kind", ["$reduce_and", "$reduce_or", "$reduce_bool", "$reduce_xor", "$reduce_xnor", "$logic_not"])
def test_reduction_knowledge_is_sound_for_all_small_concretizations(kind):
    cell = {"type": kind, "parameters": {}, "ports": {"Y": [2, 3, 4, 5]}}
    for word in abstract_words(3):
        result = _evaluate(cell, {"A": word})
        for value in completions(word):
            expected = {"$reduce_and": lambda: value == 7, "$reduce_or": lambda: value != 0,
                        "$reduce_bool": lambda: value != 0, "$reduce_xor": lambda: value.bit_count() & 1,
                        "$reduce_xnor": lambda: 1 ^ (value.bit_count() & 1), "$logic_not": lambda: value == 0}[kind]()
            assert (int(expected) ^ result.value) & result.known == 0


@pytest.mark.parametrize("kind,signed", [(kind, signed) for kind in
    ("$and", "$or", "$xor", "$xnor", "$add", "$sub", "$eq", "$ne", "$lt", "$le", "$ge", "$gt", "$eqx", "$nex")
    for signed in (False, True)])
def test_abstract_binary_knowledge_is_sound_for_all_small_concretizations(kind, signed):
    width = 3
    output_width = 4 if kind in {"$and", "$or", "$xor", "$xnor", "$add", "$sub"} else 1
    cell = {"type": kind, "parameters": {"A_SIGNED": int(signed), "B_SIGNED": int(signed)},
            "ports": {"Y": list(range(output_width))}}
    for a, b in product(abstract_words(width), repeat=2):
        result = _evaluate(cell, {"A": a, "B": b})
        if a.complete and b.complete:
            assert result.complete
        for av, bv in product(completions(a), completions(b)):
            if signed:
                av, bv = (av - 8 if av & 4 else av), (bv - 8 if bv & 4 else bv)
            expected = {"$and": lambda: av & bv, "$or": lambda: av | bv, "$xor": lambda: av ^ bv,
                        "$xnor": lambda: ~(av ^ bv), "$add": lambda: av + bv, "$sub": lambda: av - bv,
                        "$eq": lambda: av == bv, "$eqx": lambda: av == bv, "$ne": lambda: av != bv,
                        "$nex": lambda: av != bv, "$lt": lambda: av < bv, "$le": lambda: av <= bv,
                        "$ge": lambda: av >= bv, "$gt": lambda: av > bv}[kind]()
            assert (int(expected) ^ result.value) & result.known == 0


@pytest.mark.parametrize("kind,signed,out_width", [(kind, signed, width) for kind in
    ("$shl", "$sshl", "$shr", "$sshr") for signed in (False, True) for width in (2, 5)])
def test_known_shift_bits_are_sound_with_narrowing_sign_and_overshift(kind, signed, out_width):
    cell = {"type": kind, "parameters": {"A_SIGNED": int(signed)}, "ports": {"Y": list(range(out_width))}}
    for word in abstract_words(3):
        for amount in (0, 1, 2, 3, 4, 5, 63):
            result = _evaluate(cell, {"A": word, "B": _constant(amount, 6)})
            if word.complete:
                assert result.complete
            for value in completions(word):
                working_width = max(3, out_width) if kind in {"$shr", "$sshr"} else out_width
                if signed and value & 4:
                    value -= 8
                if kind in {"$shl", "$sshl"}:
                    expected = value << amount
                elif kind == "$sshr" and signed:
                    expected = value >> amount
                else:
                    expected = (value & ((1 << working_width) - 1)) >> amount
                assert (expected ^ result.value) & result.known == 0
