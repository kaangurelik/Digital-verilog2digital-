"""Mutations of native splitter offsets, bus widths, and bit drivers."""
from copy import deepcopy

import pytest

from verilog2digital.validation.bits import compare_bit_topology


def endpoint(identifier, direction, pin):
    return {"instance_id": identifier, "direction": direction, "pin": pin}


def split_bus():
    instances = [
        {"id": "a", "type": "In", "attributes": {},
         "pins": [{"direction": "output", "pin": "out", "width": 2}]},
        {"id": "split", "type": "Splitter", "attributes": {},
         "pins": [{"direction": "input", "pin": "0-1", "width": 2},
                  {"direction": "output", "pin": "0", "width": 1},
                  {"direction": "output", "pin": "1", "width": 1}],
         "splitter_bindings": [{"direction": "input", "pin": "0-1", "offset": 0, "width": 2},
                               {"direction": "output", "pin": "0", "offset": 0, "width": 1},
                               {"direction": "output", "pin": "1", "offset": 1, "width": 1}]},
        *[{"id": f"y{index}", "type": "Out", "attributes": {},
           "pins": [{"direction": "input", "pin": "in", "width": 1}]} for index in range(2)],
    ]
    nets = [{"id": "bus", "width": 2, "bits": [2, 3],
             "endpoints": [endpoint("a", "output", "out"), endpoint("split", "input", "0-1")]},
            *[{"id": f"bit{index}", "width": 1, "bits": [2 + index],
               "endpoints": [endpoint("split", "output", str(index)), endpoint(f"y{index}", "input", "in")]}
              for index in range(2)]]
    return {"nets": nets}, {"instances": instances, "nets": deepcopy(nets), "unconnected": [], "topology_available": True}


def test_split_bus_preserves_lsb_identities():
    mapped, native = split_bus()
    result = compare_bit_topology(mapped, native)
    assert result["valid"], result
    assert result["electrical_bits"] == 2 and result["pin_bits"] == 8


def test_swapped_splitter_offsets_fail_even_when_pin_partition_is_unchanged():
    mapped, native = split_bus()
    bindings = native["instances"][1]["splitter_bindings"]
    bindings[1]["offset"], bindings[2]["offset"] = 1, 0
    result = compare_bit_topology(mapped, native)
    assert not result["valid"]
    assert "bit_connection_mismatch" in {error["code"] for error in result["diagnostics"]}


@pytest.mark.parametrize("mutation", ["width", "missing_binding", "duplicate_binding", "missing_net", "unknown_endpoint"])
def test_incomplete_or_inconsistent_native_data_cannot_pass(mutation):
    mapped, native = split_bus()
    if mutation == "width":
        native["instances"][0]["pins"][0]["width"] = 1
    elif mutation == "missing_binding":
        native["instances"][1]["splitter_bindings"].pop()
    elif mutation == "duplicate_binding":
        native["instances"][1]["splitter_bindings"].append(deepcopy(native["instances"][1]["splitter_bindings"][0]))
    elif mutation == "missing_net":
        native["nets"].pop()
    else:
        native["nets"][0]["endpoints"].append(endpoint("unknown", "input", "in"))
    assert not compare_bit_topology(mapped, native)["valid"]


def test_disconnected_same_bit_and_missing_driver_are_detected():
    mapped, native = split_bus()
    disconnected = native["nets"][1]["endpoints"].pop()
    native["unconnected"].append(disconnected)
    result = compare_bit_topology(mapped, native)
    codes = {error["code"] for error in result["diagnostics"]}
    assert {"bit_disconnected", "bit_driver_count"} <= codes


def test_constant_value_is_checked_at_each_bit_index():
    mapped, native = split_bus()
    native["instances"][0].update(type="Const", attributes={"Bits": 2, "Value": "0x2"})
    for net in mapped["nets"]:
        net["bits"] = [str(bit - 2) for bit in net["bits"]]
    assert compare_bit_topology(mapped, native)["valid"]
    native["instances"][0]["attributes"]["Value"] = "0x1"
    result = compare_bit_topology(mapped, native)
    assert not result["valid"]
    assert "bit_constant_mismatch" in {error["code"] for error in result["diagnostics"]}


def test_two_nonconstant_drivers_are_rejected():
    mapped, native = split_bus()
    native["instances"].append({"id": "b", "type": "In", "attributes": {},
                                "pins": [{"direction": "output", "pin": "out", "width": 2}]})
    pin = endpoint("b", "output", "out")
    mapped["nets"][0]["endpoints"].append(pin)
    native["nets"][0]["endpoints"].append(pin)
    result = compare_bit_topology(mapped, native)
    assert not result["valid"]
    assert "bit_driver_count" in {error["code"] for error in result["diagnostics"]}
