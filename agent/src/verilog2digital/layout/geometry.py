"""Check native bounds, pin access and routed net ownership.

Bounds include strokes and text in world coordinates and may be off-grid.
Electrical topology is checked separately after Digital reloads the circuit.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from ..validation.topology import endpoint_key

Point = tuple[int, int]
Box = tuple[int, int, int, int]


def _point(value: dict) -> Point:
    point = value["x"], value["y"]
    if any(type(coordinate) is not int for coordinate in point):
        raise ValueError("Coordinates must be integers")
    return point


def _box(value: dict) -> Box:
    x0, y0 = _point(value["min"])
    x1, y1 = _point(value["max"])
    if x0 > x1 or y0 > y1:
        raise ValueError("Bounds must have ordered minimum and maximum coordinates")
    return x0, y0, x1, y1


def _expand(box: Box, amount: int) -> Box:
    return box[0] - amount, box[1] - amount, box[2] + amount, box[3] + amount


@dataclass(frozen=True)
class _Wire:
    index: int
    a: Point
    b: Point
    owner: str

    @property
    def horizontal(self) -> bool:
        return self.a[1] == self.b[1]

    @property
    def interval(self) -> tuple[int, int]:
        axis = 0 if self.horizontal else 1
        return tuple(sorted((self.a[axis], self.b[axis])))

    @property
    def fixed(self) -> int:
        return self.a[1 if self.horizontal else 0]

    def contains(self, point: Point) -> bool:
        axis = 0 if self.horizontal else 1
        first, last = self.interval
        return point[1 - axis] == self.fixed and first <= point[axis] <= last


def _wire(index: int, value: dict, *, ownership: bool = True) -> _Wire:
    a, b = _point(value["from"]), _point(value["to"])
    if a == b or a[0] != b[0] and a[1] != b[1]:
        raise ValueError(f"Wire {index} must be nonzero and orthogonal")
    owner = value.get("net_id", "")
    if ownership and (not isinstance(owner, str) or not owner):
        raise ValueError(f"Wire {index} requires a net_id")
    return _Wire(index, a, b, owner)


def _covered(first: int, last: int, intervals: list[tuple[int, int]]) -> bool:
    cursor = first
    for start, end in sorted(intervals):
        if start > cursor:
            return False
        cursor = max(cursor, end)
        if cursor >= last:
            return True
    return False


def restore_wire_ownership(original_wires: list[dict], normalized_wires: list[dict]) -> list[dict]:
    """Recover net ownership after Digital splits or merges collinear segments.

    Require full coverage by exactly one original net. Crossings and endpoint
    contact do not establish ancestry. Invalid coverage raises ValueError.
    """
    lines = defaultdict(list)
    for index, value in enumerate(original_wires):
        wire = _wire(index, value)
        lines[wire.horizontal, wire.fixed].append(wire)
    restored = []
    for index, value in enumerate(normalized_wires):
        wire = _wire(index, value, ownership=False)
        first, last = wire.interval
        owners = defaultdict(list)
        for original in lines[wire.horizontal, wire.fixed]:
            start, end = original.interval
            start, end = max(first, start), min(last, end)
            if start < end:
                owners[original.owner].append((start, end))
        if len(owners) != 1:
            raise ValueError(f"Normalized wire {index} has missing or mixed original net ownership: {sorted(owners)}")
        owner, intervals = next(iter(owners.items()))
        if not _covered(first, last, intervals):
            raise ValueError(f"Normalized wire {index} contains a gap in original net {owner}")
        restored.append({**value, "from": dict(value["from"]), "to": dict(value["to"]), "net_id": owner})
    return restored


def _clip(wire: _Wire, box: Box, *, strict: bool) -> tuple[Point, Point] | None:
    axis = 0 if wire.horizontal else 1
    low, high = box[axis], box[axis + 2]
    cross_low, cross_high = box[1 - axis], box[3 - axis]
    fixed = wire.fixed
    if not (cross_low < fixed < cross_high if strict else cross_low <= fixed <= cross_high):
        return None
    start, end = wire.interval
    start, end = max(start, low), min(end, high)
    if (strict and start >= end) or (not strict and start > end):
        return None
    return ((start, fixed), (end, fixed)) if wire.horizontal else ((fixed, start), (fixed, end))


def _on_escape_ray(clip: tuple[Point, Point], position: Point, escape: Point) -> bool:
    dx, dy = escape
    return all((point[0] - position[0]) * dy == (point[1] - position[1]) * dx
               and (point[0] - position[0]) * dx + (point[1] - position[1]) * dy >= 0
               for point in clip)


def _intersections(wires: list[_Wire]):
    """Yield intersecting pairs without comparing every disjoint segment pair."""
    lines = defaultdict(list)
    events = []
    indexed = {wire.index: wire for wire in wires}
    for wire in wires:
        lines[wire.horizontal, wire.fixed].append(wire)
        if wire.horizontal:
            first, last = wire.interval
            events.extend(((first, 0, wire.index), (last, 2, wire.index)))
        else:
            events.append((wire.fixed, 1, wire.index))
    for line in lines.values():
        active = []
        for wire in sorted(line, key=lambda item: item.interval):
            first, last = wire.interval
            active = [other for other in active if other.interval[1] >= first]
            for other in active:
                start, end = first, min(last, other.interval[1])
                point = (start, wire.fixed) if wire.horizontal else (wire.fixed, start)
                yield other, wire, point, start < end
            active.append(wire)
    active = []
    for x, event, index in sorted(events):
        wire = indexed[index]
        if event == 0:
            insort(active, (wire.fixed, index))
        elif event == 2:
            active.pop(bisect_left(active, (wire.fixed, index)))
        else:
            first, last = wire.interval
            begin = bisect_left(active, (first, -1))
            end = bisect_right(active, (last, float("inf")))
            for y, horizontal_index in active[begin:end]:
                yield indexed[horizontal_index], wire, (x, y), False


def _wire_length(wires: list[_Wire]) -> int:
    lines = defaultdict(list)
    for wire in wires:
        lines[wire.owner, wire.horizontal, wire.fixed].append(wire.interval)
    total = 0
    for intervals in lines.values():
        start = end = None
        for first, last in sorted(intervals):
            if end is None or first > end:
                if end is not None:
                    total += end - start
                start, end = first, last
            else:
                end = max(end, last)
        total += end - start
    return total


def validate_geometry(circuit: dict, descriptors: list[dict], *, nets: list[dict], clearance: int = 10) -> dict:
    """Check geometry against independently mapped pin-to-net membership.

    Nets use {id, endpoints:[{instance_id,direction,pin}]}, including singleton
    pins. Native bounds and escape vectors allow outward leads, including split
    collinear leads, but reject turns inside expanded bounds. Exact axis-aligned
    clearance is allowed.

    Metrics count per-net wire length after collinear union, degree-two bends,
    input segments, strict perpendicular crossings and occupied area.
    """
    diagnostics = []
    metrics = {"wire_length": 0, "bends": 0, "segments": 0, "crossings": 0, "bounding_area": 0}

    def error(code, message, **detail):
        diagnostics.append({"code": code, "message": message, **detail})

    def grid(point, label):
        if any(coordinate % 10 for coordinate in point):
            error("off_grid", f"{label} must lie on the 10-unit grid", position={"x": point[0], "y": point[1]})

    try:
        if type(clearance) is not int or clearance < 0:
            raise ValueError("clearance must be a nonnegative integer")
        instances = {}
        for instance in circuit["instances"]:
            identifier = instance["id"]
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Component IDs must be nonempty strings")
            if identifier in instances:
                error("duplicate_instance", f"Component ID {identifier} is repeated")
            instances[identifier] = instance
            grid(_point(instance["position"]), f"Component {identifier}")
        components, pins, pin_positions = {}, {}, {}
        for descriptor in descriptors:
            identifier = descriptor["id"]
            if identifier in components:
                error("duplicate_descriptor", f"Descriptor ID {identifier} is repeated")
            bounds, body = _box(descriptor["bounds"]), _box(descriptor["body_bounds"])
            components[identifier] = {"bounds": bounds, "body": body, "pins": []}
            if not (bounds[0] <= body[0] <= body[2] <= bounds[2]
                    and bounds[1] <= body[1] <= body[3] <= bounds[3]):
                error("invalid_native_bounds", f"Body bounds of {identifier} extend outside its complete native bounds")
            if descriptor.get("position") is not None and identifier in instances:
                if _point(descriptor["position"]) != _point(instances[identifier]["position"]):
                    error("stale_descriptor", f"Native geometry for {identifier} has a different component position")
            for pin in descriptor["pins"]:
                key = endpoint_key({"instance_id": identifier, "direction": pin["direction"], "pin": pin["pin"]})
                position, escape = _point(pin["position"]), _point(pin["escape"])
                if escape not in {(-1, 0), (1, 0), (0, -1), (0, 1)}:
                    raise ValueError(f"Pin {key} requires a cardinal unit escape vector")
                grid(position, f"Pin {key}")
                if not (bounds[0] <= position[0] <= bounds[2] and bounds[1] <= position[1] <= bounds[3]):
                    error("pin_outside_bounds", f"Pin {key} lies outside the complete native bounds")
                if key in pins:
                    error("duplicate_pin", f"Native pin {key} is repeated")
                if position in pin_positions:
                    error("coincident_pins", f"Pins {pin_positions[position]} and {key} occupy the same grid point")
                pins[key] = {"position": position, "escape": escape}
                pin_positions[position] = key
                components[identifier]["pins"].append(key)
        if set(components) != set(instances):
            error("descriptor_mismatch", "Native descriptors must cover exactly the circuit instances",
                  missing=sorted(set(instances) - set(components)), extra=sorted(set(components) - set(instances)))

        owners, net_sizes, net_ids = {}, {}, set()
        for net in nets:
            identifier = net["id"]
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Net IDs must be nonempty strings")
            if identifier in net_ids:
                error("duplicate_net", f"Net ID {identifier} is repeated")
            net_ids.add(identifier)
            if not net["endpoints"]:
                error("empty_net", f"Net {identifier} has no mapped pins")
            net_sizes[identifier] = len(net["endpoints"])
            for endpoint in net["endpoints"]:
                key = endpoint_key(endpoint)
                if key in owners:
                    error("duplicate_endpoint", f"Pin {key} is assigned more than once")
                owners[key] = identifier
        if set(owners) != set(pins):
            error("pin_ownership_mismatch", "Mapped nets must own every native pin exactly once",
                  missing=sorted(set(pins) - set(owners)), extra=sorted(set(owners) - set(pins)))

        wires = []
        for index, value in enumerate(circuit["wires"]):
            try:
                wire = _wire(index, value)
            except (ValueError, KeyError, TypeError) as invalid:
                error("invalid_wire", str(invalid), wire=index)
                continue
            grid(wire.a, f"Wire {index} start")
            grid(wire.b, f"Wire {index} end")
            if wire.owner not in net_ids:
                error("unknown_wire_net", f"Wire {index} names unmapped net {wire.owner}", wire=index)
            wires.append(wire)
        metrics["segments"] = len(circuit["wires"])
        metrics["wire_length"] = _wire_length(wires)

        for (first_id, first), (second_id, second) in combinations(components.items(), 2):
            a, b = first["bounds"], second["bounds"]
            dx, dy = max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0)
            if dx == 0 and dy == 0:
                error("component_overlap", f"Native bounds of {first_id} and {second_id} overlap", instances=[first_id, second_id])
            elif max(dx, dy) < clearance:
                error("component_clearance", f"Components {first_id} and {second_id} are closer than {clearance} units",
                      instances=[first_id, second_id])

        attached = set()
        for wire in wires:
            for key, pin in pins.items():
                if wire.contains(pin["position"]):
                    if owners.get(key) != wire.owner:
                        error("foreign_pin_contact", f"Wire {wire.index} touches pin {key} owned by another net", wire=wire.index)
                    elif pin["position"] not in {wire.a, wire.b}:
                        error("pin_in_wire_interior", f"Pin {key} lies inside wire {wire.index}; Digital may leave it disconnected", wire=wire.index)
                    else:
                        attached.add(key)
                        if not _on_escape_ray((wire.a, wire.b), pin["position"], pin["escape"]):
                            error("invalid_pin_escape", f"Wire {wire.index} approaches pin {key} from an unsupported direction",
                                  wire=wire.index)
            for identifier, component in components.items():
                clipped = _clip(wire, _expand(component["bounds"], clearance), strict=clearance > 0)
                if clipped is None:
                    continue
                own_lead = any(owners.get(key) == wire.owner
                               and _on_escape_ray(clipped, pins[key]["position"], pins[key]["escape"])
                               for key in component["pins"])
                if not own_lead:
                    body_hit = _clip(wire, component["body"], strict=False) is not None
                    error("wire_body_collision" if body_hit else "wire_label_or_clearance_collision",
                          f"Wire {wire.index} enters reserved bounds of {identifier}", wire=wire.index, instance_id=identifier)
        for key in pins:
            if key in owners and net_sizes.get(owners[key], 0) > 1 and key not in attached:
                error("unreached_pin", f"Mapped pin {key} has no endpoint of a wire on its net")

        directions = defaultdict(set)

        def add_directions(wire, point):
            for end in (wire.a, wire.b):
                if end != point:
                    dx, dy = end[0] - point[0], end[1] - point[1]
                    directions[wire.owner, point].add((0 if dx == 0 else 1 if dx > 0 else -1,
                                                       0 if dy == 0 else 1 if dy > 0 else -1))

        for wire in wires:
            add_directions(wire, wire.a)
            add_directions(wire, wire.b)
        crossings = set()
        for first, second, point, overlap in _intersections(wires):
            strict_crossing = (first.horizontal != second.horizontal
                               and point not in {first.a, first.b, second.a, second.b})
            if strict_crossing:
                crossings.add((point, tuple(sorted((first.owner, second.owner)))))
            elif first.owner != second.owner:
                error("inter_net_join", f"Wires {first.index} and {second.index} join different nets",
                      wires=[first.index, second.index], position={"x": point[0], "y": point[1]}, overlap=overlap)
            elif not overlap:
                add_directions(first, point)
                add_directions(second, point)
        metrics["crossings"] = len(crossings)
        metrics["bends"] = sum(len(rays) == 2 and len({ray[0] == 0 for ray in rays}) == 2
                               for rays in directions.values())
        points = [point for wire in wires for point in (wire.a, wire.b)]
        points.extend(point for component in components.values()
                      for point in ((component["bounds"][0], component["bounds"][1]),
                                    (component["bounds"][2], component["bounds"][3])))
        if points:
            xs, ys = zip(*points)
            metrics["bounding_area"] = (max(xs) - min(xs)) * (max(ys) - min(ys))
    except (ValueError, KeyError, TypeError, IndexError) as invalid:
        error("invalid_geometry_input", str(invalid))
    return {"valid": not diagnostics, "diagnostics": diagnostics, "metrics": metrics}
