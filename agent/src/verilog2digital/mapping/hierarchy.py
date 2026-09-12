"""Map one specialization using only registered, hash-bound child interfaces."""
from __future__ import annotations

from copy import deepcopy
import re

from .. import contracts
from ..frontend.errors import FrontendError
from ..frontend.hierarchy import validate_hierarchy
from .datapath import _map_components, _validate_mapping
from .scalar import MappingError, _endpoint_key
from .sequential import _clock


class _PackageAdapter:
    def __init__(self, adapter, package):
        self.adapter, self.package = adapter, package

    def call(self, method, params, **kwargs):
        if self.package is not None:
            params = {**params, "package": deepcopy(self.package)}
        return self.adapter.call(method, params, **kwargs)


def map_hierarchy_module(hierarchy: dict, module_id: str, adapter, child_catalog: dict,
                         *, package: dict | None = None) -> dict:
    """Map a specialization after its native child circuits have been emitted.

    Catalog entries supply type, SHA256 and optional sidecar metadata; elaborated
    Verilog ports supply expected widths. Returned mappings use relative names.
    """
    try:
        validate_hierarchy(hierarchy)
    except (FrontendError, contracts.ContractError) as error:
        raise MappingError(getattr(error, "code", "INVALID_HIERARCHY"), str(error)) from error
    expanded = hierarchy["expanded_design"]
    if any(cell["type"] == "$dff" for cell in expanded["cells"]):
        _clock(expanded)  # Dedicated global clock includes all descendant state.
    modules = {module["id"]: module for module in hierarchy["modules"]}
    if module_id not in modules:
        raise MappingError("INVALID_HIERARCHY_MODULE", "Requested specialization is absent from the hierarchy")
    module = modules[module_id]
    custom_types, children = {}, []
    for child in module["children"]:
        target_id = child["module_id"]
        entry = child_catalog.get(target_id)
        if not isinstance(entry, dict) or entry.get("type") != f"{target_id}.dig" or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise MappingError("MISSING_CHILD_COMPONENT", f"Emit and register specialization {target_id} before mapping its parent")
        ports = [{"name": port["name"], "direction": port["direction"], "width": len(port["bits"])}
                 for port in modules[target_id]["ports"]]
        custom_types[entry["type"]] = {"module_id": target_id, "type": entry["type"], "sha256": entry["sha256"], "ports": ports}
        children.append({"instance_id": child["id"], "type": entry["type"],
                         **{key: deepcopy(value) for key, value in child.items() if key != "id"}})
    design = {"top": module["hdl_name"], "ports": module["ports"], "nets": module["nets"],
              "cells": module["cells"], "children": module["children"]}
    context = {"module_id": module_id, "children": children,
               "custom_types": {key: custom_types[key] for key in sorted(custom_types)}}
    return _map_components(design, _PackageAdapter(adapter, package), hierarchy_context=context)


def validate_hierarchy_mapping(mapped: dict) -> None:
    """Check local pins and buses; compose hierarchy for cycle and alias checks.

    Child symbols may mix combinational and registered outputs, so their full
    dependencies are checked in the expanded primitive graph.
    """
    try:
        contracts.validate("mapped-hierarchy", mapped)
    except contracts.ContractError as error:
        raise MappingError("INVALID_MAPPING", str(error)) from error
    custom = {entry["type"]: entry for entry in mapped["child_types"]}
    if len(custom) != len(mapped["child_types"]) or len({entry["module_id"] for entry in custom.values()}) != len(custom):
        raise MappingError("INVALID_CHILD_COMPONENT", "Child component types and specializations must be unique")
    for entry in custom.values():
        ports = entry["ports"]
        if entry["type"] != f"{entry['module_id']}.dig" or len({port["name"] for port in ports}) != len(ports):
            raise MappingError("INVALID_CHILD_COMPONENT", "Child filenames and unique ports must identify their specialization")
        if any(type(port["width"]) is not int for port in ports):
            raise MappingError("INVALID_CHILD_COMPONENT", "Child widths must be integer bit counts")
    _validate_mapping(mapped, sequential=True, custom_types=custom)
    instances = {instance["id"]: instance for instance in mapped["instances"]}
    bindings = {_endpoint_key(binding): binding for binding in mapped["pin_bindings"]}
    children = {child["instance_id"]: child for child in mapped["children"]}
    native_children = {identifier for identifier, instance in instances.items() if instance["type"] in custom}
    if len(children) != len(mapped["children"]) or children.keys() != native_children:
        raise MappingError("INVALID_CHILD_COMPONENT", "Every custom component must have one child occurrence record")
    used_types = set()
    for identifier, child in children.items():
        instance = instances[identifier]
        entry = custom.get(child["type"])
        if entry is None or instance["type"] != child["type"] or entry["module_id"] != child["module_id"] or instance["attributes"].get("Label") != child["native_name"]:
            raise MappingError("INVALID_CHILD_COMPONENT", "Native child type, label and specialization disagree")
        expected = {port["name"]: port for port in entry["ports"]}
        if set(child["ports"]) != set(expected) or child["port_directions"] != {name: port["direction"] for name, port in expected.items()}:
            raise MappingError("INVALID_CHILD_BINDING", "Child port coverage and directions must exactly match its specialization")
        for name, port in expected.items():
            binding = bindings[identifier, port["direction"], name]
            if binding["bits"] != child["ports"][name] or binding["width"] != port["width"]:
                raise MappingError("INVALID_CHILD_BINDING", "Child native pin binding changes a local HDL bit lane")
        used_types.add(child["type"])
    if used_types != custom.keys():
        raise MappingError("INVALID_CHILD_COMPONENT", "The child catalog must contain exactly the used specialization types")
    registers = {register["instance_id"]: register for register in mapped["registers"]}
    actual_registers = {identifier for identifier, instance in instances.items() if instance["type"] == "D_FF"}
    if len(registers) != len(mapped["registers"]) or registers.keys() != actual_registers:
        raise MappingError("INVALID_MAPPING_REGISTERS", "Every local register requires one source identity")
    origins = {origin["instance_id"]: origin for origin in mapped["origins"]}
    cell_ids = set()
    for identifier, register in registers.items():
        if register["cell_id"] in cell_ids or origins[identifier].get("cell_id") != register["cell_id"] or register["width"] != instances[identifier]["attributes"]["Bits"]:
            raise MappingError("INVALID_MAPPING_REGISTERS", "Register width or source identity disagrees")
        cell_ids.add(register["cell_id"])
        if register["d_bits"] != bindings[identifier, "input", "D"]["bits"] or register["q_bits"] != bindings[identifier, "output", "Q"]["bits"]:
            raise MappingError("INVALID_MAPPING_REGISTERS", "Register data metadata changes local HDL bit lanes")
        unused = next(net for net in mapped["nets"] if any(_endpoint_key(endpoint) == (identifier, "output", "~Q") for endpoint in net["endpoints"]))
        if len(unused["endpoints"]) != 1:
            raise MappingError("INVALID_MAPPING_REGISTERS", "Complemented register outputs remain unused singleton nets")
