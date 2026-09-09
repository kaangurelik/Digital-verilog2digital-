"""Normalized design types and electrical validation independent of the HDL backend.

Bit vectors are LSB first. Net names are aliases, not separate electrical signals.
Cell rules follow the pinned Yosys share/yosys/simlib.v interface declarations.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Literal, NotRequired, TypedDict

from . import contracts
from .frontend.errors import FrontendError


Bit = int | Literal["0", "1"]
Direction = Literal["input", "output"]


class Port(TypedDict):
    name: str
    direction: Direction
    bits: list[Bit]
    offset: int
    upto: bool
    signed: bool
    source: str


class Net(TypedDict):
    name: str
    bits: list[Bit]
    offset: int
    upto: bool
    signed: bool
    source: str
    hidden: bool


class Cell(TypedDict):
    id: str
    type: str
    native_name: str
    parameters: dict[str, int]
    ports: dict[str, list[Bit]]
    port_directions: dict[str, Direction]
    source: str
    hierarchy: str


class SourceOrigin(TypedDict):
    module: str
    path: NotRequired[str]
    line: NotRequired[int]
    column: NotRequired[int]
    end_line: int | None
    end_column: int | None


class Diagnostic(TypedDict):
    code: str
    message: str
    severity: Literal["warning", "note", "error", "fatal", "ignored"]
    path: NotRequired[str]
    line: NotRequired[int]
    column: NotRequired[int]


class ToolVersions(TypedDict):
    yosys: str
    pyslang: str


class Configuration(TypedDict):
    sources: list[str]
    include_dirs: list[str]
    defines: dict[str, str]
    parameters: dict[str, str | int]


class Design(TypedDict):
    schema_version: Literal[1]
    top: str
    ports: list[Port]
    nets: list[Net]
    cells: list[Cell]
    origins: list[SourceOrigin]
    diagnostics: list[Diagnostic]
    tools: ToolVersions
    configuration: Configuration


UNARY_CELLS = frozenset({"$not", "$pos", "$neg", "$reduce_and", "$reduce_or", "$reduce_xor",
                         "$reduce_xnor", "$reduce_bool", "$logic_not"})
BINARY_CELLS = frozenset({"$and", "$or", "$xor", "$xnor", "$logic_and", "$logic_or", "$shl", "$shr",
                          "$sshl", "$sshr", "$lt", "$le", "$eq", "$ne", "$eqx", "$nex", "$ge", "$gt",
                          "$add", "$sub", "$mul", "$div", "$mod", "$pow"})
SUPPORTED_CELLS = UNARY_CELLS | BINARY_CELLS | {"$mux", "$pmux", "$dff"}


def _unique(items: Iterable[Mapping[str, object]], key: str, label: str, code: str) -> None:
    seen = set()
    for item in items:
        value = item[key]
        if value in seen:
            raise FrontendError(code, f"Duplicate {label}: {value}")
        seen.add(value)


def _validate_bits(bits: list[Bit], label: str, limit: int = 64) -> None:
    if not 1 <= len(bits) <= limit:
        raise FrontendError("UNSUPPORTED_WIDTH", f"{label} has width {len(bits)}; supported range is 1 through {limit}")
    for bit in bits:
        # JSON Schema considers 2.0 an integer. Signal identities require Python ints.
        if (type(bit) is int and bit >= 2) or (type(bit) is str and bit in {"0", "1"}):
            continue
        raise FrontendError("INVALID_BIT", f"{label} has an invalid bit identity: {bit!r}")


def _validate_cell(cell: Cell) -> None:
    kind = cell["type"]
    label = f"Cell {cell['id']} ({kind})"
    if kind not in SUPPORTED_CELLS:
        raise FrontendError("UNSUPPORTED_CELL", f"{label} is outside the current support profile")
    if kind in UNARY_CELLS:
        directions = {"A": "input", "Y": "output"}
        parameter_names = {"A_SIGNED", "A_WIDTH", "Y_WIDTH"}
    elif kind in BINARY_CELLS:
        directions = {"A": "input", "B": "input", "Y": "output"}
        parameter_names = {"A_SIGNED", "B_SIGNED", "A_WIDTH", "B_WIDTH", "Y_WIDTH"}
    elif kind in {"$mux", "$pmux"}:
        directions = {"A": "input", "B": "input", "S": "input", "Y": "output"}
        parameter_names = {"WIDTH", "S_WIDTH"} if kind == "$pmux" else {"WIDTH"}
    else:
        directions = {"CLK": "input", "D": "input", "Q": "output"}
        parameter_names = {"WIDTH", "CLK_POLARITY"}
    if set(cell["ports"]) != set(directions) or cell["port_directions"] != directions:
        raise FrontendError("INVALID_CELL_PORTS", f"{label} requires port directions {directions}")
    parameters = cell["parameters"]
    if set(parameters) != parameter_names:
        missing, extra = parameter_names - parameters.keys(), parameters.keys() - parameter_names
        raise FrontendError("INVALID_CELL_PARAMETERS",
                            f"{label} has missing parameters {sorted(missing)} and unexpected parameters {sorted(extra)}")
    for name, value in parameters.items():
        if type(value) is not int:
            raise FrontendError("INVALID_CELL_PARAMETERS", f"{label} parameter {name} must be an integer")
        if name.endswith("_WIDTH") or name == "WIDTH":
            if not 1 <= value <= 64:
                raise FrontendError("UNSUPPORTED_WIDTH", f"{label} parameter {name}={value} is outside 1 through 64")
        elif value not in {0, 1}:
            raise FrontendError("INVALID_CELL_PARAMETERS", f"{label} parameter {name} must be 0 or 1")
    if kind == "$dff" and parameters["CLK_POLARITY"] != 1:
        raise FrontendError("UNSUPPORTED_CLOCK", f"{label} must use a rising-edge clock (CLK_POLARITY=1)")
    if kind in UNARY_CELLS | BINARY_CELLS:
        widths = {pin: parameters[f"{pin}_WIDTH"] for pin in directions}
    elif kind in {"$mux", "$pmux"}:
        width, selectors = parameters["WIDTH"], parameters.get("S_WIDTH", 1)
        widths = {"A": width, "B": width * selectors, "S": selectors, "Y": width}
    else:
        widths = {"CLK": 1, "D": parameters["WIDTH"], "Q": parameters["WIDTH"]}
    for pin, bits in cell["ports"].items():
        _validate_bits(bits, f"{label}.{pin}", 4096 if kind == "$pmux" and pin == "B" else 64)
        if len(bits) != widths[pin]:
            raise FrontendError("CELL_WIDTH_MISMATCH",
                                f"{label}.{pin} has {len(bits)} bits but its parameters require {widths[pin]}")


def validate_design(design: Design) -> None:
    """Validate connectivity without mutating it; raise FrontendError on failure.

    Cycle checks use conservative operation dependencies. Register outputs are
    independent sources within each simulation step.
    """
    try:
        contracts.validate("design", design)
    except contracts.ContractError as error:
        raise FrontendError("INVALID_DESIGN", str(error)) from error
    _unique(design["ports"], "name", "port name", "DUPLICATE_PORT")
    _unique(design["nets"], "name", "net name", "DUPLICATE_NET")
    _unique(design["cells"], "id", "cell ID", "DUPLICATE_CELL")
    _unique(design["cells"], "native_name", "native cell name", "DUPLICATE_CELL")
    nets = {net["name"]: net for net in design["nets"]}
    for vector in [*design["ports"], *design["nets"]]:
        _validate_bits(vector["bits"], f"Signal {vector['name']}")
        if type(vector["offset"]) is not int:
            raise FrontendError("INVALID_DESIGN", f"Signal {vector['name']} offset must be an integer")
    for port in design["ports"]:
        alias = nets.get(port["name"])
        if alias is not None and alias["bits"] != port["bits"]:
            raise FrontendError("PORT_NET_MISMATCH", f"Port {port['name']} and its same-name net refer to different bits")
    for cell in design["cells"]:
        _validate_cell(cell)

    drivers: dict[int, tuple[str, str | None]] = {}
    used: dict[int, str] = {}
    input_bits: set[int] = set()

    def drive(bit: Bit, endpoint: str, cell_id: str | None = None) -> None:
        if type(bit) is not int:
            raise FrontendError("INVALID_DRIVER", f"Driver {endpoint} targets constant {bit}; only signal bits can be driven")
        if bit in drivers:
            raise FrontendError("MULTIPLE_DRIVERS", f"Bit {bit} has multiple drivers: {drivers[bit][0]} and {endpoint}")
        drivers[bit] = (endpoint, cell_id)

    for port in design["ports"]:
        for index, bit in enumerate(port["bits"]):
            endpoint = f"port {port['name']}[{index}]"
            if port["direction"] == "input":
                drive(bit, endpoint)
                input_bits.add(bit)
            elif type(bit) is int:
                used.setdefault(bit, endpoint)
    for cell in design["cells"]:
        for pin, bits in cell["ports"].items():
            for index, bit in enumerate(bits):
                endpoint = f"{cell['id']}.{pin}[{index}]"
                if cell["port_directions"][pin] == "output":
                    drive(bit, endpoint, cell["id"])
                elif type(bit) is int:
                    used.setdefault(bit, endpoint)
    undriven = sorted(used.keys() - drivers.keys())
    if undriven:
        detail = ", ".join(f"{bit} ({used[bit]})" for bit in undriven[:16])
        raise FrontendError("UNDRIVEN_NET", f"Used bits have no driver: {detail}")
    clocks = {cell["ports"]["CLK"][0] for cell in design["cells"] if cell["type"] == "$dff"}
    if len(clocks) > 1 or any(clock not in input_bits for clock in clocks):
        raise FrontendError("UNSUPPORTED_CLOCK", "Registers must share one clock driven directly by a top-level input")

    combinational = {cell["id"]: cell for cell in design["cells"] if cell["type"] != "$dff"}
    dependencies = {cell_id: set() for cell_id in combinational}
    dependents: dict[str, set[str]] = defaultdict(set)
    for cell_id, cell in combinational.items():
        for pin, bits in cell["ports"].items():
            if cell["port_directions"][pin] == "input":
                for bit in bits:
                    if type(bit) is int:
                        owner = drivers[bit][1]
                        if owner in combinational:
                            dependencies[cell_id].add(owner)
        for owner in dependencies[cell_id]:
            dependents[owner].add(cell_id)
    ready = deque(cell_id for cell_id, inputs in dependencies.items() if not inputs)
    visited = 0
    while ready:
        cell_id = ready.popleft()
        visited += 1
        for consumer in dependents[cell_id]:
            dependencies[consumer].remove(cell_id)
            if not dependencies[consumer]:
                ready.append(consumer)
    if visited != len(combinational):
        blocked = sorted(cell_id for cell_id, inputs in dependencies.items() if inputs)
        raise FrontendError("COMBINATIONAL_CYCLE", f"Combinational dependency cycle blocks cells: {', '.join(blocked[:16])}")
