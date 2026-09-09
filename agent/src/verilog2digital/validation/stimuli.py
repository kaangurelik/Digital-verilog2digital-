"""Generate repeatable binary vectors for 1-64-bit combinational interfaces.

Vectors use HDL port names; conversion maps them to native component IDs.
Coverage records whether the vector set is exhaustive or sampled.
"""
from __future__ import annotations

from itertools import product
import random

from ..frontend.errors import FrontendError


MAX_VECTORS = 4096
MAX_RANDOM_VECTORS = 2048
EXHAUSTIVE_BITS = 12


class StimulusError(FrontendError):
    pass


def _inputs(mapped: dict) -> list[tuple[str, int]]:
    if not isinstance(mapped, dict) or not isinstance(mapped.get("inputs"), list):
        raise StimulusError("STIMULUS_INPUTS", "A mapped interface must contain an inputs list")
    result, names = [], set()
    for port in mapped["inputs"]:
        if not isinstance(port, dict):
            raise StimulusError("STIMULUS_INPUTS", "Each mapped input must identify its HDL port and width")
        name, width = port.get("port"), port.get("width")
        if not isinstance(name, str) or not name or name in names:
            raise StimulusError("STIMULUS_INPUTS", "Mapped input port names must be unique nonempty strings")
        if type(width) is not int or not 1 <= width <= 64:
            raise StimulusError("STIMULUS_INPUTS", f"Input {name} must have width 1 through 64")
        names.add(name)
        result.append((name, width))
    return result


def _boundaries(width: int) -> tuple[int, ...]:
    mask, sign = (1 << width) - 1, 1 << (width - 1)
    alternating = sum(1 << bit for bit in range(0, width, 2))
    return tuple(value & mask for value in (0, mask, 1, mask - 1, sign, sign - 1, sign + 1,
                                           alternating, mask ^ alternating))


def _directed(widths: list[int]):
    masks = [(1 << width) - 1 for width in widths]
    boundaries = [_boundaries(width) for width in widths]
    for index in range(9):
        yield tuple(values[index] for values in boundaries)
    for index, values in enumerate(boundaries):
        for value in values:
            for baseline in ([0] * len(widths), masks):
                vector = list(baseline)
                vector[index] = value
                yield tuple(vector)
    # Visit high bits first; interleave walking bits and carry chains
    # so large interfaces exercise both within the vector budget.
    for level in range(max(widths), 0, -1):
        for index, width in enumerate(widths):
            if level > width:
                continue
            for value in (1 << (level - 1), masks[index] ^ (1 << (level - 1))):
                vector = [0] * len(widths)
                vector[index] = value
                yield tuple(vector)
            for other in range(len(widths)):
                if other == index:
                    continue
                vector = [0] * len(widths)
                vector[index], vector[other] = (1 << level) - 1, 1
                yield tuple(vector)


def stimulus_plan(mapped: dict, seed: int = 0, random_count: int = 256) -> dict:
    """Return at most 4096 vectors with coverage metadata.

    Exhaust interfaces up to 12 input bits. Larger interfaces use up to 2048
    random attempts plus directed boundaries, walking bits and carry chains.
    Remove duplicates while preserving deterministic order.
    """
    inputs = _inputs(mapped)
    if type(seed) is not int:
        raise StimulusError("STIMULUS_PARAMETERS", "The random seed must be an integer")
    if type(random_count) is not int or not 0 <= random_count <= MAX_RANDOM_VECTORS:
        raise StimulusError("STIMULUS_PARAMETERS", f"random_count must be an integer from 0 through {MAX_RANDOM_VECTORS}")
    names, widths = [item[0] for item in inputs], [item[1] for item in inputs]
    total = sum(widths)
    exhaustive = total <= EXHAUSTIVE_BITS
    coverage = "exhaustive_binary" if exhaustive else "directed_and_seeded_random" if random_count else "directed_binary"
    assumptions = {"generator": "datapath-boundaries-v1", "coverage": coverage,
                   "exhaustive": exhaustive, "total_input_bits": total, "seed": seed,
                   "random_requested": random_count, "random_attempts": 0, "random_vectors": 0,
                   "directed_vectors": 0, "directed_truncated": False,
                   "value_encoding": "unsigned_integer_bit_patterns", "input_widths": dict(inputs)}
    if exhaustive:
        rows = list(product(*(range(1 << width) for width in widths)))
    else:
        rows, seen = [], set()
        directed_budget = MAX_VECTORS - random_count
        for row in _directed(widths):
            if row in seen:
                continue
            if len(rows) == directed_budget:
                assumptions["directed_truncated"] = True
                break
            rows.append(row)
            seen.add(row)
        assumptions["directed_vectors"] = len(rows)
        generator = random.Random(seed)
        for _ in range(random_count):
            row = tuple(generator.getrandbits(width) for width in widths)
            if row not in seen:
                rows.append(row)
                seen.add(row)
                assumptions["random_vectors"] += 1
        assumptions["random_attempts"] = random_count
    stimuli = [dict(zip(names, row)) for row in rows]
    assumptions["input_vectors"] = len(stimuli)
    return {"stimuli": stimuli, "assumptions": assumptions}


def generate_stimuli(mapped: dict, seed: int = 0, random_count: int = 256) -> list[dict[str, int]]:
    """List-only convenience API; use stimulus_plan when publishing coverage."""
    return stimulus_plan(mapped, seed=seed, random_count=random_count)["stimuli"]
