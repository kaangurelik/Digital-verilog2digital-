"""Prove input-driven initialization before comparing bounded clock traces.

Start every retained register bit unknown. Bit transfers, reset/load muxes and
controlling bitwise inputs may establish known state; unknown arithmetic stays
unknown. Original-HDL simulation checks behavior after initialization.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import random

from ..frontend.errors import FrontendError
from ..ir import validate_design
from .stimuli import _boundaries


MAX_PREFIX_CYCLES = 256
MAX_CHECKED_CYCLES = 1024
PHASES = ("before_rise", "after_rise", "after_fall")


class SequentialError(FrontendError):
    pass


def sequential_interface(design: dict) -> dict:
    validate_design(design)
    registers = [cell for cell in design["cells"] if cell["type"] == "$dff"]
    if not registers:
        raise SequentialError("SEQUENTIAL_PROFILE", "Sequential validation requires a retained rising-edge register")
    clock_bits = {cell["ports"]["CLK"][0] for cell in registers}
    if len(clock_bits) != 1:
        raise SequentialError("UNSUPPORTED_CLOCK", "Sequential validation requires exactly one clock")
    clock_bit = next(iter(clock_bits))
    clocks = [port for port in design["ports"] if port["direction"] == "input" and port["bits"] == [clock_bit]]
    if len(clocks) != 1:
        raise SequentialError("UNSUPPORTED_CLOCK", "The clock must be one dedicated scalar top-level input")
    for port in design["ports"]:
        if port is not clocks[0] and clock_bit in port["bits"]:
            raise SequentialError("UNSUPPORTED_CLOCK", "The dedicated clock cannot also be a data or output port")
    for cell in design["cells"]:
        for pin, bits in cell["ports"].items():
            if clock_bit in bits and (cell["type"] != "$dff" or pin != "CLK"):
                raise SequentialError("UNSUPPORTED_CLOCK", "The dedicated clock may connect only to register clock pins")
    return {"clock": {"port": clocks[0]["name"], "edge": "rising"}, "clock_bit": clock_bit,
            "registers": registers,
            "inputs": {port["name"]: len(port["bits"]) for port in design["ports"]
                       if port["direction"] == "input" and port is not clocks[0]},
            "outputs": {port["name"]: len(port["bits"]) for port in design["ports"] if port["direction"] == "output"}}


def validate_trace(design: dict, trace: dict) -> dict:
    """Validate and copy a complete HDL-name trace; clock transitions are implicit."""
    interface = sequential_interface(design)
    if not isinstance(trace, dict) or set(trace) != {"schema_version", "clock", "initialization", "cycles"}:
        raise SequentialError("SEQUENTIAL_TRACE", "A trace requires schema_version, clock, initialization, and cycles")
    if type(trace["schema_version"]) is not int or trace["schema_version"] != 1 or trace["clock"] != interface["clock"]:
        raise SequentialError("SEQUENTIAL_TRACE", "The trace must use schema_version 1 and the design's dedicated rising-edge clock")
    initialization = trace["initialization"]
    if (not isinstance(initialization, dict) or set(initialization) != {"kind", "cycles"}
            or initialization["kind"] != "input_sequence"):
        raise SequentialError("SEQUENTIAL_TRACE", "Initialization must be an explicit input_sequence; internal state forcing is unsupported")
    for label, rows, lower, upper in (("initialization", initialization["cycles"], 1, MAX_PREFIX_CYCLES),
                                      ("checked", trace["cycles"], 1, MAX_CHECKED_CYCLES)):
        if not isinstance(rows, list) or not lower <= len(rows) <= upper:
            raise SequentialError("SEQUENTIAL_TRACE", f"Provide {lower} through {upper} {label} cycles")
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"values"} or not isinstance(row["values"], dict):
                raise SequentialError("SEQUENTIAL_TRACE", f"The {label} cycle {index} must contain exactly one values object")
            if set(row["values"]) != set(interface["inputs"]):
                raise SequentialError("SEQUENTIAL_TRACE", f"The {label} cycle {index} must specify every nonclock input exactly once: {list(interface['inputs'])}")
            for name, width in interface["inputs"].items():
                value = row["values"][name]
                if type(value) is not int or not 0 <= value < (1 << width):
                    raise SequentialError("SEQUENTIAL_TRACE", f"The {label} cycle {index} input {name} requires an unsigned {width}-bit pattern")
    return deepcopy(trace)


@dataclass(frozen=True)
class _Word:
    value: int
    known: int
    width: int

    @property
    def mask(self):
        return (1 << self.width) - 1

    @property
    def complete(self):
        return self.known == self.mask


def _word(value: int, known: int, width: int) -> _Word:
    mask = (1 << width) - 1
    return _Word(value & known & mask, known & mask, width)


def _constant(value: int, width: int) -> _Word:
    return _word(value, (1 << width) - 1, width)


def _resize(word: _Word, width: int, signed: bool = False) -> _Word:
    value, known = word.value, word.known
    if width > word.width:
        extension = ((1 << width) - 1) ^ word.mask
        if not signed:
            known |= extension
        elif word.known & (1 << (word.width - 1)):
            known |= extension
            if word.value & (1 << (word.width - 1)):
                value |= extension
    return _word(value, known, width)


def _truth(word: _Word) -> _Word:
    if word.value & word.known:
        return _constant(1, 1)
    return _word(0, int(word.complete), 1)


def _bitwise(kind: str, a: _Word, b: _Word) -> _Word:
    if kind == "$and":
        known = (a.known & b.known) | (a.known & ~a.value) | (b.known & ~b.value)
        value = a.value & b.value
    elif kind == "$or":
        known = (a.known & b.known) | (a.known & a.value) | (b.known & b.value)
        value = a.value | b.value
    else:
        known, value = a.known & b.known, a.value ^ b.value
        if kind == "$xnor":
            value = ~value
    return _word(value, known, a.width)


def _signed(word: _Word) -> int:
    return word.value - (1 << word.width) if word.value & (1 << (word.width - 1)) else word.value


def _evaluate(cell: dict, words: dict[str, _Word]) -> _Word:
    kind, parameters = cell["type"], cell["parameters"]
    width = len(cell["ports"]["Y"])
    a = words["A"]
    if kind == "$mux":
        b, select = words["B"], words["S"]
        if select.complete:
            return b if select.value else a
        return _word(a.value, a.known & b.known & ~(a.value ^ b.value), width)
    if kind in {"$reduce_and", "$reduce_or", "$reduce_bool", "$reduce_xor", "$reduce_xnor", "$logic_not"}:
        if kind == "$reduce_and":
            zero = bool(a.known & ~a.value & a.mask)
            result = _word(int(not zero), int(zero or a.complete), 1)
        elif kind in {"$reduce_xor", "$reduce_xnor"}:
            result = _word((a.value.bit_count() & 1) ^ int(kind == "$reduce_xnor"), int(a.complete), 1)
        else:
            result = _truth(a)
            if kind == "$logic_not":
                result = _word(~result.value, result.known, 1)
        return _resize(result, width)
    if kind in {"$logic_and", "$logic_or"}:
        result = _bitwise("$and" if kind == "$logic_and" else "$or", _truth(a), _truth(words["B"]))
        return _resize(result, width)
    if kind in {"$pos", "$not", "$neg"}:
        a = _resize(a, width, bool(parameters["A_SIGNED"]))
        if kind == "$pos":
            return a
        if kind == "$not":
            return _word(~a.value, a.known, width)
        return _constant(-a.value, width) if a.complete else _word(0, 0, width)
    b = words["B"]
    if kind in {"$shl", "$sshl", "$shr", "$sshr"}:
        signed = bool(parameters["A_SIGNED"])
        working_width = max(a.width, width) if kind in {"$shr", "$sshr"} else width
        a = _resize(a, working_width, signed)
        if not b.complete:
            return _word(0, 0, width)
        amount = min(b.value, working_width)
        if kind in {"$shl", "$sshl"}:
            fill = (1 << amount) - 1
            return _word(a.value << amount, (a.known << amount) | fill, width)
        fill = ((1 << amount) - 1) << (working_width - amount)
        sign = signed and kind == "$sshr"
        known_fill = fill if not sign or a.known & (1 << (working_width - 1)) else 0
        value_fill = fill if sign and a.value & (1 << (working_width - 1)) else 0
        return _resize(_word((a.value >> amount) | value_fill, (a.known >> amount) | known_fill, working_width), width)
    signed = bool(parameters.get("A_SIGNED") and parameters.get("B_SIGNED"))
    common_width = max(a.width, b.width, width)
    a, b = _resize(a, common_width, signed), _resize(b, common_width, signed)
    if kind in {"$and", "$or", "$xor", "$xnor"}:
        return _resize(_bitwise(kind, a, b), width)
    if not a.complete or not b.complete:
        # Boolean results have known-zero upper bits; keep arithmetic conservative.
        return _resize(_word(0, 0, 1), width) if kind in {"$eq", "$ne", "$eqx", "$nex", "$lt", "$le", "$ge", "$gt"} else _word(0, 0, width)
    av, bv = (_signed(a), _signed(b)) if signed else (a.value, b.value)
    operations = {"$add": lambda: av + bv, "$sub": lambda: av - bv,
                  "$eq": lambda: int(av == bv), "$ne": lambda: int(av != bv),
                  "$eqx": lambda: int(av == bv), "$nex": lambda: int(av != bv),
                  "$lt": lambda: int(av < bv), "$le": lambda: int(av <= bv),
                  "$ge": lambda: int(av >= bv), "$gt": lambda: int(av > bv)}
    if kind not in operations:
        raise SequentialError("INITIALIZATION_UNSUPPORTED", f"Initialization analysis does not support {kind}")
    return _constant(operations[kind](), width)


class _Machine:
    def __init__(self, design: dict):
        self.interface = sequential_interface(design)
        self.ports = [port for port in design["ports"] if port["direction"] == "input"]
        self.registers = self.interface["registers"]
        self.state = {bit: (0, False) for cell in self.registers for bit in cell["ports"]["Q"]}
        cells = {cell["id"]: cell for cell in design["cells"] if cell["type"] != "$dff"}
        supported = {"$pos", "$not", "$neg", "$and", "$or", "$xor", "$xnor", "$mux",
                     "$reduce_and", "$reduce_or", "$reduce_bool", "$reduce_xor", "$reduce_xnor",
                     "$logic_not", "$logic_and", "$logic_or", "$shl", "$sshl", "$shr", "$sshr",
                     "$add", "$sub", "$eq", "$ne", "$eqx", "$nex", "$lt", "$le", "$ge", "$gt"}
        for cell in cells.values():
            if cell["type"] not in supported:
                raise SequentialError("INITIALIZATION_UNSUPPORTED", f"Initialization analysis does not support {cell['type']}")
        producers = {bit: cell["id"] for cell in cells.values() for bit in cell["ports"]["Y"]}
        dependencies = {identifier: {producers[bit] for pin, bits in cell["ports"].items()
                                     if cell["port_directions"][pin] == "input" for bit in bits if bit in producers}
                        for identifier, cell in cells.items()}
        ready = deque(sorted(identifier for identifier, parents in dependencies.items() if not parents))
        self.ordered = []
        while ready:
            identifier = ready.popleft()
            self.ordered.append(cells[identifier])
            for other in sorted(dependencies):
                if identifier in dependencies[other]:
                    dependencies[other].remove(identifier)
                    if not dependencies[other]:
                        ready.append(other)
        if len(self.ordered) != len(cells):
            raise SequentialError("COMBINATIONAL_CYCLE", "Initialization cannot evaluate a combinational feedback loop")

    @staticmethod
    def word(bits, values):
        value = known = 0
        for index, bit in enumerate(bits):
            number, established = values[bit]
            value |= number << index
            known |= int(established) << index
        return _word(value, known, len(bits))

    def step(self, row: dict) -> None:
        values = {"0": (0, True), "1": (1, True), **self.state}
        for port in self.ports:
            number = 0 if port["name"] == self.interface["clock"]["port"] else row["values"][port["name"]]
            for index, bit in enumerate(port["bits"]):
                values[bit] = ((number >> index) & 1, True)
        for cell in self.ordered:
            words = {pin: self.word(bits, values) for pin, bits in cell["ports"].items()
                     if cell["port_directions"][pin] == "input"}
            result = _evaluate(cell, words)
            for index, bit in enumerate(cell["ports"]["Y"]):
                values[bit] = ((result.value >> index) & 1, bool((result.known >> index) & 1))
        # Compute every D from the old Q snapshot to preserve simultaneous capture.
        pending = {q: values[d] for cell in self.registers for d, q in zip(cell["ports"]["D"], cell["ports"]["Q"])}
        self.state = pending

    @property
    def complete(self):
        return all(known for _value, known in self.state.values())

    def report(self, prefix):
        states = []
        for cell in self.registers:
            word = self.word(cell["ports"]["Q"], self.state)
            states.append({"cell_id": cell["id"], "width": word.width, "value": hex(word.value),
                           "known_mask": hex(word.known), "known_bits": word.known.bit_count()})
        return {"valid": self.complete, "method": "conservative-bit-knowledge-v1",
                "initial_state": "all_retained_register_bits_unknown", "internal_state_forcing": False,
                "prefix": deepcopy(prefix), "prefix_cycles": len(prefix),
                "register_count": len(states), "register_bits": len(self.state),
                "known_register_count": sum(state["known_bits"] == state["width"] for state in states),
                "known_register_bits": sum(known for _value, known in self.state.values()), "registers": states}


def prove_initialization(design: dict, trace: dict) -> dict:
    trace = validate_trace(design, trace)
    machine = _Machine(design)
    prefix = trace["initialization"]["cycles"]
    for row in prefix:
        machine.step(row)
    proof = machine.report(prefix)
    if not proof["valid"]:
        unknown = [f"{item['cell_id']} ({item['width'] - item['known_bits']} unknown bits)"
                   for item in proof["registers"] if item["known_bits"] != item["width"]]
        raise SequentialError("UNINITIALIZED_STATE", "The initialization input sequence does not establish every retained register: "
                              + ", ".join(unknown[:16]) + ". Provide a reset/load trace; Digital's default state is never an initialization assumption.")
    return proof


def _default_prefix(design: dict) -> list[dict]:
    machine = _Machine(design)
    widths = machine.interface["inputs"]
    prefix = []
    # Hold each input pattern until state stops changing or the warmup limit
    # is reached. Disabled registers advance to the next all-ones pattern.
    for high in (False, True, False):
        row = {"values": {name: (1 << width) - 1 if high else 0 for name, width in widths.items()}}
        while len(prefix) < MAX_PREFIX_CYCLES:
            previous = dict(machine.state)
            machine.step(row)
            prefix.append(deepcopy(row))
            if machine.complete:
                return prefix
            if machine.state == previous:
                break
    return prefix


def _checked_cycles(inputs: dict[str, int], seed: int, random_count: int) -> tuple[list[dict], int, bool]:
    zeros = {name: 0 for name in inputs}
    ones = {name: (1 << width) - 1 for name, width in inputs.items()}
    rows = []
    budget = MAX_CHECKED_CYCLES - random_count
    truncated = False

    def add(values):
        nonlocal truncated
        if len(rows) >= budget:
            truncated = True
            return False
        rows.append({"values": dict(values)})
        return True

    for values in (zeros, zeros, ones, ones, zeros, ones):
        add(values)
    for name, width in inputs.items():
        for value in _boundaries(width):
            for baseline in (zeros, ones):
                values = {**baseline, name: value}
                if not add(values) or not add(values):
                    break
            if truncated:
                break
        if truncated:
            break
    directed = len(rows)
    generator = random.Random(seed)
    for _ in range(random_count):
        rows.append({"values": {name: generator.getrandbits(width) for name, width in inputs.items()}})
    return rows, directed, truncated


def sequential_stimulus_plan(design: dict, *, seed: int = 0, random_count: int = 256, trace: dict | None = None) -> dict:
    """Return a trace, proven initialization prefix and coverage metadata."""
    interface = sequential_interface(design)
    if type(seed) is not int or type(random_count) is not int or not 0 <= random_count <= 512:
        raise SequentialError("SEQUENTIAL_PARAMETERS", "Use an integer seed and random_count from 0 through 512")
    supplied = trace is not None
    directed, truncated = 0, False
    if trace is None:
        cycles, directed, truncated = _checked_cycles(interface["inputs"], seed, random_count)
        trace = {"schema_version": 1, "clock": interface["clock"],
                 "initialization": {"kind": "input_sequence", "cycles": _default_prefix(design)}, "cycles": cycles}
    trace = validate_trace(design, trace)
    proof = prove_initialization(design, trace)
    assumptions = {"generator": "sequential-boundaries-v1" if not supplied else "provided_trace",
                   "coverage": "provided_temporal_trace" if supplied else "directed_and_seeded_cycles" if random_count else "directed_cycles",
                   "exhaustive": False, "clock": deepcopy(trace["clock"]), "phases": list(PHASES),
                   "input_widths": interface["inputs"], "output_widths": interface["outputs"],
                   "value_encoding": "unsigned_integer_bit_patterns", "seed": None if supplied else seed,
                   "checked_cycles": len(trace["cycles"]), "sample_count": 3 * len(trace["cycles"]),
                   "random_cycles": 0 if supplied else random_count, "directed_cycles": directed,
                   "directed_truncated": truncated, "initialization": deepcopy(proof),
                   "claim": "Outputs agree for the recorded initialized temporal sequence; state-space coverage is not exhaustive"}
    return {"trace": trace, "initialization": proof, "assumptions": assumptions}
