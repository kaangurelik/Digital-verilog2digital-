"""Optional edits retain immutable intent and survive native serialization checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import pytest

from verilog2digital.adapter import Adapter, AdapterError
from verilog2digital.frontend.hierarchy import lower_hierarchy
from verilog2digital.frontend.project import Project
from verilog2digital.hierarchical_conversion import PackageAdapter
from verilog2digital.layout.candidates import compare_quality, route_placed, verify_candidate
from verilog2digital.layout.general import layout_general
from verilog2digital.layout.routing import RoutingFailure, _Search, route_orthogonal
from verilog2digital.layout.scalar import LayoutError, layout_reserved
from verilog2digital.mapping import map_datapath, map_hierarchy_module, map_scalar, map_sequential
from test_datapath_mapping import design as datapath_design
from test_geometry import crossing
from test_scalar_mapping import design
from test_sequential_mapping import register_design


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as native:
        yield native


@pytest.fixture(scope="module")
def basic(adapter):
    mapped = map_scalar(design(), adapter)
    layout = layout_reserved(mapped)
    layout["circuit"]["label"] = "Preserve this module <&> label"
    return mapped, layout


def positions(layout, dx=0, dy=0):
    return {item["id"]: {"x": item["position"]["x"] + dx, "y": item["position"]["y"] + dy}
            for item in layout["circuit"]["instances"]}


def compact_positions():
    values = {"in0000": (0, 0), "in0001": (0, 80), "in0002": (0, 160),
              "g0000": (180, 40), "out0000": (360, 40)}
    return {key: dict(zip(("x", "y"), value)) for key, value in values.items()}


def route(mapped, baseline, wanted=None, **options):
    return route_placed(mapped, baseline, positions(baseline) if wanted is None else wanted,
                        deadline=time.monotonic() + 20, **options)


def verify(mapped, layout, adapter, directory):
    return verify_candidate(mapped, layout, adapter, directory, deadline=time.monotonic() + 20)


def test_exact_move_preserves_order_attributes_labels_and_input_objects(basic):
    mapped, original = basic
    baseline = deepcopy(original)
    baseline["circuit"]["instances"].reverse()
    before = deepcopy((mapped, baseline))
    wanted = compact_positions()
    result = route(mapped, baseline, wanted)
    assert (mapped, baseline) == before
    assert result["circuit"]["label"] == baseline["circuit"]["label"]
    assert [item["id"] for item in result["circuit"]["instances"]] == [item["id"] for item in baseline["circuit"]["instances"]]
    assert positions(result) == wanted
    assert result["fallback_used"] is False
    for actual, prior in zip(result["circuit"]["instances"], baseline["circuit"]["instances"]):
        assert {k: v for k, v in actual.items() if k != "position"} == {k: v for k, v in prior.items() if k != "position"}
    assert [item["id"] for item in result["descriptors"]] == [item["id"] for item in result["circuit"]["instances"]]


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("g0000"),
    lambda p: p.update(unknown={"x": 0, "y": 0}),
    lambda p: p["g0000"].update(x=True),
    lambda p: p["g0000"].update(x=20.0),
    lambda p: p["g0000"].update(x=10),
    lambda p: p["g0000"].update(x=1_000_020),
    lambda p: p["g0000"].update(extra=0),
])
def test_invalid_position_changes_fail_without_routing(basic, mutation, monkeypatch):
    import verilog2digital.layout.candidates as candidates
    mapped, baseline = basic
    wanted = compact_positions()
    mutation(wanted)
    monkeypatch.setattr(candidates, "route_orthogonal", lambda *args, **kwargs: pytest.fail("Invalid action reached the router"))
    with pytest.raises(LayoutError) as error:
        route(mapped, baseline, wanted)
    assert error.value.code == "INVALID_CANDIDATE_POSITIONS"


def test_collision_is_rejected_without_silently_moving_a_component(basic):
    mapped, baseline = basic
    wanted = compact_positions()
    wanted["g0000"] = deepcopy(wanted["in0000"])
    before = deepcopy(wanted)
    with pytest.raises(LayoutError) as error:
        route(mapped, baseline, wanted)
    assert error.value.code == "CANDIDATE_PLACEMENT_CONFLICT"
    assert len(error.value.details["instances"]) == 2
    assert wanted == before


@pytest.mark.parametrize("mutation", [
    lambda c: c["instances"][0]["attributes"].update(Label="changed"),
    lambda c: c["instances"][0].update(rotation=90),
    lambda c: c["instances"][0].update(type="Not"),
    lambda c: c["instances"].pop(),
])
def test_candidate_baseline_cannot_redefine_mapped_components(basic, mutation):
    mapped, layout = basic
    baseline = deepcopy(layout)
    mutation(baseline["circuit"])
    with pytest.raises(LayoutError) as error:
        route(mapped, baseline, compact_positions())
    assert error.value.code == "CANDIDATE_COMPONENT_CHANGED"


def test_expired_deadline_does_not_enter_router_or_native_code(basic):
    mapped, baseline = basic
    with pytest.raises(LayoutError) as error:
        route_placed(mapped, baseline, positions(baseline), deadline=time.monotonic() - 1)
    assert error.value.code == "CANDIDATE_TIMEOUT"


def test_prioritized_net_is_actually_routed_first(monkeypatch):
    _, descriptors, nets = crossing()
    original = _Search.route
    orders = []

    def observed(self, start, tree, owner):
        orders.append(owner)
        return original(self, start, tree, owner)

    monkeypatch.setattr(_Search, "route", observed)
    route_orthogonal(descriptors, nets, deadline=time.monotonic() + 5)
    assert orders == ["data", "vertical"]
    orders.clear()
    route_orthogonal(descriptors, nets, deadline=time.monotonic() + 5, priority_nets=["vertical"])
    assert orders == ["vertical", "data"]


@pytest.mark.parametrize("options", [
    {"priority_nets": ["unknown"]}, {"priority_nets": ["data", "data"]},
    {"priority_nets": "data"}, {"priority_nets": [False]}, {"reverse": 1},
])
def test_invalid_routing_priority_is_rejected(options):
    _, descriptors, nets = crossing()
    with pytest.raises(RoutingFailure):
        route_orthogonal(descriptors, nets, deadline=time.monotonic() + 5, **options)


def report(**metrics):
    return {"valid": True, "topology": {"valid": True}, "bits": {"valid": True}, "components": {"valid": True},
            "model": {"valid": True}, "geometry": {"valid": True, "metrics": {
                "bounding_area": 1000, "wire_length": 100, "bends": 10, "crossings": 2, **metrics}}}


@pytest.mark.parametrize("candidate,relation,improved", [
    (report(), "equal", False),
    (report(bounding_area=800), "improved", True),
    (report(wire_length=80, bends=8, crossings=0), "improved", True),
    (report(bounding_area=800, crossings=3), "tradeoff", False),
    (report(wire_length=101), "worse", False),
    ({"valid": False, "metrics": {}}, "invalid", False),
    (report(bends=True), "invalid", False),
    (report(crossings=-1), "invalid", False),
    (report(bounding_area=float("nan")), "invalid", False),
])
def test_quality_requires_verified_strict_pareto_improvement(candidate, relation, improved):
    result = compare_quality(report(), candidate)
    assert result["relation"] == relation
    assert result["improved"] is improved


def test_failed_incumbent_cannot_be_laundered_by_better_candidate_metrics():
    assert compare_quality({"valid": False}, report(bounding_area=1))["relation"] == "invalid"


def test_geometry_only_or_false_electrical_subcheck_cannot_claim_improvement():
    candidate = report(bounding_area=1)
    assert compare_quality(report(), {"valid": True, "metrics": candidate["geometry"]["metrics"]})["relation"] == "invalid"
    candidate["topology"]["valid"] = False
    assert compare_quality(report(), candidate)["relation"] == "invalid"


def test_native_compact_candidate_beats_reserved_and_preserves_original_truth_table(basic, adapter, tmp_path):
    mapped, baseline = basic
    incumbent = verify(mapped, baseline, adapter, tmp_path / "baseline")
    candidate = route(mapped, baseline, compact_positions())
    checked = verify(mapped, candidate, adapter, tmp_path / "candidate")
    assert incumbent["valid"] and checked["valid"], checked["validation"]
    assert compare_quality(incumbent["validation"], checked["validation"])["improved"]
    assert checked["metrics"] == checked["validation"]["geometry"]["metrics"]
    assert checked["hashes"]["circuit"] == hashlib.sha256(Path(checked["paths"]["path"]).read_bytes()).hexdigest()
    rows = [{"a": a, "b": b, "s": s} for a in (0, 1) for b in (0, 1) for s in (0, 1)]
    values = adapter.call("circuit.simulate", {**checked["paths"], "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in mapped["inputs"]}} for row in rows]})
    output = mapped["outputs"][0]["instance_id"]
    assert [int(sample["values"][output], 16) for sample in values["samples"]] == [row["a"] & row["b"] for row in rows]


def test_native_reload_preserves_deliberately_noncanonical_serialization_order(basic, adapter, tmp_path):
    mapped, original = basic
    baseline = deepcopy(original)
    baseline["circuit"]["instances"].reverse()
    candidate = route(mapped, baseline, compact_positions())
    checked = verify(mapped, candidate, adapter, tmp_path / "attempt")
    assert checked["valid"], checked["validation"]
    assert [item["id"] for item in checked["inspection"]["instances"]] == [
        item["id"] for item in baseline["circuit"]["instances"]]


def test_native_accidental_short_rejected_without_changing_baseline_files(basic, adapter, tmp_path):
    mapped, baseline = basic
    incumbent = verify(mapped, baseline, adapter, tmp_path / "baseline")
    before = {key: Path(value).read_bytes() for key, value in incumbent["paths"].items()}
    candidate = route(mapped, baseline, compact_positions())
    descriptions = {item["id"]: item for item in candidate["descriptors"]}
    first = descriptions["in0000"]["pins"][0]["position"]
    second = descriptions["in0001"]["pins"][0]["position"]
    owner = next(net["id"] for net in mapped["nets"] if any(e["instance_id"] == "in0000" for e in net["endpoints"]))
    candidate["circuit"]["wires"].append({"from": first, "to": second, "net_id": owner})
    checked = verify(mapped, candidate, adapter, tmp_path / "shorted")
    assert not checked["valid"]
    assert not checked["validation"]["topology"]["valid"]
    assert not compare_quality(incumbent["validation"], checked["validation"])["improved"]
    assert {key: Path(value).read_bytes() for key, value in incumbent["paths"].items()} == before


@pytest.mark.parametrize("which", ["path", "sidecar_path"])
def test_native_validation_artifact_mutation_is_rejected(basic, adapter, tmp_path, which):
    class MutatesAfterInspect:
        def call(self, method, params, **options):
            result = adapter.call(method, params, **options)
            if method == "circuit.inspect":
                with Path(params[which]).open("ab") as stream:
                    stream.write(b"\n")
            return result

    with pytest.raises(LayoutError) as error:
        verify(*basic, MutatesAfterInspect(), tmp_path / "attempt")
    assert error.value.code == "ARTIFACT_CHANGED"


def test_reusing_private_artifact_path_is_rejected_before_native_call(basic, tmp_path):
    class NoCalls:
        def call(self, *args, **kwargs):
            pytest.fail("Existing candidate must not be overwritten")

    directory = tmp_path / "attempt"
    directory.mkdir()
    old = directory / "candidate.dig"
    old.write_bytes(b"keep these bytes")
    with pytest.raises(LayoutError) as error:
        verify(*basic, NoCalls(), directory)
    assert error.value.code == "CANDIDATE_ARTIFACT_EXISTS"
    assert old.read_bytes() == b"keep these bytes"


def test_optimizer_transport_failure_preserves_separate_native_baseline(basic, adapter, tmp_path):
    class ExpiredOptimizer:
        def call(self, method, params, **options):
            assert 0 < options["timeout"] <= 20
            raise AdapterError("optimizer transport timeout")

    incumbent = verify(*basic, adapter, tmp_path / "baseline")
    original = Path(incumbent["paths"]["path"]).read_bytes()
    with pytest.raises(AdapterError, match="timeout"):
        verify(*basic, ExpiredOptimizer(), tmp_path / "attempt")
    assert Path(incumbent["paths"]["path"]).read_bytes() == original
    assert adapter.call("circuit.inspect", incumbent["paths"])["model"]["valid"]


def test_candidate_replay_is_deterministic_including_serialized_bytes(basic, adapter, tmp_path):
    results = []
    for index in range(2):
        layout = route(*basic, compact_positions(), priority_nets=[basic[0]["nets"][0]["id"]])
        results.append(verify(basic[0], layout, adapter, tmp_path / str(index)))
    assert results[0]["layout"]["circuit"] == results[1]["layout"]["circuit"]
    assert results[0]["hashes"] == results[1]["hashes"]


@pytest.mark.parametrize("aw,bw,yw", [(4, 8, 8), (4, 64, 64), (64, 8, 64)])
def test_moved_mixed_width_bus_components_keep_high_bits_and_aliases(adapter, tmp_path, aw, bw, yw):
    mapped = map_datapath(datapath_design("$add", aw, bw, yw), adapter)
    # Real compact placements provide working bus corridors. Sparse reserved
    # placements can legitimately exhaust an optional reroute's search budget.
    baseline = layout_general(mapped, adapter)
    assert not baseline["fallback_used"]
    wanted = positions(baseline)
    gate = next(item for item in mapped["instances"] if item["type"] == "Add")
    wanted[gate["id"]]["y"] += 160
    candidate = route(mapped, baseline, wanted)
    checked = verify(mapped, candidate, adapter, tmp_path / "attempt")
    assert checked["valid"], checked["validation"]
    rows = [{"a": a, "b": b, "s": 0} for a, b in [
        (0, 0), ((1 << aw)-1, 1), (1 << (aw-1), 1 << (bw-1)), ((1 << aw)-1, (1 << bw)-1)]]
    native = adapter.call("circuit.simulate", {**checked["paths"], "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in mapped["inputs"]}} for row in rows]})
    output = mapped["outputs"][0]["instance_id"]
    assert [int(sample["values"][output], 16) for sample in native["samples"]] == [
        (row["a"] + row["b"]) & ((1 << yw)-1) for row in rows]


def test_moved_state_and_feedback_preserve_rising_edge_and_hold_at_64_bits(adapter, tmp_path):
    mapped = map_sequential(register_design(64, enable=True, reset=True), adapter)
    baseline = layout_general(mapped, adapter)
    candidate = route(mapped, baseline, positions(baseline, dx=40, dy=80))
    checked = verify(mapped, candidate, adapter, tmp_path / "attempt")
    assert checked["valid"], checked["validation"]
    inputs = {port["port"]: port["instance_id"] for port in mapped["inputs"]}

    def row(d, en, rst):
        return {"values": {inputs[name]: hex(value) for name, value in {"d": d, "en": en, "rst": rst}.items()}}

    native = adapter.call("circuit.simulate_cycles", {**checked["paths"], "clock_instance_id": inputs["clk"],
        "initialization": {"kind": "input_sequence", "cycles": [row(0, 0, 1)]},
        "cycles": [row(1 << 63, 1, 0), row(0, 0, 0), row(0, 0, 1)]})
    output = mapped["outputs"][0]["instance_id"]
    assert [sample["phase"] for sample in native["samples"]] == ["before_rise", "after_rise", "after_fall"] * 3
    assert [int(sample["values"][output], 16) for sample in native["samples"]] == [
        5, 1 << 63, 1 << 63, 1 << 63, 1 << 63, 1 << 63, 1 << 63, 5, 5]


def test_private_hierarchy_attempt_preserves_dependencies_and_distinct_child_buses(adapter, tmp_path):
    source = tmp_path / "hierarchy.v"
    source.write_text("""
module child(input [3:0] a,b, output [3:0] y); assign y=a^b; endmodule
module top(input [3:0] a,b,c,d, output [3:0] y,z);
 child first(a,b,y); child second(c,d,z);
endmodule
""", encoding="utf-8")
    hierarchy = lower_hierarchy(Project([source], top="top"), tmp_path / "frontend")
    leaf = next(module for module in hierarchy["modules"] if not module["children"])
    leaf_mapping = map_hierarchy_module(hierarchy, leaf["id"], adapter, {})
    directory = tmp_path / "attempt"
    directory.mkdir()
    emitted = adapter.call("circuit.emit", {"circuit": layout_reserved(leaf_mapping)["circuit"],
                                           "path": str(directory / f"{leaf['id']}.dig")})
    entry = {"type": f"{leaf['id']}.dig", "sha256": emitted["sha256"], "sidecar_sha256": emitted["sidecar_sha256"]}
    catalog = {leaf["id"]: entry}
    native = PackageAdapter(adapter, directory, catalog)
    mapped = map_hierarchy_module(hierarchy, hierarchy["top_module_id"], adapter, catalog, package=native.context())
    baseline = layout_reserved(mapped)
    baseline["circuit"]["label"] = "top preserved label"
    dependencies = {key: Path(emitted[key]).read_bytes() for key in ("path", "sidecar_path")}
    wanted = positions(baseline)
    wanted[mapped["children"][0]["instance_id"]]["y"] += 120
    candidate = route(mapped, baseline, wanted)
    checked = verify(mapped, candidate, native, directory)
    assert checked["valid"], checked["validation"]
    assert dependencies == {key: Path(emitted[key]).read_bytes() for key in dependencies}
    rows = [{"a": 3, "b": 5, "c": 9, "d": 12}, {"a": 15, "b": 0, "c": 1, "d": 1}]
    actual = native.call("circuit.simulate", {**checked["paths"], "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in mapped["inputs"]}} for row in rows]})
    assert [{port["port"]: int(sample["values"][port["instance_id"]], 16) for port in mapped["outputs"]}
            for sample in actual["samples"]] == [{"y": row["a"] ^ row["b"], "z": row["c"] ^ row["d"]} for row in rows]
    assert json.loads((directory / "candidate.json").read_text(encoding="utf-8"))["label"] == "top preserved label"
