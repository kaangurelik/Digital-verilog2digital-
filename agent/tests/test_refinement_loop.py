"""Exercise selection against native files, including rejected and stale proposals."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.jobs.publication import digest
from verilog2digital.layout.candidates import verify_candidate
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.llm.actions import candidate_id
from verilog2digital.llm.config import configuration
from verilog2digital.llm.loop import RefinementSession, install_candidate
from verilog2digital.mapping import map_scalar
from test_scalar_mapping import design


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native():
    with Adapter() as adapter:
        yield adapter


@pytest.fixture(scope="module")
def baseline(native, tmp_path_factory):
    mapped = map_scalar(design(), native)
    layout = layout_reserved(mapped)
    directory = tmp_path_factory.mktemp("loop-baseline")
    record = verify_candidate(mapped, layout, native, directory, deadline=time.monotonic() + 30)
    assert record["valid"]
    return mapped, record


def compact_response(mapped, baseline, identifier=None):
    wanted = {"in0000": (0, 0), "in0001": (0, 80), "in0002": (0, 160),
              "g0000": (180, 40), "out0000": (360, 40)}
    actions = []
    for item in baseline["layout"]["circuit"]["instances"]:
        target = wanted[item["id"]]
        delta = {axis: value - item["position"][axis] for axis, value in zip(("x", "y"), target)}
        if any(delta.values()):
            actions.append({"action": "move_component", "instance_id": item["id"], "delta": delta})
    return json.dumps({"schema_version": 1,
                      "candidate_id": identifier or candidate_id(mapped, baseline["layout"]),
                      "actions": actions, "explanation": "Arrange the visible mux and its ports in a shorter row."})


@pytest.mark.parametrize("value", [[], {"mode": "remote"}, {"max_attempts": True}, {"max_attempts": 1.0},
    {"budget_seconds": float("nan")}, {"budget_seconds": float("inf")}, {"budget_seconds": 0},
    {"mode": "off", "responses": ["{}"]}, {"shell": "ignored"}, {"request": "x" * 501},
    {"request": "\ud800"}, {"mode": "replay", "responses": ["\udc00"]}])
def test_invalid_settings_fail_before_starting_a_model(value):
    with pytest.raises(FrontendError):
        configuration(value)


def test_default_does_not_start_a_provider(baseline, tmp_path):
    mapped, record = baseline
    with RefinementSession(None, tmp_path / "off", provider=lambda *a, **k: pytest.fail("Unexpected model")) as session:
        assert session.refine(mapped, record) is record
    assert not (tmp_path / "off").exists()


@pytest.mark.parametrize("raw", ["{broken", "[]", '{"schema_version":1,"schema_version":1}',
    '{"action":"run_shell","command":"do not execute this"}', "```json\n{}\n```"])
def test_bad_model_output_keeps_the_verified_files(baseline, tmp_path, raw):
    mapped, record = baseline
    before = deepcopy((mapped, record))
    with RefinementSession({"mode": "replay", "responses": [raw], "max_attempts": 1}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert selected["paths"] == record["paths"]
    assert session.report["accepted"] == 0
    assert session.report["canvases"][0]["attempts"][0]["outcome"] == "rejected"
    assert (mapped, record) == before
    assert digest(Path(record["paths"]["path"])) == record["emitted"]["sha256"]


def test_escaped_lone_surrogate_is_rejected_and_baseline_report_serializes(baseline, tmp_path):
    mapped, record = baseline
    proposal = json.loads(compact_response(mapped, record))
    proposal["explanation"] = "\ud800"
    raw = json.dumps(proposal, ensure_ascii=True)
    with RefinementSession({"mode": "replay", "responses": [raw], "max_attempts": 1}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert selected["paths"] == record["paths"] and selected["validation"]["valid"]
    assert session.report["accepted"] == 0
    canvas = session.report["canvases"][0]
    assert canvas["baseline_id"] == canvas["selected_id"]
    outcome = json.loads((tmp_path / "run/canvas-0000/attempt-01/outcome.json").read_text(encoding="utf-8"))
    assert outcome["outcome"] == "rejected" and outcome["diagnostic"]["code"] == "ACTION_JSON"
    assert "actions" not in outcome
    assert json.loads(json.dumps(session.report, ensure_ascii=False).encode("utf-8")) == session.report
    assert digest(Path(record["paths"]["path"])) == record["emitted"]["sha256"]


def test_native_improvement_is_selected_and_stale_second_proposal_is_rejected(baseline, tmp_path):
    mapped, record = baseline
    raw = compact_response(mapped, record)
    with RefinementSession({"mode": "replay", "responses": [raw, raw], "max_attempts": 2}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert session.report["accepted"] == 1, session.report
    assert selected["validation"]["valid"]
    attempts = session.report["canvases"][0]["attempts"]
    assert attempts[0]["quality"]["improved"]
    assert attempts[1]["diagnostic"]["code"] == "ACTION_STALE"
    assert selected["paths"] != record["paths"]
    selected["validation"]["refinement"] = session.report
    serialized = json.loads(json.dumps(selected["validation"]))
    assert "refinement" not in serialized["refinement"]["canvases"][0]["attempts"][0]["native_validation"]
    assert digest(Path(record["paths"]["path"])) == record["emitted"]["sha256"]
    installed = install_candidate(selected, tmp_path / "selected.dig")
    assert digest(Path(installed["paths"]["path"])) == selected["emitted"]["sha256"]


def test_provider_timeout_stops_retries_and_retains_baseline(baseline, tmp_path):
    mapped, record = baseline
    calls = []
    def failed(prompt, *, timeout):
        calls.append(timeout)
        raise TimeoutError("Simulated model request deadline")
    with RefinementSession({"mode": "local"}, tmp_path / "run", provider=failed) as session:
        selected = session.refine(mapped, record)
    assert selected["paths"] == record["paths"]
    assert len(calls) == 1
    assert session.report["accepted"] == 0


def test_post_inspection_mutation_cannot_be_blessed_as_a_baseline(baseline, tmp_path):
    mapped, record = baseline
    altered = deepcopy(record)
    path = tmp_path / "altered.dig"
    path.write_bytes(Path(record["paths"]["path"]).read_bytes() + b"\n")
    altered["paths"]["path"] = str(path)
    with RefinementSession({"mode": "replay"}, tmp_path / "run") as session:
        with pytest.raises(FrontendError) as failure:
            session.refine(mapped, altered)
    assert failure.value.code == "ARTIFACT_CHANGED"


def test_replay_conversion_still_compares_the_original_verilog(native, tmp_path):
    project = Project([ROOT / "tests/fixtures/verilog/scalar/full_adder.v"], top="full_adder")
    result = convert(project, tmp_path / "conversion", adapter=native,
                     refinement={"mode": "replay", "responses": ["{broken"], "max_attempts": 1})
    report = json.loads(Path(result["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] and report["behavior"]["valid"]
    assert report["behavior"]["input_vectors"] == 8
    assert report["refinement"]["accepted"] == 0
    assert Path(result["baseline_preview"]).is_file()


def test_hierarchy_replay_keeps_child_bindings_and_original_behavior(native, tmp_path):
    project = Project([ROOT / "tests/fixtures/verilog/hierarchy/repeated_logic.v"], top="repeated_logic")
    result = convert(project, tmp_path / "hierarchy", adapter=native,
                     refinement={"mode": "replay", "responses": ["{broken"], "max_attempts": 1})
    report = json.loads(Path(result["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] and report["behavior"]["valid"]
    assert report["refinement"]["accepted"] == 0
    canvases = report["refinement"]["canvases"]
    assert len({canvas["module_id"] for canvas in canvases}) == len(canvases)
    assert all(canvas["module_id"] and canvas["top"] for canvas in canvases)
    assert Path(result["baseline_preview"]).is_file()


def test_small_model_context_contains_actionable_native_geometry(native, tmp_path, monkeypatch):
    from verilog2digital.llm import runtime
    captured = []

    class Model:
        def __init__(self, profile):
            self.profile = profile

        def start(self, **kwargs):
            pass

        def generate_json(self, messages, schema, **kwargs):
            context = json.loads(messages[-1]["content"])
            captured.append(context)
            return {"raw_text": json.dumps({"schema_version": 1, "candidate_id": context["candidate_id"],
                                           "actions": [], "explanation": "Keep this verified layout."})}

        def status(self):
            return {"requests": len(captured)}

        def close(self):
            pass

    monkeypatch.setattr(runtime, "select_profile", lambda model_id, **kwargs: {"context_tokens": 2048})
    monkeypatch.setattr(runtime, "LocalModelRuntime", Model)
    project = Project([ROOT / "tests/fixtures/verilog/scalar/full_adder.v"], top="full_adder")
    result = convert(project, tmp_path / "small-context", adapter=native,
                     refinement={"mode": "local", "max_attempts": 1})
    assert len(captured) == 1
    assert captured[0]["components"] and captured[0]["nets"]
    assert json.loads(Path(result["validation"]).read_text(encoding="utf-8"))["behavior"]["valid"]
