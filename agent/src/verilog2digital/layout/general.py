"""Native ELK placement followed by checked, bounded Python wire legalization."""
from __future__ import annotations

from copy import deepcopy
import math
import time

from ..adapter import AdapterError
from ..contracts import ContractError, validate_circuit
from ..mapping import validate_mapped
from .geometry import validate_geometry
from .routing import RoutingFailure, route_orthogonal
from .scalar import LayoutError, _ceil, _translate, layout_reserved


def _placements(mapped, proposal, spacing, stretch=1):
    """Resolve only positions; the planner cannot replace electrical components."""
    if not isinstance(proposal, dict) or proposal.get("schema_version") != 1 or not isinstance(proposal.get("placements"), list):
        raise RoutingFailure("The native planner returned an invalid placement proposal")
    original = {item["id"]: item for item in mapped["instances"]}
    descriptions = {item["id"]: item for item in mapped["descriptors"]}
    positions = {}
    for placement in proposal["placements"]:
        if not isinstance(placement, dict) or placement.get("id") not in original or placement["id"] in positions:
            raise RoutingFailure("Native placement IDs must match every mapped instance exactly once")
        identifier = placement["id"]
        p = placement.get("position", {})
        if any(type(p.get(axis)) is not int or abs(p[axis]) > 1000000 for axis in ("x", "y")):
            raise RoutingFailure("Native placement coordinates must be bounded integers")
        if placement.get("rotation", original[identifier]["rotation"]) != original[identifier]["rotation"]:
            raise RoutingFailure("The placement proposal attempted to change a mapped rotation")
        positions[identifier] = {axis: _ceil(p[axis] * stretch) for axis in ("x", "y")}
    if set(positions) != set(original):
        raise RoutingFailure("The placement proposal omitted mapped instances")
    placed, translated, occupied = [], [], []
    adjustments = 0
    for identifier in sorted(positions, key=lambda key: (positions[key]["x"], positions[key]["y"], key)):
        p = positions[identifier]
        descriptor = _translate(descriptions[identifier], p["x"], p["y"])
        # Move later components down if grid snapping closes a gap; verify afterward.
        while True:
            bounds = descriptor["bounds"]
            low, high = bounds["min"], bounds["max"]
            conflicts = [box for box in occupied if not (
                high["x"] + spacing <= box["min"]["x"] or box["max"]["x"] + spacing <= low["x"] or
                high["y"] + spacing <= box["min"]["y"] or box["max"]["y"] + spacing <= low["y"])]
            if not conflicts:
                break
            shift = _ceil(max(box["max"]["y"] for box in conflicts) + spacing - low["y"])
            p = {"x": p["x"], "y": p["y"] + shift}
            descriptor = _translate(descriptions[identifier], p["x"], p["y"])
            adjustments += 1
        if any(abs(value) > 1000000 for value in p.values()):
            raise RoutingFailure("Legalized placement exceeds Digital coordinate bounds")
        placed.append({**deepcopy(original[identifier]), "position": p})
        translated.append(descriptor)
        occupied.append(descriptor["bounds"])
    return placed, translated, adjustments


def layout_general(mapped: dict, adapter, *, spacing: int = 40, seed: int = 1, timeout: float = 20) -> dict:
    """Return a checked layout, using reserved routing if compact routing fails.

    The compact timeout excludes fallback time. Adapter transport failures
    propagate because a broken adapter cannot validate the fallback.
    """
    validate_mapped(mapped)
    if type(spacing) is not int or not 40 <= spacing <= 400 or spacing % 20:
        raise LayoutError("INVALID_SPACING", "Component spacing must be a multiple of 20 between 40 and 400")
    if type(seed) is not int or seed != 1:
        raise LayoutError("INVALID_LAYOUT_SEED", "The deterministic general router currently requires seed=1")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 120:
        raise LayoutError("INVALID_LAYOUT_TIMEOUT", "Compact layout timeout must be a finite number from 0 to 120 seconds")
    started = time.monotonic()
    deadline = started + timeout
    diagnostics, planning_seconds = [], 0.0
    reason = "The compact routing time budget was exhausted before native planning"
    if timeout >= 0.05:
        try:
            planning_started = time.monotonic()
            try:
                proposal = adapter.call("layout.plan", {"instances": deepcopy(mapped["instances"]),
                            "nets": [{"id": net["id"], "endpoints": deepcopy(net["endpoints"])} for net in mapped["nets"]],
                            "spacing": spacing, "seed": seed}, timeout=max(0.001, deadline - time.monotonic()))
            finally:
                planning_seconds = time.monotonic() - planning_started
            if time.monotonic() >= deadline:
                raise RoutingFailure("Native planning used the available compact layout budget")
            for attempt, stretch in enumerate((1, 2)):
                try:
                    placed, descriptors, adjustments = _placements(mapped, proposal, spacing, stretch)
                    wires, search_metrics = route_orthogonal(descriptors, mapped["nets"], deadline=deadline,
                                        hints=proposal.get("wires", ()) if stretch == 1 else (), reverse=attempt == 1)
                    circuit = {"schema_version": 1, "instances": placed, "wires": wires}
                    validate_circuit(circuit)
                    geometry = validate_geometry(circuit, descriptors, nets=mapped["nets"], clearance=10)
                    if not geometry["valid"]:
                        codes = sorted({item["code"] for item in geometry["diagnostics"]})
                        raise RoutingFailure("Compact candidate failed geometric validation: " + ", ".join(codes[:6]))
                    if time.monotonic() >= deadline:
                        raise RoutingFailure("The compact candidate verification exceeded the available time budget")
                    return {"circuit": circuit, "descriptors": descriptors, "geometry": geometry,
                            "strategy": "elk-orthogonal-v1", "spacing": spacing, "track_spacing": 10,
                            "diagnostics": diagnostics, "fallback_used": False, "fallback_reason": None,
                            "metrics": {**geometry["metrics"], **search_metrics, "placement_adjustments": adjustments,
                                        "placement_stretch": stretch, "planning_seconds": planning_seconds,
                                        "compact_seconds": time.monotonic() - started}}
                except (RoutingFailure, ContractError) as error:
                    reason = str(error)
                    diagnostics.append({"code": "compact_attempt_failed", "message": reason, "attempt": attempt + 1})
                    if time.monotonic() >= deadline:
                        break
        except AdapterError as error:
            if getattr(adapter, "_closed", False):
                raise
            reason = f"Native layout planning was unavailable: {error}"
        except (RoutingFailure, KeyError, TypeError, ValueError) as error:
            reason = str(error) or "The native placement proposal was invalid"
    compact_seconds = time.monotonic() - started
    fallback_started = time.monotonic()
    result = layout_reserved(mapped, spacing=spacing)
    diagnostics.append({"code": "reserved_layout_fallback", "message": reason[:2000]})
    return {**result, "diagnostics": diagnostics[:8], "fallback_used": True, "fallback_reason": reason[:2000],
            "metrics": {**result["geometry"]["metrics"], "planning_seconds": planning_seconds,
                        "compact_seconds": compact_seconds, "fallback_seconds": time.monotonic() - fallback_started}}
