"""Verilog-to-Digital conversion with independent electrical and behavior checks."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .adapter import Adapter, AdapterError
from .contracts import ContractError
from .frontend.errors import FrontendError
from .frontend.project import Project
from .frontend.yosys import lower, source_hashes
from .layout.geometry import restore_wire_ownership, validate_geometry
from .layout.scalar import layout_reserved
from .layout.tunnels import add_long_tunnels
from .mapping import map_design
from .preview import render_preview, write_metadata, verify_preview_bindings
from .runtime import repository_root, require_venv
from .validation.reference import simulate_reference
from .validation.bits import compare_bit_topology
from .validation.stimuli import stimulus_plan
from .validation.topology import compare_components, compare_topology


class ConversionError(FrontendError):
    pass


def _write(path: Path, value: dict | list) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def exhaustive_stimuli(mapped: dict) -> list[dict[str, int]]:
    if sum(port["width"] for port in mapped["inputs"]) > 12:
        raise ConversionError("BEHAVIOR_LIMIT", "Exhaustive validation supports at most 12 total input bits")
    return stimulus_plan(mapped)["stimuli"]


def compare_behavior(mapped: dict, stimuli: list[dict], reference: dict, native: dict, *, coverage: dict | None = None) -> dict:
    """Compare native outputs with independently simulated original Verilog."""
    diagnostics = []
    output_names = {port["port"]: port["instance_id"] for port in mapped["outputs"]}
    output_widths = {port["port"]: port["width"] for port in mapped["outputs"]}
    expected_count = len(stimuli)
    input_widths = {port["port"]: port["width"] for port in mapped["inputs"]}
    valid_inputs = bool(stimuli) and all(isinstance(row, dict) and set(row) == set(input_widths)
                       and all(type(row[name]) is int and 0 <= row[name] < 1 << width
                               for name, width in input_widths.items()) for row in stimuli)
    if not valid_inputs:
        diagnostics.append({"code": "invalid_stimuli", "message": "Validation requires nonempty complete input vectors with unsigned patterns within each port width"})
    expected, observed = reference.get("samples", []), native.get("samples", [])
    if len(expected) != expected_count or len(observed) != expected_count:
        diagnostics.append({"code": "sample_count", "message": "Reference and Digital must return every requested vector",
                            "requested": expected_count, "reference": len(expected), "digital": len(observed)})
    for index, (wanted, actual) in enumerate(zip(expected, observed)):
        if index >= expected_count:
            break
        try:
            if set(wanted["values"]) != set(output_names) or set(actual["values"]) != set(output_names.values()) or set(actual["high_z"]) != set(output_names.values()):
                raise ValueError("Output identities do not match the mapped interface")
            for name, identifier in output_names.items():
                target = wanted["values"][name]
                value = int(actual["values"][identifier], 16)
                high_z = int(actual["high_z"][identifier], 16)
                maximum = (1 << output_widths[name]) - 1
                if type(target) is not int or not 0 <= target <= maximum or not 0 <= value <= maximum or high_z != 0 or value != target:
                    diagnostics.append({"code": "behavior_mismatch", "vector": index, "inputs": stimuli[index],
                                        "output": name, "expected": target, "actual": value, "high_z": high_z,
                                        "message": "Digital output differs from the binary Verilog reference"})
        except (KeyError, TypeError, ValueError) as error:
            diagnostics.append({"code": "invalid_sample", "vector": index, "message": str(error)})
        if len(diagnostics) >= 32:
            break
    total_bits = sum(input_widths.values())
    exhaustive = False
    if valid_inputs and total_bits <= 12 and expected_count == 1 << total_bits:
        exhaustive = len({tuple(row[name] for name in input_widths) for row in stimuli}) == expected_count
    coverage = dict(coverage) if coverage is not None else {
        "coverage": "exhaustive_binary" if exhaustive else "provided_vectors", "exhaustive": exhaustive}
    if (coverage.get("coverage") == "exhaustive_binary" or coverage.get("exhaustive") is True) and not exhaustive:
        diagnostics.append({"code": "invalid_coverage_claim", "message": "The supplied vectors do not exhaust the mapped binary input space"})
    return {**coverage, "valid": not diagnostics, "input_vectors": expected_count,
            "reference": "Icarus simulation of original Verilog sources", "assumptions": reference["assumptions"],
            "diagnostics": diagnostics}


def compare_sequential_behavior(mapped: dict, trace: dict, reference: dict, native: dict,
                                *, coverage: dict, initialization: dict) -> dict:
    """Compare ordered edge observations without assuming a native initial state."""
    phases = ("before_rise", "after_rise", "after_fall")
    clock = trace["clock"]["port"]
    stimuli = [{**cycle["values"], clock: level}
               for cycle in trace["cycles"] for level in (0, 1, 0)]
    report = compare_behavior(mapped, stimuli, reference, native,
                              coverage={**coverage, "coverage": "sampled_cycles", "exhaustive": False})
    diagnostics = list(report["diagnostics"])
    if not _initialization_valid(initialization, mapped, trace):
        diagnostics.append({"code": "unproven_initialization", "message": "All retained register bits must be known after input-only initialization"})
    reference_assumptions = reference.get("assumptions", {})
    native_assumptions = native.get("assumptions", {})
    expected_defaults = {item["id"]: item["attributes"]["Default"]
                         for item in mapped.get("instances", []) if item["type"] == "D_FF"}
    native_initialization = native_assumptions.get("initialization", {})
    if (reference_assumptions.get("initialization") != initialization
            or reference_assumptions.get("clock") != trace["clock"]
            or reference_assumptions.get("checked_cycles") != len(trace["cycles"])
            or reference_assumptions.get("sample_count") != len(stimuli)
            or reference_assumptions.get("phases") != list(phases)):
        diagnostics.append({"code": "reference_trace_mismatch", "message": "Reference assumptions must describe this clock, prefix and checked trace"})
    if (native_assumptions.get("clock_instance_id") != mapped.get("clock", {}).get("instance_id")
            or not mapped.get("clock", {}).get("instance_id")
            or native_assumptions.get("phase_order") != list(phases)
            or native_initialization.get("kind") != "input_sequence"
            or native_initialization.get("internal_state_forcing") is not False
            or type(native_initialization.get("prefix_cycles")) is not int
            or native_initialization.get("prefix_cycles") != len(trace.get("initialization", {}).get("cycles", []))
            or not expected_defaults or native_initialization.get("native_defaults") != expected_defaults):
        diagnostics.append({"code": "native_trace_mismatch", "message": "Native assumptions must identify this clock, prefix length and exact register defaults"})
    expected_order = [(index, phase) for index in range(len(trace["cycles"])) for phase in phases]
    for name, result in (("reference", reference), ("digital", native)):
        samples = result.get("samples", [])
        order = [(row.get("cycle"), row.get("phase")) if isinstance(row, dict) and type(row.get("cycle")) is int else None for row in samples]
        if order != expected_order:
            diagnostics.append({"code": "cycle_order", "source": name,
                                "message": "Every cycle must contain ordered observations before rise, after rise and after fall"})
    return {**report, "valid": not diagnostics, "diagnostics": diagnostics,
            "checked_cycles": len(trace["cycles"]), "phase_order": list(phases),
            "initialization": initialization, "digital_assumptions": native.get("assumptions", {})}


def _initialization_valid(proof: dict, mapped: dict, trace: dict) -> bool:
    if not (isinstance(proof, dict) and proof.get("valid") is True
            and proof.get("internal_state_forcing") is False
            and proof.get("initial_state") == "all_retained_register_bits_unknown"
            and type(proof.get("register_count")) is int and proof["register_count"] > 0
            and type(proof.get("register_bits")) is int and proof["register_bits"] > 0
            and type(proof.get("known_register_count")) is int
            and type(proof.get("known_register_bits")) is int
            and proof.get("known_register_count") == proof["register_count"]
            and proof.get("known_register_bits") == proof["register_bits"]):
        return False
    try:
        expected = {item["cell_id"]: item["width"] for item in mapped["registers"]}
        states = {item["cell_id"]: item for item in proof["registers"]}
        if (len(expected) != len(mapped["registers"]) or len(states) != len(proof["registers"])
                or set(expected) != set(states) or proof["register_count"] != len(expected)
                or proof["register_bits"] != sum(expected.values())
                or proof["prefix"] != trace["initialization"]["cycles"]
                or type(proof["prefix_cycles"]) is not int
                or not 1 <= proof["prefix_cycles"] == len(proof["prefix"]) <= 256):
            return False
        for identifier, width in expected.items():
            state = states[identifier]
            if (type(state["width"]) is not int or state["width"] != width
                    or type(state["known_bits"]) is not int or state["known_bits"] != width
                    or any(not isinstance(state[key], str) or not 3 <= len(state[key]) <= 18
                           or not state[key].startswith("0x") for key in ("value", "known_mask"))
                    or int(state["known_mask"], 16) != (1 << width) - 1
                    or not 0 <= int(state["value"], 16) < 1 << width):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def validate_export(adapter, mapped: dict, circuit: dict, paths: dict) -> tuple[dict, dict]:
    """Reload an emitted candidate and independently check native connections."""
    inspection = adapter.call("circuit.inspect", paths)
    topology = compare_topology([net["endpoints"] for net in mapped["nets"]], inspection)
    components = compare_components(mapped["descriptors"], inspection["instances"])
    bits = compare_bit_topology(mapped, inspection)
    try:
        normalized = {**circuit, "wires": restore_wire_ownership(circuit["wires"], inspection["wires"])}
        geometry = validate_geometry(normalized, inspection["instances"], nets=mapped["nets"], clearance=10)
    except ValueError as error:
        geometry = {"valid": False, "diagnostics": [{"code": "normalized_wire_ownership", "message": str(error)}], "metrics": {}}
    checks = {"topology": topology, "bits": bits, "components": components, "geometry": geometry, "model": inspection["model"]}
    return {"schema_version": 1, "valid": all(check["valid"] for check in checks.values()), **checks}, inspection


def _preview_scale(circuit: dict, descriptors: list[dict]) -> float:
    points = [corner for item in descriptors for corner in item["bounds"].values()]
    points += [wire[end] for wire in circuit["wires"] for end in ("from", "to")]
    width = max(point["x"] for point in points) - min(point["x"] for point in points) + 80
    height = max(point["y"] for point in points) - min(point["y"] for point in points) + 80
    scale = min(2.0, 15000 / width, 15000 / height, math.sqrt(12_000_000 / (width * height)))
    if scale < 0.05:
        raise ConversionError("PREVIEW_LIMIT", "The layout exceeds the bounded preview size; use a smaller design")
    return scale


def convert(project: Project, output_dir: Path, *, adapter=None, spacing: int = 40, progress=None,
            profile: str = "auto", seed: int = 0, random_count: int = 256,
            layout_strategy: str = "auto", tunnels: bool = False, layout_timeout: float = 20,
            sequential_trace: dict | None = None, flatten: bool = False, refinement: dict | None = None) -> dict:
    """Convert into a new directory and mark success with a ready manifest.

    Failed candidates and diagnostics remain under work/. Managed replacement
    and cancellation are handled by the jobs API.
    """
    root = repository_root()
    require_venv(root)
    from .llm.config import configuration
    from .llm.loop import RefinementSession
    refinement = configuration(refinement)
    if type(flatten) is not bool:
        raise ConversionError("INVALID_HIERARCHY_OPTION", "The flatten option must be Boolean")
    if layout_strategy not in {"auto", "reserved"} or type(tunnels) is not bool:
        raise ConversionError("INVALID_LAYOUT_OPTIONS", "Layout must be auto or reserved, with a Boolean tunnel option")
    if isinstance(layout_timeout, bool) or not isinstance(layout_timeout, (float, int)) or not math.isfinite(layout_timeout) or not 1 <= layout_timeout <= 120:
        raise ConversionError("INVALID_LAYOUT_OPTIONS", "Layout timeout must be between1 and120 seconds")
    destination = Path(output_dir).resolve()
    if os.name == "nt" and not str(destination).isascii():
        raise ConversionError("UNSUPPORTED_PATH", "The current Windows HDL tools require an ASCII output directory path", path=str(destination))
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ConversionError("OUTPUT_EXISTS", "Choose a new output directory; existing results are preserved", path=str(destination)) from error
    work = destination / "work"
    work.mkdir()
    stage = "created"
    started = time.perf_counter()
    timings = {}
    progress_warnings = []
    stage_start = started

    def advance(next_stage):
        nonlocal stage, stage_start
        now = time.perf_counter()
        timings[stage] = round(timings.get(stage, 0) + now - stage_start, 6)
        stage, stage_start = next_stage, now
        if progress:
            try:
                progress({"stage": stage, "elapsed_seconds": round(now - started, 3)})
            except Exception as error:
                # Observer failures must not invalidate a published result.
                if len(progress_warnings) < 8:
                    progress_warnings.append({"stage": stage, "message": str(error)[:500]})

    try:
        advance("lowering")
        project = project.resolved()
        hierarchy = None
        if flatten:
            design = lower(project, work / "frontend")
        else:
            from .frontend.hierarchy import lower_hierarchy
            hierarchy = lower_hierarchy(project, work / "frontend")
            design = hierarchy["expanded_design"]
        sources = json.loads((work / "frontend/source-inspection.json").read_text(encoding="utf-8"))["input_hashes"]
        if hierarchy is not None and next(module for module in hierarchy["modules"]
                                           if module["id"] == hierarchy["top_module_id"])["children"]:
            from .hierarchical_conversion import convert_prepared_hierarchy
            return convert_prepared_hierarchy(project, hierarchy, destination, work, adapter=adapter,
                sources=sources, options={"spacing": spacing, "profile": profile, "seed": seed,
                    "random_count": random_count, "layout_strategy": layout_strategy, "tunnels": tunnels,
                    "layout_timeout": layout_timeout, "sequential_trace": sequential_trace,
                    "refinement": refinement},
                advance=advance, started=started, timings=timings, progress_warnings=progress_warnings)
        advance("starting_adapter")
        with (nullcontext(adapter) if adapter is not None else Adapter()) as native, \
                RefinementSession(refinement, work / "refinement", progress=advance) as refiner:
            advance("mapping")
            mapped = map_design(design, native, profile=profile)
            _write(work / "mapped.json", mapped)
            sequential = mapped["profile"] == "sequential-v1"
            if sequential:
                from .validation.sequential import sequential_stimulus_plan
                advance("validating_initialization")
                plan = sequential_stimulus_plan(design, seed=seed, random_count=random_count, trace=sequential_trace)
                if not _initialization_valid(plan["initialization"], mapped, plan["trace"]):
                    raise ConversionError("UNINITIALIZED_STATE", "The initialization sequence did not establish every retained register bit")
                _write(work / "trace.json", plan["trace"])
                _write(work / "initialization.json", plan["initialization"])
            elif sequential_trace is not None:
                raise ConversionError("UNEXPECTED_TRACE", "A sequential trace requires a design with registers")
            advance("routing")
            if layout_strategy == "reserved":
                base_layout = layout_reserved(mapped, spacing=spacing)
            else:
                from .layout.general import layout_general
                base_layout = layout_general(mapped, native, spacing=spacing, seed=1, timeout=layout_timeout)
            layout = add_long_tunnels(base_layout, mapped, native) if tunnels else base_layout
            attempts = []
            while True:
                circuit = layout["circuit"]
                expected_mapping = layout.get("validation_mapping", mapped)
                _write(work / "candidate.json", circuit)
                _write(work / "expected-nets.json", expected_mapping["nets"])
                _write(work / "routing-mapped.json", expected_mapping)
                advance("validating_connections")
                emitted = native.call("circuit.emit", {"circuit": circuit, "path": str(work / "candidate.dig")})
                paths = {key: emitted[key] for key in ("path", "sidecar_path")}
                validated_hashes = {Path(paths["path"]): emitted["sha256"],
                                    Path(paths["sidecar_path"]): _hash(Path(paths["sidecar_path"]))}
                if _hash(Path(paths["path"])) != emitted["sha256"]:
                    raise ConversionError("ARTIFACT_CHANGED", "The emitted circuit changed before validation")
                validation, inspection = validate_export(native, expected_mapping, circuit, paths)
                _write(work / "inspection.json", inspection)
                _write(work / "validation.json", validation)
                attempts.append({"strategy": layout["strategy"], "tunnels": layout.get("tunnels", {}).get("nets", []),
                                 "validation": validation})
                _write(work / "layout-attempts.json", attempts)
                if validation["valid"]:
                    break
                if len(attempts) >= 3:
                    break
                if layout.get("tunnels", {}).get("nets"):
                    advance("routing")
                    layout = {**base_layout, "fallback_used": True, "fallback_reason": "native_tunnel_validation"}
                elif layout["strategy"] not in {"scalar-reserved-tracks-v1", "datapath-reserved-tracks-v1", "sequential-reserved-tracks-v1"}:
                    advance("routing")
                    layout = {**layout_reserved(mapped, spacing=spacing), "fallback_used": True,
                              "fallback_reason": "native_layout_validation"}
                else:
                    break
            if not validation["valid"]:
                raise ConversionError("CIRCUIT_VALIDATION", "The reloaded Digital circuit failed component, connection, model, or geometry checks")
            baseline_preview = None
            if refiner.enabled:
                baseline_preview = render_preview(native, paths, work / "baseline-preview.png",
                    inspection=inspection, emitted=emitted, scale=_preview_scale(circuit, inspection["instances"]),
                    comparison_scope="before_refinement", artifact="baseline_preview")
                selected = refiner.refine(expected_mapping, {
                    "layout": layout, "mapped": expected_mapping, "validation": validation,
                    "inspection": inspection, "emitted": emitted, "paths": paths})
                layout, validation, inspection = selected["layout"], selected["validation"], selected["inspection"]
                emitted, paths = selected["emitted"], selected["paths"]
                circuit = layout["circuit"]
                validated_hashes = {Path(paths["path"]): emitted["sha256"],
                                    Path(paths["sidecar_path"]): emitted["sidecar_sha256"]}
                _write(work / "candidate.json", circuit)
                _write(work / "inspection.json", inspection)
                refiner.close()
                validation["refinement"] = refiner.report
            advance("validating_behavior")
            if sequential:
                from .validation.reference import simulate_sequential_reference
                trace = plan["trace"]
                reference = simulate_sequential_reference(project, design, work / "reference", trace)
                input_ids = {port["port"]: port["instance_id"] for port in mapped["inputs"]}
                def native_cycles(rows):
                    return [{"values": {input_ids[name]: hex(value) for name, value in row["values"].items()}}
                            for row in rows]
                simulation = native.call("circuit.simulate_cycles", {
                    **paths, "clock_instance_id": input_ids[trace["clock"]["port"]],
                    "initialization": {"kind": "input_sequence", "cycles": native_cycles(trace["initialization"]["cycles"])},
                    "cycles": native_cycles(trace["cycles"])})
                behavior = compare_sequential_behavior(mapped, trace, reference, simulation,
                    coverage=plan["assumptions"], initialization=plan["initialization"])
            else:
                plan = stimulus_plan(mapped, seed=seed, random_count=random_count)
                stimuli = plan["stimuli"]
                _write(work / "stimuli.json", stimuli)
                reference = simulate_reference(project, design, work / "reference", stimuli)
                native_stimuli = [{"values": {port["instance_id"]: hex(values[port["port"]]) for port in mapped["inputs"]}}
                                  for values in stimuli]
                simulation = native.call("circuit.simulate", {**paths, "stimuli": native_stimuli})
                behavior = compare_behavior(mapped, stimuli, reference, simulation, coverage=plan["assumptions"])
            _write(work / "reference-samples.json", reference)
            _write(work / "digital-samples.json", simulation)
            validation["behavior"] = behavior
            validation["valid"] = validation["valid"] and validation["behavior"]["valid"]
            _write(work / "validation.json", validation)
            if not validation["valid"]:
                raise ConversionError("BEHAVIOR_VALIDATION", "Digital does not match the original Verilog for the declared validation vectors")
            if source_hashes(list(sources)) != sources:
                raise ConversionError("SOURCE_CHANGED", "A source or include file changed during validation; rerun the conversion")
            advance("rendering")
            scale = _preview_scale(circuit, inspection["instances"])
            native.call("circuit.render", {"path": paths["path"], "format": "svg",
                                          "output_path": str(work / "preview.svg"), "scale": scale})
            selected_preview = render_preview(native, paths, work / "preview.png", inspection=inspection,
                                               emitted=emitted, scale=scale)
            write_metadata(work / "preview-metadata.json", design["top"], selected_preview, baseline_preview)
            validated_hashes[work / "preview.png"] = selected_preview["image"]["sha256"]
            validated_hashes[work / "preview-metadata.json"] = _hash(work / "preview-metadata.json")
            if baseline_preview is not None:
                validated_hashes[work / "baseline-preview.png"] = baseline_preview["image"]["sha256"]
            capabilities = native.capabilities
        advance("publishing")
        if source_hashes(list(sources)) != sources:
            raise ConversionError("SOURCE_CHANGED", "A source or include file changed during rendering; rerun the conversion")
        if any(_hash(path) != digest for path, digest in validated_hashes.items()):
            raise ConversionError("ARTIFACT_CHANGED", "The validated circuit or identity sidecar changed before publication")
        publication = {"circuit": (Path(paths["path"]), destination / "circuit.dig"),
                       "sidecar": (Path(paths["sidecar_path"]), destination / "circuit.dig.ids.json"),
                       "preview": (work / "preview.png", destination / "preview.png"),
                       "svg": (work / "preview.svg", destination / "preview.svg"),
                       "preview_metadata": (work / "preview-metadata.json", destination / "preview-metadata.json"),
                       "validation": (work / "validation.json", destination / "validation.json")}
        if refiner.enabled:
            publication["baseline_preview"] = (work / "baseline-preview.png", destination / "baseline-preview.png")
        for source, target in publication.values():
            source.replace(target)
        for source, digest in validated_hashes.items():
            target = next(target for previous, target in publication.values() if previous == source)
            if _hash(target) != digest:
                raise ConversionError("ARTIFACT_CHANGED", "The validated circuit or identity sidecar changed during publication")
        timings["publishing"] = round(time.perf_counter() - stage_start, 6)
        manifest = {"schema_version": 1, "state": "ready", "profile": mapped["profile"], "top": design["top"],
                    "configuration": design["configuration"], "sources": sources,
                    "tools": {**design["tools"], **reference["tools"], "digital_revision": capabilities["digital_revision"],
                              "adapter_sha256": _hash(root / "digital-adapter/target/digital-adapter.jar"),
                              "toolchain_lock_sha256": _hash(root / "scripts/toolchain-lock.json")},
                    "layout": {"strategy": layout["strategy"], "spacing": spacing, "preview_scale": scale,
                               "metrics": validation["geometry"]["metrics"], "requested": layout_strategy, "seed": 1,
                               "fallback_used": layout.get("fallback_used", False),
                               "fallback_reason": layout.get("fallback_reason"), "attempts": len(attempts),
                               "tunnels": layout.get("tunnels", {"mode": "visible", "nets": []})},
                    "behavior": validation["behavior"], "refinement": refiner.report, "timings_seconds": timings,
                    "progress_warnings": list(progress_warnings),
                    "duration_seconds": round(time.perf_counter() - started, 6),
                    "artifacts": {name: {"path": target.name, "sha256": _hash(target)}
                                  for name, (_, target) in publication.items()}}
        verify_preview_bindings(manifest, {name: (target, manifest["artifacts"][name]["sha256"])
                                          for name, (_, target) in publication.items()})
        _write(destination / "manifest.json", manifest)
        advance("ready")
        return {"schema_version": 1, "state": "ready", "top": design["top"], "directory": str(destination),
                "progress_warnings": progress_warnings,
                **{name: str(target) for name, (_, target) in publication.items()},
                "manifest": str(destination / "manifest.json")}
    except Exception as error:
        diagnostic = error.as_dict() if isinstance(error, FrontendError) else {"code": "CONVERSION_FAILED", "message": str(error)}
        if isinstance(error, AdapterError) and error.data is not None:
            diagnostic["adapter"] = error.data
        if hasattr(error, "details"):
            diagnostic["details"] = error.details
        _write(destination / "failure.json", {"schema_version": 1, "state": "failed", "stage": stage,
                                               "diagnostic": diagnostic, "timings_seconds": timings})
        if isinstance(error, (FrontendError, AdapterError, ContractError)):
            raise
        raise ConversionError("CONVERSION_FAILED", f"{stage}: {error}") from error
