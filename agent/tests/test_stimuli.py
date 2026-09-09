"""Coverage claims, bit boundaries, and repeatability are part of verification."""
from itertools import product

import pytest

from verilog2digital.validation.stimuli import StimulusError, generate_stimuli, stimulus_plan


def mapped(*widths):
    return {"inputs": [{"port": f"p{index}", "instance_id": f"input_{index + 100}", "width": width}
                       for index, width in enumerate(widths)]}


def test_no_inputs_produces_one_complete_vector():
    plan = stimulus_plan(mapped())
    assert plan["stimuli"] == [{}]
    assert plan["assumptions"]["exhaustive"] is True
    assert plan["assumptions"]["total_input_bits"] == 0
    assert plan["assumptions"]["input_vectors"] == 1


def test_exhaustive_vectors_cover_values_not_merely_port_count():
    plan = stimulus_plan(mapped(3, 2))
    assert plan["stimuli"] == [{"p0": a, "p1": b} for a, b in product(range(8), range(4))]
    assert plan["assumptions"]["coverage"] == "exhaustive_binary"
    assert plan["assumptions"]["total_input_bits"] == 5
    assert plan["assumptions"]["random_attempts"] == 0


def test_exhaustive_threshold_includes_twelve_bits():
    plan = stimulus_plan(mapped(8, 4), seed=1, random_count=0)
    assert len(plan["stimuli"]) == 4096
    assert plan["stimuli"][0] == {"p0": 0, "p1": 0}
    assert plan["stimuli"][-1] == {"p0": 255, "p1": 15}
    assert plan["assumptions"]["exhaustive"] is True


def test_thirteen_bits_are_honestly_reported_as_sampled():
    plan = stimulus_plan(mapped(13), seed=17, random_count=32)
    assert 32 < len(plan["stimuli"]) < 8192
    assert plan["assumptions"]["exhaustive"] is False
    assert plan["assumptions"]["coverage"] == "directed_and_seeded_random"
    assert plan["assumptions"]["seed"] == 17
    assert plan["assumptions"]["random_attempts"] == 32
    assert plan["assumptions"]["input_vectors"] == len(plan["stimuli"])


def test_wide_directed_patterns_reach_sign_carry_and_every_bit():
    mask, sign = (1 << 64) - 1, 1 << 63
    plan = stimulus_plan(mapped(64, 64), random_count=0)
    rows = {(row["p0"], row["p1"]) for row in plan["stimuli"]}
    assert (0, 0) in rows and (mask, mask) in rows
    assert (sign - 1, 1) in rows and (mask, 1) in rows
    assert (sign, 0) in rows and (0, sign) in rows
    for bit in range(64):
        assert (1 << bit, 0) in rows and (0, 1 << bit) in rows
        assert (mask ^ (1 << bit), 0) in rows and (0, mask ^ (1 << bit)) in rows
    assert len(rows) == len(plan["stimuli"])
    assert plan["assumptions"]["coverage"] == "directed_binary"
    assert plan["assumptions"]["directed_truncated"] is False


def test_seed_reproduces_vectors_and_randomness_does_not_change_directed_prefix():
    first = stimulus_plan(mapped(64, 7), seed=123, random_count=64)
    second = stimulus_plan(mapped(64, 7), seed=123, random_count=64)
    other = stimulus_plan(mapped(64, 7), seed=124, random_count=64)
    assert first == second and first["stimuli"] != other["stimuli"]
    count = first["assumptions"]["directed_vectors"]
    assert first["stimuli"][:count] == other["stimuli"][:count]
    assert generate_stimuli(mapped(64, 7), seed=123, random_count=64) == first["stimuli"]
    assert all(type(value) is int for row in first["stimuli"] for value in row.values())
    assert all(0 <= row["p0"] < (1 << 64) and 0 <= row["p1"] < 128 for row in first["stimuli"])


def test_many_wide_ports_have_a_bounded_plan_with_explicit_truncation():
    plan = stimulus_plan(mapped(*([64] * 16)), seed=0, random_count=256)
    assert len(plan["stimuli"]) <= 4096
    assert plan["assumptions"]["directed_truncated"] is True
    assert plan["assumptions"]["random_attempts"] == 256
    assert plan["assumptions"]["directed_vectors"] + plan["assumptions"]["random_vectors"] == len(plan["stimuli"])
    assert all(len(row) == 16 for row in plan["stimuli"])


@pytest.mark.parametrize("width", [0, 65, True, "8"])
def test_invalid_width_is_not_silently_coerced(width):
    with pytest.raises(StimulusError) as error:
        generate_stimuli(mapped(width))
    assert error.value.code == "STIMULUS_INPUTS"


def test_duplicate_hdl_names_cannot_overwrite_inputs():
    interface = mapped(8, 8)
    interface["inputs"][1]["port"] = "p0"
    with pytest.raises(StimulusError) as error:
        generate_stimuli(interface)
    assert error.value.code == "STIMULUS_INPUTS"


@pytest.mark.parametrize("options", [{"seed": True}, {"seed": "2"}, {"random_count": -1},
                                     {"random_count": 2049}, {"random_count": True}])
def test_invalid_sampling_controls_fail_before_generation(options):
    with pytest.raises(StimulusError) as error:
        generate_stimuli(mapped(64), **options)
    assert error.value.code == "STIMULUS_PARAMETERS"
