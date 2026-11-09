"""Bounded memory recovery keeps the same independently verified circuit intent."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from verilog2digital.jobs.publication import digest
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.llm import resources, runtime
from verilog2digital.llm.artifacts import ModelError
from verilog2digital.llm.loop import RefinementSession
from test_refinement_loop import baseline, native, compact_response


EVIDENCE = {"source": "owned_server_stderr", "kind": "host", "signature": "std::bad_alloc",
            "excerpt": "terminate called after throwing an instance of 'std::bad_alloc'",
            "stderr_tail_sha256": "a" * 64, "stderr_bytes": 128}


def failure(code="MODEL_ALLOCATION", *, evidence=True):
    error = ModelError(code, "Controlled test failure")
    if evidence:
        error.allocation_evidence = deepcopy(EVIDENCE)
    return error


def models(monkeypatch, *, start_errors=(), generate=None, cleanup=None):
    """Replace inference only; use production session and real circuit checks."""
    created, events, requests = [], [], []
    initial = {"model_id": "qwen3-4b-q4_k_m", "backend": "cuda", "context_tokens": 4096,
               "host_memory_bytes": 6 * 1024**3, "gpu_layers": 37, "gpu_index": 0,
               "kv_type": "q8_0", "idle_seconds": 30}
    start_errors = list(start_errors)

    class Model:
        def __init__(self, profile):
            self.profile = deepcopy(profile)
            self.index = len(created)
            self.closed = False
            created.append(self)

        def start(self, **kwargs):
            events.append(("start", self.index, kwargs["timeout"]))
            if start_errors:
                error = start_errors.pop(0)
                if error:
                    raise error

        def generate_json(self, messages, schema, **kwargs):
            context = json.loads(messages[-1]["content"])
            requests.append(context)
            if generate is not None:
                return generate(len(requests), context)
            return {"raw_text": json.dumps({"schema_version": 1, "candidate_id": context["candidate_id"],
                                           "actions": [], "explanation": "Preserve the verified circuit."})}

        def close(self):
            events.append(("close", self.index))
            self.closed = True

        def status(self):
            process = {"active_processes": 0 if self.closed else 1, "closed": self.closed}
            if self.closed and self.index == 0 and cleanup is not None:
                process.update(cleanup)
            return {"profile": self.profile, "state": "unloaded" if self.closed else "ready", "process": process}

    def smaller(previous):
        assert created[-1].closed, "Profile re-admission happened before releasing the prior runtime"
        events.append(("recovery_profile", previous["context_tokens"]))
        return {**previous, "context_tokens": 2048}

    monkeypatch.setattr(runtime, "select_profile", lambda *args, **kwargs: deepcopy(initial))
    monkeypatch.setattr(runtime, "LocalModelRuntime", Model)
    monkeypatch.setattr(resources, "recovery_profile", smaller)
    return created, events, requests


def unchanged(record, before):
    assert {key: Path(path).read_bytes() for key, path in record["paths"].items()} == before


def test_native_accepted_incumbent_survives_allocation_recovery(baseline, native, tmp_path, monkeypatch):
    mapped, record = baseline
    before = {key: Path(path).read_bytes() for key, path in record["paths"].items()}

    def generate(index, context):
        if index == 1:
            return {"raw_text": compact_response(mapped, record)}
        if index == 2:
            raise failure()
        return {"raw_text": json.dumps({"schema_version": 1, "candidate_id": context["candidate_id"],
                                       "actions": [], "explanation": "Retain the verified improvement."})}

    created, events, requests = models(monkeypatch, generate=generate)
    with RefinementSession({"mode": "local", "max_attempts": 3, "budget_seconds": 60}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert [model.profile["context_tokens"] for model in created] == [4096, 2048]
    assert all(model.closed for model in created)
    assert events.index(("close", 0)) < events.index(("recovery_profile", 4096))
    assert len(requests) == 3 and session.report["accepted"] == 1
    attempts = session.report["canvases"][0]["attempts"]
    assert attempts[0]["accepted"] and attempts[0]["native_validation"]["valid"]
    assert attempts[1]["diagnostic"]["code"] == "MODEL_ALLOCATION"
    assert attempts[1]["allocation_evidence"] == EVIDENCE
    assert attempts[2]["outcome"] == "no_change"
    assert attempts[0]["candidate_id"] != attempts[2]["candidate_id"]
    assert session.report["memory_recovery"]["after_attempt"] == 2
    assert session.report["retired_runtimes"][0]["process"] == {"active_processes": 0, "closed": True}
    assert session.context_bytes == 4224
    assert selected["validation"]["valid"] and selected["paths"] != record["paths"]
    unchanged(record, before)
    assert digest(Path(selected["paths"]["path"])) == selected["emitted"]["sha256"]
    inspection = native.call("circuit.inspect", selected["paths"])
    assert inspection["model"]["valid"]
    simulation = native.call("circuit.simulate", {**selected["paths"], "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in mapped["inputs"]}}
        for row in ({"a": a, "b": b, "s": s} for a in (0, 1) for b in (0, 1) for s in (0, 1))]})
    output = mapped["outputs"][0]["instance_id"]
    assert [int(sample["values"][output], 16) for sample in simulation["samples"]] == [0, 0, 0, 0, 0, 0, 1, 1]
    assert all(int(sample["high_z"][output], 16) == 0 for sample in simulation["samples"])
    json.dumps(session.report, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_startup_allocation_retry_is_once_and_within_existing_attempt_budget(baseline, tmp_path, monkeypatch):
    mapped, record = baseline
    created, events, requests = models(monkeypatch, start_errors=[failure(), failure()])
    with RefinementSession({"mode": "local", "max_attempts": 3, "budget_seconds": 60}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert [model.profile["context_tokens"] for model in created] == [4096, 2048]
    assert len([event for event in events if event[0] == "recovery_profile"]) == 1
    assert session.attempt_count == 2 and not requests
    assert selected["paths"] == record["paths"] and selected["validation"]["valid"]
    assert all(model.closed for model in created)


@pytest.mark.parametrize("code", ["MODEL_EXIT", "MODEL_CANCELLED", "MODEL_HASH", "MODEL_TIMEOUT"])
def test_unrelated_failure_never_retries_smaller_profile(baseline, tmp_path, monkeypatch, code):
    mapped, record = baseline
    created, events, requests = models(monkeypatch, start_errors=[failure(code)])
    with RefinementSession({"mode": "local", "max_attempts": 3}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1 and not requests
    assert not any(event[0] == "recovery_profile" for event in events)
    assert "memory_recovery" not in session.report
    assert selected["paths"] == record["paths"]


@pytest.mark.parametrize("cleanup", [{"active_processes": 1}, {"closed": False}])
def test_failed_native_cleanup_blocks_recovery(baseline, tmp_path, monkeypatch, cleanup):
    mapped, record = baseline
    created, events, _ = models(monkeypatch, start_errors=[failure()], cleanup=cleanup)
    with RefinementSession({"mode": "local", "max_attempts": 3}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1
    assert not any(event[0] == "recovery_profile" for event in events)
    assert any(item["code"] == "MODEL_RECOVERY" for item in session.report["diagnostics"])
    assert selected["paths"] == record["paths"]


def test_no_retry_when_initial_attempt_uses_the_limit(baseline, tmp_path, monkeypatch):
    mapped, record = baseline
    created, events, _ = models(monkeypatch, start_errors=[failure()])
    with RefinementSession({"mode": "local", "max_attempts": 1}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1 and session.attempt_count == 1
    assert not any(event[0] == "recovery_profile" for event in events)
    assert selected["paths"] == record["paths"]


@pytest.mark.parametrize("change", [None, {"source": "untrusted_response"}, {"kind": "guess"},
    {"signature": ""}, {"excerpt": "x" * 4097}, {"stderr_tail_sha256": "G" * 64},
    {"stderr_bytes": True}, {"stderr_bytes": 0}])
def test_missing_or_malformed_allocation_evidence_cannot_enable_retry(baseline, tmp_path, monkeypatch, change):
    mapped, record = baseline
    error = failure(evidence=False)
    if change is not None:
        error.allocation_evidence = {**EVIDENCE, **change}
    created, events, _ = models(monkeypatch, start_errors=[error])
    with RefinementSession({"mode": "local", "max_attempts": 3}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1
    assert not any(event[0] == "recovery_profile" for event in events)
    assert selected["paths"] == record["paths"]


def test_plain_frontend_error_cannot_impersonate_native_allocation(baseline, tmp_path, monkeypatch):
    mapped, record = baseline
    error = FrontendError("MODEL_ALLOCATION", "This is not the runtime's typed error")
    error.allocation_evidence = deepcopy(EVIDENCE)
    created, events, _ = models(monkeypatch, start_errors=[error])
    with RefinementSession({"mode": "local", "max_attempts": 3}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1
    assert not any(event[0] == "recovery_profile" for event in events)
    assert selected["paths"] == record["paths"]


def test_memory_denial_after_global_deadline_cannot_schedule_recovery(baseline, tmp_path, monkeypatch):
    mapped, record = baseline
    created, events, _ = models(monkeypatch)

    def expired_start(self, **kwargs):
        session.deadline = time.monotonic() - 1
        raise failure()

    monkeypatch.setattr(runtime.LocalModelRuntime, "start", expired_start)
    with RefinementSession({"mode": "local", "max_attempts": 3, "budget_seconds": 60}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert len(created) == 1 and session.attempt_count == 1
    assert not any(event[0] == "recovery_profile" for event in events)
    assert selected["paths"] == record["paths"]


def test_typed_admission_memory_failure_can_use_the_known_selected_profile(baseline, tmp_path, monkeypatch):
    mapped, record = baseline
    created, events, requests = models(monkeypatch, start_errors=[failure("MODEL_MEMORY", evidence=False)])
    with RefinementSession({"mode": "local", "max_attempts": 3}, tmp_path / "run") as session:
        selected = session.refine(mapped, record)
    assert [model.profile["context_tokens"] for model in created] == [4096, 2048]
    assert len(requests) == 1 and session.report["memory_recovery"]["reason"] == "MODEL_MEMORY"
    assert selected["paths"] == record["paths"] and selected["validation"]["valid"]
