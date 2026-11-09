"""One owned conversion process; stdout contains bounded JSON-RPC notifications."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


def _suppress_crash_dialogs():
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)


def _notify(identifier, method, **params):
    event = {"jsonrpc": "2.0", "method": method, "params": {"job_id": identifier, **params}}
    data = json.dumps(event, ensure_ascii=False, allow_nan=False)
    if len(data.encode("utf-8")) + 1 > 65536:  # The supervisor bound includes the newline.
        raise RuntimeError("Worker notification exceeds 64 KiB")
    print(data, flush=True)


def _bounded(diagnostic):
    """Keep a terminal diagnostic within the 64 KiB notification protocol."""
    message = diagnostic.get("message")
    if isinstance(message, str) and len(message.encode("utf-8")) > 16384:
        text = message.encode("utf-8")[:16384].decode("utf-8", "ignore")
        diagnostic = {**diagnostic, "message": text + " [truncated; see worker.stderr.log]"}
    if len(json.dumps(diagnostic, ensure_ascii=False, default=str).encode("utf-8")) > 32768:
        message = diagnostic.get("message")
        if not isinstance(message, str):
            message = ("" if message is None else str(message)).encode("utf-8")[:16384].decode("utf-8", "ignore")
        diagnostic = {"code": str(diagnostic.get("code", "WORKER_ERROR"))[:128], "message": message}
    return diagnostic


def main(argv=None):
    _suppress_crash_dialogs()  # Before any native library is imported.
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    from ..contracts import validate
    from ..frontend.errors import FrontendError
    from ..frontend.project import Project
    from ..runtime import repository_root, require_venv
    from .store import atomic_json, read_json
    require_venv(repository_root())
    request = read_json(args.request)
    validate("job-request-v2", request)
    identifier = request["job_id"]
    directory = args.request.resolve().parent
    if directory.name != identifier or not str(directory).isascii():
        raise RuntimeError("Owned worker directories must use their ASCII job identity")
    source_map = None
    try:
        if request["kind"] == "validate":
            from ..verification import validate_output
            _notify(identifier, "job.progress", stage="validating")
            report = validate_output(Path(request["options"]["target_path"]), artifacts_only=request["options"]["artifacts_only"])
            atomic_json(directory / "validation.json", report)
            result = {"schema_version": 2, "job_id": identifier, "kind": "validate", "validation": report,
                      "validation_path": str(directory / "validation.json")}
            path = directory / "worker-result.json"
            atomic_json(path, result)
            _notify(identifier, "job.result", result_path=str(path))
            return 0
        from .workspace import inspect_project, restore_error, restore_provenance, snapshot_project, verify_snapshot
        raw = request["project"]
        project = Project([Path(path) for path in raw["sources"]], raw["top"],
                          [Path(path) for path in raw["include_dirs"]], raw["defines"], raw["parameters"])
        _notify(identifier, "job.progress", stage="snapshotting_sources")
        snapshot = snapshot_project(project, directory)
        verify_snapshot(snapshot)
        source_map = snapshot["source_map"]
        source_snapshot = {key: snapshot[key] for key in ("sources", "binding", "source_map")}
        atomic_json(directory / "source-snapshot.json", source_snapshot)
        if request["kind"] == "inspect":
            _notify(identifier, "job.progress", stage="inspecting")
            inspected = restore_provenance(inspect_project(snapshot["project"], discover=True), source_map)
            atomic_json(directory / "inspection.json", inspected)
            result = {"kind": "inspect", "inspection": inspected, "inspection_path": str(directory / "inspection.json")}
        else:
            from ..conversion import convert
            def progress(event):
                stage = {"ready": "candidate_ready", "publishing": "preparing_candidate"}.get(event["stage"], event["stage"])
                _notify(identifier, "job.progress", stage=stage)
            context = read_json(directory / "worker-context.json")
            cached, cache_store, key = None, None, None
            cache_metadata = {"enabled": context["cache"]["enabled"], "hit": False}
            if cache_metadata["enabled"]:
                try:
                    from .cache import CandidateCache, cache_key
                    cache_store = CandidateCache(Path(context["cache"]["root"]))
                    key = cache_key(raw, request["options"], snapshot["sources"], context["runtime_fingerprint"]["sha256"])
                    cached = cache_store.restore(key, directory / "candidate")
                    cache_metadata.update(key=key, hit=cached is not None)
                except Exception as error:
                    cache_metadata["diagnostic"] = str(error)[:1000]
                    cache_store = None
            if cached is not None:
                _notify(identifier, "job.progress", stage="restoring_cached_candidate")
            else:
                cached = convert(snapshot["project"], directory / "candidate", progress=progress, **request["options"])
            verify_snapshot(snapshot)
            if cache_store is not None and not cache_metadata["hit"]:
                try:
                    from ..environment import runtime_fingerprint
                    if runtime_fingerprint()["sha256"] == context["runtime_fingerprint"]["sha256"]:
                        cache_metadata["stored"] = cache_store.store(key, directory / "candidate", source_snapshot=source_snapshot)
                    else:
                        cache_metadata["stored"] = False
                        cache_metadata["diagnostic"] = "Runtime changed before cache storage"
                except Exception as error:
                    cache_metadata["diagnostic"] = str(error)[:1000]
            result = {"kind": "convert", "candidate": cached, "cache": cache_metadata}
        verify_snapshot(snapshot)
        result.update(schema_version=2, job_id=identifier, source_snapshot=source_snapshot)
        path = directory / "worker-result.json"
        atomic_json(path, result)
        _notify(identifier, "job.result", result_path=str(path))
        return 0
    except Exception as error:
        if source_map is not None and isinstance(error, FrontendError):
            error = restore_error(error, source_map)
        diagnostic = _bounded(error.as_dict() if isinstance(error, FrontendError) else {
            "code": "WORKER_MEMORY" if isinstance(error, MemoryError) else "WORKER_ERROR", "message": str(error) or type(error).__name__})
        traceback.print_exc(file=sys.stderr)
        atomic_json(directory / "worker-error.json", {"diagnostic": diagnostic})
        _notify(identifier, "job.error", diagnostic=diagnostic)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
