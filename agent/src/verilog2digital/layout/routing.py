"""Bounded orthogonal net trees on a coordinate-compressed routing grid.

Foreign nets may cross only while both remain straight. Foreign endpoints,
collinear contacts and turns at crossings are forbidden. Native pin leads are
reserved before any search, so later nets cannot steal another pin's access.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from heapq import heappop, heappush
from itertools import count
import math
import time


class RoutingFailure(ValueError):
    pass


def point(value):
    return value["x"], value["y"]


def record(a, b, owner):
    return {"from": {"x": a[0], "y": a[1]}, "to": {"x": b[0], "y": b[1]}, "net_id": owner}


def _ceil(value):
    return math.ceil(value / 10) * 10


def _floor(value):
    return math.floor(value / 10) * 10


def _contains(segment, p):
    a, b = segment
    return (a[0] == b[0] == p[0] and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])) or (
        a[1] == b[1] == p[1] and min(a[0], b[0]) <= p[0] <= max(a[0], b[0]))


def _simplify(points):
    output = []
    for p in points:
        if output and p == output[-1]:
            continue
        if len(output) >= 2 and ((output[-2][0] == output[-1][0] == p[0]) or (output[-2][1] == output[-1][1] == p[1])):
            output[-1] = p
        else:
            output.append(p)
    return output


def merge_segments(wires):
    """Union collinear intervals while preserving same-net junction endpoints.

    Digital needs endpoints in both orientations to retain four-way connections.
    """
    lines = defaultdict(list)
    for wire in wires:
        a, b = point(wire["from"]), point(wire["to"])
        if a == b:
            continue
        axis = 0 if a[1] == b[1] else 1
        lines[wire["net_id"], axis, a[1 - axis]].append(tuple(sorted((a[axis], b[axis]))))
    output = []
    for (owner, axis, fixed), intervals in sorted(lines.items()):
        start, end = sorted(intervals)[0]
        merged = []
        for low, high in sorted(intervals)[1:]:
            if low <= end:
                end = max(end, high)
            else:
                merged.append((start, end))
                start, end = low, high
        merged.append((start, end))
        for low, high in merged:
            a, b = ((low, fixed), (high, fixed)) if axis == 0 else ((fixed, low), (fixed, high))
            output.append(record(a, b, owner))
    cuts = {}
    verticals = defaultdict(lambda: defaultdict(list))
    for index, wire in enumerate(output):
        a, b = point(wire["from"]), point(wire["to"])
        axis = 0 if a[1] == b[1] else 1
        cuts[index] = {a[axis], b[axis]}
        if axis == 1:
            verticals[wire["net_id"]][a[0]].append((a[1], b[1], index))
    columns = {owner: sorted(lines) for owner, lines in verticals.items()}
    for index, wire in enumerate(output):
        a, b = point(wire["from"]), point(wire["to"])
        if a[1] != b[1]:
            continue
        owner = wire["net_id"]
        values = columns.get(owner, ())
        for x in values[bisect_left(values, a[0]):bisect_right(values, b[0])]:
            for low, high, other in verticals[owner][x]:
                if low <= a[1] <= high:
                    cuts[index].add(x)
                    cuts[other].add(a[1])
    split = []
    for index, wire in enumerate(output):
        a, b = point(wire["from"]), point(wire["to"])
        axis = 0 if a[1] == b[1] else 1
        fixed = a[1-axis]
        values = sorted(cuts[index])
        for low, high in zip(values, values[1:]):
            first, second = ((low, fixed), (high, fixed)) if axis == 0 else ((fixed, low), (fixed, high))
            split.append(record(first, second, wire["net_id"]))
    return split


class _Index:
    def __init__(self, boxes, xs, ys, deadline=float("inf")):
        self.boxes = boxes
        self.lines = [defaultdict(list), defaultdict(list)]
        self.fixed = [[], []]
        self.obstacles = [{}, {}]
        for axis, coordinates in ((0, ys), (1, xs)):
            for index, fixed in enumerate(coordinates):
                if index % 128 == 0 and time.monotonic() >= deadline:
                    raise RoutingFailure("The compact routing time budget was exhausted while indexing obstacles")
                intervals = sorted((box[axis], box[axis + 2]) for box in boxes
                                   if box[1-axis] < fixed < box[3-axis])
                merged = []
                for low, high in intervals:
                    if merged and low <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
                    else:
                        merged.append((low, high))
                self.obstacles[axis][fixed] = merged

    def add(self, a, b, owner):
        if a == b:
            return
        axis = 0 if a[1] == b[1] else 1
        fixed = a[1 - axis]
        if fixed not in self.lines[axis]:
            insort(self.fixed[axis], fixed)
        self.lines[axis][fixed].append((*sorted((a[axis], b[axis])), owner))

    def foreign(self, p, owner):
        hits = []
        for axis in (0, 1):
            for low, high, net in self.lines[axis].get(p[1 - axis], ()):
                if net != owner and low <= p[axis] <= high:
                    hits.append((axis, p[axis] in (low, high)))
        return hits

    def node(self, p, axis, owner, *, terminal=False):
        hits = self.foreign(p, owner)
        return not hits if terminal else all(other_axis != axis and not endpoint for other_axis, endpoint in hits)

    def edge(self, a, b, owner, *, obstacles=True):
        if a == b:
            return True
        if a[0] != b[0] and a[1] != b[1]:
            return False
        axis = 0 if a[1] == b[1] else 1
        low, high = sorted((a[axis], b[axis]))
        fixed = a[1 - axis]
        if obstacles:
            for start, end in self.obstacles[axis].get(fixed, ()):
                if start >= high:
                    break
                if max(start, low) < min(end, high):
                    return False
        for start, end, net in self.lines[axis].get(fixed, ()):
            if net != owner and max(start, low) <= min(end, high):
                return False
        other = 1 - axis
        perpendiculars = self.fixed[other]
        for cross in perpendiculars[bisect_left(perpendiculars, low):bisect_right(perpendiculars, high)]:
            for start, end, net in self.lines[other][cross]:
                if net != owner and start <= fixed <= end and fixed in (start, end):
                    return False
        return True


class _Search:
    def __init__(self, boxes, coordinates, leads, deadline):
        xs, ys = coordinates
        self.xs, self.ys = sorted(xs), sorted(ys)
        self.xindex = {x: i for i, x in enumerate(self.xs)}
        self.yindex = {y: i for i, y in enumerate(self.ys)}
        self.deadline, self.expanded = deadline, 0
        self.budget()
        self.index = _Index(boxes, self.xs, self.ys, deadline)
        self.wires = []
        for lead_index, (owner, a, b) in enumerate(leads):
            if lead_index % 128 == 0:
                self.budget()
            if not self.index.edge(a, b, owner, obstacles=False):
                raise RoutingFailure("Native pin escape leads collide with another net")
            for endpoint in (a, b):
                if not self.index.node(endpoint, 0, owner, terminal=True):
                    raise RoutingFailure("Native pin escape lead ends on another net")
            self.add_path([a, b], owner)

    def budget(self):
        if time.monotonic() >= self.deadline:
            raise RoutingFailure("The compact routing time budget was exhausted")
        if self.expanded >= 300000:
            raise RoutingFailure("The compact routing search-state limit was reached")

    def add_path(self, points, owner):
        segments = []
        for a, b in zip(points, points[1:]):
            if a != b:
                self.index.add(a, b, owner)
                self.wires.append(record(a, b, owner))
                segments.append((a, b))
        return segments

    def legal_path(self, points, owner):
        points = _simplify(points)
        if len(points) < 2:
            return points
        for i, (a, b) in enumerate(zip(points, points[1:])):
            axis = 0 if a[1] == b[1] else 1
            if not self.index.edge(a, b, owner):
                return None
            if not self.index.node(a, axis, owner, terminal=i == 0):
                return None
            if i and self.index.foreign(a, owner):
                return None  # A compressed polyline vertex is always a turn.
        if not self.index.node(points[-1], 0, owner, terminal=True):
            return None
        return points

    def route(self, start, tree, owner):
        self.budget()
        if any(_contains(segment, start) for segment in tree):
            return [start]
        targets = set()
        for a, b in tree:
            if a[0] == b[0]:
                targets.add((a[0], min(max(start[1], min(a[1], b[1])), max(a[1], b[1]))))
            else:
                targets.add((min(max(start[0], min(a[0], b[0])), max(a[0], b[0])), a[1]))
            targets.update((a, b))
        targets = sorted(targets, key=lambda p: (abs(p[0]-start[0]) + abs(p[1]-start[1]), p))
        for goal in targets[:12]:
            self.budget()
            candidates = [[start, goal], [start, (start[0], goal[1]), goal], [start, (goal[0], start[1]), goal]]
            for axis, values in ((0, self.xs), (1, self.ys)):
                pivot = (start[axis] + goal[axis]) // 2
                split = bisect_left(values, pivot)
                chosen = sorted(values[max(0, split-8):split+8], key=lambda coordinate: (abs(coordinate-pivot), coordinate))[:8]
                for value in chosen:
                    if axis == 0:
                        candidates.append([start, (value, start[1]), (value, goal[1]), goal])
                    else:
                        candidates.append([start, (start[0], value), (goal[0], value), goal])
            for candidate in candidates:
                legal = self.legal_path(candidate, owner)
                if legal is not None:
                    return legal
        return self.astar(start, tree, owner)

    def astar(self, start, tree, owner):
        minx = min(p[0] for segment in tree for p in segment)
        maxx = max(p[0] for segment in tree for p in segment)
        miny = min(p[1] for segment in tree for p in segment)
        maxy = max(p[1] for segment in tree for p in segment)
        def heuristic(p):
            return max(minx-p[0], p[0]-maxx, 0) + max(miny-p[1], p[1]-maxy, 0)
        serial = count()
        initial = self.xindex[start[0]], self.yindex[start[1]], -1
        queue = [(heuristic(start), 0, next(serial), initial)]
        costs, parents = {initial: 0}, {}
        local_expanded = 0
        while queue:
            _, cost, _, state = heappop(queue)
            if cost != costs.get(state):
                continue
            ix, iy, incoming = state
            p = self.xs[ix], self.ys[iy]
            self.expanded += 1
            local_expanded += 1
            if self.expanded % 128 == 0:
                self.budget()
            if local_expanded > 60000:
                raise RoutingFailure(f"Net {owner} exceeded its bounded route search")
            if self.index.node(p, 0, owner, terminal=True) and any(_contains(segment, p) for segment in tree):
                path = [p]
                while state in parents:
                    state = parents[state]
                    path.append((self.xs[state[0]], self.ys[state[1]]))
                return _simplify(reversed(path))
            for nx, ny, axis in ((ix-1, iy, 0), (ix+1, iy, 0), (ix, iy-1, 1), (ix, iy+1, 1)):
                if not 0 <= nx < len(self.xs) or not 0 <= ny < len(self.ys):
                    continue
                q = self.xs[nx], self.ys[ny]
                if not self.index.node(p, axis, owner, terminal=incoming == -1) or not self.index.edge(p, q, owner):
                    continue
                if incoming != -1 and incoming != axis and self.index.foreign(p, owner):
                    continue
                candidate = nx, ny, axis
                distance = abs(q[0]-p[0]) + abs(q[1]-p[1])
                next_cost = cost + distance + (30 if incoming != -1 and incoming != axis else 0)
                if next_cost < costs.get(candidate, float("inf")):
                    if len(costs) >= 200000 or len(queue) >= 200000:
                        raise RoutingFailure("The compact route memory-state limit was reached")
                    costs[candidate] = next_cost
                    parents[candidate] = state
                    heappush(queue, (next_cost + heuristic(q), next_cost, next(serial), candidate))
        raise RoutingFailure(f"No legal orthogonal route was found for net {owner}")


def route_orthogonal(descriptors, nets, *, deadline, hints=(), reverse=False, priority_nets=()):
    """Route fixed native placements; return owned wires and bounded search metrics."""
    if not isinstance(priority_nets, (list, tuple)) or len(priority_nets) > 32:
        raise RoutingFailure("At most 32 existing nets may receive routing priority")
    net_ids = {net["id"] for net in nets}
    if any(not isinstance(identifier, str) or identifier not in net_ids for identifier in priority_nets):
        raise RoutingFailure("Routing priority refers to an unknown net")
    if len(set(priority_nets)) != len(priority_nets):
        raise RoutingFailure("Routing priority must not repeat a net")
    if type(reverse) is not bool:
        raise RoutingFailure("The reverse routing option must be a boolean")
    boxes, pins, xs, ys = [], {}, set(), set()
    owners = {(e["instance_id"], e["direction"], e["pin"]): net["id"] for net in nets for e in net["endpoints"]}
    connected = {net["id"] for net in nets if len(net["endpoints"]) > 1}
    leads, by_endpoint = [], {}
    for descriptor_index, descriptor in enumerate(descriptors):
        if descriptor_index % 32 == 0 and time.monotonic() >= deadline:
            raise RoutingFailure("The compact routing time budget was exhausted while preparing native geometry")
        bounds = descriptor["bounds"]
        low, high = point(bounds["min"]), point(bounds["max"])
        box = (_floor(low[0]-20), _floor(low[1]-20), _ceil(high[0]+20), _ceil(high[1]+20))
        boxes.append(box)
        xs.update((box[0]-20, box[0], box[2], box[2]+20))
        ys.update((box[1]-20, box[1], box[3], box[3]+20))
        for pin in descriptor["pins"]:
            key = descriptor["id"], pin["direction"], pin["pin"]
            p, escape = point(pin["position"]), point(pin["escape"])
            if escape == (-1, 0): exit_point = box[0], p[1]
            elif escape == (1, 0): exit_point = box[2], p[1]
            elif escape == (0, -1): exit_point = p[0], box[1]
            elif escape == (0, 1): exit_point = p[0], box[3]
            else: raise RoutingFailure(f"Native pin {key} has no cardinal escape direction")
            pins[key] = exit_point
            xs.update((p[0], exit_point[0]-10, exit_point[0], exit_point[0]+10))
            ys.update((p[1], exit_point[1]-10, exit_point[1], exit_point[1]+10))
            if owners[key] in connected:
                leads.append((owners[key], p, exit_point))
                by_endpoint[key] = p, exit_point
    for hint_index, hint in enumerate(hints):
        if hint_index % 128 == 0 and time.monotonic() >= deadline:
            raise RoutingFailure("The compact routing time budget was exhausted while preparing route hints")
        for key in ("from", "to"):
            value = hint.get(key, {}) if isinstance(hint, dict) else {}
            if all(type(value.get(axis)) is int and value[axis] % 10 == 0 and abs(value[axis]) <= 1000000 for axis in ("x", "y")):
                xs.add(value["x"])
                ys.add(value["y"])
    margin = min(400, max(80, len(connected) * 10))
    for offset in range(20, margin + 1, 20):
        xs.update((min(box[0] for box in boxes)-offset, max(box[2] for box in boxes)+offset))
        ys.update((min(box[1] for box in boxes)-offset, max(box[3] for box in boxes)+offset))
    if len(xs) * len(ys) > 8000000:
        raise RoutingFailure("The compressed routing grid exceeds its resource limit")
    if any(abs(value) > 1000000 for value in xs | ys):
        raise RoutingFailure("Compact routing would exceed Digital coordinate limits")
    search = _Search(boxes, (xs, ys), sorted(leads), deadline)
    ordered = sorted((net for net in nets if net["id"] in connected),
                     key=lambda net: (-net.get("width", 1), -len(net["endpoints"]), net["id"]), reverse=reverse)
    priority = {identifier: index for index, identifier in enumerate(priority_nets)}
    # Stable priority sorting changes search order while preserving net membership.
    ordered.sort(key=lambda net: priority.get(net["id"], len(priority)))
    for net in ordered:
        owner = net["id"]
        source = next(e for e in net["endpoints"] if e["direction"] == "output")
        source_key = source["instance_id"], source["direction"], source["pin"]
        tree = [by_endpoint[source_key]]
        sinks = [(e["instance_id"], e["direction"], e["pin"]) for e in net["endpoints"] if e != source]
        sinks.sort(key=lambda key: (abs(pins[key][0]-pins[source_key][0]) + abs(pins[key][1]-pins[source_key][1]), key))
        for key in sinks:
            path = search.route(pins[key], tree, owner)
            tree.extend(search.add_path(path, owner))
            tree.append(by_endpoint[key])
            if len(search.wires) > 20000:
                raise RoutingFailure("Compact routing exceeded the native wire segment limit")
    search.budget()
    wires = merge_segments(search.wires)
    search.budget()
    return wires, {"expanded_states": search.expanded, "grid_columns": len(xs), "grid_rows": len(ys)}
