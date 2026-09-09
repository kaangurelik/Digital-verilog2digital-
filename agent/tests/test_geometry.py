"""Geometry mutation checks with hand-specified bounds and expected pin ownership."""
from copy import deepcopy

import pytest

from verilog2digital.layout.geometry import restore_wire_ownership, validate_geometry


def point(x, y):
    return {"x": x, "y": y}


def box(x0, y0, x1, y1):
    return {"min": point(x0, y0), "max": point(x1, y1)}


def wire(a, b, owner="data"):
    return {"from": point(*a), "to": point(*b), "net_id": owner}


def pad(identifier, position, escape, direction):
    x, y = position
    dx, dy = escape
    if dx:
        bounds = box(x - 20 if dx == 1 else x - 2, y - 10,
                     x + 2 if dx == 1 else x + 20, y + 10)
    else:
        bounds = box(x - 10, y - 20 if dy == 1 else y - 2,
                     x + 10, y + 2 if dy == 1 else y + 20)
    descriptor = {"id": identifier, "position": point(x, y), "bounds": bounds, "body_bounds": deepcopy(bounds),
                  "pins": [{"pin": "p", "direction": direction, "position": point(x, y), "escape": point(dx, dy)}]}
    instance = {"id": identifier, "type": "Test", "position": point(x, y), "attributes": {}}
    endpoint = {"instance_id": identifier, "direction": direction, "pin": "p"}
    return instance, descriptor, endpoint


def direct():
    a = pad("source", (0, 0), (1, 0), "output")
    b = pad("sink", (100, 0), (-1, 0), "input")
    return ({"schema_version": 1, "instances": [a[0], b[0]], "wires": [wire((0, 0), (100, 0))]},
            [a[1], b[1]], [{"id": "data", "endpoints": [a[2], b[2]]}])


def check(case, **kwargs):
    circuit, descriptors, nets = case
    return validate_geometry(circuit, descriptors, nets=nets, **kwargs)


def assert_invalid(case, code, **kwargs):
    result = check(case, **kwargs)
    assert not result["valid"], result
    assert code in {diagnostic["code"] for diagnostic in result["diagnostics"]}, result
    return result


def crossing():
    circuit, descriptors, nets = direct()
    a = pad("upper", (50, -60), (0, 1), "output")
    b = pad("lower", (50, 60), (0, -1), "input")
    circuit["instances"].extend([a[0], b[0]])
    circuit["wires"].append(wire((50, -60), (50, 60), "vertical"))
    descriptors.extend([a[1], b[1]])
    nets.append({"id": "vertical", "endpoints": [a[2], b[2]]})
    return circuit, descriptors, nets


def test_direct_owned_leads_escape_native_bounds_without_mutating_input():
    case = direct()
    before = deepcopy(case)
    result = check(case)
    assert result["valid"], result
    assert result["metrics"] == {"wire_length": 100, "segments": 1, "bends": 0, "crossings": 0, "bounding_area": 2800}
    assert case == before


def test_distinct_net_strict_interior_crossing_is_valid():
    result = check(crossing())
    assert result["valid"], result
    assert result["metrics"]["crossings"] == 1
    assert result["metrics"]["wire_length"] == 220


@pytest.mark.parametrize("mutation", [
    lambda c: c["wires"].append(wire((50, 0), (50, 30), "vertical")),
    lambda c: c["wires"].append(wire((50, 0), (70, 0), "vertical")),
    lambda c: c["wires"].append(wire((100, 0), (130, 0), "vertical")),
])
def test_different_net_t_contacts_overlaps_and_shared_endpoints_are_rejected(mutation):
    case = crossing()
    mutation(case[0])
    assert_invalid(case, "inter_net_join")


def test_same_net_t_junction_is_allowed_and_not_counted_as_a_bend():
    case = direct()
    case[0]["wires"].append(wire((50, 0), (50, 40)))
    result = check(case)
    assert result["valid"], result
    assert result["metrics"]["bends"] == 0


def test_split_escape_leads_and_same_net_collinear_overlap_are_allowed():
    case = direct()
    case[0]["wires"] = [wire((0, 0), (10, 0)), wire((10, 0), (80, 0)), wire((70, 0), (100, 0))]
    result = check(case)
    assert result["valid"], result
    assert result["metrics"]["wire_length"] == 100
    assert result["metrics"]["segments"] == 3


def test_degree_two_orthogonal_turns_count_as_bends():
    case = direct()
    case[0]["wires"] = [wire((0, 0), (30, 0)), wire((30, 0), (30, -40)),
                        wire((30, -40), (70, -40)), wire((70, -40), (70, 0)), wire((70, 0), (100, 0))]
    result = check(case)
    assert result["valid"], result
    assert result["metrics"]["bends"] == 4
    assert result["metrics"]["wire_length"] == 180


@pytest.mark.parametrize("replacement", [wire((0, 0), (0, 0)), wire((0, 0), (100, 10)),
                                        {"from": point(0, 0), "to": point(100, 0)}])
def test_zero_diagonal_or_unowned_segments_are_invalid(replacement):
    case = direct()
    case[0]["wires"] = [replacement]
    assert_invalid(case, "invalid_wire")


@pytest.mark.parametrize("mutation", [
    lambda c: c[0]["instances"][0].update(position=point(1, 0)),
    lambda c: c[1][0]["pins"][0].update(position=point(1, 0)),
    lambda c: c[0]["wires"][0].update(to=point(101, 0)),
])
def test_grid_is_checked_for_components_pins_and_wires(mutation):
    case = direct()
    mutation(case)
    assert_invalid(case, "off_grid")


def test_bounds_are_not_required_to_lie_on_grid():
    case = direct()
    case[1][0]["bounds"] = box(-31, -15, 2, 15)
    result = check(case)
    assert result["valid"], result


@pytest.mark.parametrize("mutation,code", [
    (lambda c: c[0]["instances"].append(deepcopy(c[0]["instances"][0])), "duplicate_instance"),
    (lambda c: c[1].append(deepcopy(c[1][0])), "duplicate_descriptor"),
    (lambda c: c[1].pop(), "descriptor_mismatch"),
    (lambda c: c[1][0]["pins"].append(deepcopy(c[1][0]["pins"][0])), "duplicate_pin"),
    (lambda c: c[2].append(deepcopy(c[2][0])), "duplicate_net"),
    (lambda c: c[2][0]["endpoints"].append(deepcopy(c[2][0]["endpoints"][0])), "duplicate_endpoint"),
    (lambda c: c[2][0]["endpoints"].pop(), "pin_ownership_mismatch"),
    (lambda c: c[0]["wires"][0].update(net_id="missing"), "unknown_wire_net"),
])
def test_identifiers_descriptors_and_pin_owners_must_be_unambiguous(mutation, code):
    case = direct()
    mutation(case)
    assert_invalid(case, code)


def test_coincident_pin_positions_are_rejected_even_if_same_net():
    case = direct()
    case[1][1]["pins"][0]["position"] = point(0, 0)
    assert_invalid(case, "coincident_pins")


def test_native_descriptor_position_must_match_placement():
    case = direct()
    case[1][0]["position"] = point(10, 0)
    assert_invalid(case, "stale_descriptor")


def add_obstacle(case, bounds, body=None):
    x = (bounds["min"]["x"] + bounds["max"]["x"]) // 20 * 10
    instance, descriptor, endpoint = pad("obstacle", (x, 70), (1, 0), "output")
    descriptor["bounds"] = bounds
    descriptor["body_bounds"] = body or deepcopy(bounds)
    case[0]["instances"].append(instance)
    case[1].append(descriptor)
    case[2].append({"id": "singleton", "endpoints": [endpoint]})


def test_route_through_unrelated_component_body_is_rejected():
    case = direct()
    add_obstacle(case, box(40, -10, 60, 80))
    assert_invalid(case, "wire_body_collision")


def test_route_through_label_reservation_is_rejected():
    case = direct()
    add_obstacle(case, box(40, -10, 60, 80), box(40, 60, 60, 80))
    assert_invalid(case, "wire_label_or_clearance_collision")


def test_exact_clearance_is_allowed_and_smaller_clearance_is_rejected():
    case = direct()
    add_obstacle(case, box(40, 10, 60, 80))
    assert check(case)["valid"]
    case[1][-1]["bounds"]["min"]["y"] = 9
    assert_invalid(case, "wire_label_or_clearance_collision")


def test_component_overlap_and_spacing_use_label_inclusive_bounds():
    case = direct()
    add_obstacle(case, box(90, 5, 110, 80))
    assert_invalid(case, "component_overlap")
    case[1][-1]["bounds"] = box(90, 15, 110, 80)
    assert_invalid(case, "component_clearance")


def test_turning_inside_own_bounds_is_rejected():
    case = direct()
    case[0]["wires"] = [wire((0, 0), (0, -40)), wire((0, -40), (100, -40)), wire((100, -40), (100, 0))]
    assert_invalid(case, "wire_body_collision")


def test_lead_heading_into_own_body_is_rejected():
    case = direct()
    case[0]["wires"].append(wire((0, 0), (-50, 0)))
    assert_invalid(case, "wire_body_collision")


def test_foreign_net_contacting_a_pin_is_rejected():
    case = crossing()
    case[0]["wires"].append(wire((0, 0), (50, 0), "vertical"))
    assert_invalid(case, "foreign_pin_contact")


def test_unreached_and_strict_interior_pins_are_rejected():
    case = direct()
    case[0]["wires"][0]["to"] = point(80, 0)
    assert_invalid(case, "unreached_pin")
    case[0]["wires"][0]["to"] = point(120, 0)
    assert_invalid(case, "pin_in_wire_interior")


def test_singleton_pin_does_not_require_a_wire():
    case = direct()
    case[0]["wires"] = []
    case[2][:] = [{"id": "a", "endpoints": [case[2][0]["endpoints"][0]]},
                 {"id": "b", "endpoints": [case[2][0]["endpoints"][1]]}]
    assert check(case)["valid"]


def test_invalid_coordinate_types_and_escape_vectors_return_structured_errors():
    case = direct()
    case[1][0]["pins"][0]["escape"] = point(1, 1)
    assert_invalid(case, "invalid_geometry_input")
    case = direct()
    case[0]["instances"][0]["position"] = point(True, 0)
    assert_invalid(case, "invalid_geometry_input")
    assert_invalid(direct(), "invalid_geometry_input", clearance=-1)


def test_body_and_pin_must_stay_inside_label_inclusive_native_bounds():
    case = direct()
    case[1][0]["body_bounds"] = box(-100, -10, 2, 10)
    assert_invalid(case, "invalid_native_bounds")
    case = direct()
    case[1][0]["pins"][0]["position"] = point(-100, 0)
    assert_invalid(case, "pin_outside_bounds")


def test_pin_escape_direction_is_checked_even_at_zero_clearance():
    case = direct()
    case[0]["wires"].append(wire((0, 0), (0, -40)))
    assert_invalid(case, "invalid_pin_escape", clearance=0)


def test_restore_ownership_supports_splits_merges_and_reversed_segments():
    original = [wire((0, 0), (30, 0)), wire((70, 0), (30, 0)), wire((70, 0), (100, 0))]
    normalized = [wire((100, 0), (50, 0), "obsolete"), wire((50, 0), (0, 0), "obsolete")]
    before = deepcopy((original, normalized))
    restored = restore_wire_ownership(original, normalized)
    assert [segment["net_id"] for segment in restored] == ["data", "data"]
    assert (original, normalized) == before
    restored[0]["from"]["x"] = 999
    assert normalized[0]["from"]["x"] == 100


def test_restore_ownership_ignores_perpendicular_crossing_and_endpoint_contacts():
    original = [wire((0, 0), (100, 0)), wire((50, -50), (50, 50), "cross"), wire((100, 0), (120, 0), "next")]
    assert restore_wire_ownership(original, [wire((0, 0), (100, 0))])[0]["net_id"] == "data"


def test_restore_vertical_ownership_with_overlapping_original_segments():
    original = [wire((0, 0), (0, 50)), wire((0, 100), (0, 40))]
    restored = restore_wire_ownership(original, [wire((0, 100), (0, 0))])
    assert restored[0]["net_id"] == "data"


@pytest.mark.parametrize("original,normalized", [
    ([], [wire((0, 0), (100, 0))]),
    ([wire((0, 0), (40, 0)), wire((50, 0), (100, 0))], [wire((0, 0), (100, 0))]),
    ([wire((0, 0), (50, 0)), wire((50, 0), (100, 0), "other")], [wire((0, 0), (100, 0))]),
    ([wire((0, 0), (100, 0)), wire((40, 0), (60, 0), "other")], [wire((0, 0), (100, 0))]),
    ([wire((0, 0), (100, 0))], [wire((-10, 0), (100, 0))]),
    ([wire((0, 0), (100, 0))], [wire((0, 10), (100, 10))]),
])
def test_restore_ownership_fails_closed_on_missing_gapped_or_mixed_ancestry(original, normalized):
    with pytest.raises(ValueError):
        restore_wire_ownership(original, normalized)
