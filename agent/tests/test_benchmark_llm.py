"""The measurement script must admit documented profiles and record, not crash on, refusals."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import benchmark_llm as benchmark


def resources(gpu_free):
    return {"host": {"available_commit_bytes": 32 * 1024**3, "available_physical_bytes": 32 * 1024**3},
            "gpus": [{"index": 0, "driver": "592.82", "free_bytes": gpu_free, "used_bytes": 0}]}


def run(monkeypatch, tmp_path, name, gpu_free, *arguments):
    measured = []
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "require_venv", lambda *_: None)
    monkeypatch.setattr(benchmark, "implementation_hashes", lambda: {})
    monkeypatch.setattr(benchmark, "probe_resources", lambda: resources(gpu_free))
    monkeypatch.setattr(benchmark, "ctypes", SimpleNamespace(windll=SimpleNamespace(
        kernel32=SimpleNamespace(SetErrorMode=lambda *_: 0))))
    monkeypatch.setenv("V2D_JOB_TMP", str(tmp_path / "unused"))
    monkeypatch.setattr(benchmark, "run_profile", lambda case, profile, idle=False:
                        measured.append((case, profile)) or {"name": case, "valid": True})
    output = tmp_path / ".runs" / name
    monkeypatch.setattr(sys, "argv", ["benchmark_llm.py", "--output", str(output), *arguments])
    return benchmark.main(), dict(measured), json.loads((output / "summary.json").read_text(encoding="utf-8"))


def test_large_case_uses_the_documented_14b_profile(monkeypatch, tmp_path):
    code, measured, _ = run(monkeypatch, tmp_path, "large", 16 * 1024**3, "--large")
    assert code == 0
    large = measured["qwen3-14b-q4"]
    assert (large["model_id"], large["context_tokens"], large["host_memory_bytes"]) == (
        "qwen3-14b-q4_k_m", 2048, 11264 * 1024**2)


def test_refused_optional_profile_is_recorded_after_completed_cases(monkeypatch, tmp_path):
    code, measured, summary = run(monkeypatch, tmp_path, "refused", 8 * 1024**3, "--large")
    assert code == 1
    assert list(measured) == ["qwen3-4b-q4_k_m-ctx4096"]
    refused = summary["cases"][-1]
    assert refused["name"] == "qwen3-14b-q4" and refused["valid"] is False
    assert refused["admission"]["diagnostic"]["code"] == "MODEL_MEMORY"
    assert summary["valid"] is False and "resources_after" in summary
