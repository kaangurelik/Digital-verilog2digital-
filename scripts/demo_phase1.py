"""Emit, reload, inspect, render, and simulate a hand-specified AND circuit."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from toolchain import ROOT

sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.adapter import Adapter
from verilog2digital.validation.topology import compare_components, compare_topology


def main() -> None:
    destination = ROOT / ".runs/phase1-demo"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "validation.json").unlink(missing_ok=True)
    instances = []
    descriptors = []
    wires = []
    expected_nets = []

    with Adapter() as adapter:
        def add(identifier, kind, position, attributes):
            instance = {"id": identifier, "type": kind, "attributes": attributes,
                        "position": position, "rotation": 0}
            description = adapter.call("component.describe", {key: value for key, value in instance.items() if key != "id"})
            description["id"] = identifier
            instances.append(instance)
            descriptors.append(description)
            return description

        def endpoint(instance, pin):
            return {"instance_id": instance, "direction": pin["direction"], "pin": pin["pin"]}

        gate = add("gate", "And", {"x": 160, "y": 40}, {"Inputs": 2, "Bits": 1})
        inputs = [pin for pin in gate["pins"] if pin["direction"] == "input"]
        outputs = [pin for pin in gate["pins"] if pin["direction"] == "output"]
        if len(inputs) != 2 or len(outputs) != 1:
            raise RuntimeError("Unexpected native AND pin inventory")
        for name, pin in zip(("a", "b"), inputs):
            port = add(name, "In", {"x": 0, "y": pin["position"]["y"]}, {"Label": name, "Bits": 1})
            source = next(p for p in port["pins"] if p["direction"] == "output")
            wires.append({"from": source["position"], "to": pin["position"], "net_id": name})
            expected_nets.append([endpoint(name, source), endpoint("gate", pin)])
        pin = outputs[0]
        port = add("y", "Out", {"x": pin["position"]["x"] + 160, "y": pin["position"]["y"]},
                   {"Label": "y", "Bits": 1})
        sink = next(p for p in port["pins"] if p["direction"] == "input")
        wires.append({"from": pin["position"], "to": sink["position"], "net_id": "y"})
        expected_nets.append([endpoint("gate", pin), endpoint("y", sink)])
        specification = {"schema_version": 1, "instances": instances, "wires": wires}
        (destination / "candidate.json").write_text(json.dumps(specification, indent=2), encoding="utf-8")
        (destination / "expected-nets.json").write_text(json.dumps(expected_nets, indent=2), encoding="utf-8")
        emitted = adapter.call("circuit.emit", {"circuit": specification, "path": str(destination / "and-gate.dig")})
        paths = {key: emitted[key] for key in ("path", "sidecar_path")}
        inspection = adapter.call("circuit.inspect", paths)
        topology = compare_topology(expected_nets, inspection)
        components = compare_components(descriptors, inspection["instances"])
        if not topology["valid"] or not components["valid"] or not inspection["model"]["valid"]:
            raise RuntimeError(json.dumps({"topology": topology, "components": components, "model": inspection["model"]}))
        stimuli = [{"values": {"a": hex(a), "b": hex(b)}} for a in range(2) for b in range(2)]
        simulation = adapter.call("circuit.simulate", {**paths, "stimuli": stimuli})
        expected = [0, 0, 0, 1]
        observed = [int(sample["values"]["y"], 16) for sample in simulation["samples"]]
        binary = all(int(sample["high_z"]["y"], 16) == 0 for sample in simulation["samples"])
        if observed != expected or not binary:
            raise RuntimeError(f"Truth table mismatch: expected {expected}, observed {observed}, binary={binary}")
        for format in ("svg", "png"):
            adapter.call("circuit.render", {"path": paths["path"], "format": format,
                                            "output_path": str(destination / f"and-gate.{format}"), "scale": 2})
        report = {"schema_version": 1, "valid": True, "artifacts": emitted,
                  "topology": topology, "components": components, "model": inspection["model"],
                  "behavior": {"coverage": "exhaustive", "binary_only": True, "input_vectors": 4,
                               "reference": "independent hand-specified AND truth table", "observed": observed}}
        (destination / "inspection.json").write_text(json.dumps(inspection, indent=2), encoding="utf-8")
        (destination / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Native adapter demonstration passed: {destination}")


if __name__ == "__main__":
    main()
