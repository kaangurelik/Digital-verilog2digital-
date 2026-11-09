"""Validated result operations shared by the desktop and CLI.

Native validation uses the managed worker. Opening Digital returns a launch
plan for the desktop to execute.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import uuid

from .frontend.errors import FrontendError
from .jobs.publication import (MAX_BUNDLE, MAX_DOCUMENT, bundle_bindings,
                               contained_file, digest, read_document)
from .runtime import development_toolchain, repository_root
from .verification import result_directory


def _same_manifest(directory, expected):
    if digest(directory / "manifest.json") != expected:
        raise FrontendError("ARTIFACT_CHANGED", "The result changed while it was being opened")


def _summary(manifest, report, validation):
    refinement = manifest.get("refinement", {})
    layout = manifest.get("layout", {})
    if (not isinstance(refinement, dict) or not isinstance(layout, dict)
            or not isinstance(refinement.get("diagnostics", []), list)
            or not isinstance(refinement.get("canvases", []), list)):
        raise FrontendError("INVALID_RESULT", "The saved layout or refinement summary is malformed")
    # Send concise diagnostics; full model attempts remain in the report.
    diagnostics, seen, attempted = [], set(), 0
    def diagnostic(item):
        if not isinstance(item, dict) or len(diagnostics) >= 16:
            return
        code, message = item.get("code"), item.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            return
        key = (code[:80], message[:2000])
        if key not in seen:
            seen.add(key)
            diagnostics.append({"code": key[0], "message": key[1]})
    for item in refinement.get("diagnostics", [])[:16]:
        diagnostic(item)
    for canvas in refinement.get("canvases", [])[:128]:
        if not isinstance(canvas, dict) or not isinstance(canvas.get("attempts", []), list):
            raise FrontendError("INVALID_RESULT", "The saved refinement attempts are malformed")
        for attempt in canvas.get("attempts", [])[:8]:
            if isinstance(attempt, dict):
                attempted += 1
                diagnostic(attempt.get("diagnostic"))
    return {
        "native_model_valid": validation["checks"]["native_model"]["valid"],
        "artifact_binding_valid": validation["checks"]["artifacts"]["valid"],
        "behavior_replayed": False,
        "recorded_behavior": report.get("behavior"),
        "layout_metrics": layout.get("metrics", {}),
        "refinement": {**{key: refinement[key] for key in (
            "mode", "state", "accepted", "duration_seconds") if key in refinement},
            "attempted": attempted, "diagnostics": diagnostics},
    }


def checked_result(path, *, limits=None, progress=None):
    from .jobs.manager import run_validation
    directory = result_directory(Path(path))
    manifest_hash = digest(directory / "manifest.json")
    manifest = read_document(directory / "manifest.json")
    bundle_bindings(directory, manifest)
    validation_run = run_validation(directory, artifacts_only=True, limits=limits, progress=progress)
    validation = validation_run.get("validation", {})
    if (not isinstance(validation, dict) or validation.get("valid") is not True
            or not isinstance(validation.get("checks"), dict)
            or any(not isinstance(validation["checks"].get(key), dict)
                   or validation["checks"][key].get("valid") is not True for key in ("artifacts", "native_model"))):
        if not isinstance(validation, dict):
            validation = {}
        detail = next(iter(validation.get("diagnostics", [])), {}).get("message", "")
        raise FrontendError("INVALID_RESULT", "The saved result failed its artifact or native circuit check. " + detail[:2000])
    _same_manifest(directory, manifest_hash)
    bindings = bundle_bindings(directory, manifest)
    report = read_document(bindings["validation"][0])
    result = {
        "schema_version": 1, "state": "ready", "directory": str(directory),
        "manifest_path": str(directory / "manifest.json"), "manifest_sha256": manifest_hash,
        "manifest": {key: deepcopy(manifest[key]) for key in ("schema_version", "state", "top", "artifacts")},
        "summary": _summary(manifest, report, validation),
        "metadata_path": str(bindings["preview_metadata"][0]) if "preview_metadata" in bindings else None,
        "job_manifest": validation_run["job_manifest"],
    }
    # Keep full behavior traces in the bundle to bound GUI messages.
    import json
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 512 * 1024:
        result["summary"]["recorded_behavior"] = {
            "valid": report.get("behavior", {}).get("valid"),
            "details_in_validation_artifact": True}
    _same_manifest(directory, manifest_hash)
    return result


def export_result(path, destination, *, limits=None, progress=None):
    checked = checked_result(path, limits=limits, progress=progress)
    source = Path(checked["directory"])
    destination = Path(destination).absolute()
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise FrontendError("EXPORT_EXISTS", "Choose a new folder name for the exported circuit package")
    if destination == source or destination.is_relative_to(source):
        raise FrontendError("EXPORT_PATH", "Export outside the source result directory")
    temporary = parent / (".v2d-export-" + uuid.uuid4().hex)
    temporary.mkdir(exist_ok=False)
    try:
        _same_manifest(source, checked["manifest_sha256"])
        manifest = read_document(source / "manifest.json")
        bindings = bundle_bindings(source, manifest)
        copied_bytes = 0
        for label, (file, expected) in bindings.items():
            size = file.stat().st_size
            copied_bytes += size
            if copied_bytes > MAX_BUNDLE:
                raise FrontendError("BUNDLE_LIMIT", "The result grew beyond the bounded export size")
            target = contained_file(temporary, manifest["artifacts"][label]["path"], must_exist=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            with file.open("rb") as reader, target.open("xb") as writer:
                remaining = size
                while remaining:
                    block = reader.read(min(1024 * 1024, remaining))
                    if not block:
                        raise FrontendError("ARTIFACT_CHANGED", "A result artifact became shorter during export")
                    writer.write(block)
                    remaining -= len(block)
                if reader.read(1):
                    raise FrontendError("ARTIFACT_CHANGED", "A result artifact grew during export")
                writer.flush()
                os.fsync(writer.fileno())
            if digest(target) != expected:
                raise FrontendError("ARTIFACT_CHANGED", "A result artifact changed during export")
        with (source / "manifest.json").open("rb") as reader, (temporary / "manifest.json").open("xb") as writer:
            writer.write(reader.read(MAX_DOCUMENT + 1))
            writer.flush()
            os.fsync(writer.fileno())
        _same_manifest(source, checked["manifest_sha256"])
        _same_manifest(temporary, checked["manifest_sha256"])
        bundle_bindings(source, manifest)
        bundle_bindings(temporary, manifest)
        # Windows rename rejects existing destinations, preserving prior exports.
        temporary.rename(destination)
        return {"schema_version": 1, "state": "ready", "directory": str(destination),
                "manifest_sha256": checked["manifest_sha256"], "files": len(bindings) + 1,
                "circuit": str(contained_file(destination, manifest["artifacts"]["circuit"]["path"]))}
    finally:
        if temporary.exists():
            resolved = temporary.resolve(strict=True)
            if not temporary.is_symlink() and resolved.parent == parent and resolved.name.startswith(".v2d-export-"):
                shutil.rmtree(resolved)


def digital_launch_plan(path, *, limits=None, progress=None):
    checked = checked_result(path, limits=limits, progress=progress)
    root = repository_root().resolve(strict=True)
    java = Path(development_toolchain(root).executable("java")).with_name("javaw.exe")
    from .release import native_asset
    jar = native_asset(root, ".runs/digital-build/Digital.jar")
    library = native_asset(root, "src/main/dig/lib")
    if not java.is_file() or not jar.is_file() or not library.is_dir():
        raise FrontendError("DIGITAL_MISSING", "Build or install Digital before opening the selected circuit")
    circuit = contained_file(Path(checked["directory"]), checked["manifest"]["artifacts"]["circuit"]["path"])
    directory = Path(checked["directory"])
    relative_circuit = circuit.relative_to(directory).as_posix()
    if not relative_circuit.isascii():
        raise FrontendError("DIGITAL_ARGUMENT_PATH", "Digital's Java launcher requires an ASCII circuit filename; generated packages use circuit.dig")
    _same_manifest(directory, checked["manifest_sha256"])
    bundle_bindings(directory, read_document(directory / "manifest.json"))
    _same_manifest(directory, checked["manifest_sha256"])
    return {"schema_version": 1, "state": "ready", "argv": [str(java),
            "-Ddigital.library.path=" + str(library), "-jar", str(jar), relative_circuit],
            "cwd": checked["directory"], "manifest_sha256": checked["manifest_sha256"]}
