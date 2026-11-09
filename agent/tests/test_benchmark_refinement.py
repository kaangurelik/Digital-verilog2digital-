"""Check the fixed benchmark provider against compact, hand-specified context."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("benchmark_refinement", Path(__file__).resolve().parents[2] / "scripts/benchmark_refinement.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def prompt():
    return {"context": {"candidate_id": "a"*64, "constraints": {"grid": 20, "minimum_spacing": 40},
        "components": [{"id": "movable", "position": [0, 0], "bounds": [-10, -10, 10, 10]}],
        "nets": [{"id": "data", "wire_length": 400, "endpoints": [
            {"instance_id": "movable", "position": [10, 0]}, {"instance_id": "boundary", "position": [310, 0]}]}],
        "obstacles": [{"id": "boundary", "bounds": [310, -10, 330, 10]}]}}


def response(provider, context):
    return json.loads(provider(context, timeout=10)["raw_text"])


def test_rules_use_compact_geometry_and_external_pins_without_mutating_context():
    context = prompt()
    before = deepcopy(context)
    actual = response(benchmark.DeterministicProvider(), context)
    assert actual["actions"] == [{"action": "move_component", "instance_id": "movable", "delta": {"x": 80, "y": 0}}]
    assert context == before
    assert actual["candidate_id"] == "a"*64


def test_rules_respect_visible_obstacle_spacing_instead_of_forcing_largest_move():
    context = prompt()
    context["context"]["obstacles"].append({"id": "obstacle", "bounds": [90, -10, 110, 10]})
    actual = response(benchmark.DeterministicProvider(), context)
    assert actual["actions"] == [{"action": "move_component", "instance_id": "movable", "delta": {"x": 20, "y": 0}}]


def test_rules_do_not_repeat_actions_against_one_snapshot_and_end_with_no_change():
    provider = benchmark.DeterministicProvider()
    actual = [response(provider, prompt())["actions"] for _ in range(5)]
    assert actual[:2] == [[{"action": "move_component", "instance_id": "movable", "delta": {"x": step, "y": 0}}]
                          for step in (80, 20)]
    assert actual[2:4] == [[{"action": "reroute_nets", "net_ids": ["data"], "order": order}] for order in ("reverse", "default")]
    assert actual[4] == []


@pytest.mark.parametrize("model", [None, "qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"])
def test_cli_records_selected_quantization_in_every_worker_request_and_summary(tmp_path, monkeypatch, model):
    source = tmp_path / "tests/fixtures/verilog/scalar/full_adder.v"
    source.parent.mkdir(parents=True)
    source.write_text("module full_adder; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "runtime_fingerprint", lambda: {"sha256": "f" * 64})
    requests = []

    def run_owned(request, limits):
        requests.append(deepcopy(request))
        assert limits.deadline_seconds == 300
        return {"case": request["case"], "strategy": request["strategy"], "valid": True,
                "wall_seconds": 0, "accepted_attempts": 0, "refinement_seconds": 0,
                "accepted_electrical_regressions": 0, "identical_baseline": True,
                "proposal_provider_exercised": True, "real_model_executed": request["strategy"] in {"real4b", "real14b"}}

    monkeypatch.setattr(benchmark, "run_owned", run_owned)
    output = tmp_path / ".runs/comparison"
    args = ["--mode", "compare", "--cases", "full_adder", "--output", str(output)]
    if model is not None:
        args += ["--model", model]
    if model == "qwen3-14b-q4_k_m":
        args += ["--context-tokens", "2048", "--model-host-memory-mib", "11264", "--memory-mib", "12288"]
    assert benchmark.main(args) == 0
    expected = model or "qwen3-4b-q4_k_m"
    real = "real14b" if model == "qwen3-14b-q4_k_m" else "real4b"
    assert [request["strategy"] for request in requests] == ["baseline", "deterministic", real]
    for request in requests:
        assert request["options"]["model_id"] == expected
        assert request["provenance"]["requested_model_id"] == expected
        assert request["options"]["max_attempts"] == 3 and request["options"]["budget_seconds"] == 60
        if real == "real14b":
            assert request["options"]["context_tokens"] == 2048
            assert request["options"]["model_host_memory_mib"] == 11264
            assert request["provenance"]["requested_profile_overrides"] == {
                "context_tokens": 2048, "host_memory_bytes": 11264 * 1024**2}
            assert request["options"]["context_byte_budget"] == 4224
            assert request["provenance"]["requested_context_byte_budget"] == 4224
        else:
            assert "context_tokens" not in request["options"]
            assert "model_host_memory_mib" not in request["options"]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["requested_model_id"] == expected
    assert summary["limits"]["refinement"]["model_id"] == expected
    assert summary["policy_sha256"] == "1f72a23c43bc2030f41104d93067e1b6cb8303491ae6a305c7c457eb292caaa2"


@pytest.mark.parametrize("model", ["qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"])
def test_worker_passes_requested_model_to_real_configuration_before_native_work(tmp_path, monkeypatch, model):
    import verilog2digital.conversion as conversion
    import verilog2digital.llm.loop as loop

    observed = []
    original_session = loop.RefinementSession

    def stop_before_native(project, output_dir, **options):
        observed.append(deepcopy(options))
        raise RuntimeError("test stops before native conversion or inference")

    monkeypatch.setattr(conversion, "convert", stop_before_native)
    request = {"case": "full_adder", "strategy": benchmark.real_strategy(model), "directory": str(tmp_path),
               "options": {"model_id": model, "budget_seconds": 60, "max_attempts": 3,
                           "random_count": 256, "layout_timeout": 20}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert benchmark.worker_main(path) == 1
    assert observed[0]["refinement"] == {"mode": "local", "model_id": model,
                                        "budget_seconds": 60, "max_attempts": 3}
    assert loop.RefinementSession is original_session
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["requested_model_id"] == model
    assert result["error"]["message"] == "test stops before native conversion or inference"


def test_cli_rejects_unpinned_model_before_any_worker_launch(monkeypatch):
    monkeypatch.setattr(benchmark, "run_owned", lambda *args: pytest.fail("Unsupported model started a worker"))
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--mode", "compare", "--model", "qwen3-32b-q4_k_m"])
    assert error.value.code == 2


@pytest.mark.parametrize("strategy", ["real14b", "deterministic", "replay"])
def test_worker_applies_equal_context_budget_through_public_settings(tmp_path, monkeypatch, strategy):
    import verilog2digital.conversion as conversion
    import verilog2digital.llm.loop as loop
    import verilog2digital.llm.runtime as runtime

    selections, profiles, budgets = [], [], []
    original_session = loop.RefinementSession

    def selector(model_id, **options):
        selections.append((model_id, options))
        return {"model_id": model_id, **options}

    class NoNativeRuntime:
        def __init__(self, profile):
            profiles.append(deepcopy(profile))
        def close(self): pass
        def status(self): return {"state": "unloaded"}

    monkeypatch.setattr(runtime, "select_profile", selector)
    monkeypatch.setattr(runtime, "LocalModelRuntime", NoNativeRuntime)

    def stop_before_native(project, output_dir, **options):
        assert options["refinement"]["context_tokens"] == 2048
        assert options["refinement"]["model_memory_mib"] == 11264
        assert runtime.select_profile is selector
        with loop.RefinementSession(options["refinement"], tmp_path / "session") as session:
            session._prepare_runtime()  # Exercise the production context-budget path.
            budgets.append(session.context_bytes)
        raise RuntimeError("stopped before model startup or conversion")

    monkeypatch.setattr(conversion, "convert", stop_before_native)
    request = {"case": "full_adder", "strategy": strategy, "directory": str(tmp_path),
               "options": {"model_id": "qwen3-14b-q4_k_m", "context_tokens": 2048,
                           "model_host_memory_mib": 11264, "budget_seconds": 60, "max_attempts": 3,
                           "random_count": 256, "layout_timeout": 20}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert benchmark.worker_main(path) == 1
    expected = {"context_tokens": 2048, "host_memory_bytes": 11264 * 1024**2}
    assert budgets == [4224]
    if strategy == "real14b":
        assert selections == [("qwen3-14b-q4_k_m", {**expected, "idle_seconds": 30})]
        assert profiles == [{"model_id": "qwen3-14b-q4_k_m", **expected, "idle_seconds": 30}]
    else:
        assert selections == [] and profiles == []  # No resource/model work for alternate providers.
    assert runtime.select_profile is selector and loop.RefinementSession is original_session
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["requested_profile_overrides"] == expected
    assert result["requested_context_byte_budget"] == 4224


@pytest.mark.parametrize("flags", [
    ["--context-tokens", "1023"], ["--context-tokens", "8193"], ["--context-tokens", "2048.5"],
    ["--model-host-memory-mib", "63"], ["--model-host-memory-mib", "12289"],
    ["--model-host-memory-mib", "11264"],  # Exceeds the unchanged default outer cap.
])
def test_invalid_profile_caps_fail_before_output_or_worker_creation(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "run_owned", lambda *args: pytest.fail("Invalid profile started a worker"))
    output = tmp_path / ".runs/invalid"
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--output", str(output), *flags])
    assert error.value.code == 2 and not output.exists()


@pytest.mark.parametrize("model,folder", [("qwen3-4b-q4_k_m", "real4b"),
    ("qwen3-4b-q5_k_m", "real4b"), ("qwen3-14b-q4_k_m", "real14b")])
def test_replay_looks_up_matching_model_family_path(tmp_path, monkeypatch, model, folder):
    source = tmp_path / "tests/fixtures/verilog/scalar/full_adder.v"
    source.parent.mkdir(parents=True)
    source.write_text("module full_adder; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "runtime_fingerprint", lambda: {"sha256": "f" * 64})
    requests = []

    def run_owned(request, limits):
        requests.append(deepcopy(request))
        return {"case": request["case"], "strategy": request["strategy"], "valid": True,
                "wall_seconds": 0, "accepted_electrical_regressions": 0,
                "identical_baseline": True, "proposal_provider_exercised": True}

    monkeypatch.setattr(benchmark, "run_owned", run_owned)
    previous = tmp_path / "previous"
    assert benchmark.main(["--output", str(tmp_path / ".runs/replay"), "--mode", "replay",
                           "--model", model, "--cases", "full_adder", "--replay-root", str(previous)]) == 0
    assert [request["strategy"] for request in requests] == ["baseline", "replay"]
    assert requests[-1]["replay_file"] == str(previous / "full_adder" / folder / "replay.json")


@pytest.mark.parametrize("flags", [["--layout-timeout", "0"], ["--layout-timeout", "121"],
    ["--random-count", "513"], ["--random-count", "2049", "--cases", "full_adder"], ["--random-count", "-1"]])
def test_conversion_bounds_fail_before_output_or_worker_creation(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "run_owned", lambda *args: pytest.fail("Unsupported conversion bounds started a worker"))
    output = tmp_path / ".runs/invalid"
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--output", str(output), *flags])
    assert error.value.code == 2 and not output.exists()


def test_combinational_cases_accept_their_larger_vector_bound(tmp_path, monkeypatch):
    source = tmp_path / "tests/fixtures/verilog/scalar/full_adder.v"
    source.parent.mkdir(parents=True)
    source.write_text("module full_adder; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda _: None)
    monkeypatch.setattr(benchmark, "runtime_fingerprint", lambda: {"sha256": "f" * 64})
    requests = []
    monkeypatch.setattr(benchmark, "run_owned", lambda request, limits: requests.append(request) or {
        "case": request["case"], "strategy": request["strategy"], "valid": True, "wall_seconds": 0,
        "accepted_electrical_regressions": 0, "identical_baseline": True, "proposal_provider_exercised": True})
    assert benchmark.main(["--output", str(tmp_path / ".runs/vectors"), "--cases", "full_adder",
                           "--random-count", "2048", "--layout-timeout", "1"]) == 0
    assert {request["options"]["random_count"] for request in requests} == {2048}
