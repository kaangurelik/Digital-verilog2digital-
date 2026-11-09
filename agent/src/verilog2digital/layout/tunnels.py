"""Optional native named tunnels for selected long physical nets."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import re
import time

from ..contracts import validate_circuit
from ..mapping import validate_mapped
from ..validation.topology import endpoint_key
from .geometry import validate_geometry
from .scalar import _translate, LayoutError


def _recorded_plan(nets, mapped, scope):
    """Check recorded tunnels before reusing their names and component IDs."""
    known = {net["id"]: net for net in mapped["nets"]}
    if not isinstance(nets, list) or len(nets) > 32:
        raise LayoutError("INVALID_TUNNEL_PLAN", "A recorded tunnel plan must list at most 32 nets")
    plan, indices = [], set()
    for item in nets:
        net = known.get(item.get("net_id")) if isinstance(item, dict) and isinstance(item.get("net_id"), str) else None
        name = item.get("name") if net is not None else None
        index = int(name[5:]) if isinstance(name, str) and re.fullmatch(f"T{scope}[0-9]{{3,6}}", name) else None
        if (index is None or name != f"T{scope}{index:03d}" or index in indices or len(net["endpoints"]) < 2
                or any(net is other for _, other in plan)
                or item.get("instances") != [f"tunnel_{index:04d}_{ordinal:04d}" for ordinal in range(len(net["endpoints"]))]):
            raise LayoutError("INVALID_TUNNEL_PLAN", "Recorded tunnels must name distinct connected nets of this mapping")
        indices.add(index)
        plan.append((index, net))
    return plan


def add_long_tunnels(layout: dict, mapped: dict, adapter, *, minimum_length: int = 1200,
                     maximum_nets: int = 8, timeout: float = 5, nets: list | None = None) -> dict:
    """Replace selected long nets with pin leads and matched tunnels.

    Keep mapped components unchanged; add tunnel pins to validation_mapping
    for native reload checks. Failed replacements keep the original wires.
    nets reuses the tunnels recorded for another canvas of this mapping, with
    their names and IDs, instead of selecting long nets.
    """
    if type(minimum_length) is not int or minimum_length < 100 or type(maximum_nets) is not int or not 0 <= maximum_nets <= 32:
        raise LayoutError("INVALID_TUNNEL_OPTIONS", "Tunnel thresholds require a length of at least 100 and at most 32 nets")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
        raise LayoutError("INVALID_TUNNEL_OPTIONS", "Tunnel time budget must be a finite number from 0 to 30 seconds")
    validate_mapped(mapped)
    deadline = time.monotonic() + timeout
    current = deepcopy(layout)
    expected = deepcopy(mapped)
    placed = {item["id"]: item for item in current["descriptors"]}
    original_pins = {(item["id"], pin["direction"], pin["pin"]): pin
                     for item in current["descriptors"] for pin in item["pins"]}
    scope = hashlib.sha256(mapped["top"].encode("utf-8")).hexdigest()[:4]
    if nets is None:
        lengths = {}
        for wire in current["circuit"]["wires"]:
            lengths[wire["net_id"]] = lengths.get(wire["net_id"], 0) + sum(abs(wire["from"][axis] - wire["to"][axis]) for axis in ("x", "y"))
        selected = list(enumerate(sorted((net for net in mapped["nets"] if len(net["endpoints"]) > 1
                                          and lengths.get(net["id"], 0) >= minimum_length),
                                         key=lambda net: (not (mapped.get("clock") and net.get("bits") == [mapped["clock"]["bit"]]),
                                                          -lengths[net["id"]], net["id"]))))
    else:
        selected = _recorded_plan(nets, mapped, scope)
    original_ids = {item["id"] for item in mapped["instances"]}
    completed, skipped = [], []
    cache = {}
    for index, net in selected:
        if len(completed) >= maximum_nets or time.monotonic() >= deadline:
            break
        if len(current["circuit"]["instances"]) + len(net["endpoints"]) > 250:
            skipped.append({"net_id": net["id"], "reason": "component_limit"})
            continue
        trial, intent = deepcopy(current), deepcopy(expected)
        trial["circuit"]["wires"] = [wire for wire in trial["circuit"]["wires"] if wire["net_id"] != net["id"]]
        net_intent = next(item for item in intent["nets"] if item["id"] == net["id"])
        name = f"T{scope}{index:03d}"
        added = []
        failed = None
        for ordinal, endpoint in enumerate(sorted(net["endpoints"], key=endpoint_key)):
            if time.monotonic() >= deadline:
                failed = "budget_exhausted"
                break
            identifier = f"tunnel_{index:04d}_{ordinal:04d}"
            if identifier in original_ids:
                raise LayoutError("TUNNEL_ID_COLLISION", "A generated tunnel ID collides with an electrical component")
            pin = original_pins[endpoint_key(endpoint)]
            position, escape = pin["position"], pin["escape"]
            rotation = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[escape["x"], escape["y"]]
            key = name, rotation
            if key not in cache:
                cache[key] = adapter.call("component.describe", {"type": "Tunnel", "attributes": {"NetName": name},
                                          "position": {"x": 0, "y": 0}, "rotation": rotation},
                                          timeout=max(0.001, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                failed = "budget_exhausted"
                break
            original = cache[key]
            if original.get("type") != "Tunnel" or original.get("attributes", {}).get("NetName") != name or len(original.get("pins", [])) != 1:
                raise LayoutError("TUNNEL_NATIVE_CONTRACT", "Native Tunnel metadata differs from its expected contract")
            tunnel_pin = original["pins"][0]
            if tunnel_pin["direction"] != "input" or tunnel_pin["escape"] != {"x": -escape["x"], "y": -escape["y"]}:
                raise LayoutError("TUNNEL_NATIVE_CONTRACT", "Native Tunnel pin must face the component's exit")
            body = placed[endpoint["instance_id"]]["bounds"]
            axis = "x" if escape["x"] else "y"
            sign = escape[axis]
            boundary = body["max" if sign > 0 else "min"][axis]
            start = max(40, ((abs(boundary - position[axis]) + 39) // 20) * 20)
            accepted = None
            for distance in range(start, start + 321, 20):
                if time.monotonic() >= deadline:
                    failed = "budget_exhausted"
                    break
                target = {axis: position[axis] + distance * escape[axis] for axis in ("x", "y")}
                descriptor = _translate(original, target["x"] - tunnel_pin["position"]["x"], target["y"] - tunnel_pin["position"]["y"])
                descriptor["id"] = identifier
                instance = {"id": identifier, "type": "Tunnel", "attributes": deepcopy(descriptor["attributes"]),
                            "position": dict(descriptor["position"]), "rotation": rotation}
                tunnel_endpoint = {"instance_id": identifier, "direction": "input", "pin": tunnel_pin["pin"]}
                wire = {"from": dict(position), "to": target, "net_id": net["id"]}
                trial["circuit"]["instances"].append(instance)
                trial["descriptors"].append(descriptor)
                trial["circuit"]["wires"].append(wire)
                net_intent["endpoints"].append(tunnel_endpoint)
                geometry = validate_geometry(trial["circuit"], trial["descriptors"], nets=intent["nets"])
                # Other terminals of this one net have not been attached yet.
                # All other geometric constraints must already hold.
                if not any(error["code"] != "unreached_pin" for error in geometry["diagnostics"]):
                    accepted = instance, descriptor, tunnel_endpoint
                    break
                trial["circuit"]["instances"].pop()
                trial["descriptors"].pop()
                trial["circuit"]["wires"].pop()
                net_intent["endpoints"].pop()
            if accepted is None:
                failed = failed or "no_clear_tunnel_position"
                break
            instance, descriptor, tunnel_endpoint = accepted
            intent["instances"].append(deepcopy(instance))
            intent["descriptors"].append(deepcopy(descriptor))
            intent["origins"].append({"instance_id": identifier, "kind": "helper", "owner": endpoint["instance_id"],
                                      "purpose": "named tunnel", "source": ""})
            if "pin_bindings" in intent:
                intent["pin_bindings"].append({**tunnel_endpoint, "width": net["width"], "bits": list(net["bits"])})
            added.append(identifier)
        if failed is None:
            geometry = validate_geometry(trial["circuit"], trial["descriptors"], nets=intent["nets"])
            if geometry["valid"] and time.monotonic() < deadline:
                validate_circuit(trial["circuit"])
                trial["geometry"] = geometry
                trial["metrics"] = geometry["metrics"]
                current, expected = trial, intent
                completed.append({"net_id": net["id"], "name": name, "instances": added})
                continue
            failed = "budget_exhausted" if geometry["valid"] else "no_clear_tunnel_position"
        skipped.append({"net_id": net["id"], "reason": failed})
    current["tunnels"] = {"mode": "long", "minimum_length": minimum_length, "nets": completed, "skipped": skipped,
                          "budget_exhausted": time.monotonic() >= deadline}
    current["validation_mapping"] = expected
    return current
