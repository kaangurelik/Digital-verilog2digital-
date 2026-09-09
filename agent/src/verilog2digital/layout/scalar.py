"""Route on reserved tracks with separate component columns and pin channels.

Distinct horizontal rails keep foreign-net crossings in segment interiors.
Reloaded Digital topology verifies the final connections.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import math

from ..contracts import validate_circuit
from ..frontend.errors import FrontendError
from ..mapping import validate_mapped
from ..validation.topology import endpoint_key
from .geometry import validate_geometry


class LayoutError(FrontendError):
    pass


def _ceil(value: int, grid: int = 20) -> int:
    return math.ceil(value / grid) * grid


def _floor(value: int, grid: int = 20) -> int:
    return math.floor(value / grid) * grid


def _order(mapped: dict) -> list[str]:
    identities = {instance["id"] for instance in mapped["instances"]}
    registers = {instance["id"] for instance in mapped["instances"] if instance["type"] == "D_FF"}
    # Check mixed state/combinational child dependencies in the expanded graph.
    registers.update(child["instance_id"] for child in mapped.get("children", []))
    dependencies = {identifier: set() for identifier in identities}
    dependents = defaultdict(set)
    for net in mapped["nets"]:
        sources = [pin["instance_id"] for pin in net["endpoints"] if pin["direction"] == "output"]
        if len(sources) != 1:
            raise LayoutError("INVALID_MAPPED_NET", f"Net {net['id']} must have exactly one driver")
        for pin in net["endpoints"]:
            if pin["direction"] == "input" and pin["instance_id"] not in registers:
                dependencies[pin["instance_id"]].add(sources[0])
                dependents[sources[0]].add(pin["instance_id"])
    ready = deque(sorted(identifier for identifier in identities if not dependencies[identifier]))
    levels = {}
    while ready:
        identifier = ready.popleft()
        levels[identifier] = levels.get(identifier, 0)
        for consumer in sorted(dependents[identifier]):
            levels[consumer] = max(levels.get(consumer, 0), levels[identifier] + 1)
            dependencies[consumer].remove(identifier)
            if not dependencies[consumer]:
                ready.append(consumer)
    if any(dependencies.values()):
        raise LayoutError("COMBINATIONAL_CYCLE", "Mapped components contain a combinational cycle")
    inputs = {port["instance_id"] for port in mapped["inputs"]}
    outputs = {port["instance_id"] for port in mapped["outputs"]}
    kinds = {instance["id"]: instance["type"] for instance in mapped["instances"]}
    def key(identifier):
        role = 0 if identifier in inputs or kinds[identifier] == "Const" else 2 if identifier in outputs else 1
        return role, levels[identifier], identifier
    return sorted(identities, key=key)


def _translate(description: dict, x: int, y: int) -> dict:
    result = deepcopy(description)
    dx, dy = x - result["position"]["x"], y - result["position"]["y"]
    result["position"] = {"x": x, "y": y}
    for key in ("bounds", "body_bounds"):
        for corner in ("min", "max"):
            result[key][corner]["x"] += dx
            result[key][corner]["y"] += dy
    for pin in result["pins"]:
        pin["position"]["x"] += dx
        pin["position"]["y"] += dy
    return result


def layout_reserved(mapped: dict, *, spacing: int = 40, track_spacing: int = 20) -> dict:
    """Place and route without changing any mapped instance or pin assignment."""
    validate_mapped(mapped)
    if type(spacing) is not int or spacing < 40 or spacing > 400 or spacing % 20:
        raise LayoutError("INVALID_SPACING", "Component spacing must be a multiple of 20 between 40 and 400")
    if type(track_spacing) is not int or track_spacing < 20 or track_spacing > 100 or track_spacing % 20:
        raise LayoutError("INVALID_SPACING", "Track spacing must be a multiple of 20 between 20 and 100")
    originals = {item["id"]: item for item in mapped["descriptors"]}
    instances = {item["id"]: item for item in mapped["instances"]}
    if not originals or len(originals) != len(mapped["descriptors"]) or set(originals) != set(instances):
        raise LayoutError("INVALID_GEOMETRY", "Mapped instances and native descriptors must correspond exactly")
    limit = 64 if mapped["profile"] == "scalar-v1" else 250
    if len(originals) > limit:
        raise LayoutError("LAYOUT_LIMIT", f"The current reserved-track profile supports at most {limit} mapped components")
    if any(item.get("rotation", 0) != 0 for item in instances.values()):
        raise LayoutError("UNSUPPORTED_GEOMETRY", "The reserved-track router uses unrotated components")
    order = _order(mapped)
    connected = [net for net in mapped["nets"] if len(net["endpoints"]) > 1]
    rail_y = {net["id"]: (index + 1) * track_spacing for index, net in enumerate(connected)}
    owner = {endpoint_key(pin): net["id"] for net in mapped["nets"] for pin in net["endpoints"]}
    clearance = 20
    maximum_width = max(item["bounds"]["max"]["x"] - item["bounds"]["min"]["x"] for item in originals.values())
    maximum_pins = max(len(item["pins"]) for item in originals.values())
    pitch = _ceil(maximum_width + 2 * clearance + 2 * maximum_pins * track_spacing + spacing)
    baseline = _ceil((len(connected) + 3) * track_spacing - min(item["bounds"]["min"]["y"] for item in originals.values()))
    descriptors, placed, wires = [], [], []
    channels = defaultdict(list)

    def segment(first, second, net_id):
        if first != second:
            wires.append({"from": dict(first), "to": dict(second), "net_id": net_id})

    for index, identifier in enumerate(order):
        original = originals[identifier]
        width = original["bounds"]["max"]["x"] - original["bounds"]["min"]["x"]
        x = _ceil(index * pitch + (pitch - width) // 2 - original["bounds"]["min"]["x"])
        description = _translate(original, x, baseline)
        descriptors.append(description)
        placed.append({**deepcopy(instances[identifier]), "position": {"x": x, "y": baseline}})
        box = description["bounds"]
        counts = {-1: 0, 1: 0}
        for pin in description["pins"]:
            net_id = owner[(identifier, pin["direction"], pin["pin"])]
            if net_id not in rail_y:
                continue  # Intentionally unused singleton source pin.
            p = pin["position"]
            escape = pin.get("escape", {})
            direction = escape.get("x"), escape.get("y")
            if direction not in {(-1, 0), (1, 0), (0, -1), (0, 1)}:
                raise LayoutError("UNSUPPORTED_GEOMETRY", f"Native pin {identifier}.{pin['pin']} lacks an orthogonal escape direction")
            side = -1 if direction[0] == -1 else 1
            column = (_floor(box["min"]["x"] - clearance) - counts[side] * track_spacing if side == -1
                      else _ceil(box["max"]["x"] + clearance) + counts[side] * track_spacing)
            counts[side] += 1
            if direction[1]:
                y = (_floor(box["min"]["y"] - clearance) if direction[1] == -1
                     else _ceil(box["max"]["y"] + clearance))
                escaped = {"x": p["x"], "y": y}
                bend = {"x": column, "y": y}
                segment(p, escaped, net_id)
                segment(escaped, bend, net_id)
            else:
                bend = {"x": column, "y": p["y"]}
                segment(p, bend, net_id)
            rail = {"x": column, "y": rail_y[net_id]}
            segment(bend, rail, net_id)
            channels[net_id].append(column)
    for net in connected:
        columns = channels[net["id"]]
        segment({"x": min(columns), "y": rail_y[net["id"]]},
                {"x": max(columns), "y": rail_y[net["id"]]}, net["id"])
    circuit = {"schema_version": 1, "instances": placed, "wires": wires}
    validate_circuit(circuit)
    geometry = validate_geometry(circuit, descriptors, nets=mapped["nets"], clearance=10)
    if not geometry["valid"]:
        error = LayoutError("INVALID_LAYOUT", "The reserved-track layout failed geometric validation")
        error.details = geometry
        raise error
    strategy = {"scalar-v1": "scalar-reserved-tracks-v1", "datapath-v1": "datapath-reserved-tracks-v1",
                "sequential-v1": "sequential-reserved-tracks-v1",
                "hierarchy-v1": "hierarchy-reserved-tracks-v1"}[mapped["profile"]]
    return {"circuit": circuit, "descriptors": descriptors, "geometry": geometry,
            "strategy": strategy, "spacing": spacing, "track_spacing": track_spacing}


def layout_scalar(mapped: dict, *, spacing: int = 40, track_spacing: int = 20) -> dict:
    """Keep the original scalar API and its explicit support boundary."""
    if mapped.get("profile") != "scalar-v1":
        raise LayoutError("UNSUPPORTED_LAYOUT_PROFILE", "layout_scalar requires scalar-v1 mapping")
    return layout_reserved(mapped, spacing=spacing, track_spacing=track_spacing)
