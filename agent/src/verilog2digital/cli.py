"""Project and job CLI shared with the native desktop client."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import traceback

from .contracts import ContractError
from .frontend.errors import FrontendError
from .frontend.project import Project
from .jobs.publication import atomic_document, read_document
from .project_file import load_project
from .runtime import repository_root, require_venv


def _definitions(values, *, parameter=False):
    result = {}
    for item in values:
        if "=" in item:
            name, value = item.split("=", 1)
        elif parameter:
            raise FrontendError("INVALID_PARAMETER", "Parameter overrides use NAME=value")
        else:
            name, value = item, "1"
        if name in result:
            raise FrontendError("DUPLICATE_OPTION", f"Option {name} was supplied more than once")
        result[name] = value
    return result


def _unique_trace_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate trace field {name[:80]!r}")
        result[name] = value
    return result


def _trace(path):
    if path is None:
        return None
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise FrontendError("TRACE_LIMIT", "Sequential trace files must not exceed 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_trace_fields,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError) as error:
        raise FrontendError("INVALID_TRACE", f"Cannot read the sequential trace: {error}") from error
    if not isinstance(value, dict):
        raise FrontendError("INVALID_TRACE", "A sequential trace must be a JSON object")
    return value


def _output_flags(parser):
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    parser.add_argument("--events", action="store_true", help="Stream JSON progress and a final result")
    parser.add_argument("--quiet", action="store_true", help="Suppress human-readable progress")


def _limits_flags(parser):
    parser.add_argument("--deadline", type=float, help="Total job deadline in seconds (default 300)")
    parser.add_argument("--memory-mib", type=int, help="Aggregate helper memory ceiling (default 6144 MiB)")
    parser.add_argument("--java-heap-mib", type=int, help="Java heap ceiling inside the job (default 1024 MiB)")


def _source_flags(parser, *, conversion=False):
    parser.add_argument("sources", type=Path, nargs="*", help="Verilog files in compilation order")
    parser.add_argument("--project", type=Path, help="Project JSON with paths relative to that file")
    parser.add_argument("-t", "--top", help="Top module; discovered if unambiguous")
    parser.add_argument("-I", "--include", action="append", default=[], type=Path)
    parser.add_argument("-D", "--define", action="append", default=[], help="Macro NAME or NAME=value")
    parser.add_argument("-P", "--parameter", action="append", default=[], help="Top parameter NAME=value (0 through 2147483647, or an unsigned sized literal such as 8'hff)")
    _limits_flags(parser)
    _output_flags(parser)
    if conversion:
        parser.add_argument("-o", "--output", type=Path, help="Managed output directory")
        parser.add_argument("--spacing", type=int, help="Extra spacing, multiple of 20 from 40 to 400")
        parser.add_argument("--profile", choices=("auto", "scalar-v1", "datapath-v1", "sequential-v1"))
        parser.add_argument("--trace", type=Path, help="Sequential initialization and checked cycle trace")
        parser.add_argument("--flatten", action=argparse.BooleanOptionalAction, default=None)
        parser.add_argument("--seed", type=int)
        parser.add_argument("--random-vectors", type=int)
        parser.add_argument("--layout", choices=("auto", "reserved"))
        parser.add_argument("--tunnels", action=argparse.BooleanOptionalAction, default=None)
        parser.add_argument("--layout-timeout", type=float, help="Compact layout budget (1 through 120 seconds)")
        parser.add_argument("--llm", choices=("off", "auto", "4b", "14b"),
                            help="Optional local model refinement; default off, auto selects the default 4b model")
        parser.add_argument("--refinement-budget", type=float, help="Total optional refinement budget, 1 through 300 seconds")
        parser.add_argument("--refinement-attempts", type=int, help="Proposal limit across all canvases, 1 through 8")
        parser.add_argument("--layout-request", help="A short spacing/grouping preference for the local model")
        parser.add_argument("--model-context-tokens", type=int, help="Local model context limit, 1024 through 8192 tokens (default automatic)")
        parser.add_argument("--model-memory-mib", type=int, help="Local model memory ceiling, 64 through 12288 MiB (default 6144)")
        parser.add_argument("--model-idle-seconds", type=int, help="Unload an idle local model after 1 through 300 seconds (default 30)")
        parser.add_argument("--replay-actions", type=Path, help="Replay recorded raw action responses through the same checks")


def _project_and_options(options):
    config = load_project(options.project) if options.project else {
        "project": Project([]), "options": {}, "resources": {}, "output": None}
    if options.project and options.sources:
        raise FrontendError("PROJECT_OPTIONS", "Use the project's source list or command-line source files")
    base = config["project"]
    project = Project(options.sources or base.sources, options.top or base.top,
                      [*base.include_dirs, *options.include],
                      {**base.defines, **_definitions(options.define)},
                      {**base.parameters, **_definitions(options.parameter, parameter=True)}).resolved()
    conversion = dict(config["options"])
    fields = {"spacing": "spacing", "profile": "profile", "seed": "seed", "random_vectors": "random_count",
              "layout": "layout_strategy", "tunnels": "tunnels", "layout_timeout": "layout_timeout", "flatten": "flatten"}
    for argument, field in fields.items():
        value = getattr(options, argument, None)
        if value is not None:
            conversion[field] = value
    # Command-line values use the project-file bounds before any output is created.
    for argument, valid, rule in (
            ("spacing", lambda value: 40 <= value <= 400 and value % 20 == 0, "a multiple of 20 from 40 through 400"),
            ("layout_timeout", lambda value: 1 <= value <= 120, "from 1 through 120 seconds"),
            ("random_vectors", lambda value: 0 <= value <= 2048, "from 0 through 2048")):
        value = getattr(options, argument, None)
        if value is not None and not valid(value):
            raise FrontendError("INVALID_OPTION", f"--{argument.replace('_', '-')} must be {rule}")
    if getattr(options, "trace", None):
        conversion["sequential_trace"] = _trace(options.trace)
    refinement = dict(conversion.get("refinement", {}))
    if getattr(options, "llm", None) is not None:
        refinement.update(mode="off" if options.llm == "off" else "local", responses=[])
        refinement["model_id"] = "qwen3-14b-q4_k_m" if options.llm == "14b" else "qwen3-4b-q4_k_m"
    for argument, field in (("refinement_budget", "budget_seconds"), ("refinement_attempts", "max_attempts"),
                            ("layout_request", "request")):
        value = getattr(options, argument, None)
        if value is not None:
            refinement[field] = value
    if getattr(options, "layout_request", None) and not getattr(options, "llm", None) and not refinement.get("mode"):
        refinement["mode"] = "local"
    model_controls = {field: getattr(options, argument, None) for argument, field in (
        ("model_context_tokens", "context_tokens"), ("model_memory_mib", "model_memory_mib"),
        ("model_idle_seconds", "idle_seconds")) if getattr(options, argument, None) is not None}
    if model_controls:
        if (getattr(options, "llm", None) == "off" or getattr(options, "replay_actions", None)
                or refinement.get("mode") == "replay"):
            raise FrontendError("REFINEMENT_OPTIONS", "Explicit model limits require local refinement; remove off/replay selection or select --llm auto, 4b or 14b")
        # An explicit local-model control enables local mode, preserving a saved
        # model choice. Saved limits alone do not change off or replay modes.
        refinement.update(mode="local", **model_controls)
    if getattr(options, "replay_actions", None):
        if getattr(options, "llm", None) is not None:
            raise FrontendError("REFINEMENT_OPTIONS", "Select a local model or recorded action replay")
        recorded = read_document(options.replay_actions, limit=600000)
        if not isinstance(recorded, dict) or set(recorded) != {"schema_version", "responses"} or type(recorded["schema_version"]) is not int or recorded["schema_version"] != 1:
            raise FrontendError("INVALID_REPLAY", "Replay files require schema_version 1 and a responses list of raw JSON strings")
        refinement.update(mode="replay", responses=recorded["responses"])
    if refinement:
        from .llm.config import configuration
        conversion["refinement"] = configuration(refinement)
    return project, conversion, config["resources"], getattr(options, "output", None) or config["output"]


def _limits(options, configured=None):
    from .jobs.manager import JobLimits
    configured = dict(configured or {})
    for argument, field in (("deadline", "deadline_seconds"), ("memory_mib", "memory_mib"), ("java_heap_mib", "java_heap_mib")):
        value = getattr(options, argument, None)
        if value is not None:
            configured[field] = value
    values = {}
    for name, value in configured.items():
        if name.endswith("_mib"):
            destination = {"memory_mib": "memory_bytes", "java_heap_mib": "java_heap_bytes",
                           "process_memory_mib": "process_memory_bytes"}[name]
            values[destination] = value * 1024 * 1024 if value is not None else None
        else:
            values[name] = value
    return JobLimits(**values)


def _print_result(result, options):
    if options.events:
        print(json.dumps({"schema_version": 1, "type": "result", "result": result}, ensure_ascii=False), flush=True)
    elif options.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    elif "circuit" in result:
        print(f"Created: {result['circuit']}")
        print(f"Preview: {result['preview']}")
        print(f"Validation: {result['validation']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="v2d", description="Convert Verilog into independently checked Digital circuits")
    commands = parser.add_subparsers(dest="command", required=True)
    conversion = commands.add_parser("convert", help="Run an isolated conversion and select a verified revision")
    _source_flags(conversion, conversion=True)
    conversion.add_argument("--replace", action="store_true", help="Select a new revision and preserve the previous one")
    conversion.add_argument("--cache", action=argparse.BooleanOptionalAction, default=True,
                            help="Reuse a bound, verified candidate when all dependencies match")
    inspection = commands.add_parser("inspect", help="Discover tops and inspect source support")
    _source_flags(inspection)
    check = commands.add_parser("doctor", help="Check runtime tools, Python isolation and available memory")
    _output_flags(check)
    models = commands.add_parser("models", help="List, install or verify the pinned optional local models")
    models.add_argument("action", choices=("status", "install", "verify"), nargs="?", default="status")
    models.add_argument("--model", choices=("qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"),
                        default="qwen3-4b-q4_k_m")
    models.add_argument("--backend", choices=("cuda", "cpu"), default="cuda")
    _output_flags(models)
    validation = commands.add_parser("validate", help="Check result freshness and native model reload")
    validation.add_argument("path", type=Path)
    validation.add_argument("--artifacts-only", action="store_true", help="Validate without the original source installation")
    _limits_flags(validation)
    _output_flags(validation)
    desktop = commands.add_parser("desktop", help="Open, export or prepare Digital for a checked circuit package")
    desktop.add_argument("action", choices=("result", "export", "digital"))
    desktop.add_argument("path", type=Path)
    desktop.add_argument("-o", "--output", type=Path, help="New package folder for export")
    _limits_flags(desktop)
    _output_flags(desktop)
    benchmark = commands.add_parser("benchmark", help="Measure compact and reserved layouts on the same project")
    _source_flags(benchmark, conversion=True)
    benchmark.add_argument("--repeats", type=int, default=1, help="Runs per strategy (1 through 5)")
    job = commands.add_parser("job", help="Read, cancel or recover durable jobs")
    job.add_argument("action", choices=("status", "cancel", "recover"))
    job.add_argument("path", type=Path, nargs="?")
    _output_flags(job)
    options = parser.parse_args(argv)
    output_lock = threading.Lock()
    output_finished = False
    try:
        require_venv(repository_root())
        def progress(event):
            if options.events:
                with output_lock:
                    if not output_finished:
                        print(json.dumps({"schema_version": 1, "type": "progress", "event": event}, ensure_ascii=False), flush=True)
            elif not options.quiet:
                print(str(event.get("stage", event.get("state", "running"))).replace("_", " "), file=sys.stderr, flush=True)
        if options.command == "doctor":
            from .environment import doctor
            result = doctor()
        elif options.command == "models":
            if options.action == "status":
                from .llm.inventory import inventory
                result = inventory()
            elif options.action == "verify":
                from .llm.artifacts import verify_installation
                result = {"schema_version": 1, "state": "ready", "verified": True,
                          **verify_installation(options.model, options.backend)}
            else:
                from .llm.artifacts import provision
                progress({"stage": "downloading_verified_model"})
                result = {"schema_version": 1, "state": "ready",
                          **provision(model_ids=[options.model], backends=[options.backend])}
        elif options.command == "job":
            from .jobs.manager import cancel_job, job_status, recover_jobs
            if options.action != "recover" and options.path is None:
                raise FrontendError("JOB_PATH", "Provide the job directory or manifest path")
            path = options.path.parent if options.path and options.path.name == "job.json" else options.path
            result = {"schema_version": 2, "jobs": recover_jobs(path)} if options.action == "recover" else (
                cancel_job(path) if options.action == "cancel" else job_status(path))
        elif options.command == "desktop":
            from .desktop import checked_result, digital_launch_plan, export_result
            kwargs = {"limits": _limits(options), "progress": progress}
            if options.action == "export":
                if options.output is None:
                    raise FrontendError("OUTPUT_REQUIRED", "Choose a new folder for the exported package")
                result = export_result(options.path, options.output, **kwargs)
            else:
                if options.output is not None:
                    raise FrontendError("DESKTOP_OPTIONS", "An output folder is only used when exporting")
                result = (checked_result if options.action == "result" else digital_launch_plan)(options.path, **kwargs)
        elif options.command == "validate":
            from .jobs.manager import run_validation
            result = run_validation(options.path, artifacts_only=options.artifacts_only, limits=_limits(options), progress=progress)
        else:
            project, settings, resources, output = _project_and_options(options)
            from .jobs.manager import run_inspection, run_job
            limits = _limits(options, resources)
            if options.command == "inspect":
                result = run_inspection(project, limits=limits, progress=progress)
            elif output is None:
                raise FrontendError("OUTPUT_REQUIRED", "Provide --output or an output path in the project file")
            elif options.command == "convert":
                result = run_job(project, output, options=settings, limits=limits, replace=options.replace,
                                 cache=options.cache, progress=progress)
            else:
                if not 1 <= options.repeats <= 5:
                    raise FrontendError("BENCHMARK_LIMIT", "Benchmark repeats must be between 1 and 5")
                output = Path(output).resolve()
                try:
                    output.mkdir(parents=True, exist_ok=False)
                except FileExistsError as error:
                    raise FrontendError("OUTPUT_EXISTS", "Choose a new benchmark directory; existing results are preserved",
                                        path=str(output)) from error
                runs = []
                for index in range(options.repeats):
                    for strategy in ("auto", "reserved"):
                        run = run_job(project, output / f"{index:02d}-{strategy}",
                                      options={**settings, "layout_strategy": strategy}, limits=limits, cache=False, progress=progress)
                        manifest = read_document(Path(run["manifest"]))
                        job_record = read_document(Path(run["job_manifest"]))
                        runs.append({"strategy": strategy, "repeat": index, "result": run,
                                     "duration_seconds": job_record["duration_seconds"],
                                     "conversion_seconds": manifest["duration_seconds"],
                                     "metrics": manifest["layout"]["metrics"], "resources": manifest["job"].get("resources")})
                result = {"schema_version": 2, "state": "ready", "runs": runs}
                atomic_document(output / "benchmark.json", result)
        with output_lock:
            output_finished = True
            _print_result(result, options)
        return 0 if result.get("state") not in {"failed", "incomplete", "cancelled", "interrupted"} and result.get("valid") is not False else 1
    except Exception as error:
        if not isinstance(error, (FrontendError, ContractError, OSError, RuntimeError, ValueError)):
            # An unexpected failure still ends the --events/--json protocol with one error record.
            traceback.print_exc(file=sys.stderr)
        diagnostic = error.as_dict() if isinstance(error, FrontendError) else {
            "code": "COMMAND_FAILED", "message": str(error) or type(error).__name__}
        result = {"schema_version": 2, "state": "cancelled" if diagnostic["code"] == "CANCELLED" else "failed", "diagnostic": diagnostic}
        if options.events:
            with output_lock:
                output_finished = True
                print(json.dumps({"schema_version": 1, "type": "error", "result": result}, ensure_ascii=False), flush=True)
        elif options.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        else:
            print(f"{diagnostic['code']}: {diagnostic['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
