"""Check a saved result's freshness and reopen its current native model."""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path

from .environment import runtime_fingerprint
from .frontend.errors import FrontendError
from .jobs.publication import (bundle_bindings, contained_file, current_revision, digest, read_document,
                               verify_sources)
from .runtime import repository_root


def result_directory(path: Path) -> Path:
    path = Path(path).resolve(strict=True)
    if path.is_file():
        if path.name not in {"manifest.json", "current.json"}:
            raise FrontendError("INVALID_RESULT", "Select a result directory, manifest.json or current.json")
        path = path.parent
    current, _ = current_revision(path)
    if current:
        return contained_file(path, current["revision"] + "/manifest.json").parent
    if not (path / "manifest.json").is_file():
        raise FrontendError("NO_READY_RESULT", "This directory has no selected or completed revision", path=str(path))
    return path


def _recorded_model_bindings(refinement):
    """Retain up to two verified bindings across the memory retry.

    A retry that times out before verification must not erase the prior binding.
    """
    if not isinstance(refinement, dict):
        raise FrontendError("TOOL_BINDING", "The recorded refinement metadata is malformed")
    retired = refinement.get("retired_runtimes", [])
    if not isinstance(retired, list) or len(retired) > 1:
        raise FrontendError("TOOL_BINDING", "Refinement may record at most one retired model runtime")
    records = ([refinement["runtime"]] if "runtime" in refinement else []) + retired
    bindings, seen = [], set()
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("profile"), dict)
                or not isinstance(record["profile"].get("backend"), str)
                or record["profile"].get("backend") not in {"cpu", "cuda"}):
            raise FrontendError("TOOL_BINDING", "A recorded model runtime has no valid profile")
        if "model" not in record and "runtime" not in record:
            continue  # Startup ended before verifying either artifact family.
        model, runtime = record.get("model"), record.get("runtime")
        if (not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]
                or not isinstance(runtime, dict) or not runtime
                or "model_id" in record["profile"] and record["profile"]["model_id"] != model["id"]):
            raise FrontendError("TOOL_BINDING", "A recorded model/runtime artifact binding is incomplete")
        binding = {"model": model, "runtime": runtime, "backend": record["profile"]["backend"]}
        try:
            key = json.dumps(binding, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as error:
            raise FrontendError("TOOL_BINDING", "A recorded model/runtime binding is malformed") from error
        if key not in seen:
            seen.add(key)
            bindings.append(binding)
    if refinement.get("mode") == "local" and refinement.get("accepted", 0) and not bindings:
        raise FrontendError("TOOL_BINDING", "An accepted local-model refinement has no artifact binding")
    return bindings


def validate_output(path: Path, *, artifacts_only: bool = False, adapter=None) -> dict:
    """Hash freshness is separate from the already recorded behavior coverage."""
    checks, diagnostics, directory, manifest = {}, [], None, None
    def failed(name, error):
        value = error.as_dict() if isinstance(error, FrontendError) else {"code": "VALIDATION_FAILED", "message": str(error)}
        checks[name] = {"valid": False, "diagnostics": [value]}
        diagnostics.append({"check": name, **value})
    try:
        directory = result_directory(path)
        manifest_hash = digest(directory / "manifest.json")
        manifest = read_document(directory / "manifest.json")
        bindings = bundle_bindings(directory, manifest)
        report = read_document(bindings["validation"][0])
        checks["artifacts"] = {"valid": True, "files": len(bindings) + 1, "diagnostics": []}
    except (OSError, ValueError, RuntimeError) as error:
        failed("artifacts", error)
        return {"schema_version": 2, "state": "failed", "valid": False, "checks": checks, "diagnostics": diagnostics}
    if not artifacts_only:
        try:
            verify_sources(manifest["sources"])
            checks["sources"] = {"valid": True, "files": len(manifest["sources"]), "diagnostics": []}
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            failed("sources", error)
        try:
            recorded = manifest.get("job", {}).get("runtime_fingerprint")
            if recorded is not None:
                if not isinstance(recorded, dict) or not isinstance(recorded.get("sha256"), str):
                    raise FrontendError("TOOL_BINDING", "The recorded runtime fingerprint is malformed")
                current = runtime_fingerprint()
                if current["sha256"] != recorded.get("sha256"):
                    raise FrontendError("TOOL_CHANGED", "Runtime dependencies or converter code changed since this result was created")
                scope = "runtime_and_converter"
            elif manifest["schema_version"] == 1:
                root = repository_root()
                for field, file in {"adapter_sha256": root / "digital-adapter/target/digital-adapter.jar",
                                    "toolchain_lock_sha256": root / "scripts/toolchain-lock.json"}.items():
                    if digest(file) != manifest["tools"][field]:
                        raise FrontendError("TOOL_CHANGED", "A dependency recorded by the legacy manifest has changed")
                scope = "legacy_recorded_tools"
            else:
                raise FrontendError("TOOL_BINDING", "The job manifest has no runtime fingerprint")
            checks["runtime"] = {"valid": True, "scope": scope, "diagnostics": []}
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            failed("runtime", error)
        try:
            model_bindings = _recorded_model_bindings(manifest.get("refinement", {}))
            if model_bindings:
                from .llm.artifacts import verify_installation
                for binding in model_bindings:
                    actual = verify_installation(binding["model"]["id"], binding["backend"])
                    if actual["model"] != binding["model"] or actual["runtime"] != binding["runtime"]:
                        raise FrontendError("TOOL_CHANGED", "A local model or inference runtime differs from the recorded run")
                checks["model_runtime"] = {"valid": True, "scope": "pinned_model_and_runtime_files",
                                           "bindings": len(model_bindings), "diagnostics": []}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            failed("model_runtime", error)
    from .adapter import Adapter
    try:
        params = {"path": str(bindings["circuit"][0]), "sidecar_path": str(bindings["sidecar"][0])}
        if "package" in bindings:
            package = read_document(bindings["package"][0])
            from .validation.hierarchy import verify_package_snapshot
            binding = verify_package_snapshot(directory, package["snapshot"])
            if not binding["valid"]:
                raise FrontendError("PACKAGE_CHANGED", "A bound child circuit or identity sidecar changed")
            params["package"] = {"root": str(directory), "modules": package["modules"]}
        with (nullcontext(adapter) if adapter is not None else Adapter()) as native:
            inspected = native.call("circuit.inspect", params)
        model = inspected.get("expanded", inspected)["model"]
        checks["native_model"] = model
        if not model["valid"]:
            diagnostics.extend({"check": "native_model", **item} for item in model["diagnostics"])
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        failed("native_model", error)
    try:
        bundle_bindings(directory, manifest)
        if digest(directory / "manifest.json") != manifest_hash:
            raise FrontendError("ARTIFACT_CHANGED", "The result manifest changed during validation")
        if not artifacts_only and checks["sources"]["valid"]:
            verify_sources(manifest["sources"])
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        failed("final_binding", error)
    valid = all(check["valid"] for check in checks.values())
    return {"schema_version": 2, "state": "ready" if valid else "failed", "valid": valid,
            "directory": str(directory), "scope": "artifacts_and_native_model" if artifacts_only else "result_freshness_and_native_model",
            "checks": checks, "diagnostics": diagnostics,
            "recorded_behavior": report.get("behavior"),
            "behavior_replayed": False}
