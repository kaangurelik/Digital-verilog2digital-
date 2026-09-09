"""The scalar profile maps electrical intent before any placement or routing."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import json

from .. import contracts
from ..adapter import AdapterError
from ..frontend.errors import FrontendError
from ..ir import validate_design


class MappingError(FrontendError):
    """A structured mapping/profile failure, compatible with frontend diagnostics."""


# Names are verified against the pinned native Digital component descriptions.
_BINARY = {"$and": "And", "$or": "Or", "$xor": "XOr", "$xnor": "XNOr",
           "$logic_and": "And", "$logic_or": "Or"}
_INVERT = {"$not", "$logic_not", "$reduce_xnor"}
_IDENTITY = {"$pos", "$reduce_and", "$reduce_or", "$reduce_xor", "$reduce_bool"}
_SUPPORTED = set(_BINARY) | _INVERT | _IDENTITY | {"$mux"}
_GATES = {"And", "Or", "XOr", "XNOr"}
_PIN_SETS = {
    "In": {("output", "out")}, "Out": {("input", "in")},
    "Const": {("output", "out")}, "Not": {("input", "in"), ("output", "out")},
    **{kind: {("input", "In_1"), ("input", "In_2"), ("output", "out")} for kind in _GATES},
    "Multiplexer": {("input", "in_0"), ("input", "in_1"), ("input", "sel"), ("output", "out")},
}
MAX_INSTANCES = 64


def _net_id(bit: int | str) -> str:
    return f"b{bit}" if type(bit) is int else f"k{bit}"


def _endpoint_key(endpoint: dict) -> tuple[str, str, str]:
    return endpoint["instance_id"], endpoint["direction"], endpoint["pin"]


def _preflight(design: dict) -> None:
    try:
        validate_design(design)
    except FrontendError as error:
        raise MappingError(error.code, f"Cannot map invalid design: {error.message}",
                           path=error.path, line=error.line, column=error.column) from error
    input_count = sum(port["direction"] == "input" for port in design["ports"])
    if not any(port["direction"] == "output" for port in design["ports"]):
        raise MappingError("UNSUPPORTED_MAPPING_INTERFACE", "The scalar profile requires at least one output port to verify the generated circuit.")
    if input_count > 12:
        raise MappingError("MAPPING_RESOURCE_LIMIT", f"The scalar profile accepts at most 12 input ports (4096 exhaustive test vectors); found {input_count}.")
    for vector in [*design["ports"], *design["nets"]]:
        if len(vector["bits"]) != 1:
            raise MappingError("UNSUPPORTED_MAPPING_WIDTH",
                               f"Signal {vector['name']} has {len(vector['bits'])} bits. "
                               "The scalar-v1 mapper accepts one-bit signals; bus mapping is a later profile.")
    for cell in design["cells"]:
        if cell["type"] == "$dff":
            raise MappingError("UNSUPPORTED_MAPPING_SEQUENTIAL",
                               f"Cell {cell['id']} is a register. Use the later sequential mapping profile.")
        if cell["type"] not in _SUPPORTED:
            raise MappingError("UNSUPPORTED_MAPPING_CELL",
                               f"Cell {cell['id']} ({cell['type']}) has no scalar mapping. "
                               "Use supported bitwise logic or a multiplexer; arithmetic expansion is not enabled.")
        for pin, bits in cell["ports"].items():
            if len(bits) != 1:
                raise MappingError("UNSUPPORTED_MAPPING_WIDTH",
                                   f"Cell {cell['id']}.{pin} has {len(bits)} bits; scalar-v1 requires one-bit operands and results.")
    constants = {bit for port in design["ports"] for bit in port["bits"] if type(bit) is str}
    constants.update(bit for cell in design["cells"] for bits in cell["ports"].values()
                     for bit in bits if type(bit) is str)
    count = len(design["ports"]) + len(design["cells"]) + len(constants)
    if count > MAX_INSTANCES:
        raise MappingError("MAPPING_RESOURCE_LIMIT", f"Mapping needs {count} components; the current limit is {MAX_INSTANCES}.")


def map_scalar(design: dict, adapter) -> dict:
    """Place components at the origin and assign one expected net to every pin.

    Include unused drivers. Net names do not determine connectivity, and source
    metadata stays separate from native instance records.
    """
    _preflight(design)
    instances, descriptors, inputs, outputs, origins = [], [], [], [], []
    endpoints = defaultdict(list)
    cache = {}

    def add(identifier, kind, attributes, bindings, origin):
        request = {"type": kind, "attributes": attributes, "position": {"x": 0, "y": 0}, "rotation": 0}
        key = json.dumps(request, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            try:
                cache[key] = adapter.call("component.describe", request)
            except AdapterError as error:
                raise MappingError("NATIVE_COMPONENT_UNAVAILABLE", f"Cannot describe Digital {kind}: {error}") from error
        descriptor = deepcopy(cache[key])
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("pins"), list) or any(
                not isinstance(pin, dict) or "direction" not in pin or "pin" not in pin for pin in descriptor.get("pins", [])):
            raise MappingError("NATIVE_PIN_MISMATCH", f"Digital {kind} returned a malformed native pin inventory.")
        descriptor["id"] = identifier
        actual = {(pin["direction"], pin["pin"]) for pin in descriptor.get("pins", [])}
        if actual != set(bindings) or actual != _PIN_SETS[kind] or len(actual) != len(descriptor["pins"]):
            raise MappingError("NATIVE_PIN_MISMATCH", f"Digital {kind} pin inventory differs from the verified scalar mapping.")
        if any(type(pin.get("width")) is not int or pin["width"] != 1 for pin in descriptor["pins"]):
            raise MappingError("NATIVE_PIN_WIDTH", f"Digital {kind} must expose exactly one-bit native pins.")
        if descriptor.get("type") != kind or descriptor.get("position") != request["position"] or descriptor.get("rotation") != 0:
            raise MappingError("NATIVE_COMPONENT_MISMATCH", f"Digital {kind} did not resolve the requested component geometry.")
        resolved = descriptor.get("attributes", {})
        if any(resolved.get(name) != value for name, value in attributes.items()):
            raise MappingError("NATIVE_ATTRIBUTE_MISMATCH", f"Digital {kind} did not preserve the requested scalar attributes.")
        instances.append({"id": identifier, "type": kind, "attributes": deepcopy(resolved),
                          "position": {"x": 0, "y": 0}, "rotation": 0})
        descriptors.append(descriptor)
        origins.append({"instance_id": identifier, **origin})
        for (direction, pin), bit in bindings.items():
            endpoints[bit].append({"instance_id": identifier, "direction": direction, "pin": pin})

    for direction, target, native_type, pin_direction, pin in [
        ("input", inputs, "In", "output", "out"), ("output", outputs, "Out", "input", "in")
    ]:
        ports = sorted((port for port in design["ports"] if port["direction"] == direction), key=lambda port: port["name"])
        for index, port in enumerate(ports):
            identifier = f"{'in' if direction == 'input' else 'out'}{index:04d}"
            attributes = {"Bits": 1, "Label": port["name"]}
            if direction == "input":
                attributes.update(isHighZ=False, avoidActiveLow=True)
            add(identifier, native_type, attributes, {(pin_direction, pin): port["bits"][0]},
                {"kind": "port", "port": port["name"], "source": port["source"]})
            target.append({"port": port["name"], "instance_id": identifier, "width": 1})

    for index, cell in enumerate(sorted(design["cells"], key=lambda cell: cell["id"])):
        kind, ports = cell["type"], cell["ports"]
        attributes = {"Bits": 1}
        if kind in _BINARY:
            native = _BINARY[kind]
            attributes["Inputs"] = 2
            bindings = {("input", "In_1"): ports["A"][0], ("input", "In_2"): ports["B"][0], ("output", "out"): ports["Y"][0]}
        elif kind in _INVERT:
            native = "Not"
            bindings = {("input", "in"): ports["A"][0], ("output", "out"): ports["Y"][0]}
        elif kind in _IDENTITY:
            # AND(A,A) buffers one bit while preserving Y's distinct net identity.
            native = "And"
            attributes["Inputs"] = 2
            bindings = {("input", "In_1"): ports["A"][0], ("input", "In_2"): ports["A"][0], ("output", "out"): ports["Y"][0]}
        else:
            native = "Multiplexer"
            attributes["Selector Bits"] = 1
            bindings = {("input", "in_0"): ports["A"][0], ("input", "in_1"): ports["B"][0],
                        ("input", "sel"): ports["S"][0], ("output", "out"): ports["Y"][0]}
        add(f"g{index:04d}", native, attributes, bindings,
            {"kind": "cell", "cell_id": cell["id"], "native_name": cell["native_name"],
             "hierarchy": cell["hierarchy"], "source": cell["source"]})

    for bit in sorted(bit for bit in endpoints if type(bit) is str):
        add(f"const{bit}", "Const", {"Bits": 1, "Value": f"0x{bit}"}, {("output", "out"): bit},
            {"kind": "constant", "value": bit, "source": ""})
    nets = [{"id": _net_id(bit), "bit": bit, "width": 1,
             "endpoints": sorted(pins, key=_endpoint_key)}
            for bit, pins in sorted(endpoints.items(), key=lambda item: (type(item[0]) is str, item[0]))]
    result = {"schema_version": 1, "profile": "scalar-v1", "top": design["top"],
              "instances": instances, "descriptors": descriptors, "nets": nets,
              "inputs": inputs, "outputs": outputs, "origins": origins}
    validate_mapped(result)
    return result


def validate_mapped(mapped: dict) -> None:
    """Validate a mapped artifact's native pin coverage and binary electrical rules."""
    try:
        contracts.validate("mapped", mapped)
    except contracts.ContractError as error:
        raise MappingError("INVALID_MAPPING", str(error)) from error
    instances = {item["id"]: item for item in mapped["instances"]}
    descriptors = {item["id"]: item for item in mapped["descriptors"]}
    if len(instances) != len(mapped["instances"]) or len(descriptors) != len(mapped["descriptors"]) or instances.keys() != descriptors.keys():
        raise MappingError("INVALID_MAPPING_INSTANCES", "Mapped instance and descriptor IDs must be unique and identical.")
    actual_pins = set()
    for identifier, instance in instances.items():
        descriptor = descriptors[identifier]
        if any(descriptor.get(key) != instance[key] for key in ("type", "attributes", "position", "rotation")):
            raise MappingError("INVALID_MAPPING_DESCRIPTOR", f"Descriptor disagrees with component {identifier}.")
        if type(instance["attributes"].get("Bits")) is not int or instance["attributes"]["Bits"] != 1:
            raise MappingError("INVALID_MAPPING_WIDTH", f"Component {identifier} must have Bits=1.")
        pin_names = {(pin["direction"], pin["pin"]) for pin in descriptor["pins"]}
        if pin_names != _PIN_SETS[instance["type"]] or len(pin_names) != len(descriptor["pins"]):
            raise MappingError("INVALID_MAPPING_PINS", f"Component {identifier} has an unexpected native pin inventory.")
        if instance["type"] in _GATES and instance["attributes"].get("Inputs") != 2:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", f"Gate {identifier} requires two inputs.")
        if instance["type"] == "Multiplexer" and instance["attributes"].get("Selector Bits") != 1:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", f"Multiplexer {identifier} requires one selector bit.")
        if instance["type"] == "In" and instance["attributes"].get("isHighZ") is not False:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", f"Input {identifier} must drive a binary value.")
        for box in (descriptor["bounds"], descriptor["body_bounds"]):
            if any(box["min"][axis] > box["max"][axis] for axis in ("x", "y")):
                raise MappingError("INVALID_MAPPING_GEOMETRY", f"Component {identifier} has reversed native bounds.")
        for pin in descriptor["pins"]:
            if type(pin["width"]) is not int or any(type(value) is not int for value in pin["position"].values()):
                raise MappingError("INVALID_MAPPING_GEOMETRY", f"Component {identifier} requires integer pin widths and coordinates.")
            escape = pin["escape"]
            if any(type(value) is not int for value in escape.values()) or abs(escape["x"]) + abs(escape["y"]) != 1:
                raise MappingError("INVALID_MAPPING_GEOMETRY", f"Component {identifier} requires a cardinal native pin escape direction.")
        actual_pins.update((identifier, direction, pin) for direction, pin in pin_names)
    covered, bits, net_ids = set(), set(), set()
    logic = {identifier for identifier, item in instances.items() if item["type"] not in {"In", "Out", "Const"}}
    dependencies = {identifier: set() for identifier in logic}
    dependents = defaultdict(set)
    for net in mapped["nets"]:
        bit = net["bit"]
        if (type(bit) is not int and bit not in {"0", "1"}) or net["id"] != _net_id(bit) or bit in bits or net["id"] in net_ids:
            raise MappingError("INVALID_MAPPING_NET", f"Net {net['id']} has a duplicate or invalid bit identity.")
        bits.add(bit)
        net_ids.add(net["id"])
        drivers = []
        for endpoint in net["endpoints"]:
            key = _endpoint_key(endpoint)
            if key not in actual_pins or key in covered:
                raise MappingError("INVALID_MAPPING_ENDPOINT", f"Endpoint {key} is unknown or occurs in more than one expected net.")
            covered.add(key)
            if endpoint["direction"] == "output":
                drivers.append(instances[endpoint["instance_id"]])
        if len(drivers) != 1:
            raise MappingError("INVALID_MAPPING_DRIVERS", f"Net {net['id']} requires exactly one binary driver; found {len(drivers)}.")
        driver = drivers[0]
        if driver["id"] in logic:
            for endpoint in net["endpoints"]:
                destination = endpoint["instance_id"]
                if endpoint["direction"] == "input" and destination in logic:
                    dependencies[destination].add(driver["id"])
                    dependents[driver["id"]].add(destination)
        if type(bit) is str:
            if driver["type"] != "Const" or driver["attributes"].get("Value") != f"0x{bit}":
                raise MappingError("INVALID_MAPPING_CONSTANT", f"Net {net['id']} must be driven by the matching binary constant.")
        elif driver["type"] == "Const":
            raise MappingError("INVALID_MAPPING_CONSTANT", f"Constant component drives nonconstant bit {bit}.")
    if covered != actual_pins:
        raise MappingError("INCOMPLETE_MAPPING", f"Native pins missing expected nets: {sorted(actual_pins - covered)[:16]}.")
    ready = deque(identifier for identifier, sources in dependencies.items() if not sources)
    visited = 0
    while ready:
        identifier = ready.popleft()
        visited += 1
        for destination in dependents[identifier]:
            dependencies[destination].remove(identifier)
            if not dependencies[destination]:
                ready.append(destination)
    if visited != len(logic):
        raise MappingError("INVALID_MAPPING_CYCLE", "Mapped combinational components form a cycle; feedback requires a sequential profile.")
    ports_seen = set()
    for collection, kind in (("inputs", "In"), ("outputs", "Out")):
        listed = set()
        for port in mapped[collection]:
            identifier = port["instance_id"]
            if port["port"] in ports_seen or identifier in listed or identifier not in instances:
                raise MappingError("INVALID_MAPPING_PORT", "Port names and instance references must be unique.")
            instance = instances[identifier]
            if instance["type"] != kind or instance["attributes"].get("Label") != port["port"]:
                raise MappingError("INVALID_MAPPING_PORT", f"Port {port['port']} does not match its native {kind} component.")
            ports_seen.add(port["port"])
            listed.add(identifier)
        if listed != {identifier for identifier, instance in instances.items() if instance["type"] == kind}:
            raise MappingError("INVALID_MAPPING_PORT", f"Every native {kind} component must appear in the port interface.")
    origin_ids = [origin["instance_id"] for origin in mapped["origins"]]
    if len(origin_ids) != len(set(origin_ids)) or set(origin_ids) != set(instances):
        raise MappingError("INVALID_MAPPING_ORIGIN", "Every component must have one source-origin record.")
