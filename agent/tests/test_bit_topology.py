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


def shared_bus():
    """One 2-bit bus joins a driver, two equal readers and a splitter for a reversed reader."""
    def part(identifier, kind, pins, bindings=None):
        item = {"id": identifier, "type": kind, "attributes": {},
                "pins": [{"direction": direction, "pin": pin, "width": width} for direction, pin, width in pins]}
        if bindings is not None:
            item["splitter_bindings"] = bindings
        return item

    def splitter(identifier, wide, narrow):
        return part(identifier, "Splitter", [(wide, "0-1", 2), (narrow, "0", 1), (narrow, "1", 1)],
                    [{"direction": wide, "pin": "0-1", "offset": 0, "width": 2},
                     {"direction": narrow, "pin": "0", "offset": 0, "width": 1},
                     {"direction": narrow, "pin": "1", "offset": 1, "width": 1}])
    instances = [part("a", "In", [("output", "out", 2)]), part("y", "Out", [("input", "in", 2)]),
                 part("z", "Out", [("input", "in", 2)]), part("r", "Out", [("input", "in", 2)]),
                 splitter("split", "input", "output"), splitter("merge", "output", "input")]
    nets = [{"id": "bus", "width": 2, "bits": [2, 3], "endpoints": [endpoint("a", "output", "out"), endpoint("y", "input", "in"),
                                                                     endpoint("z", "input", "in"), endpoint("split", "input", "0-1")]},
            {"id": "reversed", "width": 2, "bits": [3, 2], "endpoints": [endpoint("merge", "output", "0-1"), endpoint("r", "input", "in")]},
            {"id": "b2", "width": 1, "bits": [2], "endpoints": [endpoint("split", "output", "0"), endpoint("merge", "input", "1")]},
            {"id": "b3", "width": 1, "bits": [3], "endpoints": [endpoint("split", "output", "1"), endpoint("merge", "input", "0")]}]
    return {"nets": nets}, {"instances": instances, "nets": deepcopy(nets), "unconnected": [], "topology_available": True}


def test_shared_bus_joins_equal_vectors_and_splits_regrouped_bits():
    mapped, native = shared_bus()
    result = compare_bit_topology(mapped, native)
    assert result["valid"], result
    assert result["electrical_bits"] == 2


@pytest.mark.parametrize("mutation,code", [
    ("swapped", "bit_connection_mismatch"), ("shifted", "invalid_bit_topology"),
    ("width", "invalid_bit_topology"), ("extra_driver", "bit_driver_count"),
    ("missing_driver", "bit_driver_count"), ("constant", "bit_constant_mismatch")])
def test_shared_bus_bit_disagreements_are_rejected(mutation, code):
    mapped, native = shared_bus()
    if mutation == "swapped":
        # A reversed reader wired straight onto the shared bus disagrees at both positions.
        native["nets"][1]["endpoints"].remove(endpoint("r", "input", "in"))
        native["nets"][0]["endpoints"].append(endpoint("r", "input", "in"))
    elif mutation == "shifted":
        for binding in native["instances"][4]["splitter_bindings"][1:]:
            binding["offset"] += 1
    elif mutation == "width":
        # The expected 3-bit reader has its own net; native wiring joins it to the 2-bit bus.
        native["instances"].append({"id": "w", "type": "Out", "attributes": {}, "pins": [{"direction": "input", "pin": "in", "width": 3}]})
        mapped["nets"].append({"id": "w", "width": 3, "bits": [5, 6, 7], "endpoints": [endpoint("w", "input", "in")]})
        native["nets"][0]["endpoints"].append(endpoint("w", "input", "in"))
    elif mutation == "extra_driver":
        native["instances"].append({"id": "b", "type": "In", "attributes": {}, "pins": [{"direction": "output", "pin": "out", "width": 2}]})
        mapped["nets"][0]["endpoints"].append(endpoint("b", "output", "out"))
        native["nets"][0]["endpoints"].append(endpoint("b", "output", "out"))
    elif mutation == "missing_driver":
        native["nets"][0]["endpoints"].remove(endpoint("a", "output", "out"))
        native["unconnected"].append(endpoint("a", "output", "out"))
    else:
        native["instances"][0].update(type="Const", attributes={"Bits": 2, "Value": "0x2"})
        for net in mapped["nets"]:
            net["bits"] = [str(bit - 2) for bit in net["bits"]]
        assert compare_bit_topology(mapped, native)["valid"]
        native["instances"][0]["attributes"]["Value"] = "0x1"
    result = compare_bit_topology(mapped, native)
    assert not result["valid"]
    assert code in {error["code"] for error in result["diagnostics"]}, result


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
