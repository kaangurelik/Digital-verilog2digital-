"""Tunnel canvases are refined as visible wires; the same tunnels are then reinserted."""
import json
from pathlib import Path
import time

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.conversion import convert
from verilog2digital.frontend.project import Project
from verilog2digital.jobs.publication import digest, read_document
from verilog2digital.layout import candidates, tunnels
from verilog2digital.layout.candidates import QUALITY_METRICS, route_placed
from verilog2digital.llm.config import DEFAULTS
from verilog2digital.llm.context import build_context


ROOT = Path(__file__).resolve().parents[2]
HIERARCHY = """module bus_children(input [3:0] a,b,c,d, output [3:0] y,z);
wire [3:0] p,q;
bus_child first(.a(a),.b(b),.y(p));
bus_child second(.a(c),.b(d),.y(q));
assign y=p^q;
assign z=p&q;
endmodule
module bus_child(input [3:0] a,b, output [3:0] y);
assign y=a^b;
endmodule
"""


@pytest.fixture(scope="module")
def native():
    with Adapter() as adapter:
        yield adapter


def run(project, output, native, responses):
    return convert(project, output, adapter=native, layout_strategy="reserved", tunnels=True, random_count=4,
                   refinement={"mode": "replay", "responses": responses, "max_attempts": max(1, len(responses))})


def improving_reroute(baseline):
    """Choose a visible reroute that the strict native Pareto rule accepts."""
    mapped, layout = baseline["mapped"], baseline["layout"]
    prompt = build_context(mapped, layout, validation=baseline["validation"], request=DEFAULTS["request"], max_bytes=6900)
    before = baseline["validation"]["geometry"]["metrics"]
    positions = {item["id"]: item["position"] for item in layout["circuit"]["instances"]}
    for net in prompt["metadata"]["visible_net_ids"]:
        for order in ("default", "reverse"):
            metrics = route_placed(mapped, layout, positions, spacing=layout["spacing"], deadline=time.monotonic() + 20,
                                   priority_nets=[net], reverse=order == "reverse")["geometry"]["metrics"]
            deltas = [metrics[key] - before[key] for key in QUALITY_METRICS]
            if max(deltas) <= 0 and min(deltas) < 0:
                return {"action": "reroute_nets", "net_ids": [net], "order": order}
    pytest.fail("No visible reroute improves the reserved baseline")


def response(baseline, actions):
    return json.dumps({"schema_version": 1, "candidate_id": baseline["candidate_id"], "actions": actions,
                       "explanation": "Reroute the visible-wire canvas."})


def recorded(project, directory, native):
    """Without proposals the validated tunneled baseline is published unchanged."""
    result = run(project, directory, native, [])
    manifest = read_document(Path(result["manifest"]))
    canvases = manifest["refinement"]["canvases"]
    target = max(index for index, canvas in enumerate(canvases) if canvas.get("tunnels"))
    canvas = canvases[target]
    assert canvas["tunnels"]["outcome"] == "baseline_retained" and canvas["tunnels"]["refined_canvas"] == "visible_wires"
    assert canvas["tunnels"]["planned"] and canvas["accepted"] == 0
    baselines = [read_document(Path(result["directory"]) / f"work/refinement/canvas-{item['canvas']:04d}/baseline.json")
                 for item in canvases]
    assert all(item["candidate_id"] == canvas["baseline_id"] for item, canvas in zip(baselines, canvases))
    # Keep earlier canvases unchanged so the target's hash is reproducible.
    responses = [response(item, []) for item in baselines[:target]]
    responses.append(response(baselines[target], [improving_reroute(baselines[target])]))
    return {"result": result, "manifest": manifest, "target": target, "responses": responses,
            "planned": canvas["tunnels"]["planned"]}


@pytest.fixture(scope="module")
def flat(native, tmp_path_factory):
    project = Project([ROOT / "tests/fixtures/verilog/scalar/scalar_mux.v"], top="scalar_mux")
    return project, recorded(project, tmp_path_factory.mktemp("flat-tunnels") / "record", native)


@pytest.fixture(scope="module")
def hierarchy(native, tmp_path_factory):
    source = tmp_path_factory.mktemp("hierarchy-source") / "bus_children.v"
    source.write_text(HIERARCHY, encoding="utf-8")
    project = Project([source], top="bus_children")
    return project, recorded(project, tmp_path_factory.mktemp("hierarchy-tunnels") / "record", native)


@pytest.fixture
def unmatched_tunnel_names(monkeypatch):
    """Corrupt only the post-refinement insertion; native validation must reject it."""
    original = tunnels.add_long_tunnels

    def corrupt(layout, mapped, adapter, **options):
        result = original(layout, mapped, adapter, **options)
        if options.get("nets") is not None:
            next(item for item in result["circuit"]["instances"] if item["type"] == "Tunnel")["attributes"]["NetName"] = "Unmatched"
        return result

    monkeypatch.setattr(tunnels, "add_long_tunnels", corrupt)


def published(result, record, module=None):
    manifest = read_document(Path(result["manifest"]))
    validation = read_document(Path(result["validation"]))
    assert manifest["state"] == "ready" and validation["valid"] and validation["behavior"]["valid"]
    canvas = manifest["refinement"]["canvases"][record["target"]]
    layout = manifest["layout"]["modules"][canvas["module_id"]] if module else manifest["layout"]
    views = read_document(Path(result["preview_metadata"]))["views"]
    identities = {name: [item["id"] for item in view["instances"]] for name, view in views.items()}
    assert identities["baseline"] == identities["selected"]
    assert views["selected"]["circuit"]["sha256"] == digest(Path(result["circuit"]))
    assert canvas["accepted"] == 1 and canvas["published_metrics"] == layout["metrics"]
    assert all(canvas["selected_metrics"][key] <= canvas["baseline_metrics"][key] for key in QUALITY_METRICS)
    assert canvas["selected_metrics"] != canvas["baseline_metrics"]
    return manifest, canvas, layout, views


def check_inserted(result, record, module=None):
    manifest, canvas, layout, views = published(result, record, module)
    assert canvas["tunnels"]["outcome"] == "inserted_after_refinement"
    assert canvas["tunnels"]["planned"] == record["planned"]
    assert [{"net_id": item["net_id"], "name": item["name"]} for item in layout["tunnels"]["nets"]] == record["planned"]
    assert layout["strategy"] == "constrained-orthogonal-v1" and not layout["fallback_used"]
    assert any(item.startswith("tunnel_") for item in (entry["id"] for entry in views["selected"]["instances"]))
    # The comparison canvas is the tunneled result published without an accepted proposal.
    record_canvas = record["manifest"]["refinement"]["canvases"][record["target"]]
    assert canvas["baseline_metrics"] == record_canvas["baseline_metrics"]
    assert views["baseline"]["circuit"]["sha256"] == record["manifest"]["artifacts"]["circuit"]["sha256"]


def check_fallback(result, record, module=None, code="TUNNEL_VALIDATION"):
    manifest, canvas, layout, views = published(result, record, module)
    assert canvas["tunnels"]["outcome"] == "visible_fallback"
    assert canvas["tunnels"]["fallback_reason"] == "native_tunnel_validation"
    assert canvas["tunnels"]["diagnostic"]["code"] == code
    assert canvas["published_metrics"] == canvas["selected_metrics"]
    assert layout["fallback_used"] and layout["fallback_reason"] == "native_tunnel_validation"
    assert layout["tunnels"] == {"mode": "visible", "nets": []}
    assert not any(entry["type"] == "Tunnel" for view in views.values() for entry in view["instances"])
    assert any(item["code"] == "TUNNEL_FALLBACK" and item["canvas"] == canvas["canvas"]
               for item in manifest["refinement"]["diagnostics"])
    return canvas, views


def test_flat_refined_visible_wires_receive_the_planned_tunnels(flat, native, tmp_path):
    project, record = flat
    result = run(project, tmp_path / "inserted", native, record["responses"])
    check_inserted(result, record)


def test_flat_tunnel_validation_failure_publishes_refined_visible_wires(flat, native, tmp_path, unmatched_tunnel_names):
    project, record = flat
    result = run(project, tmp_path / "fallback", native, record["responses"])
    _, views = check_fallback(result, record)
    visible = Path(result["directory"]) / "work/refinement-visible/candidate.dig"
    assert views["baseline"]["circuit"]["sha256"] == digest(visible)


def test_flat_incomplete_tunnel_plan_publishes_refined_visible_wires(flat, native, tmp_path, monkeypatch):
    project, record = flat
    original = tunnels.add_long_tunnels

    def exhausted(layout, mapped, adapter, **options):
        return original(layout, mapped, adapter, **{**options, "timeout": 0} if options.get("nets") is not None else options)

    monkeypatch.setattr(tunnels, "add_long_tunnels", exhausted)
    result = run(project, tmp_path / "incomplete", native, record["responses"])
    canvas, _ = check_fallback(result, record, code="TUNNEL_PLAN_INCOMPLETE")
    assert canvas["tunnels"]["budget_exhausted"] is True and canvas["tunnels"]["skipped"] == []


def test_flat_retained_baseline_equals_the_refinement_free_tunnel_result(flat, native, tmp_path):
    project, record = flat
    plain = convert(project, tmp_path / "off", adapter=native, layout_strategy="reserved", tunnels=True, random_count=4)
    manifest = read_document(Path(plain["manifest"]))
    assert manifest["artifacts"]["circuit"] == record["manifest"]["artifacts"]["circuit"]
    assert manifest["layout"]["tunnels"] == record["manifest"]["layout"]["tunnels"]
    assert record["manifest"]["refinement"]["canvases"][0]["published_metrics"] == manifest["layout"]["metrics"]
    assert "refinement-tunneled" not in {path.name for path in (Path(record["result"]["directory"]) / "work").iterdir()}


def test_hierarchy_refined_canvas_receives_the_planned_tunnels(hierarchy, native, tmp_path):
    project, record = hierarchy
    result = run(project, tmp_path / "inserted", native, record["responses"])
    check_inserted(result, record, module=True)
    package = read_document(Path(result["package"]))
    assert {item["path"] for item in package["snapshot"]["files"]} >= {"circuit.dig", "circuit.dig.ids.json"}
    assert not any("-visible" in item["path"] or "-tunneled" in item["path"] for item in package["snapshot"]["files"])


def test_hierarchy_tunnel_validation_failure_publishes_refined_visible_wires(hierarchy, native, tmp_path,
                                                                             unmatched_tunnel_names):
    project, record = hierarchy
    result = run(project, tmp_path / "fallback", native, record["responses"])
    canvas, views = check_fallback(result, record, module=True)
    manifest = read_document(Path(result["manifest"]))
    assert manifest["layout"]["fallback_used"] and canvas == manifest["refinement"]["canvases"][-1]
    # The top canvas compares against its validated visible wires under a private package basename.
    assert views["baseline"]["circuit"]["sha256"] == digest(Path(result["directory"]) / "work/package/circuit-visible.dig")


def test_unvalidated_visible_canvas_keeps_the_tunneled_baseline_unrefined(flat, native, tmp_path, monkeypatch):
    project, record = flat
    original = candidates.verify_candidate

    def rejected_visible(mapped, layout, adapter, directory, **options):
        result = original(mapped, layout, adapter, directory, **options)
        if Path(directory).name == "refinement-visible":
            result["validation"]["valid"] = False
        return result

    monkeypatch.setattr(candidates, "verify_candidate", rejected_visible)
    result = run(project, tmp_path / "unrefined", native, record["responses"])
    manifest = read_document(Path(result["manifest"]))
    canvas = manifest["refinement"]["canvases"][0]
    assert canvas["tunnels"]["outcome"] == "not_refined" and canvas["skipped"] and not canvas["attempts"]
    assert canvas["diagnostic"]["code"] == "VISIBLE_CANVAS_INVALID"
    assert manifest["refinement"]["accepted"] == 0 and manifest["refinement"]["diagnostics"][0]["code"] == "TUNNEL_CANVAS_UNREFINED"
    assert manifest["artifacts"]["circuit"] == record["manifest"]["artifacts"]["circuit"]
    assert canvas["published_metrics"] == manifest["layout"]["metrics"]
    assert read_document(Path(result["validation"]))["behavior"]["valid"]
