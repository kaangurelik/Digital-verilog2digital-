"""Test refinement recovery by denying one native allocation.

Only the first server's Job cap changes; admission, cleanup, retry selection,
generation and circuit validation use the production code.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_refinement as benchmark
from verilog2digital.environment import runtime_fingerprint
from verilog2digital.jobs.manager import JobLimits
from verilog2digital.jobs.processes import ProcessLimits
from verilog2digital.jobs.publication import atomic_document, digest, read_document
from verilog2digital.runtime import require_venv


def validate_fault_limit(value):
    if type(value) is not int or not 64 <= value <= 3072:
        raise ValueError("First-server fault cap must be 64..3072 MiB")
    return value * 1024**2


@contextmanager
def allocation_fault(runtime, cap_bytes):
    """A worker-local launch proxy; no admission or error-classifier stubs."""
    original = runtime.ManagedWorker
    launches = []

    class Launcher:
        @staticmethod
        def start(argv, **kwargs):
            if Path(argv[0]).name.lower() != "llama-server.exe":
                raise RuntimeError("Allocation fault may target only the owned llama-server")
            prior = [row["worker"].metrics() for row in launches]
            if any(row.get("active_processes") != 0 or row.get("closed") is not True for row in prior):
                raise RuntimeError("A replacement was attempted before the prior model Job closed")
            limits = kwargs["limits"]
            applied = cap_bytes if not launches else limits.job_memory_bytes
            if not launches:
                kwargs["limits"] = ProcessLimits(job_memory_bytes=applied,
                    process_memory_bytes=limits.process_memory_bytes, active_process_limit=limits.active_process_limit)
            row = {"index": len(launches) + 1, "context_tokens": int(argv[argv.index("--ctx-size") + 1]),
                   "arguments": list(argv), "configured_job_memory_bytes": limits.job_memory_bytes,
                   "applied_job_memory_bytes": applied, "prior_processes": prior,
                   "stderr_path": str(kwargs["stderr_path"])}
            worker = original.start(argv, **kwargs)
            row["worker"] = worker
            launches.append(row)
            return worker

    runtime.ManagedWorker = Launcher
    try:
        yield launches
    finally:
        runtime.ManagedWorker = original
        for row in launches:
            worker = row.pop("worker")
            row["before_harness_cleanup"] = worker.metrics()
            # Clean up on failure, recording it separately from production cleanup results.
            worker.close()
            row["closed"] = worker.metrics()
            log = Path(row["stderr_path"])
            if log.is_file():
                row["stderr_sha256"] = digest(log)
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 16384))
                    row["stderr_tail"] = stream.read(16384).decode("utf-8", errors="replace")


def recovery_checks(result, launches):
    report = result.get("refinement", {})
    attempts = [item for canvas in report.get("canvases", []) for item in canvas["attempts"]]
    decision = report.get("memory_recovery", {})
    retired = report.get("retired_runtimes", [])
    runtime = report.get("runtime", {})
    evidence = attempts[0].get("allocation_evidence", {}) if attempts else {}
    checks = {
        "conversion_valid": result.get("valid") is True,
        "byte_identical_initial_baseline": result.get("identical_baseline") is True,
        "original_hdl_behavior_valid": result.get("validation", {}).get("behavior", {}).get("valid") is True,
        "native_final_validation_valid": result.get("validation", {}).get("valid") is True,
        "actual_typed_native_denial": bool(attempts) and attempts[0].get("diagnostic", {}).get("code") == "MODEL_ALLOCATION"
            and evidence.get("source") == "owned_server_stderr" and bool(evidence.get("signature")),
        "exactly_one_4096_to_2048_recovery": len(launches) == 2
            and [row["context_tokens"] for row in launches] == [4096, 2048]
            and decision.get("after_attempt") == 1 and decision.get("reason") == "MODEL_ALLOCATION"
            and decision.get("from", {}).get("context_tokens") == 4096
            and decision.get("to", {}).get("context_tokens") == 2048,
        "only_first_server_cap_reduced": len(launches) == 2
            and 0 < launches[0]["applied_job_memory_bytes"] < launches[0]["configured_job_memory_bytes"]
            and launches[1]["applied_job_memory_bytes"] == launches[1]["configured_job_memory_bytes"],
        "unchanged_model_and_gpu": bool(decision) and all(decision.get("from", {}).get(key) == decision.get("to", {}).get(key)
            for key in ("model_id", "backend", "gpu_index", "gpu_layers", "host_memory_bytes", "kv_type"))
            and decision.get("to", {}).get("backend") == "cuda",
        "original_attempt_limit_preserved": len(attempts) <= 3,
        "retired_runtime_closed": len(retired) == 1 and retired[0].get("process", {}).get("active_processes") == 0
            and retired[0].get("process", {}).get("closed") is True,
        "production_cleanup_before_harness": bool(launches) and all(row.get("before_harness_cleanup", {}).get("active_processes") == 0
            and row.get("before_harness_cleanup", {}).get("closed") is True for row in launches),
        "real_smaller_profile_generation": result.get("real_model_executed") is True
            and runtime.get("requests", 0) > 0 and runtime.get("profile", {}).get("context_tokens") == 2048,
        "final_runtime_closed": runtime.get("state") == "unloaded" and runtime.get("process", {}).get("active_processes") == 0
            and runtime.get("process", {}).get("closed") is True,
        "no_accepted_electrical_regressions": result.get("accepted_electrical_regressions") == 0,
    }
    return checks


def verify_artifact_bindings(result):
    directory = Path(result["conversion"]["directory"]).resolve(strict=True)
    manifest = read_document(Path(result["conversion"]["manifest"]))
    verified = 0
    for item in manifest["artifacts"].values():
        path = (directory / item["path"]).resolve(strict=True)
        if not path.is_relative_to(directory) or digest(path) != item["sha256"]:
            raise ValueError("A published recovery artifact no longer matches its bound hash")
        verified += 1
    for source, expected in manifest["sources"].items():
        if digest(Path(source)) != expected:
            raise ValueError("An original Verilog source changed during recovery")
    for key in ("circuit", "sidecar"):
        if digest(Path(result["conversion"][key])) != result["selected"][key + "_sha256"]:
            raise ValueError("Recovery selection changed after native validation")
    return verified


def worker_main(path):
    from verilog2digital.llm import runtime
    request = read_document(Path(path))
    if request["strategy"] != "real4b" or request["options"]["model_id"] != "qwen3-4b-q4_k_m":
        raise ValueError("Recovery experiment supports only the measured Q4 CUDA profile")
    cap = validate_fault_limit(request["fault_memory_mib"])
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    with allocation_fault(runtime, cap) as launches:
        benchmark.worker_main(path)
    result_path = Path(request["directory"]) / "result.json"
    result = read_document(result_path)
    checks = recovery_checks(result, launches)
    result["allocation_fault"] = {"kind": "owned_host_commit_cap", "first_launch_bytes": cap,
        "normal_production_admission": True, "native_error_classifier": True, "launches": launches,
        "checks": checks, "valid": all(checks.values()),
        "notes": ["This induces a host Job allocation denial, not global GPU exhaustion.",
                  "A smaller retry is selected only by production, within the original attempt/time budgets."]}
    result["valid"] = result["valid"] and all(checks.values())
    atomic_document(result_path, result)
    print(json.dumps({"recovery_valid": result["valid"], "checks": checks}), flush=True)
    return 0 if result["valid"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fault-memory-mib", type=int, default=2048)
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require_venv(ROOT)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if args.worker_request:
        return worker_main(args.worker_request)
    try:
        validate_fault_limit(args.fault_memory_mib)
    except ValueError as error:
        parser.error(str(error))
    if args.output is None:
        parser.error("--output is required")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".runs") or not str(output).isascii():
        parser.error("Recovery output must be a new ASCII directory inside repository .runs")
    output.mkdir(parents=True, exist_ok=False)
    limits = JobLimits(deadline_seconds=300, memory_bytes=6 * 1024**3, java_heap_bytes=1024**3)
    options = {"model_id": "qwen3-4b-q4_k_m", "context_tokens": 4096,
               "max_attempts": 3, "budget_seconds": 60, "random_count": 256, "layout_timeout": 20}
    fingerprint = runtime_fingerprint()["sha256"]
    report = {"schema_version": 1, "evaluation": "production_allocation_recovery", "valid": False,
              "runtime_sha256_before": fingerprint, "limits": {"worker": asdict(limits), "refinement": options},
              "script_sha256": digest(Path(__file__)), "benchmark_script_sha256": digest(Path(benchmark.__file__)), "cases": []}
    for strategy in ("baseline", "real4b"):
        request = {"schema_version": 1, "case": "full_adder", "strategy": strategy,
                   "directory": str(output / strategy), "options": deepcopy(options),
                   "baseline_result": str(output / "baseline/supervised-result.json") if strategy != "baseline" else None,
                   "fault_memory_mib": args.fault_memory_mib,
                   "provenance": {"runtime_sha256": fingerprint, "script_sha256": report["script_sha256"]}}
        result = benchmark.run_owned(request, limits,
            worker_entrypoint=Path(__file__) if strategy == "real4b" else None)
        if result["valid"]:
            try:
                result["artifact_bindings_verified"] = verify_artifact_bindings(result)
            except Exception as error:
                result["valid"] = False
                result["error"] = {"code": "RECOVERY_ARTIFACT_BINDING", "message": str(error)[:2000]}
        report["cases"].append(result)
        atomic_document(output / "summary.json", report)
        print(json.dumps({"strategy": strategy, "valid": result["valid"], "seconds": result["wall_seconds"],
                          "error": result.get("error")}), flush=True)
        if not result["valid"]:
            break
    report["runtime_sha256_after"] = runtime_fingerprint()["sha256"]
    report["runtime_unchanged"] = report["runtime_sha256_after"] == fingerprint
    report["valid"] = len(report["cases"]) == 2 and all(row["valid"] for row in report["cases"]) and report["runtime_unchanged"]
    atomic_document(output / "summary.json", report)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
