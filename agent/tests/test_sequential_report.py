"""Check ordered clock-edge observations and reported coverage."""
from copy import deepcopy

import pytest

from verilog2digital.conversion import compare_sequential_behavior


def example():
    mapped = {"inputs": [{"port": "clk", "width": 1}, {"port": "d", "width": 64}],
              "outputs": [{"port": "q", "instance_id": "out0", "width": 64}],
              "registers": [{"cell_id": "state", "width": 64}], "clock": {"instance_id": "clock0"},
              "instances": [{"id": "ff0", "type": "D_FF", "attributes": {"Default": "0x0", "Bits": 64}}]}
    trace = {"clock": {"port": "clk"}, "cycles": [{"values": {"d": 1 << 63}}],
             "initialization": {"cycles": [{"values": {"d": 0}}]}}
    phases = ("before_rise", "after_rise", "after_fall")
    reference = {"assumptions": {"clock": trace["clock"], "initialization": proof(),
                                 "checked_cycles": 1, "sample_count": 3, "phases": list(phases)}, "samples": [
        {"cycle": 0, "phase": phase, "values": {"q": value}}
        for phase, value in zip(phases, (0, 1 << 63, 1 << 63))]}
    native = {"assumptions": {"clock_instance_id": "clock0", "phase_order": list(phases),
                               "initialization": {"kind": "input_sequence", "prefix_cycles": 1,
                                                  "internal_state_forcing": False, "native_defaults": {"ff0": "0x0"}}},
              "samples": [{**row, "values": {"out0": hex(row["values"]["q"])},
                           "high_z": {"out0": "0x0"}} for row in reference["samples"]]}
    return mapped, trace, reference, native


def proof():
    return {"valid": True, "initial_state": "all_retained_register_bits_unknown",
            "internal_state_forcing": False, "register_count": 1,
            "known_register_count": 1, "register_bits": 64, "known_register_bits": 64,
            "registers": [{"cell_id": "state", "width": 64, "known_bits": 64,
                           "known_mask": "0xffffffffffffffff", "value": "0x0"}],
            "prefix": [{"values": {"d": 0}}], "prefix_cycles": 1}


def compare(data, **metadata):
    return compare_sequential_behavior(*data, coverage=metadata, initialization=proof())


def test_all_clock_phases_and_high_bits_pass_without_exhaustive_claim():
    report = compare(example(), coverage="exhaustive_binary", exhaustive=True)
    assert report["valid"]
    assert report["coverage"] == "sampled_cycles" and not report["exhaustive"]
    assert report["checked_cycles"] == 1 and report["input_vectors"] == 3


@pytest.mark.parametrize("phase_index", [0, 1, 2])
def test_wrong_state_at_any_clock_phase_is_detected(phase_index):
    data = example()
    values = data[3]["samples"][phase_index]["values"]
    values["out0"] = hex(int(values["out0"], 16) ^ (1 << 63))
    report = compare(data, valid=True, diagnostics=[])
    assert not report["valid"]
    assert report["diagnostics"][0]["code"] == "behavior_mismatch"


@pytest.mark.parametrize("source", [2, 3])
def test_reordered_equal_values_cannot_hide_an_edge_protocol_error(source):
    data = example()
    # Rising and falling samples have identical values in this fixture.
    samples = data[source]["samples"]
    samples[1], samples[2] = samples[2], samples[1]
    report = compare(data)
    assert not report["valid"]
    assert any(item["code"] == "cycle_order" for item in report["diagnostics"])


def test_missing_falling_sample_is_rejected():
    data = deepcopy(example())
    data[3]["samples"].pop()
    report = compare(data)
    assert not report["valid"]
    assert {item["code"] for item in report["diagnostics"]} >= {"sample_count", "cycle_order"}


def test_equal_observed_values_cannot_substitute_for_initialized_state():
    report = compare_sequential_behavior(*example(), coverage={}, initialization={"valid": False})
    assert not report["valid"]
    assert any(item["code"] == "unproven_initialization" for item in report["diagnostics"])


@pytest.mark.parametrize("mutation", ["prefix", "width", "identity", "bits", "mask"])
def test_proof_must_cover_the_same_registers_and_input_prefix(mutation):
    initialization = proof()
    if mutation == "prefix":
        initialization["prefix"][0]["values"]["d"] = 1
    elif mutation == "width":
        initialization["registers"][0]["width"] = 1
    elif mutation == "identity":
        initialization["registers"][0]["cell_id"] = "another_state"
    elif mutation == "bits":
        initialization["register_bits"] = initialization["known_register_bits"] = 1
    else:
        initialization["registers"][0]["known_mask"] = "0x1"
    report = compare_sequential_behavior(*example(), coverage={}, initialization=initialization)
    assert not report["valid"]
    assert any(item["code"] == "unproven_initialization" for item in report["diagnostics"])


@pytest.mark.parametrize("mutation", ["clock", "prefix", "defaults", "reference"])
def test_simulation_assumptions_must_describe_the_requested_trace(mutation):
    data = example()
    assumptions = data[3]["assumptions"]
    if mutation == "clock":
        assumptions["clock_instance_id"] = "another_clock"
    elif mutation == "prefix":
        assumptions["initialization"]["prefix_cycles"] = 2
    elif mutation == "defaults":
        assumptions["initialization"]["native_defaults"] = {"another_register": "0x0"}
    else:
        data[2]["assumptions"]["initialization"]["prefix"][0]["values"]["d"] = 1
    report = compare(data)
    assert not report["valid"]
    assert any(item["code"] in {"native_trace_mismatch", "reference_trace_mismatch"} for item in report["diagnostics"])
