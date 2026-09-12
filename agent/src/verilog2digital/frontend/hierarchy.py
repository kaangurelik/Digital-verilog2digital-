"""Preserve specialized modules and build an unoptimized reference design.

Generated cell IDs recover occurrence paths after flattening. Each retained
cell stays identifiable, including state with unused outputs.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .. import contracts
from ..ir import _validate_cell, validate_design
from ..runtime import development_toolchain, repository_root
from .errors import FrontendError
from .inspection import inspect_sources
from .project import Project
from .yosys import _bits, _command, _normalize_unchecked, _parameter, normalize, source_hashes

MAX_MODULES = 64
MAX_DEPTH = 12
MAX_OCCURRENCES = 256
MAX_EXPANDED_CELLS = 4096


def _run(directory: Path, name: str, commands: list[str], output: str) -> dict:
    script = directory / f"{name}.tcl"
    script.write_text("\n".join(commands) + "\n", encoding="utf-8")
    result_path = directory / output
    result_path.unlink(missing_ok=True)
    toolchain = development_toolchain(repository_root())
    executable = toolchain.executable("yosys")
    environment = toolchain.environment(executable)
    library = toolchain.TOOLS / toolchain.LOCK["hdl"]["directory"] / "lib/tcl8.6"
    if library.is_dir():
        environment["TCL_LIBRARY"] = library.as_posix()
    log_path = directory / f"{name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run([executable, "-Q", "-T", "-c", script.name], cwd=directory,
                                    env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise FrontendError("FRONTEND_TIMEOUT", f"Hierarchy lowering exceeded 120 seconds; details: {log_path}") from error
    if result.returncode or not result_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        messages = [line for line in lines if "ERROR:" in line or "Warning:" in line]
        failures = "\n".join(messages).lower()
        code = "YOSYS_ERROR"
        for fragment, diagnostic in ((" is a latch of type ", "UNSUPPORTED_LATCH"),
                                     ("latch inferred for signal", "UNSUPPORTED_LATCH"),
                                     ("multiple conflicting drivers", "MULTIPLE_DRIVERS"),
                                     ("logic loop", "COMBINATIONAL_CYCLE"),
                                     ("used but has no driver", "UNDRIVEN_NET")):
            if fragment in failures:
                code = diagnostic
                break
        raise FrontendError(code, "\n".join(messages[-10:]) or f"Hierarchy lowering failed; details: {log_path}")
    if result_path.stat().st_size > 64 * 1024 * 1024:
        raise FrontendError("DESIGN_LIMIT", "The hierarchy JSON exceeds 64 MiB")
    return json.loads(result_path.read_text(encoding="utf-8"))


def _identity(name: str, module: dict) -> tuple[str, str, dict]:
    parameters = deepcopy(module.get("parameter_default_values", {}))
    hdl_name = module.get("attributes", {}).get("hdlname", name)
    # Derived names and raw sized values preserve Verilog expression sizing.
    payload = {"derived_name": name, "hdl_name": hdl_name, "parameters_raw": parameters}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return f"m_{digest}", hdl_name, parameters


def _modules(raw: dict, top: str, inspection: dict, project: Project) -> tuple[list[dict], dict, str]:
    raw_modules = raw.get("modules", {})
    if top not in raw_modules:
        raise FrontendError("MISSING_TOP", "The hierarchy backend omitted the selected top module")
    if len(raw_modules) > MAX_MODULES:
        raise FrontendError("HIERARCHY_LIMIT", f"At most {MAX_MODULES} parameter specializations are supported")
    identities = {name: _identity(name, module) for name, module in raw_modules.items()}
    if len({identity[0] for identity in identities.values()}) != len(identities):
        raise FrontendError("HIERARCHY_IDENTITY", "Parameter specialization identifiers collide")
    prepared = deepcopy(raw)
    modules = []
    for name, original in sorted(raw_modules.items()):
        primitive = deepcopy(original)
        primitive["cells"] = {key: value for key, value in original.get("cells", {}).items() if value["type"] not in raw_modules}
        local = _normalize_unchecked({"modules": {name: primitive}, "creator": raw.get("creator", "unknown")}, name, inspection, project)
        try:
            contracts.validate("design", local)
        except contracts.ContractError as error:
            raise FrontendError("INVALID_HIERARCHY", str(error)) from error
        for cell in local["cells"]:
            _validate_cell(cell)
        identifier, hdl_name, parameters = identities[name]
        children, renamed = [], {}
        all_bits = [bit for item in original.get("netnames", {}).values() for bit in item["bits"]]
        all_bits += [bit for cell in original.get("cells", {}).values() for bits in cell.get("connections", {}).values() for bit in bits]
        next_bit = max([1, *(bit for bit in all_bits if type(bit) is int)]) + 1
        for cell in local["cells"]:
            renamed[cell["id"]] = deepcopy(original["cells"][cell["native_name"]])
        for index, (native_name, cell) in enumerate(sorted((key, value) for key, value in original.get("cells", {}).items() if value["type"] in raw_modules)):
            child_id = f"u{index:04d}"
            target = raw_modules[cell["type"]]
            target_ports = target.get("ports", {})
            directions = {port: item["direction"] for port, item in target_ports.items()}
            connections = {}
            for port, definition in target_ports.items():
                bits = cell.get("connections", {}).get(port, [])
                if not bits and definition["direction"] == "output":
                    # An explicitly open child output still exists physically.
                    # Give it fresh identities without adding an external load.
                    bits = list(range(next_bit, next_bit + len(definition["bits"])))
                    next_bit += len(bits)
                connections[port] = _bits(bits, f"{native_name}.{port}")
            if set(connections) != set(target_ports) or cell.get("port_directions") != directions or any(len(connections[port]) != len(target_ports[port]["bits"]) for port in connections):
                raise FrontendError("HIERARCHY_INTERFACE", f"Child {native_name} does not exactly bind its specialized interface")
            attributes = cell.get("attributes", {})
            children.append({"id": child_id, "module_id": identities[cell["type"]][0],
                             "native_name": native_name, "ports": connections, "port_directions": directions,
                             "source": attributes.get("src", ""), "hierarchy": attributes.get("hdlname", native_name)})
            renamed[child_id] = deepcopy(cell)
            renamed[child_id]["connections"] = deepcopy(connections)
        prepared["modules"][name]["cells"] = renamed
        modules.append({"id": identifier, "hdl_name": hdl_name, "derived_name": name,
                        "parameters_raw": parameters, "ports": local["ports"], "nets": local["nets"],
                        "cells": local["cells"], "children": children})
    return modules, prepared, identities[top][0]


def _occurrences(modules: list[dict], top_module_id: str) -> list[dict]:
    by_id = {module["id"]: module for module in modules}
    occurrences = []
    count = 0

    def visit(identifier, path, ancestry):
        nonlocal count
        if identifier in ancestry:
            raise FrontendError("RECURSIVE_HIERARCHY", "Recursive module instantiation is unsupported")
        if len(path) > MAX_DEPTH or len(occurrences) >= MAX_OCCURRENCES:
            raise FrontendError("HIERARCHY_LIMIT", f"Hierarchy is limited to depth {MAX_DEPTH} and {MAX_OCCURRENCES} occurrences")
        module = by_id[identifier]
        count += len(module["cells"])
        if count > MAX_EXPANDED_CELLS:
            raise FrontendError("HIERARCHY_LIMIT", f"Expansion exceeds {MAX_EXPANDED_CELLS} physical primitive cells")
        occurrences.append({"path": list(path), "module_id": identifier, "cell_map": {}})
        for child in module["children"]:
            visit(child["module_id"], [*path, child["id"]], {*ancestry, identifier})

    visit(top_module_id, [], set())
    return occurrences


def lower_hierarchy(project: Project, workdir: Path) -> dict:
    """Return local specialization IR plus a separately validated expanded design."""
    directory = Path(workdir).resolve()
    if os.name == "nt" and not str(directory).isascii():
        raise FrontendError("UNSUPPORTED_PATH", "The current Windows HDL tools require ASCII paths. Choose an ASCII work directory.", path=str(directory))
    project = project.resolved()
    original_hashes = source_hashes(project.sources)
    parameters = {name: _parameter(value) for name, value in project.parameters.items()}
    inspection = inspect_sources(project.sources, top=project.top, include_dirs=project.include_dirs,
                                 defines=project.effective_defines(), parameters=parameters)
    hashes = source_hashes(inspection["source_files"])
    if any(hashes.get(path) != value for path, value in original_hashes.items()):
        raise FrontendError("SOURCE_CHANGED", "A source changed during hierarchy inspection; rerun conversion")
    inspection["input_hashes"] = hashes
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source-inspection.json").write_text(json.dumps(inspection, indent=2, sort_keys=True), encoding="utf-8")
    top = inspection["top"]
    arguments = ["read_verilog", "-defer", "-nosynthesis", "-nodpi"]
    for name, value in sorted(project.effective_defines().items()):
        arguments.extend(["-D", f"{name}={value}"])
    for path in project.include_dirs:
        arguments.extend(["-I", path.as_posix()])
    arguments.extend(path.as_posix() for path in project.sources)
    hierarchy = ["hierarchy", "-simcheck", "-top", top]
    for name, value in sorted(parameters.items()):
        hierarchy.extend(["-chparam", name, value])
    raw = _run(directory, "hierarchy", [_command(*arguments), _command(*hierarchy), _command("proc"),
               _command("check", "-assert", "-noinit", "-nolatches"),
               _command("opt", "-nodffe", "-nosdff", "-noff"), _command("pmuxtree"),
               _command("check", "-assert", "-noinit", "-nolatches"), _command("write_json", "specialized.json")], "specialized.json")
    modules, prepared, top_module_id = _modules(raw, top, inspection, project)
    occurrences = _occurrences(modules, top_module_id)  # Bound expansion before native flatten.
    (directory / "prepared.json").write_text(json.dumps(prepared, sort_keys=True), encoding="utf-8")
    expanded = _run(directory, "expand", [_command("read_json", "prepared.json"), _command("hierarchy", "-simcheck", "-top", top),
                    _command("flatten", "-noscopeinfo"), _command("check", "-assert", "-noinit", "-nolatches"),
                    _command("write_json", "lowered.json")], "lowered.json")
    design = normalize(expanded, top, inspection, project)
    by_path = {tuple(item["path"]): item for item in occurrences}
    by_module = {module["id"]: module for module in modules}
    for cell in design["cells"]:
        atoms = cell["native_name"].split(".")
        occurrence = by_path.get(tuple(atoms[:-1]))
        if occurrence is None or not re.fullmatch(r"c[0-9]{4}", atoms[-1]) or atoms[-1] in occurrence["cell_map"]:
            raise FrontendError("HIERARCHY_IDENTITY", "Expanded cell has no unique generated occurrence identity")
        occurrence["cell_map"][atoms[-1]] = cell["id"]
    for occurrence in occurrences:
        expected = {cell["id"] for cell in by_module[occurrence["module_id"]]["cells"]}
        if occurrence["cell_map"].keys() != expected:
            raise FrontendError("HIERARCHY_IDENTITY", "Expansion did not preserve every physical specialization cell")
    if source_hashes(list(hashes)) != hashes:
        raise FrontendError("SOURCE_CHANGED", "A source or include changed during hierarchy lowering; rerun conversion")
    result = {"schema_version": 1, "top_module_id": top_module_id, "modules": modules,
              "expanded_design": design, "occurrences": occurrences}
    validate_hierarchy(result)
    for name, value in (("design.json", design), ("hierarchy.json", result)):
        (directory / name).write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return result


def validate_hierarchy(hierarchy: dict) -> None:
    """Validate structural identities and every primitive in the proof design."""
    try:
        contracts.validate("hierarchy", hierarchy)
    except contracts.ContractError as error:
        raise FrontendError("INVALID_HIERARCHY", str(error)) from error
    if not isinstance(hierarchy, dict) or hierarchy.get("schema_version") != 1:
        raise FrontendError("INVALID_HIERARCHY", "Expected hierarchy schema version 1")
    modules = hierarchy.get("modules", [])
    if not 1 <= len(modules) <= MAX_MODULES or len({module["id"] for module in modules}) != len(modules):
        raise FrontendError("INVALID_HIERARCHY", "Specialization IDs must be unique and bounded")
    by_id = {module["id"]: module for module in modules}
    if hierarchy.get("top_module_id") not in by_id:
        raise FrontendError("INVALID_HIERARCHY", "Root specialization is missing")
    for module in modules:
        expected_id = _identity(module["derived_name"], {"parameter_default_values": module["parameters_raw"],
                                                        "attributes": {"hdlname": module["hdl_name"]}})[0]
        if module["id"] != expected_id:
            raise FrontendError("HIERARCHY_IDENTITY", "Specialization ID does not match its raw sized parameter identity")
        if len({port["name"] for port in module["ports"]}) != len(module["ports"]) or len({net["name"] for net in module["nets"]}) != len(module["nets"]):
            raise FrontendError("INVALID_HIERARCHY", "Local port and net names must be unique")
        if len({cell["id"] for cell in module["cells"]}) != len(module["cells"]) or any(not re.fullmatch(r"c[0-9]{4}", cell["id"]) for cell in module["cells"]):
            raise FrontendError("INVALID_HIERARCHY", "Primitive source IDs must be unique generated atoms")
        children = module["children"]
        if len({child["id"] for child in children}) != len(children) or any(not re.fullmatch(r"u[0-9]{4}", child["id"]) or child["module_id"] not in by_id for child in children):
            raise FrontendError("INVALID_HIERARCHY", "Child IDs or specialization targets are invalid")
        for child in children:
            interface = {port["name"]: port for port in by_id[child["module_id"]]["ports"]}
            if set(interface) != set(child["ports"]) or child["port_directions"] != {name: port["direction"] for name, port in interface.items()}:
                raise FrontendError("HIERARCHY_INTERFACE", "Child pin names/directions differ from the specialized interface")
            for name, bits in child["ports"].items():
                _bits(bits, f"{child['id']}.{name}")
                if len(bits) != len(interface[name]["bits"]):
                    raise FrontendError("HIERARCHY_INTERFACE", "Child pin width differs from the specialized interface")
        for cell in module["cells"]:
            _validate_cell(cell)
    expected = _occurrences(modules, hierarchy["top_module_id"])
    actual = hierarchy.get("occurrences", [])
    if [(item["path"], item["module_id"]) for item in actual] != [(item["path"], item["module_id"]) for item in expected]:
        raise FrontendError("HIERARCHY_IDENTITY", "Occurrences do not match recursive specialization expansion")
    design = hierarchy["expanded_design"]
    validate_design(design)
    expanded = {cell["id"]: cell for cell in design["cells"]}
    covered = set()
    for occurrence in actual:
        local_cells = {cell["id"]: cell for cell in by_id[occurrence["module_id"]]["cells"]}
        if occurrence["cell_map"].keys() != local_cells.keys():
            raise FrontendError("HIERARCHY_IDENTITY", "Occurrence must bind every primitive source cell")
        for local_id, expanded_id in occurrence["cell_map"].items():
            cell = expanded.get(expanded_id)
            local = local_cells[local_id]
            if cell is None or expanded_id in covered or cell["native_name"] != ".".join([*occurrence["path"], local_id]) or cell["type"] != local["type"] or cell["parameters"] != local["parameters"]:
                raise FrontendError("HIERARCHY_IDENTITY", "Expanded cell identity or operation differs from its occurrence")
            covered.add(expanded_id)
    if covered != expanded.keys():
        raise FrontendError("HIERARCHY_IDENTITY", "Every expanded primitive requires one occurrence")
    _verify_expansion(hierarchy)


def _verify_expansion(hierarchy: dict) -> None:
    """Compare local bit partitions with the unoptimized native flatten.

    Covers every primitive pin and top-level port, independent of test vectors
    and generated net names.
    """
    modules = {module["id"]: module for module in hierarchy["modules"]}
    parents = {}

    def key(path, bit):
        return ("constant", bit) if isinstance(bit, str) else (tuple(path), bit)

    def find(item):
        parents.setdefault(item, item)
        origin = item
        while parents[item] != item:
            item = parents[item]
        while parents[origin] != item:
            next_item = parents[origin]
            parents[origin] = item
            origin = next_item
        return item

    def union(first, second):
        a, b = find(first), find(second)
        if a != b:
            parents[b] = a

    for occurrence in hierarchy["occurrences"]:
        module = modules[occurrence["module_id"]]
        path = occurrence["path"]
        for child in module["children"]:
            target = modules[child["module_id"]]
            for port in target["ports"]:
                for outer, inner in zip(child["ports"][port["name"]], port["bits"]):
                    union(key(path, outer), key([*path, child["id"]], inner))
    observations, reverse = {}, {}

    def observe(local, bit):
        representative = find(local)
        if representative in observations and observations[representative] != bit:
            raise FrontendError("HIERARCHY_BINDING", "Expanded cell or port bits disagree with composed child aliases")
        if bit in reverse and reverse[bit] != representative:
            raise FrontendError("HIERARCHY_BINDING", "Expansion joins distinct composed local signal identities")
        observations[representative], reverse[bit] = bit, representative

    observe(key([], "0"), "0")
    observe(key([], "1"), "1")
    design = hierarchy["expanded_design"]
    root = modules[hierarchy["top_module_id"]]
    global_ports = {port["name"]: port for port in design["ports"]}
    if global_ports.keys() != {port["name"] for port in root["ports"]}:
        raise FrontendError("HIERARCHY_INTERFACE", "Expanded top interface differs from the root specialization")
    for port in root["ports"]:
        target = global_ports[port["name"]]
        if any(port[field] != target[field] for field in ("direction", "offset", "upto", "signed")) or len(port["bits"]) != len(target["bits"]):
            raise FrontendError("HIERARCHY_INTERFACE", "Expanded top interface changes a port width or declaration")
        for local, bit in zip(port["bits"], target["bits"]):
            observe(key([], local), bit)
    global_cells = {cell["id"]: cell for cell in design["cells"]}
    for occurrence in hierarchy["occurrences"]:
        for cell in modules[occurrence["module_id"]]["cells"]:
            target = global_cells[occurrence["cell_map"][cell["id"]]]
            for pin, vector in cell["ports"].items():
                if len(vector) != len(target["ports"][pin]):
                    raise FrontendError("HIERARCHY_BINDING", "Expanded primitive changes a pin width")
                for local, bit in zip(vector, target["ports"][pin]):
                    observe(key(occurrence["path"], local), bit)
