"""Map binary datapaths with explicit LSB-first bus adapters.

Each wide native pin gets a bus and splitter connected to canonical HDL bits.
Slices, repeated bits, extensions and truncation are represented by wiring.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import json

from .. import contracts
from ..adapter import AdapterError
from ..frontend.errors import FrontendError
from ..ir import validate_design
from .scalar import MappingError, _endpoint_key, _net_id

MAX_INSTANCES = 250
_GATES = {"$and": "And", "$or": "Or", "$xor": "XOr", "$xnor": "XNOr"}
_REDUCTIONS = {"$reduce_and": "And", "$reduce_or": "Or", "$reduce_bool": "Or",
               "$reduce_xor": "XOr", "$reduce_xnor": "XNOr"}
_COMPARE = {"$lt": "<", "$le": ">", "$gt": ">", "$ge": "<", "$eq": "=", "$ne": "=",
            "$eqx": "=", "$nex": "="}
_SHIFTS = {"$shl", "$shr", "$sshl", "$sshr"}
_SUPPORTED = set(_GATES) | set(_REDUCTIONS) | set(_COMPARE) | _SHIFTS | {
    "$not", "$pos", "$neg", "$add", "$sub", "$mux", "$logic_not", "$logic_and", "$logic_or"}


def _resize(bits, width, signed=False):
    return list(bits[:width]) + [bits[-1] if signed else "0"] * max(0, width - len(bits))


def _split_pin(offset, width):
    return str(offset) if width == 1 else (f"{offset},{offset + 1}" if width == 2 else f"{offset}-{offset + width - 1}")


def _split_ports(attributes):
    """Accept only the two canonical, unambiguous splitter forms we emit."""
    left, right = attributes.get("Input Splitting"), attributes.get("Output Splitting")
    for wide, narrow, wide_direction in ((left, right, "input"), (right, left, "output")):
        if isinstance(wide, str) and wide.isascii() and wide.isdecimal() and 2 <= int(wide) <= 64 and narrow == f"1*{int(wide)}":
            width = int(wide)
            other = "output" if wide_direction == "input" else "input"
            return {(wide_direction, _split_pin(0, width)): (0, width),
                    **{(other, str(index)): (index, 1) for index in range(width)}}
    raise MappingError("INVALID_MAPPING_SPLITTER", "Datapath splitters must map a complete 2–64-bit bus to/from ordered single bits.")


def _widths(kind, attributes, custom_ports=None):
    """Independent native contract; never infer electrical width from a null pin."""
    if custom_ports is not None:
        return {(port["direction"], port["name"]): port["width"] for port in custom_ports}
    if kind == "Splitter":
        return {key: width for key, (_, width) in _split_ports(attributes).items()}
    width = attributes.get("Bits")
    if type(width) is not int or not 1 <= width <= 64:
        raise MappingError("INVALID_MAPPING_WIDTH", f"Digital {kind} requires an integer Bits value from 1 to 64.")
    if kind in {"In", "Const"}:
        return {("output", "out"): width}
    if kind == "Out":
        return {("input", "in"): width}
    if kind == "Not":
        return {("input", "in"): width, ("output", "out"): width}
    if kind in set(_GATES.values()):
        count = attributes.get("Inputs")
        if type(count) is not int or not 2 <= count <= 64:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", f"Digital {kind} requires 2–64 inputs in this profile.")
        return {**{("input", f"In_{i+1}"): width for i in range(count)}, ("output", "out"): width}
    if kind == "Multiplexer":
        if attributes.get("Selector Bits") != 1:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", "Datapath multiplexers require exactly one selector bit.")
        return {("input", "in_0"): width, ("input", "in_1"): width,
                ("input", "sel"): 1, ("output", "out"): width}
    if kind in {"Add", "Sub"}:
        return {("input", "a"): width, ("input", "b"): width, ("input", "c_i"): 1,
                ("output", "s"): width, ("output", "c_o"): 1}
    if kind == "Comparator":
        if type(attributes.get("Signed")) is not bool:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", "Comparator requires an explicit binary Signed attribute.")
        return {("input", "a"): width, ("input", "b"): width,
                **{("output", pin): 1 for pin in (">", "=", "<")}}
    if kind == "BarrelShifter":
        if attributes.get("barrelSigned") is not False or attributes.get("direction") not in {"left", "right"} or attributes.get("barrelShifterMode") not in {"logical", "arithmetic"}:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", "Datapath shifts require an unsigned amount and an explicit logical/arithmetic direction.")
        return {("input", "in"): width, ("input", "shift"): width.bit_length(), ("output", "out"): width}
    if kind == "D_FF":
        if attributes.get("Default") != "0x0":
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", "Register bootstrap must be explicit Default=0x0; HDL initialization is proved separately from input sequences.")
        return {("input", "D"): width, ("input", "C"): 1,
                ("output", "Q"): width, ("output", "~Q"): width}
    raise MappingError("INVALID_MAPPING_COMPONENT", f"Unsupported datapath native component {kind}.")


def _preflight(design, *, sequential=False, hierarchy_validated=False):
    if not hierarchy_validated:
        try:
            validate_design(design)
        except FrontendError as error:
            raise MappingError(error.code, f"Cannot map invalid design: {error.message}",
                               path=error.path, line=error.line, column=error.column) from error
    if not any(port["direction"] == "output" for port in design["ports"]):
        raise MappingError("UNSUPPORTED_MAPPING_INTERFACE", "The datapath profile requires at least one output port.")
    if len(design["ports"]) + len(design["cells"]) > MAX_INSTANCES:
        raise MappingError("MAPPING_RESOURCE_LIMIT", f"The datapath profile permits at most {MAX_INSTANCES} native components including bus helpers.")
    for cell in design["cells"]:
        if cell["type"] == "$dff":
            if sequential:
                continue
            raise MappingError("UNSUPPORTED_MAPPING_SEQUENTIAL", f"Cell {cell['id']} is a register; select the sequential-v1 mapping profile.")
        if cell["type"] not in _SUPPORTED:
            raise MappingError("UNSUPPORTED_MAPPING_CELL", f"Cell {cell['id']} ({cell['type']}) has no datapath-v1 mapping. Use bitwise logic, muxes, add/subtract, comparisons, reductions or shifts.")


class _Builder:
    def __init__(self, design, adapter, custom_types=None):
        self.adapter = adapter
        self.custom_types = custom_types or {}
        self.instances, self.descriptors, self.origins, self.pin_bindings = [], [], [], []
        self.nets, self.cache = {}, {}
        all_bits = [bit for vector in [*design["ports"], *design["nets"]] for bit in vector["bits"]]
        all_bits += [bit for cell in design["cells"] for vector in cell["ports"].values() for bit in vector]
        all_bits += [bit for child in design.get("children", []) for vector in child["ports"].values() for bit in vector]
        self.next_bit = max([1, *(bit for bit in all_bits if type(bit) is int)]) + 1
        self.helper_count = self.bus_count = 0

    def fresh(self, count=1):
        bits = list(range(self.next_bit, self.next_bit + count))
        self.next_bit += count
        if self.next_bit > 2147483647:
            raise MappingError("MAPPING_RESOURCE_LIMIT", "No normalized bit identities remain for mapping helpers.")
        return bits

    def helper(self):
        identifier = f"h{self.helper_count:05d}"
        self.helper_count += 1
        return identifier

    def raw(self, identifier, kind, attributes, bindings, origin):
        if len(self.instances) >= MAX_INSTANCES:
            raise MappingError("MAPPING_RESOURCE_LIMIT", f"Datapath mapping exceeds {MAX_INSTANCES} native components after explicit bus adaptation; split the HDL into smaller blocks.")
        widths = _widths(kind, attributes, self.custom_types.get(kind, {}).get("ports"))
        if set(widths) != set(bindings) or any(len(bindings[key]) != width for key, width in widths.items()):
            raise MappingError("INVALID_MAPPING_BINDING", f"Internal binding disagrees with native {kind} pin widths.")
        request = {"type": kind, "attributes": attributes, "position": {"x": 0, "y": 0}, "rotation": 0}
        key = json.dumps(request, sort_keys=True, ensure_ascii=False)
        if key not in self.cache:
            try:
                self.cache[key] = self.adapter.call("component.describe", request)
            except AdapterError as error:
                raise MappingError("NATIVE_COMPONENT_UNAVAILABLE", f"Cannot describe Digital {kind}: {error}") from error
        descriptor = deepcopy(self.cache[key])
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("pins"), list):
            raise MappingError("NATIVE_PIN_MISMATCH", f"Digital {kind} returned a malformed pin inventory.")
        pins = descriptor["pins"]
        actual = {(pin.get("direction"), pin.get("pin")) for pin in pins if isinstance(pin, dict)}
        if actual != set(widths) or len(actual) != len(pins):
            raise MappingError("NATIVE_PIN_MISMATCH", f"Digital {kind} pin names differ from the verified datapath contract.")
        for pin in pins:
            native_width = pin.get("width")
            if native_width is not None and (type(native_width) is not int or native_width != widths[pin["direction"], pin["pin"]]):
                raise MappingError("NATIVE_PIN_WIDTH", f"Digital {kind}.{pin['pin']} width differs from the independent datapath contract.")
        if descriptor.get("type") != kind or descriptor.get("position") != request["position"] or descriptor.get("rotation") != 0:
            raise MappingError("NATIVE_COMPONENT_MISMATCH", f"Digital {kind} did not preserve requested geometry.")
        resolved = descriptor.get("attributes", {})
        if not isinstance(resolved, dict) or any(resolved.get(name) != value for name, value in attributes.items()):
            raise MappingError("NATIVE_ATTRIBUTE_MISMATCH", f"Digital {kind} did not preserve requested attributes.")
        descriptor["id"] = identifier
        self.instances.append({"id": identifier, "type": kind, "attributes": deepcopy(resolved),
                               "position": {"x": 0, "y": 0}, "rotation": 0})
        self.descriptors.append(descriptor)
        self.origins.append({"instance_id": identifier, **origin})
        self.pin_bindings.extend({"instance_id": identifier, "direction": direction, "pin": pin,
                                  "width": len(bits), "bits": list(bits)} for (direction, pin), bits in bindings.items())

    def connect(self, identifier, direction, pin, bits, net_id=None):
        if net_id is None:
            if len(bits) != 1:
                raise MappingError("INVALID_MAPPING_BINDING", "Only scalar identities share canonical physical nets.")
            net_id = _net_id(bits[0])
        net = self.nets.setdefault(net_id, {"id": net_id, "width": len(bits), "bits": list(bits), "endpoints": []})
        if net["bits"] != list(bits):
            raise MappingError("INVALID_MAPPING_BINDING", "A physical net was assigned conflicting bit vectors.")
        net["endpoints"].append({"instance_id": identifier, "direction": direction, "pin": pin})

    def add(self, identifier, kind, attributes, bindings, origin, *, unused_outputs=()):
        self.raw(identifier, kind, attributes, bindings, origin)
        for (direction, pin), bits in bindings.items():
            if len(bits) == 1:
                self.connect(identifier, direction, pin, bits)
                continue
            if (direction, pin) in unused_outputs:
                bus = f"v{self.bus_count:05d}"
                self.bus_count += 1
                self.connect(identifier, direction, pin, bits, bus)
                continue
            helper = self.helper()
            wide_pin = _split_pin(0, len(bits))
            wide_direction = "output" if direction == "input" else "input"
            narrow_direction = direction
            split_attrs = {"Input Splitting": f"1*{len(bits)}" if direction == "input" else str(len(bits)),
                           "Output Splitting": str(len(bits)) if direction == "input" else f"1*{len(bits)}"}
            split_bindings = {(wide_direction, wide_pin): bits,
                              **{(narrow_direction, str(index)): [bit] for index, bit in enumerate(bits)}}
            self.raw(helper, "Splitter", split_attrs, split_bindings,
                     {"kind": "helper", "owner": identifier, "purpose": "merge" if direction == "input" else "split", "source": origin.get("source", "")})
            bus = f"v{self.bus_count:05d}"
            self.bus_count += 1
            self.connect(identifier, direction, pin, bits, bus)
            self.connect(helper, wide_direction, wide_pin, bits, bus)
            for index, bit in enumerate(bits):
                self.connect(helper, narrow_direction, str(index), [bit])

    def gate(self, kind, arguments, output, origin, identifier=None):
        self.add(identifier or self.helper(), kind, {"Bits": len(output), "Inputs": len(arguments)},
                 {**{("input", f"In_{index+1}"): bits for index, bits in enumerate(arguments)}, ("output", "out"): output}, origin)

    def reduce(self, kind, bits, output, origin, identifier=None):
        if len(bits) == 1:
            if kind == "XNOr":
                self.add(identifier or self.helper(), "Not", {"Bits": 1}, {("input", "in"): bits, ("output", "out"): output}, origin)
            else:
                self.gate("And", [bits, bits], output, origin, identifier)
        else:
            self.gate(kind, [[bit] for bit in bits], output, origin, identifier)


def map_datapath(design: dict, adapter) -> dict:
    """Map a validated 1–64-bit combinational design to native readable blocks."""
    return _map_components(design, adapter)


def _map_components(design: dict, adapter, *, clock=None, hierarchy_context=None) -> dict:
    _preflight(design, sequential=clock is not None or hierarchy_context is not None,
               hierarchy_validated=hierarchy_context is not None)
    custom_types = {} if hierarchy_context is None else hierarchy_context["custom_types"]
    builder = _Builder(design, adapter, custom_types)
    inputs, outputs, registers = [], [], []
    for direction, collection, kind, pin_direction, pin in (
            ("input", inputs, "In", "output", "out"), ("output", outputs, "Out", "input", "in")):
        for index, port in enumerate(sorted((item for item in design["ports"] if item["direction"] == direction), key=lambda item: item["name"])):
            identifier = f"{'in' if direction == 'input' else 'out'}{index:04d}"
            attributes = {"Bits": len(port["bits"]), "Label": port["name"]}
            if kind == "In":
                attributes.update(isHighZ=False, avoidActiveLow=True)
            builder.add(identifier, kind, attributes, {(pin_direction, pin): port["bits"]},
                        {"kind": "port", "port": port["name"], "source": port["source"]})
            collection.append({"port": port["name"], "instance_id": identifier, "width": len(port["bits"])})
    for index, cell in enumerate(sorted(design["cells"], key=lambda item: item["id"])):
        identifier, kind, pins, params = f"g{index:04d}", cell["type"], cell["ports"], cell["parameters"]
        origin = {"kind": "cell", "cell_id": cell["id"], "native_name": cell["native_name"], "hierarchy": cell["hierarchy"], "source": cell["source"]}
        helper_origin = {"kind": "helper", "owner": identifier, "purpose": "operand adaptation", "source": cell["source"]}
        if kind == "$dff":
            width = params["WIDTH"]
            builder.add(identifier, "D_FF", {"Bits": width, "Default": "0x0"},
                        {("input", "D"): pins["D"], ("input", "C"): pins["CLK"],
                         ("output", "Q"): pins["Q"], ("output", "~Q"): builder.fresh(width)}, origin,
                        unused_outputs={("output", "~Q")})
            registers.append({"instance_id": identifier, "cell_id": cell["id"], "width": width,
                              "d_bits": list(pins["D"]), "q_bits": list(pins["Q"])})
            continue
        output, a = pins["Y"], pins["A"]
        width = len(output)
        signed = bool(params.get("A_SIGNED") and params.get("B_SIGNED"))
        if kind in _GATES:
            builder.gate(_GATES[kind], [_resize(a, width, signed), _resize(pins["B"], width, signed)], output, origin, identifier)
        elif kind in {"$not", "$pos"}:
            argument = _resize(a, width, bool(params["A_SIGNED"]))
            if kind == "$not":
                builder.add(identifier, "Not", {"Bits": width}, {("input", "in"): argument, ("output", "out"): output}, origin)
            else:
                builder.gate("And", [argument, argument], output, origin, identifier)
        elif kind in {"$add", "$sub", "$neg"}:
            left = ["0"] * width if kind == "$neg" else _resize(a, width, signed)
            right = _resize(a, width, bool(params["A_SIGNED"])) if kind == "$neg" else _resize(pins["B"], width, signed)
            builder.add(identifier, "Add" if kind == "$add" else "Sub", {"Bits": width},
                        {("input", "a"): left, ("input", "b"): right, ("input", "c_i"): ["0"],
                         ("output", "s"): output, ("output", "c_o"): builder.fresh()}, origin)
        elif kind == "$mux":
            builder.add(identifier, "Multiplexer", {"Bits": width, "Selector Bits": 1},
                        {("input", "in_0"): a, ("input", "in_1"): pins["B"], ("input", "sel"): pins["S"], ("output", "out"): output}, origin)
        elif kind in _SHIFTS:
            data_width = max(len(a), width)
            shift_width = data_width.bit_length()
            amount = _resize(pins["B"], shift_width)
            if len(pins["B"]) > shift_width:
                overflow = builder.fresh()
                builder.reduce("Or", pins["B"][shift_width:], overflow, helper_origin)
                saturated = builder.fresh(shift_width)
                limit = [str((data_width >> bit) & 1) for bit in range(shift_width)]
                builder.add(builder.helper(), "Multiplexer", {"Bits": shift_width, "Selector Bits": 1},
                            {("input", "in_0"): amount, ("input", "in_1"): limit, ("input", "sel"): overflow, ("output", "out"): saturated}, helper_origin)
                amount = saturated
            builder.add(identifier, "BarrelShifter", {"Bits": data_width, "barrelSigned": False,
                        "direction": "right" if kind in {"$shr", "$sshr"} else "left",
                        "barrelShifterMode": "arithmetic" if kind == "$sshr" and params["A_SIGNED"] else "logical"},
                        {("input", "in"): _resize(a, data_width, bool(params["A_SIGNED"])), ("input", "shift"): amount,
                         ("output", "out"): list(output) + builder.fresh(data_width - width)}, origin)
        else:
            # Verilog boolean results are zero-extended, even with signed operands.
            boolean = output if width == 1 else builder.fresh()
            if kind in _COMPARE:
                compare_width = max(len(a), len(pins["B"]))
                selected = _COMPARE[kind]
                inverted = kind in {"$le", "$ge", "$ne", "$nex"}
                raw = builder.fresh() if inverted else boolean
                builder.add(identifier, "Comparator", {"Bits": compare_width, "Signed": signed},
                            {("input", "a"): _resize(a, compare_width, signed), ("input", "b"): _resize(pins["B"], compare_width, signed),
                             **{("output", pin): raw if pin == selected else builder.fresh() for pin in (">", "=", "<")}}, origin)
                if inverted:
                    builder.add(builder.helper(), "Not", {"Bits": 1}, {("input", "in"): raw, ("output", "out"): boolean}, helper_origin)
            elif kind in _REDUCTIONS:
                builder.reduce(_REDUCTIONS[kind], a, boolean, origin, identifier)
            elif kind == "$logic_not":
                reduced = builder.fresh()
                builder.reduce("Or", a, reduced, helper_origin)
                builder.add(identifier, "Not", {"Bits": 1}, {("input", "in"): reduced, ("output", "out"): boolean}, origin)
            else:
                left, right = builder.fresh(), builder.fresh()
                builder.reduce("Or", a, left, helper_origin)
                builder.reduce("Or", pins["B"], right, helper_origin)
                builder.gate("And" if kind == "$logic_and" else "Or", [left, right], boolean, origin, identifier)
            if width > 1:
                extended = list(boolean) + ["0"] * (width - 1)
                builder.gate("And", [extended, extended], output, helper_origin)
    if hierarchy_context is not None:
        for child in hierarchy_context["children"]:
            bindings = {(child["port_directions"][pin], pin): bits for pin, bits in child["ports"].items()}
            builder.add(child["instance_id"], child["type"], {"Label": child["native_name"]}, bindings,
                        {"kind": "child", "module_id": child["module_id"], "native_name": child["native_name"],
                         "hierarchy": child["hierarchy"], "source": child["source"]})
    constants = sorted(net["bits"][0] for net in builder.nets.values() if net["width"] == 1 and type(net["bits"][0]) is str)
    for bit in constants:
        builder.add(f"const{bit}", "Const", {"Bits": 1, "Value": f"0x{bit}"}, {("output", "out"): [bit]},
                    {"kind": "constant", "value": bit, "source": ""})
    nets = sorted(builder.nets.values(), key=lambda item: item["id"])
    for net in nets:
        net["endpoints"].sort(key=_endpoint_key)
        if net["width"] == 1:
            net["bit"] = net["bits"][0]
    result = {"schema_version": 1, "profile": "sequential-v1" if clock is not None else "datapath-v1", "top": design["top"],
              "instances": builder.instances, "descriptors": builder.descriptors, "nets": nets,
              "inputs": inputs, "outputs": outputs, "origins": builder.origins,
              "pin_bindings": sorted(builder.pin_bindings, key=_endpoint_key)}
    if hierarchy_context is not None:
        from .hierarchy import validate_hierarchy_mapping
        result.update(profile="hierarchy-v1", module_id=hierarchy_context["module_id"], registers=registers,
                      child_types=list(custom_types.values()), children=deepcopy(hierarchy_context["children"]))
        validate_hierarchy_mapping(result)
    elif clock is not None:
        from .sequential import validate_sequential
        clock_port = next(port for port in inputs if port["port"] == clock["port"])
        result.update(clock={**clock, "instance_id": clock_port["instance_id"]}, registers=registers)
        validate_sequential(result)
    else:
        validate_datapath(result)
    return result


def validate_datapath(mapped: dict) -> None:
    """Check widths, complete pin coverage, transfer identity, drivers and cycles."""
    _validate_mapping(mapped)


def _validate_mapping(mapped: dict, *, sequential=False, custom_types=None) -> None:
    hierarchical = custom_types is not None
    try:
        contracts.validate("mapped-hierarchy" if hierarchical else "mapped-sequential" if sequential else "mapped-datapath", mapped)
    except contracts.ContractError as error:
        raise MappingError("INVALID_MAPPING", str(error)) from error
    instances = {item["id"]: item for item in mapped["instances"]}
    descriptors = {item["id"]: item for item in mapped["descriptors"]}
    if len(instances) != len(mapped["instances"]) or len(descriptors) != len(mapped["descriptors"]) or instances.keys() != descriptors.keys():
        raise MappingError("INVALID_MAPPING_INSTANCES", "Instance and descriptor IDs must be unique and identical.")
    bindings = {_endpoint_key(item): item for item in mapped["pin_bindings"]}
    if len(bindings) != len(mapped["pin_bindings"]):
        raise MappingError("INVALID_MAPPING_BINDING", "Pin bindings must be unique.")
    actual_pins, canonical_drivers = set(), defaultdict(list)
    for identifier, instance in instances.items():
        descriptor = descriptors[identifier]
        if any(descriptor.get(key) != instance[key] for key in ("type", "attributes", "position", "rotation")):
            raise MappingError("INVALID_MAPPING_DESCRIPTOR", f"Descriptor disagrees with {identifier}.")
        widths = _widths(instance["type"], instance["attributes"], (custom_types or {}).get(instance["type"], {}).get("ports"))
        native = {(pin["direction"], pin["pin"]): pin for pin in descriptor["pins"]}
        if native.keys() != widths.keys() or len(native) != len(descriptor["pins"]):
            raise MappingError("INVALID_MAPPING_PINS", f"Native pin inventory differs for {identifier}.")
        for box in (descriptor["bounds"], descriptor["body_bounds"]):
            if any(box["min"][axis] > box["max"][axis] for axis in ("x", "y")):
                raise MappingError("INVALID_MAPPING_GEOMETRY", f"Reversed native bounds on {identifier}.")
        for (direction, pin), width in widths.items():
            key = identifier, direction, pin
            actual_pins.add(key)
            binding, native_pin = bindings.get(key), native[direction, pin]
            if binding is None or binding["width"] != width or len(binding["bits"]) != width or (native_pin["width"] is not None and native_pin["width"] != width):
                raise MappingError("INVALID_MAPPING_WIDTH", f"Native, intended or resolved width differs at {key}.")
            if abs(native_pin["escape"]["x"]) + abs(native_pin["escape"]["y"]) != 1:
                raise MappingError("INVALID_MAPPING_GEOMETRY", f"Pin {key} needs a cardinal escape direction.")
            if direction == "output" and instance["type"] != "Splitter":
                for bit in binding["bits"]:
                    canonical_drivers[bit].append(instance)
        if instance["type"] == "In" and instance["attributes"].get("isHighZ") is not False:
            raise MappingError("INVALID_MAPPING_ATTRIBUTE", f"Input {identifier} must be a binary driver.")
        if instance["type"] == "Splitter":
            expected = _split_ports(instance["attributes"])
            native_split = {(item["direction"], item["pin"]): (item["offset"], item["width"]) for item in descriptor.get("splitter_bindings", [])}
            if native_split != expected or len(native_split) != len(descriptor.get("splitter_bindings", [])):
                raise MappingError("INVALID_MAPPING_SPLITTER", f"Native splitter offsets disagree on {identifier}.")
            slots = {}
            for (direction, pin), (offset, width) in expected.items():
                for index, bit in enumerate(bindings[identifier, direction, pin]["bits"]):
                    slot = offset + index
                    if slot in slots and slots[slot] != bit:
                        raise MappingError("INVALID_MAPPING_SPLITTER", f"Splitter {identifier} changes the identity at bit {slot}.")
                    slots[slot] = bit
    if set(bindings) != actual_pins:
        raise MappingError("INVALID_MAPPING_BINDING", "Binding coverage must exactly match native pins.")
    covered, net_ids, scalar_ids, isolated_bits = set(), set(), set(), set()
    dependencies = {identifier: set() for identifier in instances}
    dependents = defaultdict(set)
    all_bits = set()
    for net in mapped["nets"]:
        if net["id"] in net_ids or net["width"] != len(net["bits"]) or ("bit" in net and (net["width"] != 1 or net["bit"] != net["bits"][0])):
            raise MappingError("INVALID_MAPPING_NET", "Expected nets need unique IDs and consistent LSB-first widths.")
        net_ids.add(net["id"])
        all_bits.update(net["bits"])
        if net["width"] == 1:
            bit = net["bits"][0]
            if net["id"] != _net_id(bit) or bit in scalar_ids:
                raise MappingError("INVALID_MAPPING_NET", "Each scalar identity must have one canonical physical net.")
            scalar_ids.add(bit)
        elif not net["id"].startswith("v") or len(net["endpoints"]) not in ({1, 2} if sequential else {2}):
            raise MappingError("INVALID_MAPPING_NET", "Wide pins require a dedicated bus adapter or an explicitly unused sequential output.")
        elif len(net["endpoints"]) == 1:
            endpoint = net["endpoints"][0]
            instance = instances.get(endpoint["instance_id"], {})
            if instance.get("type") != "D_FF" or endpoint["direction"] != "output" or endpoint["pin"] != "~Q" or len(set(net["bits"])) != net["width"] or any(type(bit) is not int or bit in isolated_bits for bit in net["bits"]):
                raise MappingError("INVALID_MAPPING_NET", "Only an isolated D_FF.~Q vector may omit its bus adapter.")
            isolated_bits.update(net["bits"])
        drivers = []
        for endpoint in net["endpoints"]:
            key = _endpoint_key(endpoint)
            if key not in actual_pins or key in covered:
                raise MappingError("INVALID_MAPPING_ENDPOINT", f"Unknown or repeated net endpoint {key}.")
            covered.add(key)
            if bindings[key]["bits"] != net["bits"]:
                raise MappingError("INVALID_MAPPING_BINDING", f"Pin {key} disagrees with its physical net bit order.")
            if endpoint["direction"] == "output":
                drivers.append(endpoint["instance_id"])
        if len(drivers) != 1:
            raise MappingError("INVALID_MAPPING_DRIVERS", f"Net {net['id']} needs exactly one physical driver.")
        for endpoint in net["endpoints"]:
            if endpoint["direction"] == "input":
                destination = endpoint["instance_id"]
                if not (sequential and instances[destination]["type"] == "D_FF") and instances[destination]["type"] not in (custom_types or {}):
                    dependencies[destination].add(drivers[0])
                    dependents[drivers[0]].add(destination)
    if covered != actual_pins:
        raise MappingError("INCOMPLETE_MAPPING", "Every native pin must occur in exactly one expected physical net.")
    if scalar_ids & isolated_bits or scalar_ids | isolated_bits != all_bits:
        raise MappingError("INVALID_MAPPING_BINDING", "Every bus bit must reach its canonical scalar net through an explicit splitter.")
    if isolated_bits:
        occurrences = defaultdict(int)
        for binding in bindings.values():
            for bit in binding["bits"]:
                if bit in isolated_bits:
                    occurrences[bit] += 1
        if any(occurrences[bit] != 1 for bit in isolated_bits):
            raise MappingError("INVALID_MAPPING_BINDING", "Unused register complement bits must occur only on their isolated native output pin.")
    for bit in all_bits:
        drivers = canonical_drivers[bit]
        if len(drivers) != 1:
            raise MappingError("INVALID_MAPPING_DRIVERS", f"Bit {bit} requires one non-splitter source; found {len(drivers)}.")
        driver = drivers[0]
        if type(bit) is str:
            if driver["type"] != "Const" or driver["attributes"].get("Bits") != 1 or driver["attributes"].get("Value") != f"0x{bit}":
                raise MappingError("INVALID_MAPPING_CONSTANT", f"Bit {bit} requires a matching scalar constant.")
        elif driver["type"] == "Const":
            raise MappingError("INVALID_MAPPING_CONSTANT", f"Constant drives nonconstant identity {bit}.")
    ready = deque(identifier for identifier, sources in dependencies.items() if not sources)
    visited = 0
    while ready:
        identifier = ready.popleft()
        visited += 1
        for destination in dependents[identifier]:
            dependencies[destination].remove(identifier)
            if not dependencies[destination]:
                ready.append(destination)
    if visited != len(instances):
        raise MappingError("INVALID_MAPPING_CYCLE", "Mapped components contain combinational feedback.")
    names = set()
    for collection, kind in (("inputs", "In"), ("outputs", "Out")):
        listed = set()
        for port in mapped[collection]:
            identifier = port["instance_id"]
            instance = instances.get(identifier)
            if port["port"] in names or identifier in listed or instance is None or instance["type"] != kind or instance["attributes"].get("Label") != port["port"] or instance["attributes"].get("Bits") != port["width"]:
                raise MappingError("INVALID_MAPPING_PORT", "Port names, native instances and widths must match uniquely.")
            names.add(port["port"])
            listed.add(identifier)
        if listed != {identifier for identifier, item in instances.items() if item["type"] == kind}:
            raise MappingError("INVALID_MAPPING_PORT", f"Every {kind} must appear in the port interface.")
    origin_ids = [item["instance_id"] for item in mapped["origins"]]
    if len(origin_ids) != len(set(origin_ids)) or set(origin_ids) != set(instances):
        raise MappingError("INVALID_MAPPING_ORIGIN", "Every native instance requires one origin record.")
