"""Untrusted later evidence cannot destroy a previously verified improvement."""
from itertools import product
import json
from pathlib import Path

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs.publication import digest
from verilog2digital.llm.loop import RefinementSession
from test_refinement_loop import baseline, native, compact_response


@pytest.mark.parametrize("failure", ["metadata", "diagnostic"])
def test_invalid_unicode_after_native_acceptance_preserves_selected_files(baseline, native, tmp_path, failure):
    mapped, record = baseline
    initial = {key: Path(path).read_bytes() for key, path in record["paths"].items()}
    responses = []

    def provider(prompt, *, timeout):
        responses.append(prompt["metadata"]["candidate_id"])
        if len(responses) == 1:
            return compact_response(mapped, record)
        if failure == "diagnostic":
            raise FrontendError("MODEL_GENERATION", "Invalid model detail: \ud800", path="provider/\udfff")
        raw = json.dumps({"schema_version": 1, "candidate_id": prompt["metadata"]["candidate_id"],
                          "actions": [], "explanation": "Keep the improvement."})
        return {"raw_text": raw, "details": {"untrusted": "\ud800"}}

    with RefinementSession({"mode": "local", "max_attempts": 2}, tmp_path / "refinement",
                           provider=provider) as session:
        selected = session.refine(mapped, record)
    assert len(responses) == 2 and responses[0] != responses[1]
    assert session.report["accepted"] == 1 and selected["validation"]["valid"]
    attempts = session.report["canvases"][0]["attempts"]
    assert attempts[0]["accepted"] and attempts[0]["quality"]["improved"]
    assert attempts[1]["outcome"] == "rejected" and not attempts[1]["accepted"]
    if failure == "diagnostic":
        assert attempts[1]["diagnostic"]["code"] == "MODEL_GENERATION"
    assert selected["paths"] != record["paths"]
    assert {key: Path(path).read_bytes() for key, path in record["paths"].items()} == initial
    for path_key, hash_key in (("path", "sha256"), ("sidecar_path", "sidecar_sha256")):
        assert digest(Path(selected["paths"][path_key])) == selected["emitted"][hash_key]

    selected["validation"]["refinement"] = session.report
    serialized = json.loads(json.dumps(selected["validation"], ensure_ascii=False).encode("utf-8"))
    assert serialized["refinement"]["accepted"] == 1
    for path in (tmp_path / "refinement").rglob("outcome.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)

    # Reopen the selected bytes with the unaffected conversion adapter and
    # independently check all inputs, including the deliberately unused s pin.
    rows = [dict(zip(("a", "b", "s"), values)) for values in product((0, 1), repeat=3)]
    simulation = native.call("circuit.simulate", {**selected["paths"], "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in mapped["inputs"]}}
        for row in rows]})
    output = mapped["outputs"][0]["instance_id"]
    assert [int(sample["values"][output], 16) for sample in simulation["samples"]] == [
        row["a"] & row["b"] for row in rows]
    assert all(int(sample["high_z"][output], 16) == 0 for sample in simulation["samples"])


@pytest.mark.parametrize("code", ["MODEL_JSON", "MODEL_SCHEMA", "MODEL_TRUNCATED"])
def test_completed_bad_response_can_retry_and_accept_a_native_improvement(baseline, tmp_path, code):
    mapped, record = baseline
    calls = []

    def provider(prompt, *, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise FrontendError(code, "The completed response is unusable; the provider remains available.")
        return compact_response(mapped, record)

    with RefinementSession({"mode": "local", "max_attempts": 2}, tmp_path / "refinement",
                           provider=provider) as session:
        selected = session.refine(mapped, record)
    assert len(calls) == 2 and 0 < calls[1] <= calls[0]
    attempts = session.report["canvases"][0]["attempts"]
    assert attempts[0]["diagnostic"]["code"] == code and attempts[0]["outcome"] == "rejected"
    assert attempts[1]["accepted"] and session.report["accepted"] == 1
    assert selected["validation"]["valid"] and selected["paths"] != record["paths"]
    assert digest(Path(selected["paths"]["path"])) == selected["emitted"]["sha256"]


@pytest.mark.parametrize("kind", ["complete_but_rejected", "invalid_unicode", "oversized"])
def test_rejected_generation_evidence_never_becomes_a_replayable_action(baseline, tmp_path, kind):
    mapped, record = baseline
    raw = compact_response(mapped, record)
    if kind == "invalid_unicode":
        raw = "\ud800"
    elif kind == "oversized":
        raw = "x" * 65537

    def provider(prompt, *, timeout):
        error = FrontendError("MODEL_TRUNCATED", "Generation did not end with a complete stop")
        error.rejected_response = {"raw_text": raw, "truncated": False}
        raise error

    with RefinementSession({"mode": "local", "max_attempts": 1}, tmp_path / "refinement",
                           provider=provider) as session:
        selected = session.refine(mapped, record)
    assert selected["paths"] == record["paths"] and session.report["accepted"] == 0
    attempt = session.report["canvases"][0]["attempts"][0]
    assert attempt["diagnostic"]["code"] == "MODEL_TRUNCATED"
    assert "response" not in attempt and "actions" not in attempt
    captured = kind == "complete_but_rejected"
    assert ("rejected_response" in attempt) == captured
    assert len(list((tmp_path / "refinement").rglob("rejected-response.json"))) == int(captured)
    if captured:
        assert attempt["rejected_response"]["raw_text"] == raw
    json.dumps(session.report, ensure_ascii=False).encode("utf-8")
    assert digest(Path(record["paths"]["path"])) == record["emitted"]["sha256"]
