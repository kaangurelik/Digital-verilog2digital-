"""Single-clock synchronous mapping; startup behavior is an external checked trace."""
from __future__ import annotations

from .datapath import _map_components, _preflight, _validate_mapping
from .scalar import MappingError, _endpoint_key


def _clock(design):
    registers = [cell for cell in design["cells"] if cell["type"] == "$dff"]
    if not registers:
        raise MappingError("UNSUPPORTED_MAPPING_SEQUENTIAL", "The sequential profile requires at least one rising-edge register.")
    clocks = {cell["ports"]["CLK"][0] for cell in registers}
    if len(clocks) != 1:
        raise MappingError("UNSUPPORTED_CLOCK", "All registers must share one dedicated clock input.")
    bit = next(iter(clocks))
    ports = [port for port in design["ports"] if port["direction"] == "input" and port["bits"] == [bit]]
    if len(ports) != 1:
        raise MappingError("UNSUPPORTED_CLOCK", "The sequential profile requires a dedicated one-bit top-level clock port, not a bit of a data bus.")
    for port in design["ports"]:
        if port is not ports[0] and bit in port["bits"]:
            raise MappingError("UNSUPPORTED_CLOCK_USE", "The dedicated clock cannot also be forwarded as a data output.")
    for cell in design["cells"]:
        for pin, bits in cell["ports"].items():
            if bit in bits and not (cell["type"] == "$dff" and pin == "CLK"):
                raise MappingError("UNSUPPORTED_CLOCK_USE", f"Clock is used as data by {cell['id']}.{pin}; the sequential profile requires a dedicated clock.")
    return {"port": ports[0]["name"], "bit": bit, "edge": "rising"}


def map_sequential(design: dict, adapter) -> dict:
    """Map mux-preserved reset/enable logic and DFFs without inventing HDL state."""
    _preflight(design, sequential=True)
    return _map_components(design, adapter, clock=_clock(design))


def validate_sequential(mapped: dict) -> None:
    _validate_mapping(mapped, sequential=True)
    instances = {item["id"]: item for item in mapped["instances"]}
    bindings = {_endpoint_key(item): item for item in mapped["pin_bindings"]}
    clock = mapped["clock"]
    clock_port = next((port for port in mapped["inputs"] if port["port"] == clock["port"]), None)
    if clock_port is None or clock_port["width"] != 1 or clock_port["instance_id"] != clock["instance_id"] or bindings.get((clock["instance_id"], "output", "out"), {}).get("bits") != [clock["bit"]]:
        raise MappingError("INVALID_MAPPING_CLOCK", "Clock metadata must identify the dedicated native one-bit input and its exact signal bit.")
    register_ids = {identifier for identifier, instance in instances.items() if instance["type"] == "D_FF"}
    records = {register["instance_id"]: register for register in mapped["registers"]}
    if not register_ids or set(records) != register_ids or len(records) != len(mapped["registers"]):
        raise MappingError("INVALID_MAPPING_REGISTERS", "Every native D_FF requires exactly one register metadata record.")
    cell_ids = set()
    origins = {origin["instance_id"]: origin for origin in mapped["origins"]}
    for identifier, register in records.items():
        if register["cell_id"] in cell_ids or origins[identifier].get("cell_id") != register["cell_id"]:
            raise MappingError("INVALID_MAPPING_REGISTERS", "Register records must identify their unique source cell.")
        cell_ids.add(register["cell_id"])
        width = instances[identifier]["attributes"]["Bits"]
        if register["width"] != width or register["d_bits"] != bindings[identifier, "input", "D"]["bits"] or register["q_bits"] != bindings[identifier, "output", "Q"]["bits"]:
            raise MappingError("INVALID_MAPPING_REGISTERS", f"Register {identifier} metadata disagrees with native data pins.")
        if bindings[identifier, "input", "C"]["bits"] != [clock["bit"]]:
            raise MappingError("INVALID_MAPPING_CLOCK", f"Register {identifier} uses a different clock bit.")
        inverted = bindings[identifier, "output", "~Q"]
        if any(type(bit) is not int for bit in inverted["bits"]):
            raise MappingError("INVALID_MAPPING_REGISTERS", "Unused complemented register outputs require fresh signal identities.")
        net = next(net for net in mapped["nets"] if any(_endpoint_key(endpoint) == (identifier, "output", "~Q") for endpoint in net["endpoints"]))
        if len(net["endpoints"]) != 1:
            raise MappingError("INVALID_MAPPING_REGISTERS", "The complemented D_FF output must remain an explicit unused singleton net.")
    for key, binding in bindings.items():
        if clock["bit"] in binding["bits"] and key != (clock["instance_id"], "output", "out") and not (key[0] in register_ids and key[1:] == ("input", "C")):
            raise MappingError("INVALID_MAPPING_CLOCK", "The clock bit may connect only its source input and native D_FF.C pins.")
