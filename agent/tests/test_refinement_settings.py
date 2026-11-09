"""Bounded settings do not independently activate local inference."""
from copy import deepcopy

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.llm.config import configuration


def test_default_model_controls_preserve_automatic_profile_and_off_mode():
    settings = configuration()
    assert settings["mode"] == "off"
    assert settings["context_tokens"] is None
    assert settings["model_memory_mib"] == 6144
    assert settings["idle_seconds"] == 30
    settings["responses"].append("unused")
    assert configuration()["responses"] == []


@pytest.mark.parametrize("mode", ["off", "local", "replay"])
@pytest.mark.parametrize("limits", [
    {"context_tokens": None, "model_memory_mib": 6144, "idle_seconds": 30},
    {"context_tokens": 1024, "model_memory_mib": 64, "idle_seconds": 1},
    {"context_tokens": 8192, "model_memory_mib": 12288, "idle_seconds": 300},
])
def test_supported_model_limits_normalize_without_changing_mode_or_input(mode, limits):
    original = {"mode": mode, "model_id": "qwen3-14b-q4_k_m", **limits}
    before = deepcopy(original)
    result = configuration(original)
    assert result["mode"] == mode and result["model_id"] == original["model_id"]
    assert all(result[key] == value for key, value in limits.items())
    assert original == before


@pytest.mark.parametrize("field,value", [
    ("context_tokens", True), ("context_tokens", 2048.0), ("context_tokens", float("nan")),
    ("context_tokens", float("inf")), ("context_tokens", 1023), ("context_tokens", 8193),
    ("context_tokens", "2048"), ("model_memory_mib", False), ("model_memory_mib", 6144.0),
    ("model_memory_mib", float("nan")), ("model_memory_mib", float("inf")),
    ("model_memory_mib", None), ("model_memory_mib", 63), ("model_memory_mib", 12289),
    ("idle_seconds", True), ("idle_seconds", 30.0), ("idle_seconds", float("nan")),
    ("idle_seconds", float("inf")), ("idle_seconds", None), ("idle_seconds", 0), ("idle_seconds", 301),
])
def test_model_limits_reject_nonintegers_nonfinite_and_out_of_bounds(field, value):
    with pytest.raises(FrontendError) as caught:
        configuration({field: value})
    assert caught.value.code == "INVALID_REFINEMENT"


@pytest.mark.parametrize("context_tokens,expected_bytes", [(None, 6900), (1024, 2048), (2048, 4224)])
def test_replay_context_preserves_visibility_without_selecting_or_starting_a_model(
        tmp_path, monkeypatch, context_tokens, expected_bytes):
    from verilog2digital.llm import runtime
    from verilog2digital.llm.loop import RefinementSession

    def unexpected(*args, **kwargs):
        pytest.fail("Replay selected or constructed a local model")

    monkeypatch.setattr(runtime, "select_profile", unexpected)
    monkeypatch.setattr(runtime, "LocalModelRuntime", unexpected)
    directory = tmp_path / "replay"
    with RefinementSession({"mode": "replay", "context_tokens": context_tokens,
                            "model_memory_mib": 11264, "idle_seconds": 1}, directory) as session:
        session._prepare_runtime()
        assert session.context_bytes == expected_bytes
        assert session.runtime is None
    assert not directory.exists()
