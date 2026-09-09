"""Declare electrical expectations independently of emitted Digital wires.

Query native geometry before emission; specify expected net membership here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass
class CircuitCase:
    name: str
    circuit: dict
    expected_nets: list[list[dict]]
    descriptors: list[dict]
    topology_valid: bool = True
    model_valid: bool = True
    topology_available: bool = True


def point(x: int, y: int) -> dict:
    return {"x": x, "y": y}


class FixtureBuilder:
    def __init__(self, adapter):
        self.adapter = adapter
        self.instances: dict[str, dict] = {}
        self.descriptors: dict[str, dict] = {}
        self.wires: list[dict] = []
        self.nets: list[list[dict]] = []

    def describe(self, identifier: str) -> dict:
        instance = self.instances[identifier]
        descriptor = self.adapter.call(
            "component.describe", {key: value for key, value in instance.items() if key != "id"}
        )
        descriptor = deepcopy(descriptor)
        descriptor["id"] = identifier
        self.descriptors[identifier] = descriptor
        return descriptor

    def add(self, identifier: str, kind: str, *, at=(0, 0), attributes=None, rotation=0):
        assert identifier not in self.instances
        attributes = dict(attributes or {})
        if kind in ("In", "Out"):
            attributes.setdefault("Label", identifier)
            attributes.setdefault("Bits", 1)
        self.instances[identifier] = {
            "id": identifier,
            "type": kind,
            "attributes": attributes,
            "position": point(*at),
            "rotation": rotation,
        }
        self.describe(identifier)

    def pin(self, identifier: str, direction: str, index: int = 0) -> dict:
        return [p for p in self.descriptors[identifier]["pins"] if p["direction"] == direction][index]

    def endpoint(self, identifier: str, direction: str, index: int = 0) -> dict:
        pin = self.pin(identifier, direction, index)
        return {"instance_id": identifier, "direction": pin["direction"], "pin": pin["pin"]}

    def at_pin(self, identifier: str, kind: str, at: dict, direction: str, *, index=0,
               attributes=None, rotation=0):
        self.add(identifier, kind, attributes=attributes, rotation=rotation)
        local = self.pin(identifier, direction, index)["position"]
        self.instances[identifier]["position"] = point(at["x"] - local["x"], at["y"] - local["y"])
        self.describe(identifier)
        assert self.pin(identifier, direction, index)["position"] == at

    def wire(self, start: dict, end: dict, net_id: str | None = None):
        assert start != end, "Fixtures must not accidentally create zero-length wires"
        assert start["x"] == end["x"] or start["y"] == end["y"], "Fixture wires must be orthogonal"
        wire = {"from": dict(start), "to": dict(end)}
        if net_id is not None:
            wire["net_id"] = net_id
        self.wires.append(wire)

    def expect(self, *endpoints: dict):
        self.nets.append(list(endpoints))

    def finish(self, name: str, *, topology_valid=True, model_valid=True, topology_available=True) -> CircuitCase:
        declared = [
            (endpoint["instance_id"], endpoint["direction"], endpoint["pin"])
            for net in self.nets for endpoint in net
        ]
        available = {
            (identifier, pin["direction"], pin["pin"])
            for identifier, descriptor in self.descriptors.items() for pin in descriptor["pins"]
        }
        assert len(declared) == len(set(declared)), "A fixture endpoint belongs to two expected nets"
        assert set(declared) == available, "Declare every fixture pin, including isolated pins"
        return CircuitCase(name, {"schema_version": 1, "instances": list(self.instances.values()),
                                  "wires": self.wires}, self.nets, list(self.descriptors.values()),
                           topology_valid, model_valid, topology_available)


def direct_wire(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(200, 0), "input")
    b.wire(point(0, 0), point(200, 0), "a_to_y")
    b.expect(b.endpoint("a", "output"), b.endpoint("y", "input"))
    return b.finish("direct_wire")


def and_gate(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.add("gate", "And", at=(140, 0), attributes={"Bits": 1, "Inputs": 2})
    for index, identifier in enumerate(("a", "b")):
        destination = b.pin("gate", "input", index)["position"]
        source = point(destination["x"] - 140, destination["y"])
        b.at_pin(identifier, "In", source, "output")
        b.wire(source, destination, identifier)
        b.expect(b.endpoint(identifier, "output"), b.endpoint("gate", "input", index))
    source = b.pin("gate", "output")["position"]
    destination = point(source["x"] + 140, source["y"])
    b.at_pin("y", "Out", destination, "input")
    b.wire(source, destination, "result")
    b.expect(b.endpoint("gate", "output"), b.endpoint("y", "input"))
    return b.finish("and_gate")


def fanout(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(200, 0), "input")
    b.at_pin("z", "Out", point(200, 100), "input")
    b.wire(point(0, 0), point(200, 0))
    b.wire(point(0, 0), point(0, 100))
    b.wire(point(0, 100), point(200, 100))
    b.expect(b.endpoint("a", "output"), b.endpoint("y", "input"), b.endpoint("z", "input"))
    return b.finish("fanout")


def t_junction(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(200, 0), "input")
    b.at_pin("z", "Out", point(100, 100), "input", rotation=90)
    # The branch endpoint meets the interior of an unsplit horizontal segment.
    b.wire(point(0, 0), point(200, 0))
    b.wire(point(100, 0), point(100, 100))
    b.expect(b.endpoint("a", "output"), b.endpoint("y", "input"), b.endpoint("z", "input"))
    return b.finish("t_junction")


def nonjoining_crossing(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(200, 0), "input")
    b.at_pin("b", "In", point(100, -100), "output", rotation=90)
    b.at_pin("z", "Out", point(100, 100), "input", rotation=90)
    b.wire(point(0, 0), point(200, 0), "horizontal")
    b.wire(point(100, -100), point(100, 100), "vertical")
    b.expect(b.endpoint("a", "output"), b.endpoint("y", "input"))
    b.expect(b.endpoint("b", "output"), b.endpoint("z", "input"))
    return b.finish("nonjoining_crossing")


def intentional_join(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(200, 0), "input")
    b.at_pin("z", "Out", point(100, 100), "input", rotation=90)
    b.at_pin("w", "Out", point(100, -100), "input", rotation=270)
    center = point(100, 0)
    for outer in (point(0, 0), point(200, 0), point(100, -100), point(100, 100)):
        b.wire(center, outer)
    b.expect(b.endpoint("a", "output"), *(b.endpoint(name, "input") for name in ("y", "z", "w")))
    return b.finish("intentional_join")


def accidental_short(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    for source, sink, y in (("a", "y", 0), ("b", "z", 120)):
        b.at_pin(source, "In", point(0, y), "output")
        b.at_pin(sink, "Out", point(200, y), "input")
        b.wire(point(0, y), point(200, y), source)
        b.expect(b.endpoint(source, "output"), b.endpoint(sink, "input"))
    # Deliberately wrong physical bridge; expected nets remain independent.
    b.wire(point(100, 0), point(100, 120), "accidental_bridge")
    return b.finish("accidental_short", topology_valid=False, model_valid=False)


def disconnected_pin(adapter) -> CircuitCase:
    case = direct_wire(adapter)
    case.name = "disconnected_pin"
    case.circuit["wires"][0]["to"] = point(180, 0)
    case.topology_valid = False
    case.model_valid = False
    return case


def coincident_pins(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("y", "Out", point(0, 0), "input")
    b.expect(b.endpoint("a", "output"), b.endpoint("y", "input"))
    return b.finish("coincident_pins")


def unused_output(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("unused", "In", point(0, 0), "output")
    b.expect(b.endpoint("unused", "output"))
    return b.finish("unused_output")


def collinear_pin_endpoint_lost(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("tap", "Out", point(100, 0), "input")
    b.at_pin("y", "Out", point(200, 0), "input")
    # Digital merges these collinear segments. The interior pin is no longer a
    # wire endpoint, so the oracle must reject the originally intended fanout.
    b.wire(point(0, 0), point(100, 0))
    b.wire(point(100, 0), point(200, 0))
    b.expect(b.endpoint("a", "output"), b.endpoint("tap", "input"), b.endpoint("y", "input"))
    return b.finish("collinear_pin_endpoint_lost", topology_valid=False, model_valid=False)


def splitter(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.add("split", "Splitter", at=(160, 0),
          attributes={"Input Splitting": "2", "Output Splitting": "1,1"})
    destination = b.pin("split", "input")["position"]
    source = point(destination["x"] - 160, destination["y"])
    b.at_pin("a", "In", source, "output", attributes={"Bits": 2})
    b.wire(source, destination)
    b.expect(b.endpoint("a", "output"), b.endpoint("split", "input"))
    for index, identifier in enumerate(("lo", "hi")):
        source = b.pin("split", "output", index)["position"]
        destination = point(source["x"] + 160, source["y"])
        b.at_pin(identifier, "Out", destination, "input")
        b.wire(source, destination)
        b.expect(b.endpoint("split", "output", index), b.endpoint(identifier, "input"))
    return b.finish("splitter")


def matching_tunnels(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("a", "In", point(0, 0), "output")
    b.at_pin("left", "Tunnel", point(100, 0), "input", attributes={"NetName": "data"})
    b.at_pin("right", "Tunnel", point(300, 0), "input", attributes={"NetName": "data"})
    b.at_pin("y", "Out", point(400, 0), "input")
    b.wire(point(0, 0), point(100, 0))
    b.wire(point(300, 0), point(400, 0))
    b.expect(b.endpoint("a", "output"), b.endpoint("left", "input"),
             b.endpoint("right", "input"), b.endpoint("y", "input"))
    return b.finish("matching_tunnels")


def distinct_tunnels(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    for source, sink, y, label in (("a", "y", 0, "alpha"), ("b", "z", 120, "beta")):
        b.at_pin(source, "In", point(0, y), "output")
        b.at_pin(source + "_left", "Tunnel", point(100, y), "input", attributes={"NetName": label})
        b.at_pin(source + "_right", "Tunnel", point(300, y), "input", attributes={"NetName": label})
        b.at_pin(sink, "Out", point(400, y), "input")
        b.wire(point(0, y), point(100, y))
        b.wire(point(300, y), point(400, y))
        b.expect(b.endpoint(source, "output"), b.endpoint(source + "_left", "input"),
                 b.endpoint(source + "_right", "input"), b.endpoint(sink, "input"))
    return b.finish("distinct_tunnels")


def named_tunnel_without_net(adapter) -> CircuitCase:
    b = FixtureBuilder(adapter)
    b.at_pin("orphan", "Tunnel", point(0, 0), "input", attributes={"NetName": "orphan_net"})
    b.expect(b.endpoint("orphan", "input"))
    return b.finish("named_tunnel_without_net", topology_valid=False, model_valid=False, topology_available=False)


CASE_FACTORIES = (
    direct_wire, and_gate, fanout, t_junction, nonjoining_crossing, intentional_join,
    accidental_short, disconnected_pin, coincident_pins, unused_output, collinear_pin_endpoint_lost,
    splitter, matching_tunnels, distinct_tunnels, named_tunnel_without_net,
)
