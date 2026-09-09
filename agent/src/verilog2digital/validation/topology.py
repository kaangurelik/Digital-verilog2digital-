"""Compare electrical endpoint partitions, without consulting route net labels."""
from __future__ import annotations

def endpoint_key(endpoint: dict) -> tuple[str, str, str]:
    values = tuple(endpoint.get(field) for field in ("instance_id", "direction", "pin"))
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"Invalid endpoint: {endpoint!r}")
    if values[1] not in {"input", "output", "both"}:
        raise ValueError(f"Invalid pin direction: {values[1]}")
    return values


def _endpoint(key: tuple) -> dict:
    return dict(zip(("instance_id", "direction", "pin"), key))


def _partition(groups: list, source: str) -> tuple[dict, list]:
    partition = {}
    errors = []
    for group in groups:
        keys = [endpoint_key(endpoint) for endpoint in group]
        members = frozenset(keys)
        if len(members) != len(keys):
            errors.append({"code": "duplicate_endpoint", "source": source, "message": "Endpoint repeated within one net"})
        for key in members:
            if key in partition:
                errors.append({"code": "duplicate_endpoint", "source": source, "endpoint": _endpoint(key),
                               "message": "Endpoint belongs to multiple nets"})
            partition[key] = members
    return partition, errors


def compare_topology(expected_nets: list[list[dict]], inspection: dict) -> dict:
    """Check every expected pin and the complete net equivalence relation.

    Include unused pins; matching net or fanout counts alone is insufficient.
    """
    if inspection.get("topology_available") is False:
        return {"valid": False, "diagnostics": [{"code": "topology_unavailable",
                                                  "message": "Digital could not construct an inspectable netlist"}]}
    try:
        expected, errors = _partition(expected_nets, "expected")
        actual_groups = [net["endpoints"] for net in inspection["nets"]]
        actual_groups += [[pin] for pin in inspection["unconnected"]]
        actual, found_errors = _partition(actual_groups, "actual")
        errors += found_errors
        descriptors = inspection.get("instances")
        if descriptors is not None:
            universe = []
            for instance in descriptors:
                for pin in instance["pins"]:
                    universe.append({"instance_id": instance["id"], "direction": pin["direction"], "pin": pin["pin"]})
            reported = {endpoint_key(pin) for pin in universe}
            if len(reported) != len(universe):
                errors.append({"code": "duplicate_pin", "message": "Reloaded component pin identities are not unique"})
            if reported != set(actual):
                errors.append({"code": "incomplete_inspection", "message": "Actual net groups do not cover exactly the reloaded pins",
                               "omitted": [_endpoint(k) for k in sorted(reported - set(actual))],
                               "unknown": [_endpoint(k) for k in sorted(set(actual) - reported)]})
        for key in sorted(set(expected) - set(actual)):
            errors.append({"code": "missing_endpoint", "endpoint": _endpoint(key), "message": "Expected pin is absent"})
        for key in sorted(set(actual) - set(expected)):
            errors.append({"code": "unexpected_endpoint", "endpoint": _endpoint(key), "message": "Unmapped pin is present"})
        seen = set()
        for key in sorted(set(expected) & set(actual)):
            missing = expected[key] - actual[key]
            extra = actual[key] - expected[key]
            signature = (expected[key], actual[key])
            if (missing or extra) and signature not in seen:
                seen.add(signature)
                errors.append({"code": "connection_mismatch", "endpoint": _endpoint(key),
                               "missing_connections": [_endpoint(k) for k in sorted(missing)],
                               "unexpected_connections": [_endpoint(k) for k in sorted(extra)],
                               "message": "Reloaded electrical connections differ from the expected net"})
        empty_nets = [net["id"] for net in inspection["nets"] if not net["endpoints"]]
        if empty_nets:
            errors.append({"code": "wire_without_pins", "nets": empty_nets, "message": "Wire groups contain no component pins"})
        return {"valid": not errors, "diagnostics": errors,
                "expected_endpoints": len(expected), "actual_endpoints": len(actual)}
    except (ValueError, KeyError, TypeError) as error:
        return {"valid": False, "diagnostics": [{"code": "invalid_topology_report", "message": str(error)}]}


def _component_signature(component: dict) -> dict:
    pins = [{key: pin.get(key) for key in ("direction", "pin")} for pin in component["pins"]]
    signature = {"type": component["type"], "attributes": component["attributes"],
                 "rotation": component.get("rotation", 0),
                 "pins": sorted(pins, key=lambda p: (p["direction"], p["pin"]))}
    if "splitter_bindings" in component:
        signature["splitter_bindings"] = sorted(component["splitter_bindings"], key=lambda p: (p["direction"], p["pin"]))
    return signature


def compare_components(expected_descriptors: list[dict], actual_descriptors: list[dict]) -> dict:
    """Compare requested component descriptors with native reloads.

    Check known widths; model construction may resolve unknown input widths.
    Geometry is checked separately.
    """
    errors = []
    try:
        expected = {component["id"]: component for component in expected_descriptors}
        actual = {component["id"]: component for component in actual_descriptors}
        if len(expected) != len(expected_descriptors) or len(actual) != len(actual_descriptors):
            errors.append({"code": "duplicate_instance", "message": "Component identities are not unique"})
        if set(expected) != set(actual):
            errors.append({"code": "component_universe", "missing": sorted(set(expected) - set(actual)),
                           "unexpected": sorted(set(actual) - set(expected)), "message": "Component identities differ"})
        for identifier in sorted(set(expected) & set(actual)):
            before, after = expected[identifier], actual[identifier]
            if _component_signature(before) != _component_signature(after):
                errors.append({"code": "component_changed", "instance_id": identifier,
                               "message": "Native type, attributes, or pins changed during export/reload"})
            widths = {(pin["direction"], pin["pin"]): pin.get("width") for pin in after["pins"]}
            for pin in before["pins"]:
                width = pin.get("width")
                if width is not None and widths.get((pin["direction"], pin["pin"])) != width:
                    errors.append({"code": "pin_width_changed", "instance_id": identifier, "pin": pin["pin"],
                                   "direction": pin["direction"], "message": "Pin width changed"})
    except (KeyError, TypeError, ValueError) as error:
        errors.append({"code": "invalid_component_report", "message": str(error)})
    return {"valid": not errors, "diagnostics": errors}
