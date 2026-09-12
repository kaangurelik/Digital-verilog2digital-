"""Independently declared local electrical intent for hierarchy oracle tests."""
from collections import defaultdict
from copy import deepcopy

from test_scalar_mapping import design, port


def primitive(identifier, kind, pins, **attributes):
    instance = {"id": identifier, "type": kind, "attributes": {"Bits": 1, **attributes},
                "position": {"x": 0, "y": 0}, "rotation": 0}
    descriptor = {**deepcopy(instance), "pins": [{"direction": direction, "pin": pin, "width": 1,
                   "position": {"x": 0, "y": index * 20}, "escape": {"x": 1 if direction == "output" else -1, "y": 0}}
                  for index, (direction, pin) in enumerate(pins)],
                  "bounds": {"min": {"x": -10, "y": -10}, "max": {"x": 10, "y": 10}},
                  "body_bounds": {"min": {"x": -10, "y": -10}, "max": {"x": 10, "y": 10}}}
    return instance, descriptor


def local_mapping(ports, children=(), flipflop=False):
    instances, descriptors, inputs, outputs, bindings = [], [], [], [], []
    groups = defaultdict(list)

    def add(identifier, kind, pins, **attributes):
        instance, descriptor = primitive(identifier, kind, pins, **attributes)
        instances.append(instance)
        descriptors.append(descriptor)
        for (direction, pin), bit in pins.items():
            endpoint = {"instance_id": identifier, "direction": direction, "pin": pin}
            groups[bit].append(endpoint)
            bindings.append({**endpoint, "width": 1, "bits": [bit]})

    for item in ports:
        incoming = item["direction"] == "input"
        collection = inputs if incoming else outputs
        identifier = f"{'in' if incoming else 'out'}{len(collection)}"
        add(identifier, "In" if incoming else "Out", {("output", "out") if incoming else ("input", "in"): item["bits"][0]}, Label=item["name"])
        collection.append({"port": item["name"], "instance_id": identifier, "width": 1})
    for child in children:
        pins = {(child["port_directions"][name], name): bits[0] for name, bits in child["ports"].items()}
        add(child["id"], "child.dig", pins)
    registers = []
    if flipflop:
        add("g0", "D_FF", {("input", "C"): 2, ("input", "D"): 3, ("output", "Q"): 4, ("output", "~Q"): 5}, Default="0x0")
        registers.append({"instance_id": "g0", "cell_id": "c0", "width": 1, "d_bits": [3], "q_bits": [4]})
    return {"schema_version": 1, "profile": "hierarchy-v1", "top": "fixture",
            "instances": instances, "descriptors": descriptors, "inputs": inputs, "outputs": outputs,
            "nets": [{"id": f"n{bit}", "width": 1, "bits": [bit], "endpoints": endpoints} for bit, endpoints in groups.items()],
            "pin_bindings": bindings, "registers": registers,
            "origins": [{"instance_id": item["id"], "kind": "fixture"} for item in instances]}


def alias_hierarchy(repeated=False):
    top_ports = [port("a", 2), port("y", 3, "output")]
    children = [{"id": "u0", "module_id": "leaf", "native_name": "first.child", "ports": {"a": [2], "y": [3]},
                 "port_directions": {"a": "input", "y": "output"}}]
    if repeated:
        top_ports.extend([port("b", 4), port("z", 5, "output")])
        children.append({"id": "u1", "module_id": "leaf", "native_name": "second.child", "ports": {"a": [4], "y": [5]},
                         "port_directions": {"a": "input", "y": "output"}})
    child_ports = [port("a", 2), port("y", 2, "output")]
    modules = [{"id": "top", "ports": top_ports, "cells": [], "children": children},
               {"id": "leaf", "ports": child_ports, "cells": [], "children": []}]
    hierarchy = {"schema_version": 1, "top_module_id": "top", "modules": modules,
                 "expanded_design": {"top": "top", "cells": []},
                 "occurrences": [{"path": [], "module_id": "top", "cell_map": {}},
                                 *[{"path": [child["id"]], "module_id": "leaf", "cell_map": {}} for child in children]]}
    mappings = {"top": local_mapping(top_ports, children), "leaf": local_mapping(child_ports)}
    descriptors = deepcopy([item for item in mappings["top"]["descriptors"] if item["type"] in {"In", "Out"}])
    groups = [[{"instance_id": "in0", "direction": "output", "pin": "out"}, {"instance_id": "out0", "direction": "input", "pin": "in"}]]
    if repeated:
        groups.append([{"instance_id": "in1", "direction": "output", "pin": "out"}, {"instance_id": "out1", "direction": "input", "pin": "in"}])
    inspected = {"instances": descriptors, "nets": [{"id": f"native{index}", "endpoints": group} for index, group in enumerate(groups)],
                 "unconnected": [], "topology_available": True, "model": {"valid": True, "diagnostics": []}}
    return hierarchy, mappings, inspected


def register_hierarchy():
    child_ports = [port("clk", 2), port("d", 3), port("q", 4, "output")]
    top_ports = [port("clk", 2), port("a", 3), port("b", 4), port("ya", 5, "output"), port("yb", 6, "output")]
    children = [{"id": f"u{index}", "module_id": "leaf", "native_name": name,
                 "ports": {"clk": [2], "d": [3 + index], "q": [5 + index]},
                 "port_directions": {"clk": "input", "d": "input", "q": "output"}}
                for index, name in enumerate(("first.child", "second.child"))]
    cell = {"id": "c0", "type": "$dff", "native_name": "q", "hierarchy": "q", "source": "fixture.v",
            "parameters": {"WIDTH": 1, "CLK_POLARITY": 1}, "ports": {"CLK": [2], "D": [3], "Q": [4]},
            "port_directions": {"CLK": "input", "D": "input", "Q": "output"}}
    flat = design()
    flat["ports"] = deepcopy(top_ports)
    flat["cells"] = [{**deepcopy(cell), "id": f"c{index}", "native_name": f"u{index}.q",
                      "ports": {"CLK": [2], "D": [3 + index], "Q": [5 + index]}} for index in range(2)]
    hierarchy = {"schema_version": 1, "top_module_id": "top", "expanded_design": flat,
                 "modules": [{"id": "top", "ports": top_ports, "children": children, "cells": []},
                             {"id": "leaf", "ports": child_ports, "children": [], "cells": [cell]}],
                 "occurrences": [{"path": [], "module_id": "top", "cell_map": {}},
                                 *[{"path": [f"u{index}"], "module_id": "leaf", "cell_map": {"c0": f"c{index}"}} for index in range(2)]]}
    return hierarchy, {"top": local_mapping(top_ports, children), "leaf": local_mapping(child_ports, flipflop=True)}
