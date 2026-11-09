"""Compare bounded layout refinement with original-HDL validation.

Each strategy starts from identical validated artifacts and uses the same
attempt and time limits.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))

from verilog2digital.environment import runtime_fingerprint
from verilog2digital.jobs.manager import JobLimits
from verilog2digital.jobs.processes import ManagedWorker, ProcessLimits, memory_status
from verilog2digital.jobs.publication import atomic_document, digest, read_document
from verilog2digital.jobs.store import directory_bytes
from verilog2digital.runtime import development_toolchain, require_venv


CASES = {"full_adder": "scalar/full_adder.v", "mux8": "datapath/mux8.v", "counter": "sequential/counter.v"}
SEQUENTIAL_CASES = {"counter"}  # Sequential plans accept at most 512 random cycles.
MODELS = ("qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m")
POLICY = {
    "id": "connected_pin_median_v1",
    "information": "same bounded context and feedback supplied to the model",
    "moves": "one component; one axis; 20 or 80 units toward median connected-pin displacement",
    "selection": "largest estimated Manhattan-distance reduction; stable component/delta tie breaks",
    "obstacles": "reject conflicts against every supplied component/obstacle bound at declared spacing",
    "repeat": "do not repeat an action against the same candidate snapshot",
    "routing_alternative": "up to four longest visible nets; reverse then default route order",
    "training_or_case_specific_rules": False,
}


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def real_strategy(model_id):
    if model_id not in MODELS:
        raise ValueError("The held-out benchmark supports only its pinned model choices")
    return "real14b" if model_id == "qwen3-14b-q4_k_m" else "real4b"


def profile_overrides(options):
    """Record explicit public settings using the native profile's units."""
    result = {}
    context = options.get("context_tokens")
    if context is not None:
        if type(context) is not int or not 1024 <= context <= 8192:
            raise ValueError("Context must contain 1024..8192 tokens")
        result["context_tokens"] = context
    memory = options.get("model_host_memory_mib")
    if memory is not None:
        if type(memory) is not int or not 64 <= memory <= 12288:
            raise ValueError("Model host memory must be 64..12288 MiB")
        result["host_memory_bytes"] = memory * 1024**2
    return result


class DeterministicProvider:
    """Estimate pin-to-pin Manhattan distance from the bounded model context.

    Native routing and electrical checks determine whether the proposal is valid.
    """
    def __init__(self):
        self.seen = set()

    def __call__(self, prompt, *, timeout):
        if timeout <= 0:
            raise TimeoutError("Deterministic proposal budget expired")
        context = prompt["context"]
        components, nets = context["components"], context["nets"]
        grid = context["constraints"]["grid"]
        spacing = context["constraints"]["minimum_spacing"]
        obstacles = [{"id": item["id"], "bounds": item["bounds"]} for item in components] + context["obstacles"]
        candidate_id = context["candidate_id"]
        choices = []
        for component in components:
            pairs = []
            for net in nets:
                own = [pin for pin in net["endpoints"] if pin["instance_id"] == component["id"]]
                external = [pin for pin in net["endpoints"] if pin["instance_id"] != component["id"]]
                pairs.extend((a["position"], b["position"]) for a in own for b in external)
            if not pairs:
                continue
            original_distance = sum(abs(a[axis]-b[axis]) for a, b in pairs for axis in (0, 1))
            for axis_index, axis in enumerate(("x", "y")):
                offsets = sorted(b[axis_index]-a[axis_index] for a, b in pairs)
                median = offsets[len(offsets)//2]
                if not median:
                    continue
                for amount in (grid, 4*grid):
                    delta = {"x": 0, "y": 0}
                    delta[axis] = amount if median > 0 else -amount
                    bounds = [value + delta["x" if index % 2 == 0 else "y"] for index, value in enumerate(component["bounds"])]
                    if any(not (bounds[2] + spacing <= other["bounds"][0]
                        or other["bounds"][2] + spacing <= bounds[0]
                        or bounds[3] + spacing <= other["bounds"][1]
                        or other["bounds"][3] + spacing <= bounds[1])
                           for other in obstacles if other["id"] != component["id"]):
                        continue
                    distance = sum(abs(a[index]+delta[key]-b[index]) for a, b in pairs for index, key in enumerate(("x", "y")))
                    gain = original_distance-distance
                    if gain <= 0:
                        continue
                    action = {"action": "move_component", "instance_id": component["id"], "delta": delta}
                    choices.append((-gain, component["id"], delta["x"], delta["y"], action))
        proposals = [item[-1] for item in sorted(choices, key=lambda item: item[:-1])]
        priority = [net["id"] for net in sorted(nets, key=lambda net: (-net["wire_length"], net["id"]))[:4]]
        if priority:
            proposals.extend({"action": "reroute_nets", "net_ids": priority, "order": order} for order in ("reverse", "default"))
        action = next((item for item in proposals if (candidate_id, json_hash(item)) not in self.seen), None)
        if action:
            self.seen.add((candidate_id, json_hash(action)))
        value = {"schema_version": 1, "candidate_id": candidate_id,
                 "actions": [action] if action else [],
                 "explanation": "Fixed connected-pin median policy; native validation decides acceptance."}
        return {"raw_text": json.dumps(value, separators=(",", ":")), "source": "deterministic_rules", "policy": POLICY["id"]}


def baseline_binding(result, validation):
    return {"circuit_sha256": digest(Path(result["circuit"])), "sidecar_sha256": digest(Path(result["sidecar"])),
            "metrics": deepcopy(validation["geometry"]["metrics"])}


def initial_binding(record):
    return {"circuit_sha256": record["emitted"]["sha256"], "sidecar_sha256": record["emitted"]["sidecar_sha256"],
            "metrics": deepcopy(record["validation"]["geometry"]["metrics"])}


def worker_main(request_path):
    # Suppress crash dialogs; the parent still observes exits and owns the process tree.
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
    from verilog2digital.conversion import convert
    from verilog2digital.frontend.project import Project
    from verilog2digital.layout.candidates import compare_quality
    import verilog2digital.llm.loop as loop

    request = read_document(Path(request_path))
    directory = Path(request["directory"])
    case, strategy = request["case"], request["strategy"]
    options = request["options"]
    model_id = options.get("model_id", MODELS[0])
    real = real_strategy(model_id)
    if strategy not in {"baseline", "deterministic", "replay", real}:
        raise ValueError("The requested strategy does not match its pinned model")
    overrides = profile_overrides(options)
    context_budget = loop.context_byte_budget(overrides["context_tokens"]) if "context_tokens" in overrides else None
    baseline = read_document(Path(request["baseline_result"])) if request.get("baseline_result") else None
    original_session = loop.RefinementSession
    sessions, observed = [], []
    replay = read_document(Path(request["replay_file"])) if request.get("replay_file") else None

    class CapturingSession(original_session):
        def refine(self, mapped, record, **kwargs):
            binding = initial_binding(record)
            observed.append(binding)
            if baseline is None or binding != baseline["selected"]:
                raise RuntimeError("Refinement did not start from the byte-identical validated baseline")
            return super().refine(mapped, record, **kwargs)

    def session_factory(*args, **kwargs):
        if strategy == "deterministic":
            kwargs["provider"] = DeterministicProvider()
        session = CapturingSession(*args, **kwargs)
        sessions.append(session)
        return session

    configuration = {"mode": "off" if strategy == "baseline" else "local" if strategy == real else "replay",
                     "model_id": model_id, "budget_seconds": options["budget_seconds"],
                     "max_attempts": options["max_attempts"]}
    if "context_tokens" in overrides:
        configuration["context_tokens"] = overrides["context_tokens"]
    if "host_memory_bytes" in overrides:
        configuration["model_memory_mib"] = overrides["host_memory_bytes"] // 1024**2
    if replay is not None:
        if replay["case"] != case or replay["baseline"] != baseline["selected"]:
            raise RuntimeError("Recorded responses do not belong to this exact circuit baseline")
        configuration["responses"] = replay["responses"]
    started = time.monotonic()
    record = {"schema_version": 1, "case": case, "strategy": strategy,
              "requested_model_id": model_id, "requested_profile_overrides": overrides,
              "requested_context_byte_budget": context_budget, "valid": False}
    try:
        loop.RefinementSession = session_factory
        result = convert(Project([ROOT / "tests/fixtures/verilog" / CASES[case]], top=case), directory / "conversion",
                         flatten=True, seed=0, random_count=options["random_count"],
                         layout_strategy="auto", layout_timeout=options["layout_timeout"], refinement=configuration)
        validation = read_document(Path(result["validation"]))
        manifest = read_document(Path(result["manifest"]))
        selected = baseline_binding(result, validation)
        refinement = manifest["refinement"]
        attempts = [attempt for canvas in refinement["canvases"] for attempt in canvas["attempts"]]
        accepted = [attempt for attempt in attempts if attempt.get("accepted")]
        regressions = sum(attempt.get("native_validation", {}).get("valid") is not True for attempt in accepted)
        if accepted and validation["behavior"].get("valid") is not True:
            regressions += 1
        native_requests = refinement.get("runtime", {}).get("requests", 0)
        quality = compare_quality(baseline["validation"] if baseline else validation, validation)
        record.update(valid=validation["valid"] is True and validation["behavior"].get("valid") is True and regressions == 0,
            selected=selected, validation=validation, quality=quality, original_hdl_behavior_validated=True,
            observed_initial_baselines=observed, identical_baseline=True if strategy == "baseline" else observed == [baseline["selected"]],
            conversion=result, refinement=refinement, refinement_seconds=refinement.get("session_seconds", 0),
            original_hdl_coverage=validation["behavior"], conversion_stage_seconds=manifest["timings_seconds"],
            attempts=len(attempts), accepted_attempts=len(accepted), accepted_actions=sum(len(attempt["actions"]["actions"]) for attempt in accepted),
            rejected_attempts=sum(attempt.get("outcome") == "rejected" for attempt in attempts),
            valid_nonimproving_attempts=sum(attempt.get("outcome") == "retained_incumbent" for attempt in attempts),
            accepted_electrical_regressions=regressions, real_model_requests=native_requests,
            real_model_executed=strategy == real and native_requests > 0,
            proposal_provider_exercised=strategy == "baseline" or any("response" in attempt for attempt in attempts))
        response_record = {"schema_version": 1, "case": case, "strategy": strategy,
            "requested_model_id": model_id,
            "requested_profile_overrides": overrides,
            "requested_context_byte_budget": context_budget,
            "baseline": baseline["selected"] if baseline else selected, "selected": selected,
            "responses": [attempt["response"] for attempt in attempts if "response" in attempt],
            "policy": POLICY, "limits": options, "runtime": refinement.get("runtime"),
            "provenance": request.get("provenance", {})}
        atomic_document(directory / "replay.json", response_record)
        record["replay_file"] = str(directory / "replay.json")
        if replay is not None:
            record["replay_identical_selected_artifacts"] = selected == replay["selected"]
            record["valid"] = record["valid"] and record["replay_identical_selected_artifacts"]
    except Exception as error:
        record["error"] = {"code": getattr(error, "code", type(error).__name__), "message": str(error)[:3000]}
        record["observed_initial_baselines"] = observed
        record["accepted_electrical_regressions"] = None
    finally:
        for session in sessions:
            session.close()
        loop.RefinementSession = original_session
    record["conversion_wall_seconds"] = time.monotonic()-started
    atomic_document(directory / "result.json", record)
    print(json.dumps({"case": case, "strategy": strategy, "valid": record["valid"],
                      "conversion_wall_seconds": record["conversion_wall_seconds"], "error": record.get("error")}), flush=True)
    return 0 if record["valid"] else 1


def run_owned(request, limits, *, worker_entrypoint=None):
    directory = Path(request["directory"])
    directory.mkdir(parents=True, exist_ok=False)
    atomic_document(directory / "request.json", request)
    private_tmp = directory / "tmp"
    private_tmp.mkdir()
    toolchain = development_toolchain(ROOT)
    executable = toolchain.executable("python")
    env = toolchain.environment(executable)
    env.update(V2D_JAVA_HEAP_BYTES=str(limits.java_heap_bytes), V2D_JOB_TMP=str(private_tmp),
               TMP=str(private_tmp), TEMP=str(private_tmp), PYTHONIOENCODING="utf-8")
    worker = None
    reader = None
    errors = []
    result = {"schema_version": 1, "case": request["case"], "strategy": request["strategy"],
              "requested_model_id": request["options"].get("model_id", MODELS[0]),
              "requested_profile_overrides": profile_overrides(request["options"]),
              "requested_context_byte_budget": request["options"].get("context_byte_budget"), "valid": False}
    started = time.monotonic()
    before = memory_status()
    try:
        worker = ManagedWorker.start([executable, "-X", "faulthandler", str(Path(worker_entrypoint or __file__).resolve()),
                                      "--worker-request", str(directory / "request.json")],
                    cwd=ROOT, env=env, stderr_path=directory / "stderr.log",
                    limits=ProcessLimits(job_memory_bytes=limits.memory_bytes, process_memory_bytes=limits.process_memory_bytes,
                                         active_process_limit=limits.active_process_limit))

        def drain():
            try:
                total = 0
                with (directory / "stdout.log").open("xb") as stream:
                    while block := worker.stdout.read(8192):
                        total += len(block)
                        if total > 8*1024**2:
                            raise RuntimeError("Benchmark stdout exceeds its 8 MiB bound")
                        stream.write(block)
            except Exception as error:
                errors.append(str(error))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        next_disk = time.monotonic()
        while worker.poll() is None:
            if time.monotonic()-started > limits.deadline_seconds:
                raise TimeoutError("Benchmark worker exceeded its complete process-tree deadline")
            if errors:
                raise RuntimeError(errors[0])
            if time.monotonic() >= next_disk:
                directory_bytes(directory, limit=256*1024**2)
                next_disk = time.monotonic()+.5
            time.sleep(.05)
        grace = min(started+limits.deadline_seconds, time.monotonic()+1)
        while worker.metrics()["active_processes"] and time.monotonic() < grace:
            time.sleep(.05)
        if worker.metrics()["active_processes"]:
            raise RuntimeError("Benchmark worker exited with live descendants")
        reader.join(timeout=2)
        if reader.is_alive() or errors:
            raise RuntimeError(errors[0] if errors else "Benchmark output did not drain")
        result = read_document(directory / "result.json")
        result["exit_code"] = worker.poll()
        result["valid"] = result["valid"] and result["exit_code"] == 0
    except Exception as error:
        result["error"] = {"code": getattr(error, "code", type(error).__name__), "message": str(error)[:3000]}
    finally:
        if worker is not None:
            worker.close()
            result["process_metrics"] = worker.metrics()
            result["owned_processes_remaining"] = result["process_metrics"]["active_processes"]
        else:
            result["owned_processes_remaining"] = 0
        if reader is not None:
            reader.join(timeout=2)
    result["wall_seconds"] = time.monotonic()-started
    result["memory_before"] = before
    result["memory_after"] = memory_status()
    result["valid"] = result["valid"] and result["owned_processes_remaining"] == 0
    atomic_document(directory / "supervised-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New ASCII directory inside the repository .runs")
    parser.add_argument("--mode", choices=("deterministic", "compare", "replay"), default="deterministic")
    parser.add_argument("--model", choices=MODELS, default=MODELS[0],
                        help="Pinned local model; 4B uses real4b paths and 14B uses real14b (default: %(default)s)")
    parser.add_argument("--context-tokens", type=int, help="Explicit production model context override, 1024..8192 tokens")
    parser.add_argument("--model-host-memory-mib", type=int, help="Explicit model Job commit ceiling, 64..12288 MiB")
    parser.add_argument("--cases", nargs="+", choices=list(CASES), default=list(CASES))
    parser.add_argument("--replay-root", type=Path, help="Prior benchmark root containing case/real4b or case/real14b/replay.json")
    parser.add_argument("--budget", type=float, default=60)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--layout-timeout", type=float, default=20)
    parser.add_argument("--random-count", type=int, default=256)
    parser.add_argument("--deadline", type=float, default=300)
    parser.add_argument("--memory-mib", type=int, default=6144)
    parser.add_argument("--java-heap-mib", type=int, default=1024)
    parser.add_argument("--dry-run", action="store_true", help="Development validation only; not accepted comparative timing evidence")
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require_venv(ROOT)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if args.worker_request:
        return worker_main(args.worker_request)
    if args.output is None:
        parser.error("--output is required")
    if args.mode == "replay" and args.replay_root is None:
        parser.error("--replay-root is required in replay mode")
    if args.dry_run and args.mode != "deterministic":
        parser.error("Development dry-run uses deterministic mode only")
    if not math.isfinite(args.budget) or not 1 <= args.budget <= 300 or not 1 <= args.attempts <= 8:
        parser.error("Refinement requires a 1..300 second budget and 1..8 attempts")
    vector_limit = 512 if SEQUENTIAL_CASES.intersection(args.cases) else 2048
    if not math.isfinite(args.layout_timeout) or not 1 <= args.layout_timeout <= 120 or not 0 <= args.random_count <= vector_limit:
        parser.error(f"Use a 1..120 second layout timeout and 0..{vector_limit} random vectors for the selected cases")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".runs") or not str(output).isascii():
        parser.error("Benchmark output must be an ASCII path inside this repository's .runs directory")
    limits = JobLimits(deadline_seconds=args.deadline, memory_bytes=args.memory_mib*1024**2,
                       java_heap_bytes=args.java_heap_mib*1024**2)
    limits.validate()
    requested_profile = {"context_tokens": args.context_tokens, "model_host_memory_mib": args.model_host_memory_mib}
    try:
        overrides = profile_overrides(requested_profile)
    except ValueError as error:
        parser.error(str(error))
    if "host_memory_bytes" in overrides and overrides["host_memory_bytes"] + limits.java_heap_bytes > limits.memory_bytes:
        parser.error("Explicit model and Java heap ceilings must fit within --memory-mib")
    if limits.deadline_seconds < args.budget+30:
        parser.error("Worker deadline needs at least 30 seconds beyond the refinement cap for conversion and cleanup")
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = runtime_fingerprint()
    options = {"model_id": args.model, "budget_seconds": args.budget, "max_attempts": args.attempts,
               "layout_timeout": args.layout_timeout, "random_count": args.random_count,
               **{key: value for key, value in requested_profile.items() if value is not None}}
    context_budget = None
    if args.context_tokens is not None:
        from verilog2digital.llm.loop import context_byte_budget
        context_budget = context_byte_budget(args.context_tokens)
        options["context_byte_budget"] = context_budget
    report = {"schema_version": 1, "mode": args.mode, "evaluation": "development_dry_run" if args.dry_run else "held_out_comparison",
              "requested_model_id": args.model,
              "requested_profile_overrides": overrides,
              "requested_context_byte_budget": context_budget,
              "policy": POLICY, "policy_sha256": json_hash(POLICY), "script_sha256": digest(Path(__file__)),
              "runtime_sha256_before": fingerprint["sha256"], "limits": {"worker": asdict(limits), "refinement": options},
              "cases": [], "sources": {name: digest(ROOT / "tests/fixtures/verilog" / CASES[name]) for name in args.cases},
              "platform": platform.platform(), "python": sys.version,
              "notes": ["Proposal policy is fixed before evaluating these three held-out refinement cases; no result-driven tuning.",
                        "The initial circuit/sidecar hashes and native metrics must match the fully validated deterministic baseline exactly.",
                        "Equal refinement caps do not imply equal observed running times; actual worker and refinement times are recorded.",
                        "A fresh process owns each strategy, including model startup and shutdown; operating-system file caches are uncontrolled.",
                        "Every selected result is checked against original Icarus Verilog under the reported exhaustive or sampled coverage.",
                        "Windows Job Object measurements are committed-memory counters, not RSS; peaks may include denied commits.",
                        "GPU measurements, when available, are device-wide sampled usage including unrelated applications."]}
    real = real_strategy(args.model)
    strategies = ["deterministic", real] if args.mode == "compare" else [args.mode]
    for case in args.cases:
        baseline_path = output / case / "baseline" / "supervised-result.json"
        for strategy in ["baseline", *strategies]:
            request = {"schema_version": 1, "case": case, "strategy": strategy,
                       "directory": str(output / case / strategy), "options": options,
                       "baseline_result": str(baseline_path) if strategy != "baseline" else None,
                       "provenance": {"script_sha256": report["script_sha256"], "policy_sha256": report["policy_sha256"],
                                      "requested_model_id": args.model,
                                      "requested_profile_overrides": overrides,
                                      "requested_context_byte_budget": context_budget,
                                      "runtime_sha256": fingerprint["sha256"], "source_sha256": report["sources"][case]}}
            if strategy == "replay":
                request["replay_file"] = str(args.replay_root.resolve() / case / real / "replay.json")
            result = run_owned(request, limits)
            report["cases"].append(result)
            atomic_document(output / "summary.json", report)
            print(json.dumps({"case": case, "strategy": strategy, "valid": result["valid"],
                  "accepted": result.get("accepted_attempts"), "wall_seconds": result["wall_seconds"],
                  "refinement_seconds": result.get("refinement_seconds"), "error": result.get("error")}), flush=True)
            if strategy == "baseline" and not result["valid"]:
                break
    after = runtime_fingerprint()
    report["runtime_sha256_after"] = after["sha256"]
    report["runtime_unchanged"] = after["sha256"] == fingerprint["sha256"]
    report["all_selected_outcomes_valid"] = all(case["valid"] for case in report["cases"])
    report["all_proposal_providers_exercised"] = all(case.get("proposal_provider_exercised") for case in report["cases"])
    real_cases = [case for case in report["cases"] if case["strategy"] in {"real4b", "real14b"}]
    report["real_model_exercised"] = bool(real_cases) and all(case.get("real_model_executed") for case in real_cases)
    report["accepted_electrical_regressions"] = (sum(case["accepted_electrical_regressions"] for case in report["cases"])
        if all(case.get("accepted_electrical_regressions") is not None for case in report["cases"]) else None)
    report["comparable"] = (not args.dry_run and report["runtime_unchanged"]
        and report["all_selected_outcomes_valid"] and report["all_proposal_providers_exercised"]
        and all(case.get("identical_baseline") for case in report["cases"])
        and (args.mode != "compare" or report["real_model_exercised"]))
    report["valid"] = report["all_selected_outcomes_valid"] and report["all_proposal_providers_exercised"] and (args.dry_run or report["comparable"])
    atomic_document(output / "summary.json", report)
    print(f"Refinement report: {output / 'summary.json'}", flush=True)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
