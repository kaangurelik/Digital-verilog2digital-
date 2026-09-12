"""Crossing, ownership and obstacle rules for compact routing, independent of ELK."""
from copy import deepcopy
import time

import pytest

from verilog2digital.adapter import Adapter, AdapterError
from verilog2digital.layout.general import layout_general
from verilog2digital.layout.geometry import validate_geometry
from verilog2digital.layout.routing import RoutingFailure, _Index, _Search, merge_segments, route_orthogonal
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.mapping import map_scalar
from test_geometry import direct, crossing, pad, wire
from test_scalar_mapping import design


def routed(case):
    circuit, descriptors, nets = deepcopy(case)
    wires, metrics = route_orthogonal(descriptors, nets, deadline=time.monotonic()+5)
    circuit["wires"] = wires
    result = validate_geometry(circuit, descriptors, nets=nets)
    assert result["valid"], result["diagnostics"]
    return circuit, result, metrics


def test_straight_native_pin_leads_merge_without_changing_attachment():
    circuit, geometry, _ = routed(direct())
    assert geometry["metrics"]["wire_length"] == 100
    assert len(circuit["wires"]) == 1


def test_distinct_net_strict_crossing_remains_electrically_separate():
    _, geometry, _ = routed(crossing())
    assert geometry["metrics"]["crossings"] == 1


def test_obstacle_labels_and_singleton_pins_are_reserved():
    case = direct()
    sink = pad("sink", (200, 0), (-1, 0), "input")
    case[0]["instances"][1], case[1][1], case[2][0]["endpoints"][1] = sink
    extra = pad("obstacle", (100, 0), (0, 1), "output")
    case[0]["instances"].append(extra[0])
    case[1].append(extra[1])
    case[2].append({"id": "unused", "endpoints": [extra[2]]})
    circuit, geometry, _ = routed(case)
    assert geometry["metrics"]["wire_length"] > 200
    assert not any(wire["net_id"] == "unused" for wire in circuit["wires"])


def test_fanout_branches_share_one_owned_tree():
    case = direct()
    for index, y in enumerate((-100, 100)):
        extra = pad(f"sink{index}", (100, y), (-1, 0), "input")
        case[0]["instances"].append(extra[0])
        case[1].append(extra[1])
        case[2][0]["endpoints"].append(extra[2])
    circuit, geometry, _ = routed(case)
    assert {wire["net_id"] for wire in circuit["wires"]} == {"data"}
    assert geometry["metrics"]["wire_length"] < 500


def test_index_rejects_foreign_t_joins_overlaps_and_crossing_turns():
    index = _Index([], range(-100, 101, 10), range(-100, 101, 10))
    index.add((-100, 0), (100, 0), "foreign")
    assert index.edge((0, -20), (0, 20), "mine")
    assert index.node((0, 0), 1, "mine")
    assert not index.node((0, 0), 0, "mine")
    assert not index.node((0, 0), 1, "mine", terminal=True)
    assert not index.edge((100, -20), (100, 20), "mine")
    assert not index.edge((100, 0), (120, 0), "mine")
    assert not index.edge((-20, 0), (20, 0), "mine")


def test_same_net_four_way_junction_retains_both_endpoint_orientations():
    wires = [wire((-100, 0), (0, 0)), wire((0, 0), (100, 0)),
             wire((0, -100), (0, 0)), wire((0, 0), (0, 100))]
    merged = merge_segments(wires)
    assert len(merged) == 4
    assert all({"x": 0, "y": 0} in (segment["from"], segment["to"]) for segment in merged)
    foreign = merge_segments([wire((-100, 0), (100, 0), "horizontal"), wire((0, -100), (0, 100), "vertical")])
    assert len(foreign) == 2
    assert all({"x": 0, "y": 0} not in (segment["from"], segment["to"]) for segment in foreign)


def test_astar_direction_state_crosses_foreign_wire_without_a_turn():
    coordinates = set(range(-100, 101, 10))
    search = _Search([], (coordinates, coordinates), [("foreign", (-100, 0), (100, 0))], time.monotonic()+5)
    path = search.astar((0, -50), [((0, 50), (0, 60))], "mine")
    assert path == [(0, -50), (0, 50)]


def test_expired_route_budget_is_bounded():
    with pytest.raises(RoutingFailure, match="time budget"):
        route_orthogonal(direct()[1], direct()[2], deadline=time.monotonic()-1)


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as worker:
        yield worker


class FixedPlacement:
    def __init__(self, adapter):
        self.adapter = adapter

    def call(self, method, params, **kwargs):
        if method != "layout.plan":
            return self.adapter.call(method, params, **kwargs)
        positions = {"in0000": (0, 0), "in0001": (0, 80), "in0002": (0, 160), "g0000": (180, 40), "out0000": (360, 40)}
        return {"schema_version": 1, "placements": [{"id": item["id"], "position": dict(zip(("x", "y"), positions[item["id"]])), "rotation": item["rotation"]}
                for item in params["instances"]], "wires": []}


@pytest.mark.parametrize("planner", ["fixed", "elk"])
def test_fixed_native_placement_routes_compactly_and_simulates_original_logic(adapter, tmp_path, planner):
    mapped = map_scalar(design(), adapter)
    before = deepcopy(mapped)
    result = layout_general(mapped, FixedPlacement(adapter) if planner == "fixed" else adapter)
    assert mapped == before
    assert not result["fallback_used"], result["diagnostics"]
    reserved = layout_reserved(mapped)
    assert any(result["metrics"][metric] < reserved["geometry"]["metrics"][metric]
               for metric in ("bounding_area", "wire_length"))
    emitted = adapter.call("circuit.emit", {"circuit": result["circuit"], "path": str(tmp_path / "compact.dig")})
    paths = {key: emitted[key] for key in ("path", "sidecar_path")}
    assert adapter.call("circuit.inspect", paths)["model"]["valid"]
    vectors = [{"values": {"in0000": hex(a), "in0001": hex(b), "in0002": "0x0"}} for a in (0, 1) for b in (0, 1)]
    samples = adapter.call("circuit.simulate", {**paths, "stimuli": vectors})["samples"]
    assert [sample["values"]["out0000"] for sample in samples] == ["0x0", "0x0", "0x0", "0x1"]


def test_compact_output_is_deterministic_under_mapped_record_permutations(adapter):
    mapped = map_scalar(design(), adapter)
    first = layout_general(mapped, FixedPlacement(adapter))
    for key in ("instances", "descriptors", "nets"):
        mapped[key].reverse()
    second = layout_general(mapped, FixedPlacement(adapter))
    assert first["circuit"] == second["circuit"]


def test_zero_budget_fallback_is_truthful_without_a_native_request(adapter):
    class NoCalls:
        def call(self, *_args, **_kwargs):
            pytest.fail("Zero compact budget must skip native planning")
    mapped = map_scalar(design(), adapter)
    result = layout_general(mapped, NoCalls(), timeout=0)
    assert result["fallback_used"] and result["fallback_reason"]
    assert result["strategy"] == "scalar-reserved-tracks-v1"
    assert result["geometry"]["valid"]


def test_late_valid_candidate_is_reported_as_budget_fallback(adapter, monkeypatch):
    from verilog2digital.layout import general
    original = general.route_orthogonal
    def slow_finalization(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.12)
        return result
    monkeypatch.setattr(general, "route_orthogonal", slow_finalization)
    result = layout_general(map_scalar(design(), adapter), FixedPlacement(adapter), timeout=0.1)
    assert result["fallback_used"]
    assert "verification exceeded" in result["fallback_reason"]
    assert result["metrics"]["fallback_seconds"] >= 0


def test_unusable_native_transport_failure_propagates(adapter):
    class Broken:
        _closed = True
        def call(self, *_args, **_kwargs):
            raise AdapterError("Adapter closed after timeout")
    with pytest.raises(AdapterError, match="closed"):
        layout_general(map_scalar(design(), adapter), Broken())


def test_recoverable_planning_failure_reports_elapsed_planning(adapter):
    class Unavailable:
        _closed = False
        def call(self, *_args, **_kwargs):
            time.sleep(0.02)
            raise AdapterError("Native layout planning timed out", code=-32000)
    result = layout_general(map_scalar(design(), adapter), Unavailable())
    assert result["fallback_used"]
    assert result["metrics"]["planning_seconds"] >= 0.02
    assert result["metrics"]["compact_seconds"] >= result["metrics"]["planning_seconds"]


def test_malformed_planner_cannot_change_electrical_components(adapter):
    class Changed(FixedPlacement):
        def call(self, method, params, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "layout.plan":
                response["placements"][0]["id"] = "unknown"
            return response
    mapped = map_scalar(design(), adapter)
    result = layout_general(mapped, Changed(adapter))
    assert result["fallback_used"]
    assert {item["id"] for item in result["circuit"]["instances"]} == {item["id"] for item in mapped["instances"]}


@pytest.mark.parametrize("params", [{"spacing": 10}, {"seed": 0}, {"seed": True}, {"timeout": -1}, {"timeout": float("nan")}])
def test_invalid_general_options_fail_clearly(adapter, params):
    with pytest.raises(ValueError):
        layout_general(map_scalar(design(), adapter), FixedPlacement(adapter), **params)
