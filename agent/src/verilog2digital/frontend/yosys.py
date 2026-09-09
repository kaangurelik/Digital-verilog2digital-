"""Lower inspected Verilog through Yosys and retain explicit, ordered bit identities."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess

from ..runtime import development_toolchain, repository_root
from ..ir import Design, SUPPORTED_CELLS, validate_design
from .errors import FrontendError
from .inspection import inspect_sources
from .project import Project


# Mapping profiles further restrict this frontend cell inventory.
CELLS = SUPPORTED_CELLS


def source_hashes(paths: list[str | Path]) -> dict[str, str]:
    """Hash source bytes with bounded memory, including parsed include files."""
    result = {}
    for path in sorted(str(Path(path).resolve()) for path in paths):
        try:
            with open(path, "rb") as source:
                result[path] = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as error:
            raise FrontendError("SOURCE_CHANGED", f"Cannot read source dependency: {error}", path=path) from error
    return result


def _tcl_word(value: str) -> str:
    """Escape one Tcl argument without substitution or splitting.

    Tcl forwards argv directly; ordinary .ys quoting breaks spaced -I paths.
    """
    if "\x00" in value:
        raise FrontendError("INVALID_ARGUMENT", "A frontend argument contains a NUL character")
    translations = {"\\": "\\\\", '"': '\\"', "$": "\\$", "[": "\\[", "]": "\\]", "\n": "\\n", "\r": "\\r"}
    return '"' + "".join(translations.get(character, character) for character in value) + '"'


def _command(*words: str) -> str:
    return "yosys " + " ".join(_tcl_word(word) for word in words)


def _parameter(value: str | int) -> str:
    value = str(value).strip()
    if re.fullmatch(r"[+-]?[0-9][0-9_]*", value):
        if not -(1 << 63) <= int(value.replace("_", "")) < (1 << 64):
            raise FrontendError("PARAMETER_LIMIT", "Integer parameter override exceeds 64 bits")
        return value.replace("_", "")
    literal = re.fullmatch(r"([0-9]+)'([sS]?)([bBoOdDhH])([0-9a-fA-F_]+)", value)
    if literal:
        width, signed, base, digits = literal.groups()
        if not 1 <= int(width) <= 64:
            raise FrontendError("PARAMETER_LIMIT", "Parameter literal width must be between 1 and 64")
        try:
            int(digits.replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[base.lower()])
        except ValueError as error:
            raise FrontendError("INVALID_PARAMETER", f"Invalid parameter literal: {value}") from error
        return value
    raise FrontendError("INVALID_PARAMETER", "Parameter overrides must be decimal integers or sized binary/octal/decimal/hex literals")


def _bits(bits: list, label: str, *, width_limit: int = 64) -> list:
    if not 1 <= len(bits) <= width_limit:
        raise FrontendError("UNSUPPORTED_WIDTH", f"{label} has width {len(bits)}; supported widths are 1 through {width_limit}")
    for bit in bits:
        if type(bit) is int and bit >= 2:
            continue
        if bit not in ("0", "1"):
            raise FrontendError("UNSUPPORTED_FOUR_STATE", f"{label} contains an unresolved or unknown bit: {bit!r}")
    return list(bits)


def _number(value):
    if isinstance(value, str) and re.fullmatch("[01]+", value):
        return int(value, 2)
    return value


def _normalize_unchecked(raw: dict, top: str, inspection: dict, project: Project) -> Design:
    """Decode primitive records; callers must validate the completed electrical graph."""
    try:
        module = raw["modules"][top]
    except KeyError as error:
        raise FrontendError("MISSING_TOP", f"Yosys did not produce the selected top module {top}") from error
    if module.get("memories"):
        raise FrontendError("UNSUPPORTED_MEMORY", "Memory arrays require a mapping that is not implemented")
    if module.get("processes"):
        raise FrontendError("UNLOWERED_PROCESS", "The HDL backend left an unlowered process")
    if len(module.get("cells", {})) > 4096:
        raise FrontendError("DESIGN_LIMIT", "The elaborated design exceeds 4096 cells")
    nets = []
    for name, net in sorted(module.get("netnames", {}).items()):
        attributes = net.get("attributes", {})
        if "init" in attributes:
            raise FrontendError("UNSUPPORTED_INITIALIZATION", f"Net {name} has an initialization value")
        nets.append({"name": name, "bits": _bits(net["bits"], f"Net {name}"),
                     "offset": int(net.get("offset", 0)), "upto": bool(net.get("upto", 0)),
                     "signed": bool(net.get("signed", 0)), "source": attributes.get("src", ""),
                     "hidden": bool(net.get("hide_name", 0))})
    by_name = {net["name"]: net for net in nets}
    ports = []
    for name, port in module.get("ports", {}).items():
        if port["direction"] not in {"input", "output"}:
            raise FrontendError("UNSUPPORTED_INOUT", f"Port {name} is not an ordinary input or output")
        ports.append({"name": name, "direction": port["direction"], "bits": _bits(port["bits"], f"Port {name}"),
                      "offset": int(port.get("offset", 0)), "upto": bool(port.get("upto", 0)),
                      "signed": bool(port.get("signed", 0)), "source": by_name.get(name, {}).get("source", "")})
    cells = []
    for index, (native_name, cell) in enumerate(sorted(module.get("cells", {}).items())):
        kind = cell["type"]
        if "latch" in kind.lower():
            raise FrontendError("UNSUPPORTED_LATCH", f"Cell {native_name} requires latch storage")
        if kind.startswith("$mem"):
            raise FrontendError("UNSUPPORTED_MEMORY", f"Cell {native_name} requires memory storage")
        if kind not in CELLS:
            raise FrontendError("UNSUPPORTED_CELL", f"Frontend cell {kind} is outside the current support profile")
        parameters = {key: _number(value) for key, value in sorted(cell.get("parameters", {}).items())}
        if kind == "$dff" and parameters.get("CLK_POLARITY") != 1:
            raise FrontendError("UNSUPPORTED_CLOCK", "Only rising-edge registers are supported")
        connections = {pin: _bits(bits, f"{native_name}.{pin}",
                                  width_limit=4096 if kind == "$pmux" and pin == "B" else 64)
                       for pin, bits in sorted(cell["connections"].items())}
        directions = dict(sorted(cell.get("port_directions", {}).items()))
        if set(directions) != set(connections) or any(direction not in {"input", "output"} for direction in directions.values()):
            raise FrontendError("INVALID_CELL_PORTS", f"Cell {native_name} has missing or unsupported port directions")
        cells.append({"id": f"c{index:04d}", "type": kind, "native_name": native_name,
                      "parameters": parameters, "ports": connections, "port_directions": directions,
                      "source": cell.get("attributes", {}).get("src", ""),
                      "hierarchy": cell.get("attributes", {}).get("hdlname", native_name)})
    design = {"schema_version": 1, "top": top, "ports": ports, "cells": cells, "nets": nets,
              "origins": inspection.get("origins", []), "diagnostics": inspection.get("diagnostics", []),
              "tools": {"yosys": raw.get("creator", "unknown"), "pyslang": "11.0.0"},
              "configuration": {"sources": [str(path) for path in project.sources],
                                "include_dirs": [str(path) for path in project.include_dirs],
                                "defines": project.effective_defines(), "parameters": project.parameters}}
    return design


def normalize(raw: dict, top: str, inspection: dict, project: Project) -> Design:
    design = _normalize_unchecked(raw, top, inspection, project)
    validate_design(design)
    return design


def lower(project: Project, workdir: Path) -> Design:
    directory = Path(workdir).resolve()
    if os.name == "nt" and not str(directory).isascii():
        raise FrontendError("UNSUPPORTED_PATH", "The current Windows HDL tools require ASCII paths. "
                            "Choose a work directory whose full path contains only ASCII characters.", path=str(directory))
    project = project.resolved()
    original_hashes = source_hashes(project.sources)
    parameters = {name: _parameter(value) for name, value in project.parameters.items()}
    inspection = inspect_sources(project.sources, top=project.top, include_dirs=project.include_dirs,
                                 defines=project.effective_defines(), parameters=parameters)
    inspected_hashes = source_hashes(inspection["source_files"])
    if any(inspected_hashes.get(path) != value for path, value in original_hashes.items()):
        raise FrontendError("SOURCE_CHANGED", "A source file changed during inspection; rerun the conversion")
    inspection["input_hashes"] = inspected_hashes
    top = inspection["top"]
    root = repository_root()
    toolchain = development_toolchain(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source-inspection.json").write_text(json.dumps(inspection, indent=2, sort_keys=True), encoding="utf-8")
    normalized_path = directory / "design.json"
    normalized_path.unlink(missing_ok=True)
    raw_path = directory / "lowered.json"
    raw_path.unlink(missing_ok=True)
    arguments = ["read_verilog", "-defer", "-nosynthesis", "-nodpi"]
    for name, value in sorted(project.effective_defines().items()):
        arguments.extend(["-D", f"{name}={value}"])
    for path in project.include_dirs:
        arguments.extend(["-I", path.as_posix()])
    arguments.extend(path.as_posix() for path in project.sources)
    hierarchy = ["hierarchy", "-simcheck", "-top", top]
    for name, value in sorted(parameters.items()):
        hierarchy.extend(["-chparam", name, value])
    commands = [_command(*arguments), _command(*hierarchy), _command("proc"),
                _command("check", "-assert", "-noinit", "-nolatches"),
                _command("write_json", "elaborated.json"), _command("flatten", "-noscopeinfo"),
                # Keep clocked constant loads as state: before their first edge
                # original Verilog remains uninitialized, unlike a constant wire.
                _command("opt", "-nodffe", "-nosdff", "-noff"),
                _command("pmuxtree"),
                _command("check", "-assert", "-noinit", "-nolatches"), _command("write_json", "lowered.json")]
    script = directory / "lower.tcl"
    script.write_text("\n".join(commands) + "\n", encoding="utf-8")
    executable = toolchain.executable("yosys")
    environment = toolchain.environment(executable)
    tcl_library = toolchain.TOOLS / toolchain.LOCK["hdl"]["directory"] / "lib/tcl8.6"
    if tcl_library.is_dir():
        environment["TCL_LIBRARY"] = tcl_library.as_posix()
    log_path = directory / "yosys.log"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run([executable, "-Q", "-T", "-c", "lower.tcl"], cwd=directory,
                                    env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise FrontendError("FRONTEND_TIMEOUT", f"HDL lowering exceeded 120 seconds; details: {log_path}") from error
    if result.returncode != 0 or not raw_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        messages = [line for line in text.splitlines() if "ERROR:" in line or "Warning:" in line]
        code = "YOSYS_ERROR"
        failures = "\n".join(messages).lower()
        if " is a latch of type " in failures or "latch inferred for signal" in failures:
            code = "UNSUPPORTED_LATCH"
        elif "multiple conflicting drivers" in failures:
            code = "MULTIPLE_DRIVERS"
        elif "logic loop" in failures:
            code = "COMBINATIONAL_CYCLE"
        elif "used but has no driver" in failures:
            code = "UNDRIVEN_NET"
        raise FrontendError(code, "\n".join(messages[-10:]) or f"HDL lowering failed; details: {log_path}")
    if raw_path.stat().st_size > 64 * 1024 * 1024:
        raise FrontendError("DESIGN_LIMIT", "The elaborated JSON exceeds 64 MiB")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if source_hashes(list(inspected_hashes)) != inspected_hashes:
        raise FrontendError("SOURCE_CHANGED", "A source or include file changed during lowering; rerun the conversion")
    design = normalize(raw, top, inspection, project)
    normalized_path.write_text(json.dumps(design, indent=2, sort_keys=True), encoding="utf-8")
    return design
