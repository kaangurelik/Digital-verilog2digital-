"""Model ceilings are checked before creating jobs or replacing published output."""
import pytest

from verilog2digital.frontend.project import Project
from verilog2digital.jobs import manager


MIB = 1024**2


@pytest.mark.parametrize("limits", [
    manager.JobLimits(memory_bytes=4096 * MIB),
    manager.JobLimits(memory_bytes=8192 * MIB, process_memory_bytes=4096 * MIB),
])
def test_local_model_ceiling_must_fit_each_applicable_job_limit(tmp_path, monkeypatch, limits):
    output = tmp_path / "published"
    output.mkdir()
    current = output / "current.json"
    current.write_bytes(b"previous verified revision")
    monkeypatch.setattr(manager, "_run", lambda *a, **k: pytest.fail("Invalid limits created a job"))
    with pytest.raises(manager.JobError, match="Model memory ceiling") as error:
        manager.run_job(Project([tmp_path / "unused.v"]), output, replace=True,
                        options={"refinement": {"mode": "local"}}, limits=limits)
    assert error.value.code == "INVALID_LIMITS"
    assert current.read_bytes() == b"previous verified revision"
    assert list(output.iterdir()) == [current]


@pytest.mark.parametrize("mode,model_mib,outer_mib", [
    ("local", 6144, 6144),  # Nested ceilings are not summed as allocations.
    ("local", 11264, 12288),
    ("local", 4096, 4096),
    ("off", 11264, 6144),
    ("replay", 11264, 6144),
])
def test_valid_model_limits_and_inert_saved_settings_reach_normal_job_path(
        tmp_path, monkeypatch, mode, model_mib, outer_mib):
    calls = []
    monkeypatch.setattr(manager, "_run", lambda *a, **k: calls.append(k) or "ready")
    limits = manager.JobLimits(memory_bytes=outer_mib * MIB)
    assert manager.run_job(Project([tmp_path / "unused.v"]), tmp_path / "out", limits=limits,
                           options={"refinement": {"mode": mode, "model_memory_mib": model_mib}}) == "ready"
    assert len(calls) == 1 and calls[0]["limits"] is limits
    assert calls[0]["options"]["refinement"]["model_memory_mib"] == model_mib
    assert calls[0]["cache"] is (mode == "off")


def test_invalid_limit_object_fails_before_local_job_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda *a, **k: pytest.fail("Invalid limits created a job"))
    with pytest.raises(manager.JobError, match="JobLimits"):
        manager.run_job(Project([tmp_path / "unused.v"]), tmp_path / "out",
                        options={"refinement": {"mode": "local"}}, limits={"memory_bytes": 1})
