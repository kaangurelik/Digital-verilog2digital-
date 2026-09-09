from __future__ import annotations

import pytest

from verilog2digital.adapter import Adapter, AdapterError, _java_heap_options


def test_standalone_heap_defaults_are_unchanged(monkeypatch):
    monkeypatch.delenv("V2D_JAVA_HEAP_BYTES", raising=False)
    assert _java_heap_options() == []


@pytest.mark.parametrize("value", [64 * 1024**2, 1024**3, 64 * 1024**3])
def test_explicit_job_heap_is_small_initial_allocation_and_bounded_maximum(monkeypatch, value):
    monkeypatch.setenv("V2D_JAVA_HEAP_BYTES", str(value))
    assert _java_heap_options() == ["-Xms32m", f"-Xmx{value}"]


@pytest.mark.parametrize("value", ["", "1g", "-1", "+67108864", " 67108864", "67108864 ",
    "٦٧١٠٨٨٦٤", "1.0", "0", "67108863", str(64 * 1024**3 + 1), "9" * 100])
def test_invalid_job_heap_is_rejected_before_starting_java(monkeypatch, value, tmp_path):
    monkeypatch.setenv("V2D_JAVA_HEAP_BYTES", value)
    with pytest.raises(AdapterError, match="V2D_JAVA_HEAP_BYTES"):
        Adapter(root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_real_native_adapter_starts_with_explicit_job_heap(monkeypatch):
    monkeypatch.setenv("V2D_JAVA_HEAP_BYTES", str(1024**3))
    with Adapter() as adapter:
        assert "-Xms32m" in adapter._process.args
        assert "-Xmx1073741824" in adapter._process.args
        assert adapter.capabilities["schema_version"] == 1
