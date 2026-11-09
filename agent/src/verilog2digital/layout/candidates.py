"""Apply placement edits while preserving electrical intent.

The caller selects the result and supplies a separate adapter for refinement,
so its failure leaves the baseline adapter available for validation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

from ..contracts import validate_circuit
from ..mapping import validate_mapped
from .geometry import validate_geometry
from .routing import route_orthogonal
from .scalar import LayoutError, _translate


QUALITY_METRICS = ("bounding_area", "wire_length", "bends", "crossings")


def _remaining(deadline):
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise LayoutError("INVALID_CANDIDATE_DEADLINE", "Candidate deadline must be a finite monotonic timestamp")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LayoutError("CANDIDATE_TIMEOUT", "The optional layout candidate exhausted its time budget")
    return remaining


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def route_placed(mapped: dict, baseline_layout: dict, positions: dict, *, spacing: int = 40,
                 deadline: float, priority_nets=(), reverse: bool = False) -> dict:
    """Apply complete canvas positions and reroute every net, or reject the edit.

    Preserve instance order: Digital uses it to construct custom component pins.
    """
    started = time.monotonic()
    _remaining(deadline)
    validate_mapped(mapped)
    if type(spacing) is not int or not 40 <= spacing <= 400 or spacing % 20:
        raise LayoutError("INVALID_SPACING", "Component spacing must be a multiple of 20 between 40 and 400")
    original = {item["id"]: item for item in mapped["instances"]}
    native = {item["id"]: item for item in mapped["descriptors"]}
    if not original or len(original) > 250:
        raise LayoutError("CANDIDATE_LIMIT", "Optional layout refinement supports at most 250 components per canvas")
    if any(item["type"] == "Tunnel" for item in original.values()):
        raise LayoutError("UNSUPPORTED_CANDIDATE_TUNNELS", "Refine the visible-wire canvas before inserting named tunnels")
    if not isinstance(baseline_layout, dict) or not isinstance(baseline_layout.get("circuit"), dict):
        raise LayoutError("INVALID_CANDIDATE_BASELINE", "A candidate needs the current circuit snapshot")
    baseline = baseline_layout["circuit"]
    validate_circuit(baseline)
    if {item["id"] for item in baseline["instances"]} != set(original):
        raise LayoutError("CANDIDATE_COMPONENT_CHANGED", "Candidate and mapped component IDs must agree exactly")
    for item in baseline["instances"]:
        before = original[item["id"]]
        if {key: value for key, value in item.items() if key != "position"} != {
                key: value for key, value in before.items() if key != "position"}:
            raise LayoutError("CANDIDATE_COMPONENT_CHANGED", "Layout actions cannot change component types, attributes, or rotations")
    if not isinstance(positions, dict) or set(positions) != set(original):
        raise LayoutError("INVALID_CANDIDATE_POSITIONS", "Positions must cover every existing component exactly once")
    for identifier, position in positions.items():
        if (not isinstance(position, dict) or set(position) != {"x", "y"}
                or any(type(position[axis]) is not int or abs(position[axis]) > 1_000_000
                       or position[axis] % 20 for axis in ("x", "y"))):
            raise LayoutError("INVALID_CANDIDATE_POSITIONS", f"Position of {identifier} must be on the bounded 20-unit grid")
    circuit = deepcopy(baseline)
    descriptors = []
    for instance in circuit["instances"]:
        position = deepcopy(positions[instance["id"]])
        instance["position"] = position
        descriptor = _translate(native[instance["id"]], position["x"], position["y"])
        bounds = descriptor["bounds"]
        if any(abs(bounds[corner][axis]) > 1_000_000 for corner in ("min", "max") for axis in ("x", "y")):
            raise LayoutError("INVALID_CANDIDATE_POSITIONS", "Translated native bounds exceed Digital's coordinate limits")
        for other in descriptors:
            box = other["bounds"]
            if not (bounds["max"]["x"] + spacing <= box["min"]["x"]
                    or box["max"]["x"] + spacing <= bounds["min"]["x"]
                    or bounds["max"]["y"] + spacing <= box["min"]["y"]
                    or box["max"]["y"] + spacing <= bounds["min"]["y"]):
                error = LayoutError("CANDIDATE_PLACEMENT_CONFLICT", "Exact component moves violate native bounds or required spacing")
                error.details = {"instances": [other["id"], descriptor["id"]], "spacing": spacing}
                raise error
        descriptors.append(descriptor)
        _remaining(deadline)
    # Translate channel hints with a rigid canvas move; local edits may reuse
    # old channels. The router still determines connectivity.
    hints = deepcopy(baseline["wires"])
    offsets = {(positions[item["id"]]["x"] - item["position"]["x"],
                positions[item["id"]]["y"] - item["position"]["y"]) for item in baseline["instances"]}
    if len(offsets) == 1:
        dx, dy = next(iter(offsets))
        for wire in hints:
            for end in ("from", "to"):
                wire[end]["x"] += dx
                wire[end]["y"] += dy
    wires, search_metrics = route_orthogonal(descriptors, mapped["nets"], deadline=deadline,
                                            hints=hints, priority_nets=priority_nets, reverse=reverse)
    circuit["wires"] = wires
    validate_circuit(circuit)
    geometry = validate_geometry(circuit, descriptors, nets=mapped["nets"], clearance=10)
    _remaining(deadline)
    if not geometry["valid"]:
        error = LayoutError("CANDIDATE_GEOMETRY", "The proposed placement could not be routed with valid geometry")
        error.details = geometry
        raise error
    return {"circuit": circuit, "descriptors": descriptors, "geometry": geometry,
            "strategy": "constrained-orthogonal-v1", "spacing": spacing, "track_spacing": 10,
            "diagnostics": [], "fallback_used": False, "fallback_reason": None,
            "metrics": {**geometry["metrics"], **search_metrics, "routing_seconds": time.monotonic() - started},
            "routing": {"priority_nets": list(priority_nets), "reverse": reverse}}


class _DeadlineAdapter:
    def __init__(self, adapter, deadline):
        self.adapter, self.deadline = adapter, deadline

    def call(self, method, params, **options):
        remaining = _remaining(self.deadline)
        options["timeout"] = min(remaining, options.get("timeout", remaining))
        return self.adapter.call(method, params, **options)


def verify_candidate(mapped: dict, layout: dict, adapter, attempt_directory: Path, *, deadline: float,
                     target: Path | None = None) -> dict:
    """Emit and reload a candidate, binding validation to its exact bytes.

    Electrical or geometry failures return valid=False. Transport, deadline and
    integrity errors propagate. Use an adapter owned by the refinement attempt,
    or a target .dig beside a package's bound dependencies. As in conversion, a
    tunneled layout is checked against its validation_mapping.
    """
    # Import at call time: conversion also invokes the optional refinement loop.
    from ..conversion import validate_export

    _remaining(deadline)
    validate_mapped(mapped)
    circuit = deepcopy(layout["circuit"])
    validate_circuit(circuit)
    directory = Path(attempt_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "candidate.dig" if target is None else Path(target).resolve()
    expected_paths = {"path": output, "sidecar_path": output.with_name(output.name + ".ids.json")}
    # Existing files may only be this hierarchy attempt's bound dependencies.
    for path in (directory / "candidate.json", *expected_paths.values(), directory / "inspection.json",
                 directory / "validation.json"):
        if path.exists() or path.is_symlink():
            raise LayoutError("CANDIDATE_ARTIFACT_EXISTS", "Candidate verification requires unused private artifact paths")
    _write(directory / "candidate.json", circuit)
    native = _DeadlineAdapter(adapter, deadline)
    emitted = native.call("circuit.emit", {"circuit": circuit, "path": str(output)})
    paths = {key: emitted[key] for key in ("path", "sidecar_path")}
    if any(Path(paths[key]).resolve() != expected for key, expected in expected_paths.items()):
        raise LayoutError("CANDIDATE_ARTIFACT_PATH", "The optimizer adapter returned an unexpected candidate artifact path")
    hashes = {"circuit": emitted.get("sha256"), "sidecar": emitted.get("sidecar_sha256")}

    def check_hashes():
        _remaining(deadline)
        if (hashes["circuit"] != _hash(expected_paths["path"])
                or hashes["sidecar"] != _hash(expected_paths["sidecar_path"])):
            raise LayoutError("ARTIFACT_CHANGED", "An optional candidate circuit or identity sidecar changed during validation")

    check_hashes()
    validation, inspection = validate_export(native, layout.get("validation_mapping", mapped), circuit, paths)
    # Custom pin order depends on serialized In/Out order as well as labels.
    order_valid = [item["id"] for item in inspection["instances"]] == [item["id"] for item in circuit["instances"]]
    validation["serialization_order"] = {"valid": order_valid, "diagnostics": [] if order_valid else [{
        "code": "component_order_changed", "message": "Native reload changed the component serialization order"}]}
    validation["valid"] = validation["valid"] and order_valid
    check_hashes()
    _write(directory / "inspection.json", inspection)
    _write(directory / "validation.json", validation)
    _remaining(deadline)
    return {"valid": validation["valid"], "layout": deepcopy(layout), "validation": validation,
            "inspection": inspection, "emitted": emitted, "paths": paths, "hashes": hashes,
            "metrics": deepcopy(validation["geometry"]["metrics"])}


def compare_quality(incumbent_report: dict, candidate_report: dict) -> dict:
    """Require improvement without worsening any verified layout metric.

    Deltas are candidate minus incumbent; negative is better. Search time and
    statistics do not affect acceptance.
    """
    reports = {}
    for name, report in (("incumbent", incumbent_report), ("candidate", candidate_report)):
        checks = ("topology", "bits", "components", "geometry", "model")
        if (not isinstance(report, dict) or report.get("valid") is not True
                or any(not isinstance(report.get(key), dict) or report[key].get("valid") is not True for key in checks)
                or ("serialization_order" in report and (not isinstance(report["serialization_order"], dict)
                    or report["serialization_order"].get("valid") is not True))):
            return {"improved": False, "relation": "invalid", "deltas": {}, "diagnostics": [{
                "code": "unverified_layout", "message": f"The {name} layout did not pass independent validation"}]}
        metrics = report["geometry"].get("metrics", {})
        if not isinstance(metrics, dict) or any(type(metrics.get(key)) is not int or metrics[key] < 0 for key in QUALITY_METRICS):
            return {"improved": False, "relation": "invalid", "deltas": {}, "diagnostics": [{
                "code": "invalid_quality_metrics", "message": f"The {name} report lacks nonnegative native geometry metrics"}]}
        reports[name] = metrics
    deltas = {key: reports["candidate"][key] - reports["incumbent"][key] for key in QUALITY_METRICS}
    better, worse = any(value < 0 for value in deltas.values()), any(value > 0 for value in deltas.values())
    relation = "tradeoff" if better and worse else "improved" if better else "worse" if worse else "equal"
    return {"improved": better and not worse, "relation": relation, "deltas": deltas, "diagnostics": []}
