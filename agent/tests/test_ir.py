"""Reject malformed normalized circuits independently of Yosys and native parsers."""
from copy import deepcopy

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.ir import BINARY_CELLS, UNARY_CELLS, validate_design


def port(name, bits, direction="input"):
    return {"name": name, "bits": list(bits), "direction": direction, "offset": 0,
            "upto": False, "signed": False, "source": "fixture.v:1"}


def net(name, bits):
    value = port(name, bits)
    del value["direction"]
    return {**value, "hidden": False}


def cell(kind="$and", *, identity="c0", pins=None, parameters=None):
    pins = pins or {"A": [2], "B": [3], "Y": [4]}
    parameters = parameters or {"A_SIGNED": 0, "B_SIGNED": 0, "A_WIDTH": 1, "B_WIDTH": 1, "Y_WIDTH": 1}
    return {"id": identity, "type": kind, "native_name": f"native_{identity}", "parameters": parameters,
            "ports": pins, "port_directions": {name: "output" if name in {"Y", "Q"} else "input" for name in pins},
            "source": "fixture.v:2", "hierarchy": f"top {identity}"}


def design():
    return {"schema_version": 1, "top": "top", "ports": [port("a", [2]), port("b", [3]), port("y", [4], "output")],
            "nets": [net("a", [2]), net("b", [3]), net("y", [4])], "cells": [cell()],
            "origins": [{"module": "top", "path": "fixture.v", "line": 1, "column": 1, "end_line": 3, "end_column": 10}],
            "diagnostics": [], "tools": {"yosys": "fixture", "pyslang": "fixture"},
            "configuration": {"sources": ["fixture.v"], "include_dirs": [], "defines": {}, "parameters": {}}}


def reject(value, code, message=None):
    with pytest.raises(FrontendError) as error:
        validate_design(value)
    assert error.value.code == code
    if message:
        assert message in error.value.message


def test_validation_preserves_all_data_and_accepts_aliases_and_fanout():
    value = design()
    value["nets"].extend([net("alias_a", [2]), net("broadcast", [4, 4])])
    value["ports"].append(port("another_output", [4], "output"))
    before = deepcopy(value)
    validate_design(value)
    assert value == before


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(origins={}),
    lambda d: d.update(extra=True),
    lambda d: d["cells"][0].update(typo=1),
    lambda d: d["ports"][0].update(direction="inout"),
    lambda d: d["ports"][0].update(bits=[True]),
    lambda d: d["nets"][0].update(bits=["x"]),
    lambda d: d["nets"][0].update(bits=["z"]),
    lambda d: d["nets"][0].update(bits=[0]),
    lambda d: d["nets"][0].update(bits=[]),
    lambda d: d["configuration"].update(sources=[]),
    lambda d: d["configuration"].update(parameters={"WIDTH": True}),
])
def test_schema_rejects_invalid_shapes(mutation):
    value = design()
    mutation(value)
    reject(value, "INVALID_DESIGN", "design at")


def test_numerically_integral_float_is_not_a_signal_identity():
    value = design()
    value["cells"][0]["ports"]["A"] = [2.0]
    reject(value, "INVALID_BIT", "c0")


@pytest.mark.parametrize("collection,key,code", [
    ("ports", "name", "DUPLICATE_PORT"), ("nets", "name", "DUPLICATE_NET"),
    ("cells", "id", "DUPLICATE_CELL"), ("cells", "native_name", "DUPLICATE_CELL"),
])
def test_rejects_ambiguous_identifiers(collection, key, code):
    value = design()
    duplicate = deepcopy(value[collection][0])
    if collection == "cells":
        duplicate["id" if key == "native_name" else "native_name"] = "unique_other"
    value[collection].append(duplicate)
    reject(value, code, duplicate[key])


def test_same_name_port_net_disagreement_is_rejected():
    value = design()
    value["nets"][0]["bits"] = [3]
    reject(value, "PORT_NET_MISMATCH", "a")


@pytest.mark.parametrize("mutation", [
    lambda c: c["port_directions"].pop("A"),
    lambda c: c["port_directions"].update(Y="input"),
    lambda c: c["ports"].update(EXTRA=[2]),
    lambda c: c["ports"].pop("B"),
])
def test_requires_exact_native_pin_set_and_directions(mutation):
    value = design()
    mutation(value["cells"][0])
    reject(value, "INVALID_CELL_PORTS", "c0")


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.pop("A_SIGNED"), "INVALID_CELL_PARAMETERS"),
    (lambda p: p.update(UNKNOWN=1), "INVALID_CELL_PARAMETERS"),
    (lambda p: p.update(A_SIGNED=2), "INVALID_CELL_PARAMETERS"),
    (lambda p: p.update(A_WIDTH=1.0), "INVALID_CELL_PARAMETERS"),
    (lambda p: p.update(A_WIDTH=65), "UNSUPPORTED_WIDTH"),
    (lambda p: p.update(Y_WIDTH=0), "UNSUPPORTED_WIDTH"),
    (lambda p: p.update(Y_WIDTH=2), "CELL_WIDTH_MISMATCH"),
])
def test_validates_cell_parameter_meaning(mutation, code):
    value = design()
    mutation(value["cells"][0]["parameters"])
    reject(value, code, "c0")


@pytest.mark.parametrize("kind", sorted(UNARY_CELLS | BINARY_CELLS))
def test_supported_arithmetic_and_logic_interfaces_allow_unequal_widths(kind):
    value = design()
    pins = {"A": [2, 3], "Y": [4, 5, 6]}
    parameters = {"A_WIDTH": 2, "Y_WIDTH": 3, "A_SIGNED": 1}
    if kind in BINARY_CELLS:
        pins["B"] = [2]
        parameters.update(B_WIDTH=1, B_SIGNED=1)
    value["cells"] = [cell(kind, pins=pins, parameters=parameters)]
    validate_design(value)


def test_unknown_cell_cannot_claim_a_supported_interface():
    value = design()
    value["cells"][0]["type"] = "$mystery"
    reject(value, "UNSUPPORTED_CELL", "$mystery")


@pytest.mark.parametrize("kind,selectors", [("$mux", 1), ("$pmux", 2), ("$pmux", 64)])
def test_mux_dimensions_include_packed_branch_vectors(kind, selectors):
    value = design()
    width = 64 if selectors == 64 else 2
    pins = {"A": [2] * width, "B": [3] * (width * selectors), "S": [2] * selectors,
            "Y": list(range(4, 4 + width))}
    parameters = {"WIDTH": width}
    if kind == "$pmux":
        parameters["S_WIDTH"] = selectors
    value["cells"] = [cell(kind, pins=pins, parameters=parameters)]
    validate_design(value)
    value["cells"][0]["ports"]["B"].pop()
    reject(value, "CELL_WIDTH_MISMATCH", ".B")


def test_constant_outputs_and_unused_nets_do_not_need_drivers():
    value = design()
    value["ports"].append(port("constant", ["1", "0"], "output"))
    value["nets"].append(net("unused", [999]))
    validate_design(value)


@pytest.mark.parametrize("endpoint", ["input", "cell"])
def test_driving_a_constant_is_invalid(endpoint):
    value = design()
    if endpoint == "input":
        value["ports"].append(port("bad", ["0"]))
    else:
        value["cells"][0]["ports"]["Y"] = ["1"]
    reject(value, "INVALID_DRIVER", "constant")


@pytest.mark.parametrize("endpoint", ["input_alias", "cell_output", "same_cell_duplicate_bit"])
def test_detects_multiple_drivers_even_within_one_endpoint_vector(endpoint):
    value = design()
    if endpoint == "input_alias":
        value["ports"].append(port("alias_input", [2]))
    elif endpoint == "cell_output":
        value["cells"].append(cell(identity="other"))
    else:
        value["cells"][0]["ports"]["Y"] = [4, 4]
        value["cells"][0]["parameters"]["Y_WIDTH"] = 2
    reject(value, "MULTIPLE_DRIVERS")


@pytest.mark.parametrize("endpoint", ["output", "cell_input"])
def test_used_but_undriven_bits_report_the_consumer(endpoint):
    value = design()
    if endpoint == "output":
        value["ports"].append(port("dangling", [999], "output"))
    else:
        value["cells"][0]["ports"]["A"] = [999]
    reject(value, "UNDRIVEN_NET", "999")


def register(identity="ff", *, clock=2, data=3, output=5):
    return cell("$dff", identity=identity, pins={"CLK": [clock], "D": [data], "Q": [output]},
                parameters={"WIDTH": 1, "CLK_POLARITY": 1})


def test_register_feedback_is_a_sequential_boundary():
    value = design()
    value["cells"][0]["ports"]["A"] = [5]
    value["cells"].append(register(data=4))
    validate_design(value)


def test_register_can_hold_its_previous_output():
    value = design()
    value["cells"].append(register(data=5))
    validate_design(value)


@pytest.mark.parametrize("mutation", [
    lambda d: d["cells"].append(register(clock="0")),
    lambda d: d["cells"].append(register(clock=4)),
    lambda d: d["cells"].extend([register(), register("ff2", clock=3, output=6)]),
])
def test_clock_must_be_one_shared_top_level_input(mutation):
    value = design()
    mutation(value)
    reject(value, "UNSUPPORTED_CLOCK")


def test_clock_polarity_and_width_are_checked():
    value = design()
    value["cells"].append(register())
    value["cells"][-1]["parameters"]["CLK_POLARITY"] = 0
    reject(value, "UNSUPPORTED_CLOCK", "rising-edge")
    value["cells"][-1]["parameters"]["CLK_POLARITY"] = 1
    value["cells"][-1]["ports"]["CLK"] = [2, 3]
    reject(value, "CELL_WIDTH_MISMATCH", ".CLK")


def test_combinational_self_loop_is_rejected():
    value = design()
    value["cells"][0]["ports"]["A"] = [4]
    reject(value, "COMBINATIONAL_CYCLE", "c0")


def test_combinational_cycle_is_rejected_even_when_registers_exist_elsewhere():
    value = design()
    value["cells"][0]["ports"]["A"] = [5]
    value["cells"].extend([cell("$not", identity="inverter", pins={"A": [4], "Y": [5]},
                               parameters={"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1}),
                            register(output=6)])
    reject(value, "COMBINATIONAL_CYCLE", "inverter")


def test_long_reverse_ordered_dag_is_valid_without_recursive_graph_traversal():
    value = design()
    count = 1100
    value["cells"] = [cell("$not", identity=f"n{index}", pins={"A": [2 if index == 0 else index + 9], "Y": [index + 10]},
                           parameters={"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1}) for index in range(count)]
    value["cells"].reverse()
    value["ports"][-1]["bits"] = [count + 9]
    value["nets"][-1]["bits"] = [count + 9]
    validate_design(value)
