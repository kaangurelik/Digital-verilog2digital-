"""Check sampled and exhaustive behavior coverage metadata."""
import pytest

from verilog2digital.conversion import compare_behavior, exhaustive_stimuli


def compare(rows, *, coverage=None, width=1):
    mapped = {"inputs": [{"port": "a", "width": width}],
              "outputs": [{"port": "y", "instance_id": "out0", "width": width}]}
    reference = {"samples": [{"values": {"y": row["a"]}} for row in rows], "assumptions": {}}
    native = {"samples": [{"values": {"out0": hex(row["a"])}, "high_z": {"out0": "0x0"}} for row in rows]}
    return compare_behavior(mapped, rows, reference, native, coverage=coverage)


def test_duplicate_inputs_do_not_count_as_exhaustive_coverage():
    result = compare([{"a": 0}, {"a": 0}])
    assert result["valid"]
    assert result["coverage"] == "provided_vectors" and not result["exhaustive"]


def test_forged_exhaustive_metadata_is_rejected():
    result = compare([{"a": 0}, {"a": 0}], coverage={"coverage": "exhaustive_binary", "exhaustive": True})
    assert not result["valid"]
    assert result["diagnostics"][-1]["code"] == "invalid_coverage_claim"


def test_complete_input_space_is_recognized_independently():
    result = compare([{"a": 1}, {"a": 0}])
    assert result["valid"] and result["exhaustive"]


def test_64bit_pattern_is_compared_without_truncation_or_exhaustive_claim():
    result = compare([{"a": 1 << 63}, {"a": (1 << 64) - 1}], width=64)
    assert result["valid"] and not result["exhaustive"]


def test_exhaustive_helper_counts_bits_inside_each_port():
    assert exhaustive_stimuli({"inputs": [{"port": "a", "width": 2}]}) == [
        {"a": 0}, {"a": 1}, {"a": 2}, {"a": 3}]


def test_coverage_metadata_cannot_override_validation_failure():
    mapped = {"inputs": [{"port": "a", "width": 1}],
              "outputs": [{"port": "y", "instance_id": "out0", "width": 1}]}
    result = compare_behavior(mapped, [{"a": 0}],
                              {"samples": [{"values": {"y": 0}}], "assumptions": {}},
                              {"samples": [{"values": {"out0": "0x1"}, "high_z": {"out0": "0x0"}}]},
                              coverage={"valid": True, "coverage": "provided_vectors", "diagnostics": []})
    assert not result["valid"]
    assert result["diagnostics"][0]["code"] == "behavior_mismatch"


@pytest.mark.parametrize("rows", [[], [{}], [{"a": -1}], [{"a": 2}], [{"a": True}], [{"a": 0, "extra": 1}]])
def test_sampled_reports_require_valid_input_patterns(rows):
    mapped = {"inputs": [{"port": "a", "width": 1}],
              "outputs": [{"port": "y", "instance_id": "out0", "width": 1}]}
    result = compare_behavior(mapped, rows,
                              {"samples": [{"values": {"y": 0}} for row in rows], "assumptions": {}},
                              {"samples": [{"values": {"out0": "0x0"}, "high_z": {"out0": "0x0"}} for row in rows]})
    assert not result["valid"]
    assert result["diagnostics"][0]["code"] == "invalid_stimuli"
