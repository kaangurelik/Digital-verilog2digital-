"""Check native scalar truth tables independently of the mapping port inventory."""
from copy import deepcopy
from itertools import product

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.mapping import MappingError, map_scalar, validate_mapped


def port(name, bit, direction="input"):
    return {"name": name, "bits": [bit], "direction": direction, "offset": 0,
            "upto": False, "signed": False, "source": f"fixture.v:{name}"}


def operation(kind="$and"):
    unary = kind in {"$not", "$logic_not", "$reduce_xnor", "$pos", "$reduce_and", "$reduce_or", "$reduce_xor", "$reduce_bool", "$neg"}
    pins = {"A": [2], "Y": [5]} if unary else {"A": [2], "B": [3], "Y": [5]}
    if kind == "$mux":
        pins["S"] = [4]
        parameters = {"WIDTH": 1}
    else:
        parameters = {"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1}
        if not unary:
            parameters.update(B_SIGNED=0, B_WIDTH=1)
    return {"id": "c0", "type": kind, "native_name": "native_c0", "parameters": parameters,
            "ports": pins, "port_directions": {pin: "output" if pin == "Y" else "input" for pin in pins},
            "source": "fixture.v:2", "hierarchy": "top c0"}


def design(kind="$and"):
    return {"schema_version": 1, "top": "top", "ports": [port("a", 2), port("b", 3), port("s", 4), port("y", 5, "output")],
            "nets": [{"name": "misleading_alias", "bits": [2], "offset": 0, "upto": False,
                      "signed": False, "source": "fixture.v:1", "hidden": False}],
            "cells": [operation(kind)], "origins": [], "diagnostics": [],
            "tools": {"yosys": "fixture", "pyslang": "fixture"},
            "configuration": {"sources": ["fixture.v"], "include_dirs": [], "defines": {}, "parameters": {}}}


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as worker:
        yield worker


def simulate_mapping(mapped, adapter, directory, vectors):
    """Use named tunnels to test native mapping independently of wire routing.

    Expected behavior is independent of the mapper's operation and pin tables.
    """
    instances, wires = deepcopy(mapped["instances"]), []
    descriptors = {item["id"]: item for item in mapped["descriptors"]}
    offsets = {}
    for index, instance in enumerate(instances):
        instance["position"] = {"x": 400 * index, "y": 200}
        offsets[instance["id"]] = instance["position"]
    for net in mapped["nets"]:
        if len(net["endpoints"]) == 1:
            continue
        for index, endpoint in enumerate(net["endpoints"]):
            descriptor = descriptors[endpoint["instance_id"]]
            pin = next(pin for pin in descriptor["pins"] if pin["pin"] == endpoint["pin"] and pin["direction"] == endpoint["direction"])
            base = offsets[endpoint["instance_id"]]
            start = {axis: base[axis] + pin["position"][axis] for axis in ("x", "y")}
            end = {axis: start[axis] + 60 * pin["escape"][axis] for axis in ("x", "y")}
            instances.append({"id": f"t_{net['id']}_{index}", "type": "Tunnel", "attributes": {"NetName": net["id"]},
                              "position": end, "rotation": 0})
            wires.append({"from": start, "to": end})
    emitted = adapter.call("circuit.emit", {"circuit": {"schema_version": 1, "instances": instances, "wires": wires},
                                             "path": str(directory / "mapping.dig")})
    paths = {key: emitted[key] for key in ("path", "sidecar_path")}
    inspection = adapter.call("circuit.inspect", paths)
    assert inspection["model"]["valid"], inspection["model"]["diagnostics"]
    samples = adapter.call("circuit.simulate", {**paths, "stimuli": [
        {"values": {port["instance_id"]: hex(vector[port["port"]]) for port in mapped["inputs"]}} for vector in vectors
    ]})["samples"]
    assert all(all(int(value, 0) == 0 for value in sample["high_z"].values()) for sample in samples)
    return [{port["port"]: int(sample["values"][port["instance_id"]], 0) for port in mapped["outputs"]} for sample in samples]


@pytest.mark.parametrize("kind,truth", [
    ("$and", lambda a,b,s: a & b), ("$or", lambda a,b,s: a | b),
    ("$xor", lambda a,b,s: a ^ b), ("$xnor", lambda a,b,s: 1 ^ a ^ b),
    ("$logic_and", lambda a,b,s: int(bool(a) and bool(b))), ("$logic_or", lambda a,b,s: int(bool(a) or bool(b))),
    ("$not", lambda a,b,s: 1-a), ("$logic_not", lambda a,b,s: int(not a)),
    ("$reduce_xnor", lambda a,b,s: 1-a), ("$pos", lambda a,b,s: a),
    ("$reduce_and", lambda a,b,s: a), ("$reduce_or", lambda a,b,s: a),
    ("$reduce_xor", lambda a,b,s: a), ("$reduce_bool", lambda a,b,s: a),
    ("$mux", lambda a,b,s: b if s else a),
])
def test_native_scalar_truth_tables(adapter, tmp_path, kind, truth):
    source = design(kind)
    before = deepcopy(source)
    mapped = map_scalar(source, adapter)
    assert source == before
    vectors = [dict(zip(("a", "b", "s"), values)) for values in product((0, 1), repeat=3)]
    actual = simulate_mapping(mapped, adapter, tmp_path, vectors)
    assert actual == [{"y": truth(**vector)} for vector in vectors]


def test_alias_fanout_constants_and_unused_sources_preserve_bit_identity(adapter, tmp_path):
    source = design()
    source["cells"] = []
    source["ports"] = [port("input", 2), port("unused", 3), port("z", 2, "output"),
                       port("other_alias", 2, "output"), port("lo", "0", "output"), port("hi", "1", "output")]
    mapped = map_scalar(source, adapter)
    assert sum(instance["type"] == "Const" for instance in mapped["instances"]) == 2
    assert len(next(net for net in mapped["nets"] if net["bit"] == 3)["endpoints"]) == 1
    assert len(next(net for net in mapped["nets"] if net["bit"] == 2)["endpoints"]) == 3
    assert simulate_mapping(mapped, adapter, tmp_path, [{"input": 0, "unused": 1}, {"input": 1, "unused": 0}]) == [
        {"z": 0, "other_alias": 0, "lo": 0, "hi": 1}, {"z": 1, "other_alias": 1, "lo": 0, "hi": 1}]


def test_constants_on_gate_pins_and_tied_inputs(adapter, tmp_path):
    source = design("$xor")
    source["cells"][0]["ports"]["B"] = ["1"]
    mapped = map_scalar(source, adapter)
    assert simulate_mapping(mapped, adapter, tmp_path, [{"a": 0, "b": 1, "s": 0}, {"a": 1, "b": 0, "s": 1}]) == [{"y": 1}, {"y": 0}]


def test_mapping_is_stable_under_input_record_reordering(adapter):
    source = design()
    first = map_scalar(source, adapter)
    for collection in ("ports", "nets", "cells"):
        source[collection].reverse()
    assert map_scalar(source, adapter) == first
    assert all(item["position"] == {"x": 0, "y": 0} for item in first["instances"])


class NoNativeCalls:
    def call(self, *_):
        pytest.fail("Unsupported designs must be rejected before native component calls")


@pytest.mark.parametrize("change,code", [
    (lambda d: d["ports"].pop(), "UNSUPPORTED_MAPPING_INTERFACE"),
    (lambda d: d["nets"][0].update(bits=[2, 3]), "UNSUPPORTED_MAPPING_WIDTH"),
    (lambda d: d["cells"][0].update(type="$add"), "UNSUPPORTED_MAPPING_CELL"),
    (lambda d: d["cells"][0]["ports"].update(A=[999]), "UNDRIVEN_NET"),
    (lambda d: d["ports"].extend(port(f"more{i}", 20+i) for i in range(10)), "MAPPING_RESOURCE_LIMIT"),
    (lambda d: d["ports"].extend(port(f"alias{i}", 5, "output") for i in range(60)), "MAPPING_RESOURCE_LIMIT"),
])
def test_rejection_precedes_native_work(change, code):
    source = design()
    change(source)
    with pytest.raises(MappingError) as failure:
        map_scalar(source, NoNativeCalls())
    assert failure.value.code == code


def test_sequential_mapping_is_explicitly_deferred():
    source = design()
    source["cells"][0].update(type="$dff", parameters={"WIDTH": 1, "CLK_POLARITY": 1},
                              ports={"CLK": [2], "D": [3], "Q": [5]}, port_directions={"CLK": "input", "D": "input", "Q": "output"})
    with pytest.raises(MappingError) as failure:
        map_scalar(source, NoNativeCalls())
    assert failure.value.code == "UNSUPPORTED_MAPPING_SEQUENTIAL"


@pytest.mark.parametrize("change,code", [
    (lambda m: m["nets"][0]["endpoints"].append(deepcopy(m["nets"][0]["endpoints"][0])), "INVALID_MAPPING_ENDPOINT"),
    (lambda m: m["nets"][0]["endpoints"].pop(0), "INCOMPLETE_MAPPING"),
    (lambda m: m["nets"][0]["endpoints"].pop(), "INVALID_MAPPING_DRIVERS"),
    (lambda m: m["nets"][0]["endpoints"][0].update(pin="nonexistent"), "INVALID_MAPPING_ENDPOINT"),
    (lambda m: m["descriptors"][0]["pins"][0].update(width=2), "INVALID_MAPPING"),
    (lambda m: m["instances"][0]["attributes"].update(Bits=2), "INVALID_MAPPING_DESCRIPTOR"),
    (lambda m: m["outputs"][0].update(port="wrong_port"), "INVALID_MAPPING_PORT"),
    (lambda m: m["origins"].pop(), "INVALID_MAPPING_ORIGIN"),
])
def test_mapped_artifact_mutations_are_rejected(adapter, change, code):
    mapped = map_scalar(design(), adapter)
    change(mapped)
    with pytest.raises(MappingError) as failure:
        validate_mapped(mapped)
    assert failure.value.code == code


def test_no_input_constant_design_has_one_native_constant_driver(adapter, tmp_path):
    source = design()
    source["cells"] = []
    source["ports"] = [port("one", "1", "output"), port("alias", "1", "output")]
    mapped = map_scalar(source, adapter)
    assert len(mapped["instances"]) == 3
    assert mapped["inputs"] == []
    assert simulate_mapping(mapped, adapter, tmp_path, [{}]) == [{"one": 1, "alias": 1}]


@pytest.mark.parametrize("mutation,code", [
    (lambda result: result["pins"][0].update(pin="unknown_pin"), "NATIVE_PIN_MISMATCH"),
    (lambda result: result["pins"][0].update(width=None), "NATIVE_PIN_WIDTH"),
    (lambda result: result["attributes"].update(Bits=2), "NATIVE_ATTRIBUTE_MISMATCH"),
    (lambda result: result.update(pins=None), "NATIVE_PIN_MISMATCH"),
])
def test_native_inventory_changes_are_rejected(adapter, mutation, code):
    class ChangedAdapter:
        def call(self, method, params):
            response = adapter.call(method, params)
            mutation(response)
            return response
    with pytest.raises(MappingError) as failure:
        map_scalar(design(), ChangedAdapter())
    assert failure.value.code == code


def test_mapped_feedback_is_rejected_even_with_one_driver_per_net(adapter):
    mapped = map_scalar(design(), adapter)
    source_net = next(net for net in mapped["nets"] if net["bit"] == 2)
    output_net = next(net for net in mapped["nets"] if net["bit"] == 5)
    sink = next(endpoint for endpoint in source_net["endpoints"] if endpoint["direction"] == "input")
    source_net["endpoints"].remove(sink)
    output_net["endpoints"].append(sink)
    with pytest.raises(MappingError) as failure:
        validate_mapped(mapped)
    assert failure.value.code == "INVALID_MAPPING_CYCLE"
