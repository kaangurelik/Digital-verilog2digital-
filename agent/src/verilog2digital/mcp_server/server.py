"""Expose the verified converter to MCP hosts over stdio; the host's model drives it."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

import anyio
import anyio.to_thread
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resources import FunctionResource
from mcp.server.mcpserver.tools import Tool
from mcp_types import CallToolResult, ImageContent, ToolAnnotations

from .. import __version__
from ..contracts import ContractError, validate
from ..frontend.errors import FrontendError
from ..jobs.publication import bundle_bindings, current_revision, digest, read_document
from ..runtime import repository_root
from . import results, schemas
from .policy import PathPolicy
from .refinement import IDLE_SECONDS, MAX_SESSION_SECONDS, MAX_SESSIONS, Refinement, Scratch
from .runner import JobRunner, capped


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_SIDE = 8000
MAX_RESOURCE_BYTES = 256 * 1024
MAX_INSTANCES = 128
# Folders that the release integrity check or the runtime fingerprint covers; writing there breaks the installation.
INSTALLATION = ("agent/src/verilog2digital", "schemas", "scripts", "gui/build", "digital-adapter/target", "src/main/dig/lib",
                ".venv", ".tools")
RESOURCES = (
    ("v2d://docs/user-guide", "USER_GUIDE.md", "User guide", "Supported Verilog subset, sequential traces and limits."),
    ("v2d://docs/scalar-conversion", "docs/scalar-conversion.md", "Scalar conversion", "One-bit logic profile, options and checks."),
    ("v2d://docs/datapath-conversion", "docs/datapath-conversion.md", "Datapath conversion", "Buses, arithmetic and multiplexers."),
    ("v2d://docs/sequential-conversion", "docs/sequential-conversion.md", "Sequential conversion", "Registers, clocks, initialization and traces."),
    ("v2d://docs/hierarchy", "docs/hierarchy.md", "Hierarchy", "Reusable native subcircuits and flattening."),
    ("v2d://docs/local-model", "docs/local-model.md", "Layout refinement rules", "Action bounds, the Pareto acceptance rule and replay."),
    ("v2d://docs/mcp", "docs/mcp.md", "MCP server", "Tools, path policy, refinement workflow and limits."),
    ("v2d://schemas/layout-actions", "schemas/layout-actions.schema.json", "Layout action schema",
     "JSON schema of one refinement proposal."),
)
logger = logging.getLogger("verilog2digital.mcp")


def _png(path):
    from ..preview import png_dimensions
    return png_dimensions(path)


def _private(job_root):
    """Server-owned storage and the installation: job records, the candidate cache beside them and program folders."""
    root = repository_root()
    paths = [job_root, job_root.parent / "cache", *(root / name for name in INSTALLATION)]
    mirror = os.environ.get("V2D_NATIVE_TOOLS_ROOT")
    if mirror and Path(mirror).is_absolute():
        paths.append(Path(mirror))
    return paths


class ServerState:
    """Shared server state: path policy, serialized jobs and refinement sessions."""

    def __init__(self, allow_dirs=(), *, deadline=None, memory_mib=None, java_heap_mib=None):
        from ..cli import _limits
        from ..jobs.manager import _store_root
        self.flags = argparse.Namespace(deadline=deadline, memory_mib=memory_mib, java_heap_mib=java_heap_mib)
        # The flags, or the defaults when a flag is omitted, are the ceiling of every job this server runs.
        self.limits = _limits(self.flags)
        self.limits.validate()
        self.job_root = _store_root()
        self.policy = PathPolicy(list(allow_dirs), private=_private(self.job_root))
        self.runner = JobRunner()
        self.scratch = Scratch(self.job_root)
        explicit = {"deadline_seconds": deadline, "memory_bytes": None if memory_mib is None else memory_mib * 1024**2,
                    "java_heap_bytes": None if java_heap_mib is None else java_heap_mib * 1024**2}
        self.refinement = Refinement(self.runner, self.policy, self.scratch, explicit, self.limits, self._preflight)
        self.definitions = schemas.tools()
        self.validators = {item[0]: Draft202012Validator(schemas.argument_schema(item[3])) for item in self.definitions}
        self.handlers = {"doctor": self.doctor, "inspect_design": self.inspect_design, "convert_design": self.convert_design,
                         "validate_result": self.validate_result, "get_result": self.get_result, "get_preview": self.get_preview,
                         "job_status": self.job_status, "cancel_job": self.cancel_job, "refinement_start": self.refinement_start,
                         "refinement_propose": self.refinement_propose, "refinement_finish": self.refinement_finish}

    def close(self):
        try:
            self.refinement.close()
        finally:
            self.scratch.close()

    async def call(self, name, ctx):
        params = ctx.request_context.params or {}
        arguments = params.get("arguments")
        arguments = {} if arguments is None else arguments
        try:
            self.refinement.sweep()
            if not isinstance(arguments, dict):
                raise FrontendError("INVALID_ARGUMENTS", "Tool arguments must be a JSON object")
            arguments = _decoded(arguments, self.validators[name].schema)
            error = best_match(self.validators[name].iter_errors(arguments))
            if error is not None:
                location = "/".join(str(part) for part in error.absolute_path) or "arguments"
                raise FrontendError("INVALID_ARGUMENTS", f"{location}: {results.text(error.message, 500)}")
            document = await self.handlers[name](ctx, arguments)
            return document if isinstance(document, CallToolResult) else results.success(document)
        except anyio.get_cancelled_exc_class():
            raise
        except FrontendError as error:
            return results.failure(error, self.policy)
        except ContractError as error:
            return results.failure(FrontendError("INVALID_INPUT", results.text(str(error))), self.policy)
        except OSError as error:
            return results.failure(FrontendError("FILE_ERROR", f"{type(error).__name__}: {results.text(str(error), 500)}"), self.policy)
        except Exception as error:
            code = getattr(error, "code", None)
            if isinstance(code, str) and code.isupper():
                return results.failure(FrontendError(code, results.text(str(error))), self.policy)
            logger.exception("Tool %s failed unexpectedly", name)
            return results.failure(FrontendError("SERVER_ERROR", f"Unexpected {type(error).__name__}; details are in the server log"))

    # Shared helpers

    def _job_limits(self, resources=None, notes=None):
        from ..cli import _limits
        requested = _limits(self.flags, resources)
        limits = capped(requested, self.limits)
        if notes is not None and limits != requested:
            notes.append({"code": "PROJECT_RESOURCES_CAPPED", "message": "The project file's resources exceed this server's "
                          "job limits (doctor default_limits), so the server's limits apply; restart the server with larger "
                          "--deadline, --memory-mib or --java-heap-mib to allow more."})
        return limits

    def _project_file(self, value, *, output):
        """Check every path the project names that the call will use; load_project always reads the trace."""
        path = self.policy.resolve(value, "project file")
        document = read_document(path, limit=1024 * 1024)
        validate("project", document)
        base = path.parent
        for item in document["sources"]:
            self.policy.resolve(str(base / item), "project source file")
        for item in document.get("include_dirs", []):
            self.policy.resolve(str(base / item), "project include directory", kind="directory")
        if "trace" in document:
            self.policy.resolve(str(base / document["trace"]), "project trace file")
        if output and "output" in document:
            self.policy.resolve(str(base / document["output"]), "project output folder", kind="output", write=True)
        return path

    def _project(self, arguments, *, conversion):
        """Apply the command line's project merging and option bounds to checked paths."""
        from ..cli import _project_and_options
        if ("sources" in arguments) == ("project" in arguments):
            raise FrontendError("SOURCE_OR_PROJECT", "Provide either sources (Verilog files in compilation order) or project (a v2d project JSON file)")
        namespace = argparse.Namespace(
            sources=[self.policy.resolve(item, "source file") for item in arguments.get("sources", [])],
            project=self._project_file(arguments["project"], output=conversion and "output" not in arguments)
            if "project" in arguments else None,
            top=arguments.get("top"),
            include=[self.policy.resolve(item, "include directory", kind="directory") for item in arguments.get("include_dirs", [])],
            define=[f"{name}={value}" for name, value in arguments.get("defines", {}).items()],
            parameter=[f"{name}={value}" for name, value in arguments.get("parameters", {}).items()])
        if conversion:
            namespace.output = (self.policy.resolve(arguments["output"], "output folder", kind="output", write=True)
                                if "output" in arguments else None)
            namespace.trace = self.policy.resolve(arguments["trace"], "trace file") if "trace" in arguments else None
            for argument in ("spacing", "profile", "seed", "random_vectors", "layout", "tunnels", "layout_timeout", "flatten"):
                setattr(namespace, argument, arguments.get(argument))
        return _project_and_options(namespace)

    def _snapshot(self, job_dir):
        """A job's source snapshot record, or None when snapshotting did not finish."""
        from ..jobs.manager import job_status
        try:
            found = job_status(job_dir).get("source_snapshot")
            if not isinstance(found, dict) or not isinstance(found.get("sources"), dict):
                path = Path(job_dir) / "source-snapshot.json"
                found = read_document(path) if path.is_file() else None
        except (OSError, FrontendError, ValueError):
            return None
        return found if isinstance(found, dict) and isinstance(found.get("sources"), dict) else None

    def _read_sources(self, job_dir):
        """The original files a job snapshotted, or None when snapshotting did not finish."""
        found = self._snapshot(job_dir)
        return found["sources"] if found else None

    def _confine(self, job_manifest, role):
        """Refuse results derived from files outside the allowed directories, including include uses."""
        if job_manifest:
            found = self._read_sources(Path(job_manifest).parent)
            if found:
                self.policy.require(found, role)

    def _hdl_job(self, function, role):
        def run(*args):
            from ..jobs.manager import JobError
            try:
                result = function(*args)
            except JobError as error:
                self._confine(error.job_manifest, role)
                raise
            self._confine(result.get("job_manifest"), role)
            return result
        return run

    def _preflight(self, project, limits, progress, cancel):
        """Inspect the sources before any tool runs; every file the inspection reads must be inside the allowed directories.

        The inspection, Yosys and the Icarus reference preprocess one identical design, so these are the files every
        later tool compiles. Returns the inspection and the files it read with their hashes; runs inside the job runner."""
        from ..jobs import manager

        def relabel(event):
            if isinstance(event, dict):
                event = {**event, "stage": f"preflight_{event.get('stage') or event.get('state') or 'running'}"}
            progress(event)
        result = self._hdl_job(lambda: manager.run_inspection(project, limits=limits, progress=relabel, cancel_event=cancel),
                               "included or source file")()
        snapshot = self._snapshot(Path(result["job_manifest"]).parent) or {}
        return result["inspection"], dict(snapshot.get("sources") or {})

    def _bundle(self, value):
        from ..verification import result_directory
        path = self.policy.resolve(value, "result path", kind="any")
        directory = result_directory(path)
        self.policy.require([directory], "result directory")
        manifest_sha = digest(directory / "manifest.json")
        manifest = read_document(directory / "manifest.json")
        bindings = bundle_bindings(directory, manifest)
        if digest(directory / "manifest.json") != manifest_sha:
            raise FrontendError("ARTIFACT_CHANGED", "The result manifest changed while it was read", path=str(directory))
        return path, directory, manifest, manifest_sha, bindings

    def _selected(self, directory):
        try:
            if directory.parent.name != "revisions":
                return None
            current, _ = current_revision(directory.parent.parent)
            return current is not None and current["job_id"] == directory.name
        except (OSError, FrontendError):
            return None

    def _image(self, bindings, label):
        """Hash-checked PNG content and its pixel-mapped metadata, within fixed size bounds."""
        if label not in bindings:
            raise FrontendError("PREVIEW_UNAVAILABLE", "This result has no before_refinement preview; it exists only when layout "
                                "refinement ran" if label == "baseline_preview" else "This result has no preview image")
        file, sha = bindings[label]
        size = file.stat().st_size
        width, height = _png(file)
        document = {"path": str(file), "bytes": size, "sha256": sha, "width_px": width, "height_px": height}
        image = None
        if size > MAX_IMAGE_BYTES or max(width, height) > MAX_IMAGE_SIDE:
            document.update(image_included=False, image_omitted_reason=f"The PNG exceeds the {MAX_IMAGE_BYTES}-byte or "
                            f"{MAX_IMAGE_SIDE}-pixel embedding bound; open the file at path instead")
        else:
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise FrontendError("ARTIFACT_CHANGED", "The preview changed after it was verified", path=str(file))
            image = ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png")
            document["image_included"] = True
        if "preview_metadata" in bindings:
            metadata = read_document(bindings["preview_metadata"][0])
            view = metadata.get("views", {}).get("selected" if label == "preview" else "baseline")
            if isinstance(view, dict):
                transform = results.scalars(view.get("circuit_to_pixel"), 8)
                a, d, tx, ty = (transform.get(key, 0) for key in ("a", "d", "tx", "ty"))
                instances = []
                for item in view.get("instances", [])[:MAX_INSTANCES]:
                    low, high = item["bounds"]["min"], item["bounds"]["max"]
                    instances.append({"id": results.text(item.get("id"), 128), "type": results.text(item.get("type"), 64),
                                      "label": results.text(item.get("label"), 80),
                                      "bounds": [low["x"], low["y"], high["x"], high["y"]],
                                      "pixel_bounds": [round(a * low["x"] + tx), round(d * low["y"] + ty),
                                                       round(a * high["x"] + tx), round(d * high["y"] + ty)]})
                document["metadata"] = {"comparison_scope": results.text(view.get("comparison_scope"), 64),
                                        "module_id": view.get("module_id"), "circuit_to_pixel": transform,
                                        "instances": instances,
                                        "instances_omitted": max(0, len(view.get("instances", [])) - MAX_INSTANCES)}
        return document, image

    def _published(self, result, manifest, report):
        """The model-facing summary of a selected revision."""
        return {"state": "ready", "job_id": result["job_id"], "top": results.text(result.get("top") or manifest.get("top"), 256),
                "circuit": result["circuit"], "revision": result["revision"], "output_root": result["output_root"],
                "manifest": result["manifest"], "profile": results.text(manifest.get("profile"), 32),
                "preview": {"selected": "preview" in result, "before_refinement": "baseline_preview" in result,
                            "path": result.get("preview")},
                "validation": results.validation(report, self.policy), "layout": results.layout(manifest),
                "notice": results.UNTRUSTED}

    # Tools

    async def doctor(self, ctx, arguments):
        from ..environment import doctor
        report = await anyio.to_thread.run_sync(doctor)
        tools = {}
        for name, item in (report.get("tools") or {}).items():
            item = item if isinstance(item, dict) else {}
            tools[str(name)[:32]] = {"available": item.get("available") is True,
                                     **({"version": results.text(item["version"], 200)} if item.get("version") else {}),
                                     **({"error": results.text(item["error"], 500)} if item.get("error") else {})}
        memory = report.get("memory") if isinstance(report.get("memory"), dict) else {}
        limits = self.limits
        return {"state": report.get("state", "incomplete"), "tools": tools,
                "memory": {"system": results.scalars(memory.get("system")),
                           "gpus": [results.scalars(item) for item in (memory.get("gpus") or [])[:8]]},
                "runtime_fingerprint_sha256": (report.get("fingerprint") or {}).get("sha256"),
                "paths": self.policy.describe(), "job_root": str(self.job_root),
                "default_limits": {"deadline_seconds": limits.deadline_seconds, "memory_mib": limits.memory_bytes // 1024**2,
                                   "java_heap_mib": limits.java_heap_bytes // 1024**2},
                "refinement_sessions": {"open": len(self.refinement.sessions), "max": MAX_SESSIONS,
                                        "idle_expiry_seconds": IDLE_SECONDS, "max_age_seconds": MAX_SESSION_SECONDS},
                "server": {"name": "verilog2digital", "version": __version__, "transport": "stdio",
                           "protocol_versions": ["2025-11-25", "2026-07-28"]}}

    async def inspect_design(self, ctx, arguments):
        from ..jobs import manager
        project, _, resources, _ = self._project(arguments, conversion=False)
        limits = self._job_limits(resources)
        job = self._hdl_job(lambda progress, cancel: manager.run_inspection(project, limits=limits, progress=progress,
                                                                           cancel_event=cancel), "included or source file")
        result = await self.runner.run(ctx, "inspect_design", job)
        inspection = result["inspection"]
        found, omitted = results.diagnostics(inspection.get("diagnostics"), self.policy)

        def names(key):
            return [results.text(item, 256) for item in (inspection.get(key) or [])[:256]]
        document = {"state": "ready", "job_id": result["job_id"], "top": inspection.get("top"),
                    "top_candidates": names("top_candidates"), "modules": names("modules"),
                    "source_files": [results.text(item, 1024) for item in (inspection.get("source_files") or [])[:64]],
                    "origins": [{key: item[key] for key in ("module", "path", "line", "column", "end_line", "end_column") if key in item}
                                for item in (inspection.get("origins") or [])[:64] if isinstance(item, dict)],
                    "diagnostics": found, "inspection_path": result.get("inspection_path"), "notice": results.UNTRUSTED}
        if omitted:
            document["diagnostics_omitted"] = omitted
        return document

    async def convert_design(self, ctx, arguments):
        from ..jobs import manager
        project, settings, resources, output = self._project(arguments, conversion=True)
        if output is None:
            raise FrontendError("OUTPUT_REQUIRED", "Provide output, or use a project file that names an output folder")
        output = self.policy.resolve(str(output), "output folder", kind="output", write=True)
        notes = []
        limits = self._job_limits(resources, notes)
        if (settings.pop("refinement", None) or {}).get("mode", "off") != "off":
            notes.append({"code": "PROJECT_REFINEMENT_IGNORED", "message": "convert_design runs the deterministic pipeline; "
                          "the project's refinement settings were ignored. Use refinement_start on the converted output."})
        replace = arguments.get("replace", False)
        try:
            selected = current_revision(output)[0] if output.is_dir() else None
        except FrontendError:
            selected = None
        if selected is not None and not replace:
            raise FrontendError("OUTPUT_EXISTS", "This output has a valid revision; use replace=true to select a new one", path=str(output))
        inspected = {}

        def work(progress, cancel):
            # The preflight checks every include before anything is written or simulated.
            inspection, _ = self._preflight(project, limits, progress, cancel)
            inspected.update(inspection)
            result = self._hdl_job(lambda: manager.run_job(project, output, options=settings, limits=limits, replace=replace,
                                                          cache=not arguments.get("no_cache", False), progress=progress,
                                                          cancel_event=cancel), "source file read after the preflight check")()
            manifest = read_document(Path(result["manifest"]))
            return result, manifest, read_document(Path(result["validation"]))

        try:
            result, manifest, report = await self.runner.run(ctx, "convert_design", work)
        except FrontendError as error:
            candidates = inspected.get("top_candidates") if inspected.get("top") is None else None
            return results.failure(error, self.policy, **({"top_candidates": candidates[:64]} if candidates else {}))
        document = self._published(result, manifest, report)
        document.update(cache={"hit": bool(result.get("cache", {}).get("hit"))}, notes=notes)
        return document

    async def validate_result(self, ctx, arguments):
        from ..jobs import manager
        from ..verification import result_directory
        path = self.policy.resolve(arguments["path"], "result path", kind="any")
        directory = result_directory(path)
        self.policy.require([directory], "result directory")
        artifacts_only = arguments.get("artifacts_only", False)
        if not artifacts_only:
            recorded = read_document(directory / "manifest.json").get("sources")
            if isinstance(recorded, dict):
                outside = [name for name in recorded if not self.policy.contains(name)]
                if outside:
                    raise FrontendError("PATH_NOT_ALLOWED", f"Full validation re-reads the recorded sources, and {outside[0]!r} is "
                                        "outside the allowed directories; use artifacts_only=true or allow that folder", path=outside[0])
        result = await self.runner.run(ctx, "validate_result", lambda progress, cancel: manager.run_validation(
            path, artifacts_only=artifacts_only, limits=self.limits, progress=progress, cancel_event=cancel))
        report = result["validation"]
        checks = {}
        for name, value in (report.get("checks") or {}).items():
            if isinstance(value, dict):
                checks[str(name)[:64]] = {"valid": value.get("valid") is True,
                                          "diagnostics": results.diagnostics(value.get("diagnostics"), self.policy, 4)[0]}
        found, omitted = results.diagnostics(report.get("diagnostics"), self.policy)
        document = {"state": "ready" if report.get("valid") is True else "invalid", "valid": report.get("valid") is True,
                    "scope": results.text(report.get("scope"), 64), "directory": report.get("directory"), "checks": checks,
                    "diagnostics": found, "recorded_behavior": results.behavior(report.get("recorded_behavior")),
                    "behavior_replayed": False, "job_id": result["job_id"], "validation_path": result.get("validation_path")}
        if omitted:
            document["diagnostics_omitted"] = omitted
        return document

    async def get_result(self, ctx, arguments):
        def read():
            _, directory, manifest, manifest_sha, bindings = self._bundle(arguments["path"])
            report = read_document(bindings["validation"][0])
            configuration = manifest.get("configuration") if isinstance(manifest.get("configuration"), dict) else {}
            files = [{"artifact": label, "path": str(file), "bytes": file.stat().st_size, "sha256": sha}
                     for label, (file, sha) in list(bindings.items())[:results.MAX_LIST]]
            sources = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
            return {"state": "ready", "directory": str(directory), "manifest": str(directory / "manifest.json"),
                    "manifest_sha256": manifest_sha, "selected_revision": self._selected(directory),
                    "top": results.text(manifest.get("top"), 256), "profile": results.text(manifest.get("profile"), 32),
                    "job_id": manifest.get("job_id"),
                    "configuration": {"sources": [results.text(item, 1024) for item in (configuration.get("sources") or [])[:64]],
                                      "include_dirs": [results.text(item, 1024) for item in (configuration.get("include_dirs") or [])[:64]],
                                      "defines": {str(key)[:256]: results.text(value, 200) for key, value in
                                                  list((configuration.get("defines") or {}).items())[:64]},
                                      "parameters": {str(key)[:256]: value if isinstance(value, int) else results.text(value, 200)
                                                     for key, value in list((configuration.get("parameters") or {}).items())[:64]}},
                    "sources": [{"path": results.text(name, 1024), "sha256": value} for name, value in list(sources.items())[:64]],
                    "layout": results.layout(manifest), "validation": results.validation(report, self.policy),
                    "refinement": results.refinement(manifest.get("refinement")),
                    "duration_seconds": manifest.get("duration_seconds"), "files": files,
                    "files_omitted": max(0, len(bindings) - results.MAX_LIST), "notice": results.UNTRUSTED}
        return await anyio.to_thread.run_sync(read)

    async def get_preview(self, ctx, arguments):
        which = arguments.get("which", "selected")

        def read():
            _, directory, manifest, _, bindings = self._bundle(arguments["path"])
            document, image = self._image(bindings, "preview" if which == "selected" else "baseline_preview")
            return {"state": "ready", "which": which, "directory": str(directory), **document, "notice": results.UNTRUSTED}, image

        document, image = await anyio.to_thread.run_sync(read)
        return results.success(document, extra=[image] if image is not None else [])

    async def job_status(self, ctx, arguments):
        from ..jobs.manager import job_status
        if "job_id" not in arguments:
            return {"state": "ready", "jobs": self.runner.recent()}
        job_id = arguments["job_id"]
        if self.runner.known(job_id) is None:
            raise FrontendError("JOB_UNKNOWN", "This server did not start that job (or no longer tracks it); "
                                "job_status only reports jobs started through this server")
        try:
            status = await anyio.to_thread.run_sync(job_status, self.job_root / job_id)
        except (OSError, FrontendError) as error:
            raise FrontendError("JOB_NOT_FOUND", "The job record no longer exists; job storage keeps a bounded number of "
                                "recent jobs") from error
        resources = status.get("resources") if isinstance(status.get("resources"), dict) else {}
        metrics = resources.get("metrics") if isinstance(resources.get("metrics"), dict) else {}
        found, _ = results.diagnostics(status.get("diagnostics"), self.policy, 8)
        result = status.get("result") if isinstance(status.get("result"), dict) else {}
        job = {"job_id": job_id, "tool": self.runner.known(job_id)["tool"], "kind": status.get("kind"),
               "state": status.get("state"), "stage": status.get("stage"),
               **{key: status[key] for key in ("created_utc", "updated_utc", "finished_utc", "duration_seconds") if key in status},
               "output_root": status.get("request", {}).get("output_root"), "diagnostics": found,
               "limits": results.scalars(resources.get("limits"), 8),
               "measured": {key: metrics[key] for key in ("peak_job_memory_bytes", "total_processes", "active_processes")
                            if type(metrics.get(key)) is int},
               "result": {key: result[key] for key in ("circuit", "revision", "manifest", "validation_path", "inspection_path")
                          if isinstance(result.get(key), str)}}
        return {"state": "ready", "job": job}

    async def cancel_job(self, ctx, arguments):
        from ..jobs.manager import cancel_job
        job_id = arguments["job_id"]
        if self.runner.known(job_id) is None:
            raise FrontendError("JOB_UNKNOWN", "This server did not start that job (or no longer tracks it)")
        signalled = self.runner.cancel(job_id)
        try:
            outcome = await anyio.to_thread.run_sync(cancel_job, self.job_root / job_id)
        except (OSError, FrontendError) as error:
            raise FrontendError("JOB_NOT_FOUND", "The job record no longer exists") from error
        return {"state": results.text(outcome.get("state"), 32), "job_id": job_id, "accepted": outcome.get("accepted") is True,
                "signalled_running_call": signalled,
                **({"reason": results.text(outcome["reason"], 64)} if outcome.get("reason") else {})}

    async def _with_preview(self, document, include):
        if include and document.get("session"):
            session = self.refinement.sessions.get(document["session"])
            if session is not None and session.run is not None:
                def read():
                    from ..verification import result_directory
                    directory = result_directory(session.run)
                    return self._image(bundle_bindings(directory, read_document(directory / "manifest.json")), "preview")
                preview, image = await anyio.to_thread.run_sync(read)
                document["preview"] = preview
                if image is not None:
                    return results.success(document, extra=[image])
        return document

    async def refinement_start(self, ctx, arguments):
        document = await self.refinement.start(ctx, arguments)
        return await self._with_preview(document, arguments.get("include_preview", False))

    async def refinement_propose(self, ctx, arguments):
        document = await self.refinement.propose(ctx, arguments)
        return await self._with_preview(document, arguments.get("include_preview", False))

    async def refinement_finish(self, ctx, arguments):
        document = await self.refinement.finish(ctx, arguments)
        if document.get("state") != "ready":
            return document
        result, manifest = document.pop("result"), document.pop("manifest")
        report = read_document(Path(result["validation"]))
        return {**self._published(result, manifest, report), "published": True, "reproduced": document["reproduced"],
                "refinement": results.refinement(manifest.get("refinement"))}


def _decoded(arguments, schema):
    """Some hosts send arrays and objects as JSON text; accept those as the SDK's typed tools do."""
    result = dict(arguments)
    for name, value in arguments.items():
        expected = schema["properties"].get(name, {}).get("type")
        if isinstance(value, str) and expected in ("array", "object") and len(value) <= 1024 * 1024:
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                continue
            if isinstance(decoded, list if expected == "array" else dict):
                result[name] = decoded
    return result


def _handler(state, name):
    async def handler(ctx: Context) -> CallToolResult:
        return await state.call(name, ctx)
    handler.__name__ = name
    return handler


def _reader(path):
    def read() -> str:
        with path.open("rb") as stream:
            data = stream.read(MAX_RESOURCE_BYTES + 1)
        if len(data) > MAX_RESOURCE_BYTES:
            raise ValueError(f"{path.name} exceeds the {MAX_RESOURCE_BYTES}-byte resource bound")
        return data.decode("utf-8")
    return read


def build_server(state):
    tools = []
    for name, title, description, inputs, outputs, annotations in state.definitions:
        tool = Tool.from_function(_handler(state, name), name=name, title=title, description=description,
                                  annotations=ToolAnnotations(**annotations), structured_output=False)
        tool.parameters = inputs
        tool.fn_metadata.output_schema = outputs
        tools.append(tool)
    paths = ("Paths must resolve inside these allowed directories: " + "; ".join(str(root) for root in state.policy.roots) + "."
             if state.policy.configured else "No --allow-dir is configured, so every path argument is refused until the server is restarted with one.")
    # The tool, resource and prompt lists never change and no resource publishes updates: serve no subscriptions/listen.
    server = MCPServer(
        name="verilog2digital", title="Verilog to Digital", version=__version__, log_level="WARNING", tools=tools, subscriptions=False,
        description="Converts Verilog into independently verified Digital circuits.",
        instructions=("Convert synthesizable Verilog-2005 into verified, editable Digital circuits. Typical flow: inspect_design, "
                      "convert_design, get_result or get_preview, validate_result; optionally refine the layout with "
                      "refinement_start, refinement_propose and refinement_finish, where you are the model. " + paths + " "
                      + results.UNTRUSTED))
    root = repository_root()
    for uri, relative, title, description in RESOURCES:
        path = root / relative
        if path.is_file():
            server.add_resource(FunctionResource.from_function(
                _reader(path), uri=uri, name=uri.rsplit("/", 1)[1], title=title, description=description,
                mime_type="application/json" if relative.endswith(".json") else "text/markdown"))
    return server


def serve(options):
    """Serve MCP over stdio until stdin closes; only protocol messages reach stdout."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="v2d-mcp %(levelname)s: %(message)s")
    from ..runtime import require_venv
    try:
        require_venv(repository_root())
        state = ServerState(options.allow_dir or [], deadline=options.deadline, memory_mib=options.memory_mib,
                            java_heap_mib=options.java_heap_mib)
    except (FrontendError, RuntimeError, OSError, ValueError, TypeError) as error:
        code = getattr(error, "code", None) or "MCP_STARTUP"
        print(f"{code}: {getattr(error, 'message', None) or error}", file=sys.stderr, flush=True)
        return 1
    print("v2d mcp: serving over stdio; allowed directories: "
          + ("; ".join(str(root) for root in state.policy.roots) or "none"), file=sys.stderr, flush=True)
    try:
        build_server(state).run("stdio")
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("The MCP server stopped unexpectedly")
        return 1
    finally:
        try:
            state.close()
        except (OSError, FrontendError) as error:
            logger.warning("Refinement scratch cleanup failed: %s", error)
    return 0
