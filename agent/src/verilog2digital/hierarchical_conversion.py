"""Build a relocatable native hierarchy and validate its expanded electrical graph."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import time

from .adapter import Adapter
from .conversion import (ConversionError, _hash, _initialization_valid, _preview_scale, _write,
                         compare_behavior, compare_sequential_behavior, validate_export)
from .frontend.yosys import source_hashes
from .layout.general import layout_general
from .layout.scalar import layout_reserved
from .layout.tunnels import add_long_tunnels
from .mapping import map_design
from .mapping.hierarchy import map_hierarchy_module
from .preview import render_preview, write_metadata, verify_preview_bindings
from .runtime import repository_root
from .validation.hierarchy import compose_hierarchy, snapshot_package, verify_hierarchy, verify_package_snapshot
from .validation.reference import simulate_reference, simulate_sequential_reference
from .validation.sequential import sequential_stimulus_plan
from .validation.stimuli import stimulus_plan


RESERVED_STRATEGIES = {"scalar-reserved-tracks-v1", "datapath-reserved-tracks-v1",
                       "sequential-reserved-tracks-v1", "hierarchy-reserved-tracks-v1"}


class PackageAdapter:
    """Supply an explicit, immutable dependency snapshot on each native request."""
    def __init__(self, native, directory: Path, catalog: dict):
        self.native, self.directory, self.catalog = native, directory, catalog

    @property
    def capabilities(self):
        return self.native.capabilities

    @property
    def _closed(self):
        return getattr(self.native, "_closed", False)

    def context(self):
        return {"root": str(self.directory), "modules": [deepcopy(value) for _, value in sorted(self.catalog.items())]}

    def call(self, method, params, **options):
        return self.native.call(method, {**params, "package": self.context()}, **options)


def _module_order(hierarchy):
    modules = {module["id"]: module for module in hierarchy["modules"]}
    ordered, visited = [], set()
    def visit(identifier):
        if identifier in visited:
            return
        visited.add(identifier)
        for child in modules[identifier]["children"]:
            visit(child["module_id"])
        ordered.append(modules[identifier])
    visit(hierarchy["top_module_id"])
    return ordered


def _display_name(module):
    parameters = []
    for name, value in sorted(module["parameters_raw"].items()):
        if isinstance(value, str) and value and set(value) <= {"0", "1"}:
            value = f"{len(value)}'h{int(value, 2):x}"
        parameters.append(f"{name}={value}")
    suffix = " (" + ", ".join(parameters) + ")" if parameters else ""
    return (module["hdl_name"] + suffix)[:256]


def _route_module(mapped, adapter, path, record_dir, options, label, advance, *, refiner=None, baseline_preview=None):
    advance("routing")
    if options["layout_strategy"] == "reserved":
        base = layout_reserved(mapped, spacing=options["spacing"])
    else:
        base = layout_general(mapped, adapter, spacing=options["spacing"], timeout=options["layout_timeout"])
    layout = add_long_tunnels(base, mapped, adapter) if options["tunnels"] else base
    attempts = []
    while True:
        circuit = {**layout["circuit"], "label": label}
        intent = layout.get("validation_mapping", mapped)
        _write(record_dir / "candidate.json", circuit)
        _write(record_dir / "routing-mapped.json", intent)
        advance("validating_connections")
        emitted = adapter.call("circuit.emit", {"circuit": circuit, "path": str(path)})
        paths = {key: emitted[key] for key in ("path", "sidecar_path")}
        hashes = {Path(paths["path"]): emitted["sha256"],
                  Path(paths["sidecar_path"]): emitted["sidecar_sha256"]}
        if any(_hash(file) != digest for file, digest in hashes.items()):
            raise ConversionError("ARTIFACT_CHANGED", "An emitted specialization changed before validation")
        validation, inspection = validate_export(adapter, intent, circuit, paths)
        if any(_hash(file) != digest for file, digest in hashes.items()):
            raise ConversionError("ARTIFACT_CHANGED", "An emitted specialization changed during validation")
        _write(record_dir / "inspection.json", inspection)
        _write(record_dir / "validation.json", validation)
        attempts.append({"strategy": layout["strategy"], "tunnels": layout.get("tunnels", {}).get("nets", []),
                         "validation": validation})
        _write(record_dir / "layout-attempts.json", attempts)
        if validation["valid"]:
            record = {"layout": {**layout, "circuit": circuit}, "mapped": intent, "inspection": inspection,
                      "validation": validation, "emitted": emitted, "paths": paths, "attempts": attempts}
            if refiner is not None and refiner.enabled:
                if baseline_preview is not None:
                    record["baseline_preview_metadata"] = render_preview(adapter, paths, baseline_preview,
                        inspection=inspection, emitted=emitted, scale=_preview_scale(circuit, inspection["instances"]),
                        module_id=mapped["module_id"], comparison_scope="top_after_child_refinement_before_top_refinement",
                        artifact="baseline_preview")
                from .llm.loop import install_candidate
                selected = refiner.refine(intent, record, package=adapter.context())
                record = {**record, **install_candidate(selected, path)}
                record["validation"]["refinement"] = record["refinement"]
                _write(record_dir / "inspection.json", record["inspection"])
                _write(record_dir / "validation.json", record["validation"])
            return record
        if len(attempts) >= 3:
            break
        if layout.get("tunnels", {}).get("nets"):
            layout = {**base, "fallback_used": True, "fallback_reason": "native_tunnel_validation"}
        elif layout["strategy"] not in RESERVED_STRATEGIES:
            layout = {**layout_reserved(mapped, spacing=options["spacing"]), "fallback_used": True,
                      "fallback_reason": "native_layout_validation"}
        else:
            break
        advance("routing")
    raise ConversionError("CIRCUIT_VALIDATION", f"Native specialization {label} failed component, bit, connection, model or geometry checks")


def _check_package(directory, snapshot):
    result = verify_package_snapshot(directory, snapshot)
    if not result["valid"]:
        raise ConversionError("ARTIFACT_CHANGED", "A validated hierarchy circuit or identity sidecar changed")


def convert_prepared_hierarchy(project, hierarchy, destination, work, *, adapter, sources, options,
                               advance, started, timings, progress_warnings):
    """Complete a prepared conversion inside the caller's failure/progress scope."""
    root = repository_root()
    design = hierarchy["expanded_design"]
    sequential = any(cell["type"] == "$dff" for cell in design["cells"])
    plan = None
    if sequential:
        advance("validating_initialization")
        plan = sequential_stimulus_plan(design, seed=options["seed"], random_count=options["random_count"],
                                       trace=options["sequential_trace"])
        _write(work / "trace.json", plan["trace"])
        _write(work / "initialization.json", plan["initialization"])
    elif options["sequential_trace"] is not None:
        raise ConversionError("UNEXPECTED_TRACE", "A sequential trace requires a design with registers")
    package_root = work / "package"
    package_root.mkdir()
    module_records = work / "modules"
    module_records.mkdir()
    mappings, records, catalog, files = {}, {}, {}, []
    top_id = hierarchy["top_module_id"]
    advance("starting_adapter")
    from .llm.loop import RefinementSession
    with (nullcontext(adapter) if adapter is not None else Adapter()) as worker, \
            RefinementSession(options.get("refinement"), work / "refinement", progress=advance) as refiner:
        native = PackageAdapter(worker, package_root, catalog)
        if options["profile"] != "auto":
            advance("mapping")
            # Check the electrical profile on the expanded design and each native canvas.
            map_design(design, worker, profile=options["profile"])
        for module in _module_order(hierarchy):
            identifier = module["id"]
            directory = module_records / identifier
            directory.mkdir()
            advance("mapping")
            mapped = map_hierarchy_module(hierarchy, identifier, native, catalog)
            _write(directory / "mapped.json", mapped)
            filename = "circuit.dig" if identifier == top_id else f"{identifier}.dig"
            record = _route_module(mapped, native, package_root / filename, directory,
                                   options, _display_name(module), advance, refiner=refiner,
                                   baseline_preview=work / "baseline-preview.png" if identifier == top_id else None)
            mappings[identifier], records[identifier] = record["mapped"], record
            files.extend([filename, filename + ".ids.json"])
            if identifier != top_id:
                catalog[identifier] = {"type": filename, "sha256": record["emitted"]["sha256"],
                                       "sidecar_sha256": record["emitted"]["sidecar_sha256"]}
        refiner.close()
        expected = compose_hierarchy(hierarchy, mappings)
        limit = 64 if options["profile"] == "scalar-v1" else 250
        if len(expected["expanded_mapping"]["instances"]) > limit:
            raise ConversionError("HIERARCHY_LIMIT", f"The generated hierarchy exceeds {limit} expanded native components including helpers")
        behavior_mapping = expected["behavior_mapping"]
        if sequential and not _initialization_valid(plan["initialization"], behavior_mapping, plan["trace"]):
            raise ConversionError("UNINITIALIZED_STATE", "The initialization proof does not match every physical hierarchy register")
        _write(work / "hierarchy-mapped.json", expected)
        _write(work / "mapped.json", behavior_mapping)
        top = records[top_id]
        advance("validating_connections")
        # Freshly reload the complete package after all dependencies are final.
        inspection = native.call("circuit.inspect", top["paths"])
        electrical = verify_hierarchy(expected, inspection)
        _write(work / "inspection.json", inspection)
        # Compare against emission hashes to detect later dependency changes.
        for record in records.values():
            if (_hash(Path(record["paths"]["path"])) != record["emitted"]["sha256"]
                    or _hash(Path(record["paths"]["sidecar_path"])) != record["emitted"]["sidecar_sha256"]):
                raise ConversionError("ARTIFACT_CHANGED", "A hierarchy artifact changed after emission")
        snapshot = snapshot_package(package_root, files)
        _write(work / "package-binding.json", snapshot)
        geometry = {"valid": all(record["validation"]["geometry"]["valid"] for record in records.values()),
                    "diagnostics": [], "metrics": top["validation"]["geometry"]["metrics"]}
        validation = {**electrical, "geometry": geometry, "refinement": refiner.report,
                      "modules": {key: value["validation"] for key, value in sorted(records.items())},
                      "hierarchy": {"module_count": len(records), "occurrence_count": len(hierarchy["occurrences"]),
                                    "expanded_components": len(expected["expanded_mapping"]["instances"]),
                                    "dependencies": expected["dependencies"]}}
        validation["valid"] = electrical["valid"] and geometry["valid"] and all(
            record["validation"]["valid"] for record in records.values())
        _write(work / "validation.json", validation)
        if not validation["valid"]:
            raise ConversionError("CIRCUIT_VALIDATION", "The expanded native hierarchy differs from its recursively composed electrical intent")
        advance("validating_behavior")
        if sequential:
            trace = plan["trace"]
            reference = simulate_sequential_reference(project, design, work / "reference", trace)
            input_ids = {port["port"]: port["instance_id"] for port in behavior_mapping["inputs"]}
            def rows(cycles):
                return [{"values": {input_ids[name]: hex(value) for name, value in row["values"].items()}} for row in cycles]
            simulation = native.call("circuit.simulate_cycles", {**top["paths"],
                "clock_instance_id": behavior_mapping["clock"]["instance_id"],
                "initialization": {"kind": "input_sequence", "cycles": rows(trace["initialization"]["cycles"])},
                "cycles": rows(trace["cycles"])})
            behavior = compare_sequential_behavior(behavior_mapping, trace, reference, simulation,
                coverage=plan["assumptions"], initialization=plan["initialization"])
        else:
            plan = stimulus_plan(behavior_mapping, seed=options["seed"], random_count=options["random_count"])
            stimuli = plan["stimuli"]
            _write(work / "stimuli.json", stimuli)
            reference = simulate_reference(project, design, work / "reference", stimuli)
            simulation = native.call("circuit.simulate", {**top["paths"], "stimuli": [
                {"values": {port["instance_id"]: hex(values[port["port"]]) for port in behavior_mapping["inputs"]}}
                for values in stimuli]})
            behavior = compare_behavior(behavior_mapping, stimuli, reference, simulation, coverage=plan["assumptions"])
        _write(work / "reference-samples.json", reference)
        _write(work / "digital-samples.json", simulation)
        validation["behavior"] = behavior
        validation["valid"] = validation["valid"] and behavior["valid"]
        _write(work / "validation.json", validation)
        if not validation["valid"]:
            raise ConversionError("BEHAVIOR_VALIDATION", "Native hierarchy behavior does not match the original Verilog for the declared trace")
        _check_package(package_root, snapshot)
        if source_hashes(list(sources)) != sources:
            raise ConversionError("SOURCE_CHANGED", "A source or include changed during hierarchy validation")
        advance("rendering")
        scale = _preview_scale(top["layout"]["circuit"], inspection["instances"])
        native.call("circuit.render", {"path": top["paths"]["path"], "format": "svg",
                    "output_path": str(work / "preview.svg"), "scale": scale})
        selected_preview = render_preview(native, top["paths"], work / "preview.png", inspection=inspection,
                                           emitted=top["emitted"], scale=scale, module_id=top_id)
        baseline_preview = top.get("baseline_preview_metadata")
        write_metadata(work / "preview-metadata.json", design["top"], selected_preview, baseline_preview)
        preview_hashes = {"preview.png": selected_preview["image"]["sha256"],
                          "preview-metadata.json": _hash(work / "preview-metadata.json")}
        if baseline_preview is not None:
            preview_hashes["baseline-preview.png"] = baseline_preview["image"]["sha256"]
        capabilities = native.capabilities
    advance("publishing")
    publishing_started = time.perf_counter()
    if source_hashes(list(sources)) != sources:
        raise ConversionError("SOURCE_CHANGED", "A source or include changed during hierarchy rendering")
    _check_package(package_root, snapshot)
    if any(_hash(work / name) != wanted for name, wanted in preview_hashes.items()):
        raise ConversionError("ARTIFACT_CHANGED", "Preview metadata or image changed before publication")
    for item in snapshot["files"]:
        (package_root / item["path"]).replace(destination / item["path"])
    _check_package(destination, snapshot)
    for name in ("preview.png", "preview.svg", "preview-metadata.json", "validation.json"):
        (work / name).replace(destination / name)
    package = {"schema_version": 1, "root_file": "circuit.dig", "modules": native.context()["modules"],
               "snapshot": snapshot, "occurrences": expected["occurrences"],
               "specializations": [{"module_id": module["id"], "name": module["hdl_name"],
                    "display_name": _display_name(module), "parameters_raw": module["parameters_raw"],
                    "file": "circuit.dig" if module["id"] == top_id else module["id"] + ".dig"}
                    for module in _module_order(hierarchy)]}
    _write(destination / "package.json", package)
    artifacts = {"circuit": destination / "circuit.dig", "sidecar": destination / "circuit.dig.ids.json",
                 "preview": destination / "preview.png", "svg": destination / "preview.svg",
                 "preview_metadata": destination / "preview-metadata.json",
                 "validation": destination / "validation.json", "package": destination / "package.json"}
    if refiner.enabled:
        (work / "baseline-preview.png").replace(destination / "baseline-preview.png")
        artifacts["baseline_preview"] = destination / "baseline-preview.png"
    if any(_hash(destination / name) != wanted for name, wanted in preview_hashes.items()):
        raise ConversionError("ARTIFACT_CHANGED", "Preview metadata or image changed during publication")
    for index, item in enumerate(snapshot["files"]):
        if item["path"] not in {"circuit.dig", "circuit.dig.ids.json"}:
            artifacts[f"dependency_{index:04d}"] = destination / item["path"]
    timings["publishing"] = round(time.perf_counter() - publishing_started, 6)
    manifest = {"schema_version": 1, "state": "ready", "profile": "hierarchy-v1", "top": design["top"],
                "configuration": design["configuration"], "sources": sources,
                "tools": {**design["tools"], **reference["tools"], "digital_revision": capabilities["digital_revision"],
                          "adapter_sha256": _hash(root / "digital-adapter/target/digital-adapter.jar"),
                          "toolchain_lock_sha256": _hash(root / "scripts/toolchain-lock.json")},
                "hierarchy": validation["hierarchy"], "behavior": behavior, "refinement": refiner.report,
                "layout": {"strategy": top["layout"]["strategy"], "spacing": options["spacing"],
                           "requested": options["layout_strategy"], "seed": 1, "preview_scale": scale,
                           "metrics": geometry["metrics"], "fallback_used": any(
                               record["layout"].get("fallback_used", False) for record in records.values()),
                           "modules": {key: {"strategy": value["layout"]["strategy"],
                               "fallback_used": value["layout"].get("fallback_used", False),
                               "fallback_reason": value["layout"].get("fallback_reason"),
                               "metrics": value["validation"]["geometry"]["metrics"],
                               "tunnels": value["layout"].get("tunnels", {"mode": "visible", "nets": []})}
                               for key, value in sorted(records.items())}},
                "timings_seconds": timings, "progress_warnings": list(progress_warnings),
                "duration_seconds": round(time.perf_counter() - started, 6),
                "artifacts": {key: {"path": path.name, "sha256": _hash(path)} for key, path in artifacts.items()}}
    verify_preview_bindings(manifest, {key: (path, manifest["artifacts"][key]["sha256"]) for key, path in artifacts.items()})
    _write(destination / "manifest.json", manifest)
    advance("ready")
    return {"schema_version": 1, "state": "ready", "top": design["top"], "directory": str(destination),
            "progress_warnings": progress_warnings, "manifest": str(destination / "manifest.json"),
            **{key: str(path) for key, path in artifacts.items()}}
