"""Compare reserved and compact routing after Digital reloads each result.

Use HDL examples and a deterministic 248-component fixture. Record geometry
metrics and fallback status for every result.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "agent/src") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent/src"))

from verilog2digital.adapter import Adapter
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.ir import validate_design
from verilog2digital.layout.geometry import restore_wire_ownership, validate_geometry
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.mapping import map_design
from verilog2digital.runtime import require_venv
from verilog2digital.validation.bits import compare_bit_topology
from verilog2digital.validation.topology import compare_components, compare_topology


HDL_CASES = ("chain_small", "chain_medium", "reconvergent", "high_fanout",
             "mixed_symbols", "crossed_datapaths", "compact_alu", "congested")
CASES = (*HDL_CASES, "near_limit_248")
FIXTURES = ROOT / "tests/fixtures/verilog/layout"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def near_limit_design() -> dict:
    """Build 248 native components: two inputs, 244 gates and two outputs.

    Construct directly to prevent synthesis from collapsing the stress fixture.
    """
    source = "native-layout-fixture:near_limit_248"

    def vector(name, bits):
        return {"name": name, "bits": bits, "offset": 0, "upto": False,
                "signed": False, "source": source}

    cells = []
    nets = [{**vector("a", [2]), "hidden": False}, {**vector("b", [3]), "hidden": False}]
    for index in range(244):
        kind = ("$xor", "$and", "$or", "$not")[index % 4]
        output = 4 + index
        previous = 2 if index == 0 else output - 1
        connections = {"A": [previous], "Y": [output]}
        directions = {"A": "input", "Y": "output"}
        parameters = {"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1}
        if kind != "$not":
            # Long reconvergent branches mix with persistent input fanout.
            second = (2 if index % 3 == 0 else 3 if index % 3 == 1 else 4 + max(0, index // 2 - 1)) if index else 3
            connections["B"] = [second]
            directions["B"] = "input"
            parameters.update(B_SIGNED=0, B_WIDTH=1)
        name = f"node_{index:03d}"
        cells.append({"id": name, "type": kind, "native_name": name,
                      "parameters": parameters, "ports": connections,
                      "port_directions": directions, "source": source, "hierarchy": name})
        nets.append({**vector(name, [output]), "hidden": False})
    ports = [{**vector("a", [2]), "direction": "input"},
             {**vector("b", [3]), "direction": "input"},
             {**vector("y", [247]), "direction": "output"},
             {**vector("tap", [124]), "direction": "output"}]
    nets.extend({**vector(port["name"], port["bits"]), "hidden": False} for port in ports[2:])
    design = {"schema_version": 1, "top": "near_limit_248", "ports": ports, "nets": nets,
              "cells": cells, "diagnostics": [],
              "origins": [{"module": "near_limit_248", "path": str(Path(__file__).resolve()),
                           "line": near_limit_design.__code__.co_firstlineno, "column": 1,
                           "end_line": None, "end_column": None}],
              "tools": {"yosys": "not run: deterministic native fixture", "pyslang": "not run: deterministic native fixture"},
              "configuration": {"sources": [str(Path(__file__).resolve())], "include_dirs": [],
                                "defines": {"SYNTHESIS": "1", "YOSYS": "1"}, "parameters": {}}}
    validate_design(design)
    return design


def load_case(name: str, adapter, directory: Path) -> dict:
    if name not in CASES:
        raise ValueError(f"Unknown layout corpus case: {name}")
    directory.mkdir(parents=True, exist_ok=True)
    if name == "near_limit_248":
        design = near_limit_design()
        mapped = map_design(design, adapter, profile="datapath-v1")
        if len(mapped["instances"]) != 248:
            raise AssertionError("The native stress corpus must contain exactly 248 components")
        write_json(directory / "native-design.json", design)
    else:
        source = FIXTURES / f"{name}.v"
        design = lower(Project(sources=[source], top=name), directory / "frontend")
        mapped = map_design(design, adapter)
    write_json(directory / "mapped.json", mapped)
    return mapped


def verify_layout(mapped: dict, layout: dict, adapter, directory: Path) -> dict:
    """Check the serialized result with native wires and independent bit intent."""
    directory.mkdir(parents=True, exist_ok=True)
    circuit = layout["circuit"]
    emitted = adapter.call("circuit.emit", {"circuit": circuit, "path": str(directory / "circuit.dig")})
    paths = {key: emitted[key] for key in ("path", "sidecar_path")}
    inspected = adapter.call("circuit.inspect", paths)
    checks = {
        "topology": compare_topology([net["endpoints"] for net in mapped["nets"]], inspected),
        "bits": compare_bit_topology(mapped, inspected),
        "components": compare_components(mapped["descriptors"], inspected["instances"]),
        "model": inspected["model"],
        "proposed_geometry": validate_geometry(circuit, layout["descriptors"], nets=mapped["nets"], clearance=10),
    }
    try:
        normalized = {**circuit, "wires": restore_wire_ownership(circuit["wires"], inspected["wires"])}
        checks["geometry"] = validate_geometry(normalized, inspected["instances"], nets=mapped["nets"], clearance=10)
    except ValueError as error:
        checks["geometry"] = {"valid": False, "diagnostics": [{"code": "wire_ownership", "message": str(error)}], "metrics": {}}
    result = {"valid": all(check["valid"] for check in checks.values()), "checks": checks,
              "metrics": checks["geometry"]["metrics"], "circuit": paths["path"], "sidecar": paths["sidecar_path"]}
    write_json(directory / "candidate.json", circuit)
    write_json(directory / "inspection.json", inspected)
    write_json(directory / "validation.json", result)
    return result


def render_result(layout: dict, verified: dict, adapter, directory: Path) -> dict:
    corners = [point for item in layout["descriptors"] for point in item["bounds"].values()]
    corners += [wire[end] for wire in layout["circuit"]["wires"] for end in ("from", "to")]
    width = max(point["x"] for point in corners) - min(point["x"] for point in corners) + 80
    height = max(point["y"] for point in corners) - min(point["y"] for point in corners) + 80
    scale = min(1.0, 12000 / width, 12000 / height, math.sqrt(4_000_000 / (width * height)))
    artifacts = {}
    for format in ("png", "svg"):
        output = directory / f"preview.{format}"
        adapter.call("circuit.render", {"path": verified["circuit"], "format": format,
                                        "output_path": str(output), "scale": scale})
        artifacts[format] = str(output)
    return {"scale": scale, **artifacts}


def benchmark_case(name: str, mapped: dict, adapter, directory: Path, *, spacing: int = 40,
                   seed: int = 1, timeout: float = 20, render: bool = False) -> dict:
    from verilog2digital.layout.general import layout_general

    directory.mkdir(parents=True, exist_ok=True)
    before = deepcopy(mapped)
    records = {}
    for kind in ("reserved", "general"):
        started = time.perf_counter()
        layout = (layout_reserved(mapped, spacing=spacing) if kind == "reserved" else
                  layout_general(mapped, adapter, spacing=spacing, seed=seed, timeout=timeout))
        elapsed = time.perf_counter() - started
        if mapped != before:
            raise AssertionError(f"{kind} routing mutated the independent mapped intent")
        if kind == "general" and type(layout.get("fallback_used")) is not bool:
            raise AssertionError("General routing must explicitly report whether it used fallback")
        verify_started = time.perf_counter()
        verified = verify_layout(mapped, layout, adapter, directory / kind)
        record = {"strategy": layout["strategy"], "runtime_seconds": elapsed,
                  "verification_seconds": time.perf_counter() - verify_started,
                  "routing_metrics": layout.get("metrics", layout["geometry"]["metrics"]),
                  "fallback_used": layout.get("fallback_used", False),
                  "fallback_reason": layout.get("fallback_reason"),
                  "diagnostics": layout.get("diagnostics", []),
                  **verified}
        if render and verified["valid"]:
            record["preview"] = render_result(layout, verified, adapter, directory / kind)
        records[kind] = record
    reserved, general = records["reserved"], records["general"]
    ratios = {key: general["metrics"][key] / reserved["metrics"][key]
              for key in ("bounding_area", "wire_length", "bends", "crossings")
              if general["valid"] and reserved["valid"] and reserved["metrics"].get(key)}
    result = {"case": name, "components": len(mapped["instances"]), "nets": len(mapped["nets"]),
              "spacing": spacing, "seed": seed, "timeout_seconds": timeout,
              "valid": reserved["valid"] and general["valid"], "results": records,
              "general_to_reserved_ratios": ratios,
              "fallback_count": int(general["fallback_used"])}
    write_json(directory / "benchmark.json", result)
    return result


def fresh_destination() -> Path:
    base = ROOT / ".runs/layout-benchmark"
    result, index = base, 0
    while result.exists():
        index += 1
        result = base.with_name(f"{base.name}-{index:03d}")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New directory for native artifacts and benchmark JSON")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--spacing", type=int, default=40)
    parser.add_argument("--seed", type=int, choices=[1], default=1, help="Native ELK planner seed; only 1 is supported")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--stress-timeout", type=float, default=2, help="General search budget for the 248-component case")
    parser.add_argument("--no-render", action="store_true", help="Skip compact ALU comparison previews")
    arguments = parser.parse_args(argv)
    require_venv(ROOT)
    destination = (arguments.output or fresh_destination()).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    with Adapter() as adapter:
        for name in arguments.cases:
            print(f"Layout benchmark: {name}", flush=True)
            directory = destination / name
            timeout = arguments.stress_timeout if name == "near_limit_248" else arguments.timeout
            try:
                mapped = load_case(name, adapter, directory / "input")
                record = benchmark_case(name, mapped, adapter, directory, spacing=arguments.spacing,
                                        seed=arguments.seed, timeout=timeout,
                                        render=name == "compact_alu" and not arguments.no_render)
            except Exception as error:
                directory.mkdir(parents=True, exist_ok=True)
                record = {"case": name, "valid": False,
                          "error": {"code": getattr(error, "code", type(error).__name__), "message": str(error)}}
                write_json(directory / "benchmark.json", record)
            records.append(record)
            print(json.dumps({"case": name, "valid": record["valid"],
                              "ratios": record.get("general_to_reserved_ratios"),
                              "fallbacks": record.get("fallback_count"), "error": record.get("error")}), flush=True)
    summary = {"schema_version": 1, "valid": all(record["valid"] for record in records),
               "cases": records, "fallback_count": sum(record.get("fallback_count", 0) for record in records),
               "seed": arguments.seed, "spacing": arguments.spacing,
               "corpus_kind": "eight HDL examples plus one deterministic native component stress fixture",
               "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(destination / "summary.json", summary)
    print(f"Benchmark report: {destination / 'summary.json'}", flush=True)
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
