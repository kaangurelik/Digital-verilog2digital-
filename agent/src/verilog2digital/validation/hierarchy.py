"""Compose native subcircuits through declared parent/child ports.

Namespace bits by occurrence before unifying port connections. This preserves
aliases, permutations, repeated bits and independent child state; geometry
stays local to each canvas.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path, PurePosixPath
import re

from ..frontend.errors import FrontendError
from .bits import compare_bit_topology
from .sequential import sequential_interface
from .topology import compare_components, compare_topology, endpoint_key


MAX_DEPTH = 12
MAX_OCCURRENCES = 1024
MAX_EXPANDED_INSTANCES = 1024
MAX_MODULES = 65  # The root plus at most 64 registered child specializations.
MAX_PACKAGE_FILES = 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_ATOM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")


class HierarchyError(FrontendError):
    pass


def scoped_id(path: list[str] | tuple[str, ...], identifier: str) -> str:
    parts = [*path, identifier]
    if any(not isinstance(part, str) or not _ATOM.fullmatch(part) for part in parts):
        raise HierarchyError("HIERARCHY_ID", "Occurrence IDs require generated atoms without dots or HDL punctuation")
    result = ".".join(parts)
    if len(path) > MAX_DEPTH or len(result) > 128:
        raise HierarchyError("HIERARCHY_LIMIT", "The occurrence path exceeds the native depth or 128-character identity limit")
    return result


class _Union:
    def __init__(self):
        self.parents = {}

    def add(self, value):
        self.parents.setdefault(value, value)
        return value

    def find(self, value):
        self.add(value)
        root = value
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[value] != value:
            previous = self.parents[value]
            self.parents[value] = root
            value = previous
        return root

    def join(self, first, second):
        a, b = self.find(first), self.find(second)
        if a != b:
            # Stable representatives make output independent of record order.
            first, second = sorted((a, b), key=repr)
            self.parents[second] = first


def _bits(net):
    bits = net.get("bits", [net.get("bit")])
    if (type(net.get("width")) is not int or len(bits) != net["width"] or not 1 <= len(bits) <= 64
            or any(not (type(bit) is int and bit >= 2 or type(bit) is str and bit in {"0", "1"}) for bit in bits)):
        raise HierarchyError("HIERARCHY_BITS", "Each local physical net needs an explicit 1-64-bit identity vector")
    return bits


def compose_hierarchy(hierarchy_ir: dict, module_mappings: dict[str, dict]) -> dict:
    """Expand mapped specializations into electrical and behavioral views.

    Bind cell IDs through frontend occurrences. Declared port transfers join
    nets across custom symbols and nested In/Out boundaries.
    """
    try:
        return _compose(hierarchy_ir, module_mappings)
    except HierarchyError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise HierarchyError("HIERARCHY_CONTRACT", f"Invalid hierarchy mapping contract: {error}") from error


def _compose(hierarchy_ir, module_mappings):
    modules = {module["id"]: module for module in hierarchy_ir["modules"]}
    if len(modules) != len(hierarchy_ir["modules"]) or hierarchy_ir.get("schema_version") != 1:
        raise HierarchyError("HIERARCHY_CONTRACT", "Hierarchy modules must have unique IDs and schema_version 1")
    if len(modules) > MAX_MODULES:
        raise HierarchyError("HIERARCHY_LIMIT", "A native package supports the root plus at most 64 child specializations")
    top = hierarchy_ir["top_module_id"]
    occurrences = {tuple(item["path"]): item for item in hierarchy_ir["occurrences"]}
    if len(occurrences) != len(hierarchy_ir["occurrences"]) or not 1 <= len(occurrences) <= MAX_OCCURRENCES:
        raise HierarchyError("HIERARCHY_LIMIT", "Hierarchy occurrence paths must be unique and bounded")
    expected_paths, dependencies = {}, set()

    def visit(module_id, path, ancestors, hdl_path):
        if module_id not in modules or module_id not in module_mappings:
            raise HierarchyError("HIERARCHY_MISSING_MODULE", f"No mapped specialization exists for {module_id}")
        if module_id in ancestors:
            raise HierarchyError("HIERARCHY_RECURSION", "Recursive native module dependencies are unsupported")
        if len(path) > MAX_DEPTH or len(expected_paths) >= MAX_OCCURRENCES:
            raise HierarchyError("HIERARCHY_LIMIT", "Hierarchy expansion exceeds its depth or occurrence limit")
        module = modules[module_id]
        children = module["children"]
        child_ids = [child["id"] for child in children]
        if len(child_ids) != len(set(child_ids)):
            raise HierarchyError("HIERARCHY_ID", "Child instance IDs must be unique in each specialization")
        expected_paths[path] = {"module_id": module_id, "hdl_path": hdl_path}
        for child in sorted(children, key=lambda child: child["id"]):
            scoped_id(path, child["id"])
            dependencies.add((module_id, child["module_id"]))
            visit(child["module_id"], (*path, child["id"]), (*ancestors, module_id), (*hdl_path, child["native_name"]))

    visit(top, (), (), ())
    if sum(len(module_mappings[item["module_id"]]["instances"]) for item in expected_paths.values()) > MAX_EXPANDED_INSTANCES:
        raise HierarchyError("HIERARCHY_LIMIT", "Native occurrence expansion exceeds 1024 elements including module boundaries")
    if set(expected_paths) != set(occurrences) or any(occurrences[path]["module_id"] != value["module_id"] for path, value in expected_paths.items()):
        raise HierarchyError("HIERARCHY_OCCURRENCE", "Frontend occurrence provenance differs from the declared module tree")
    physical, bit_union = _Union(), _Union()
    net_records, endpoint_nets, records, instance_lookup = {}, {}, {}, {}

    def bit_key(path, bit):
        if type(bit) is str and bit in {"0", "1"}:
            return bit_union.add(("constant", bit))
        if type(bit) is not int or bit < 2:
            raise HierarchyError("HIERARCHY_BITS", f"Invalid module-relative bit identity {bit!r}")
        return bit_union.add(("bit", path, bit))

    for path in sorted(expected_paths):
        module_id = expected_paths[path]["module_id"]
        mapped = module_mappings[module_id]
        if mapped.get("module_id", module_id) != module_id:
            raise HierarchyError("HIERARCHY_CONTRACT", "A local mapping names a different specialization")
        instances = {item["id"]: item for item in mapped["instances"]}
        descriptions = {item["id"]: item for item in mapped["descriptors"]}
        if len(instances) != len(mapped["instances"]) or len(descriptions) != len(mapped["descriptors"]) or set(instances) != set(descriptions):
            raise HierarchyError("HIERARCHY_INSTANCE", "Local instances and native descriptors must match uniquely")
        records[path] = mapped
        instance_lookup[path] = instances, descriptions
        widths = {(identifier, pin["direction"], pin["pin"]): pin["width"] for identifier, descriptor in descriptions.items() for pin in descriptor["pins"]}
        expected_pins = set(widths)
        if len(widths) != sum(len(descriptor["pins"]) for descriptor in descriptions.values()):
            raise HierarchyError("HIERARCHY_ENDPOINT", "Local native pin identities must be unique")
        declared_bindings = {endpoint_key(binding): binding for binding in mapped.get("pin_bindings", [])}
        if len(declared_bindings) != len(mapped.get("pin_bindings", [])):
            raise HierarchyError("HIERARCHY_BITS", "Independent pin bindings must have unique endpoint identities")
        local_bindings = {}
        for net in mapped["nets"]:
            key = path, net["id"]
            if key in net_records or not net["endpoints"]:
                raise HierarchyError("HIERARCHY_NET", "Local physical nets must be unique and nonempty")
            physical.add(key)
            bits = _bits(net)
            for bit in bits:
                bit_key(path, bit)
            net_records[key] = net
            for endpoint in net["endpoints"]:
                local = endpoint_key(endpoint)
                if (local not in expected_pins or local in local_bindings
                        or widths[local] is not None and widths[local] != len(bits)
                        or widths[local] is None and (type(declared_bindings.get(local, {}).get("width")) is not int
                                                     or declared_bindings[local]["width"] != len(bits))):
                    raise HierarchyError("HIERARCHY_ENDPOINT", "Local nets must own every native pin exactly once")
                local_bindings[local] = list(bits)
                endpoint_nets[path, local] = key
        if set(local_bindings) != expected_pins:
            raise HierarchyError("HIERARCHY_ENDPOINT", "Local expected nets omit native pins")
        if "pin_bindings" in mapped:
            bindings = {endpoint_key(binding): binding["bits"] for binding in mapped["pin_bindings"]}
            if bindings != local_bindings or any(type(binding.get("width")) is not int or binding["width"] != len(binding["bits"])
                                                 for binding in mapped["pin_bindings"]):
                raise HierarchyError("HIERARCHY_BITS", "Local net identities differ from independent pin bindings")

    def io_endpoint(path, port):
        mapped = records[path]
        collection = mapped["inputs" if port["direction"] == "input" else "outputs"]
        matches = [item for item in collection if item["port"] == port["name"]]
        if len(matches) != 1 or matches[0]["width"] != len(port["bits"]):
            raise HierarchyError("HIERARCHY_PORT", f"Child interface omits or changes port {port['name']}")
        identifier = matches[0]["instance_id"]
        instance = instance_lookup[path][0][identifier]
        if instance["type"] != ("In" if port["direction"] == "input" else "Out") or instance["attributes"].get("Label") != port["name"]:
            raise HierarchyError("HIERARCHY_PORT", "Each HDL port requires its exact labeled native In/Out boundary")
        pins = instance_lookup[path][1][identifier]["pins"]
        if len(pins) != 1:
            raise HierarchyError("HIERARCHY_PORT", "A native In/Out boundary must have exactly one pin")
        pin = pins[0]
        if pin["direction"] != ("output" if port["direction"] == "input" else "input"):
            raise HierarchyError("HIERARCHY_PORT", "A native boundary pin must face into its declared module interface")
        return identifier, pin["direction"], pin["pin"]

    for path in sorted(expected_paths):
        module = modules[expected_paths[path]["module_id"]]
        for kind, direction in (("inputs", "input"), ("outputs", "output")):
            names = [item["port"] for item in records[path][kind]]
            if len(names) != len(set(names)) or set(names) != {port["name"] for port in module["ports"] if port["direction"] == direction}:
                raise HierarchyError("HIERARCHY_PORT", "Mapped interfaces must cover each declared HDL port exactly once")
        for port in module["ports"]:
            key = endpoint_nets[path, io_endpoint(path, port)]
            if _bits(net_records[key]) != port["bits"]:
                raise HierarchyError("HIERARCHY_BITS", "A mapped module interface changes its declared HDL bit order")
        for child in module["children"]:
            child_path = (*path, child["id"])
            ports = modules[child["module_id"]]["ports"]
            if set(child["ports"]) != {port["name"] for port in ports}:
                raise HierarchyError("HIERARCHY_PORT", "Every child instance must bind each specialized HDL port exactly once")
            for port in ports:
                parent_bits = child["ports"][port["name"]]
                if len(parent_bits) != len(port["bits"]) or child["port_directions"][port["name"]] != port["direction"]:
                    raise HierarchyError("HIERARCHY_PORT", "Parent and specialized child port widths or directions differ")
                parent_pin = child["id"], port["direction"], port["name"]
                parent_net = endpoint_nets[path, parent_pin]
                nested_net = endpoint_nets[child_path, io_endpoint(child_path, port)]
                if _bits(net_records[parent_net]) != parent_bits or _bits(net_records[nested_net]) != port["bits"]:
                    raise HierarchyError("HIERARCHY_BITS", "Mapped boundary pin identities differ from the elaborated port binding")
                physical.join(parent_net, nested_net)
                for parent_bit, nested_bit in zip(parent_bits, port["bits"]):
                    bit_union.join(bit_key(path, parent_bit), bit_key(child_path, nested_bit))
    zero, one = bit_union.find(("constant", "0")), bit_union.find(("constant", "1"))
    if zero == one:
        raise HierarchyError("HIERARCHY_CONSTANT_SHORT", "Module port aliases would join constant zero to constant one")
    numeric_roots = sorted({bit_union.find(key) for key in bit_union.parents} - {zero, one}, key=repr)
    global_bits = {root: index + 2 for index, root in enumerate(numeric_roots)}
    global_bits.update({zero: "0", one: "1"})

    def translated_bits(path, bits):
        return [global_bits[bit_union.find(bit_key(path, bit))] for bit in bits]

    instances, descriptors, origins, registers = [], [], [], []
    kept = set()
    all_cell_bindings = {}
    for path in sorted(expected_paths):
        module_id = expected_paths[path]["module_id"]
        module, mapped = modules[module_id], records[path]
        children = {child["id"] for child in module["children"]}
        if not children <= set(instance_lookup[path][0]):
            raise HierarchyError("HIERARCHY_INSTANCE", "Mapped custom symbols omit an elaborated child instance")
        boundary = {port["instance_id"] for kind in ("inputs", "outputs") for port in mapped[kind]} if path else set()
        cell_map = occurrences[path]["cell_map"]
        if set(cell_map) != {cell["id"] for cell in module["cells"]}:
            raise HierarchyError("HIERARCHY_PROVENANCE", "Each occurrence must identify every local cell in the expanded reference")
        for local, expanded in cell_map.items():
            if expanded in all_cell_bindings:
                raise HierarchyError("HIERARCHY_PROVENANCE", "Different cell occurrences share one expanded reference cell")
            all_cell_bindings[expanded] = path, local
        local_origins = {item["instance_id"]: item for item in mapped.get("origins", [])}
        for identifier, instance in sorted(instance_lookup[path][0].items()):
            if identifier in children | boundary:
                continue
            scoped = scoped_id(path, identifier)
            kept.add((path, identifier))
            instances.append({**deepcopy(instance), "id": scoped})
            descriptors.append({**deepcopy(instance_lookup[path][1][identifier]), "id": scoped})
            origins.append({**deepcopy(local_origins.get(identifier, {})), "instance_id": scoped,
                            "instance_path": list(path), "hdl_path": list(expected_paths[path]["hdl_path"]), "module_id": module_id})
        for register in mapped.get("registers", []):
            local_cell = next((cell for cell in module["cells"] if cell["id"] == register["cell_id"]), None)
            if (local_cell is None or local_cell["type"] != "$dff" or register["width"] != local_cell["parameters"]["WIDTH"]
                    or register["d_bits"] != local_cell["ports"]["D"] or register["q_bits"] != local_cell["ports"]["Q"]):
                raise HierarchyError("HIERARCHY_STATE_BINDING", "Native register metadata changes its local source cell or bit lanes")
            if (path, register["instance_id"]) not in kept:
                raise HierarchyError("HIERARCHY_STATE_BINDING", "Register provenance points outside primitive occurrence state")
            registers.append({**deepcopy(register), "instance_id": scoped_id(path, register["instance_id"]),
                              "cell_id": cell_map[register["cell_id"]],
                              "d_bits": translated_bits(path, register["d_bits"]), "q_bits": translated_bits(path, register["q_bits"])})
    if len(instances) > MAX_EXPANDED_INSTANCES:
        raise HierarchyError("HIERARCHY_LIMIT", "Expanded native primitives exceed the 1024-component oracle limit")
    expanded_design = hierarchy_ir["expanded_design"]
    if set(all_cell_bindings) != {cell["id"] for cell in expanded_design["cells"]}:
        raise HierarchyError("HIERARCHY_PROVENANCE", "Hierarchy occurrences do not cover every expanded reference cell")
    expected_registers = {cell["id"]: cell["parameters"]["WIDTH"] for cell in expanded_design["cells"] if cell["type"] == "$dff"}
    observed_registers = {item["cell_id"]: item["width"] for item in registers}
    native_register_ids = {item["id"] for item in instances if item["type"] == "D_FF"}
    if (len(observed_registers) != len(registers) or observed_registers != expected_registers
            or {item["instance_id"] for item in registers} != native_register_ids):
        raise HierarchyError("HIERARCHY_STATE_BINDING", "Every physical native register must match one expanded reference register and width")
    grouped = defaultdict(list)
    for key in net_records:
        grouped[physical.find(key)].append(key)
    nets = []
    for members in grouped.values():
        endpoints, vectors = [], []
        for path, identifier in members:
            net = net_records[path, identifier]
            vectors.append(translated_bits(path, _bits(net)))
            for endpoint in net["endpoints"]:
                if (path, endpoint["instance_id"]) in kept:
                    endpoints.append({**endpoint, "instance_id": scoped_id(path, endpoint["instance_id"])})
        if any(vector != vectors[0] for vector in vectors[1:]):
            raise HierarchyError("HIERARCHY_BITS", "Joined physical module nets disagree about bit order")
        if endpoints:
            nets.append({"width": len(vectors[0]), "bits": vectors[0], "endpoints": sorted(endpoints, key=endpoint_key)})
    nets.sort(key=lambda net: [endpoint_key(endpoint) for endpoint in net["endpoints"]])
    for index, net in enumerate(nets):
        net["id"] = f"net{index:06d}"
    pin_bindings = [{**endpoint, "width": net["width"], "bits": list(net["bits"])} for net in nets for endpoint in net["endpoints"]]
    root = records[()]
    expanded = {"schema_version": 1, "profile": "hierarchy-expanded-v1", "top": expanded_design["top"],
                "instances": instances, "descriptors": descriptors, "nets": nets,
                "inputs": deepcopy(root["inputs"]), "outputs": deepcopy(root["outputs"]),
                "origins": origins, "pin_bindings": pin_bindings, "registers": registers}
    behavior = {key: deepcopy(expanded[key]) for key in ("schema_version", "top", "inputs", "outputs", "instances", "registers")}
    behavior["profile"] = "sequential-v1" if registers else "datapath-v1"
    if registers:
        interface = sequential_interface(expanded_design)
        clock_port = interface["clock"]["port"]
        input_port = next(port for port in root["inputs"] if port["port"] == clock_port)
        root_port = next(port for port in modules[top]["ports"] if port["name"] == clock_port)
        behavior["clock"] = {**interface["clock"], "instance_id": input_port["instance_id"],
                             "bit": translated_bits((), root_port["bits"])[0]}
        expanded["clock"] = deepcopy(behavior["clock"])
    return {"schema_version": 1, "expanded_mapping": expanded, "behavior_mapping": behavior,
            "occurrences": [{"path": list(path), "module_id": value["module_id"], "hdl_path": list(value["hdl_path"])}
                            for path, value in sorted(expected_paths.items())],
            "dependencies": [{"parent_module_id": parent, "child_module_id": child} for parent, child in sorted(dependencies)]}


def verify_hierarchy(expected: dict, native_inspection: dict) -> dict:
    """Validate expanded native electrical state; callers check per-file geometry."""
    mapped = expected["expanded_mapping"]
    inspected = native_inspection.get("expanded", native_inspection)
    checks = {"topology": compare_topology([net["endpoints"] for net in mapped["nets"]], inspected),
              "bits": compare_bit_topology(mapped, inspected),
              "components": compare_components(mapped["descriptors"], inspected.get("instances", [])),
              "model": inspected.get("model", {"valid": False, "diagnostics": [{"code": "missing_model", "message": "No native model result"}]})}
    return {"schema_version": 1, "valid": all(check["valid"] for check in checks.values()), **checks}


def _package_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise HierarchyError("PACKAGE_PATH", "Package paths must be nonempty relative POSIX paths")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts) or ":" in name or relative.as_posix() != name:
        raise HierarchyError("PACKAGE_PATH", "Package paths must not contain absolute paths or traversal")
    path = (root / Path(*relative.parts)).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise HierarchyError("PACKAGE_PATH", "Every package artifact must resolve to a regular file inside its package")
    return path


def snapshot_package(package_root: Path, relative_paths: list[str]) -> dict:
    """Bind all circuit/sidecar dependencies with relocatable, streamed hashes."""
    root = Path(package_root).resolve(strict=True)
    if (not isinstance(relative_paths, list) or not 1 <= len(relative_paths) <= MAX_PACKAGE_FILES
            or len({name.casefold() for name in relative_paths if isinstance(name, str)}) != len(relative_paths)):
        raise HierarchyError("PACKAGE_PATH", "Package artifact paths must be unique, bounded, and case-insensitively distinct")
    files, total = [], 0
    for name in sorted(relative_paths):
        try:
            path = _package_path(root, name)
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
                raise HierarchyError("PACKAGE_LIMIT", "Package artifacts exceed the 64 MiB bound")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({"path": name, "sha256": digest, "bytes": size})
        except OSError as error:
            raise HierarchyError("PACKAGE_FILE", f"Cannot read package artifact {name}: {error}") from error
    return {"schema_version": 1, "files": files, "total_bytes": total}


def verify_package_snapshot(package_root: Path, expected: dict) -> dict:
    try:
        if expected.get("schema_version") != 1:
            raise HierarchyError("PACKAGE_BINDING", "Unsupported package binding schema")
        actual = snapshot_package(package_root, [item["path"] for item in expected["files"]])
        if actual != expected:
            return {"valid": False, "diagnostics": [{"code": "package_changed", "message": "A bound package circuit or sidecar changed"}]}
        return {"valid": True, "diagnostics": []}
    except (HierarchyError, OSError, KeyError, TypeError, ValueError) as error:
        return {"valid": False, "diagnostics": [{"code": "package_changed", "message": str(error)}]}
