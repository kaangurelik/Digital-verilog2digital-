"""Measure conversion jobs and native layout stress with the repository .venv.

Each run writes to a new folder. Memory reports use Windows Job Object
committed-byte accounting.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))

from verilog2digital.environment import runtime_fingerprint
from verilog2digital.frontend.project import Project
from verilog2digital.jobs.manager import JobLimits, run_job
from verilog2digital.jobs.processes import ManagedWorker, ProcessLimits, memory_status
from verilog2digital.jobs.publication import atomic_document, read_document
from verilog2digital.runtime import development_toolchain, require_venv


CASES = {"buffer": ("scalar/buffer.v", "buffer"),
         "alu": ("layout/compact_alu.v", "compact_alu"),
         "counter": ("sequential/counter.v", "counter"),
         "hierarchy": ("hierarchy/stateful_children.v", "stateful_children")}


def conversion_case(name, destination, limits, *, layout_timeout, cache=False, label=None):
    relative, top = CASES[name]
    project = Project([ROOT / "tests/fixtures/verilog" / relative], top=top)
    started = time.monotonic()
    try:
        result = run_job(project, destination / (label or name), limits=limits, cache=cache,
                         options={"layout_timeout": layout_timeout, "random_count": 0})
        wall = time.monotonic() - started
        manifest = read_document(Path(result["manifest"]))
        record = read_document(Path(result["job_manifest"]))
        validation = read_document(Path(result["validation"]))
        metrics = record["resources"]["metrics"]
        return {"case": label or name, "kind": "original_hdl_conversion", "valid": validation["valid"] is True,
                "wall_seconds": wall, "job_seconds": record["duration_seconds"],
                "worker_and_setup_seconds": manifest["job"]["timings"]["worker_seconds"],
                "conversion_stages_seconds": manifest.get("timings_seconds", {}),
                "metrics": metrics, "cache": result.get("cache", {"enabled": cache, "hit": False}),
                "layout": manifest["layout"], "behavior": {key: validation["behavior"][key] for key in
                    ("coverage", "input_vectors", "checked_cycles", "sample_count") if key in validation["behavior"]},
                "job_manifest": result["job_manifest"], "manifest": result["manifest"],
                "owned_processes_remaining": metrics["active_processes"]}
    except Exception as error:
        record = {"case": label or name, "kind": "original_hdl_conversion", "valid": False,
                  "wall_seconds": time.monotonic() - started,
                  "error": {"code": getattr(error, "code", type(error).__name__), "message": str(error)}}
        manifest = getattr(error, "job_manifest", None)
        if manifest and Path(manifest).is_file():
            job = read_document(Path(manifest))
            record.update(job_manifest=str(manifest), metrics=job.get("resources", {}).get("metrics", {}))
        return record


def stress_case(destination, limits, *, layout_timeout):
    """Own the existing 248-component benchmark and every native helper it uses."""
    toolchain = development_toolchain(ROOT)
    python = toolchain.executable("python")
    environment = toolchain.environment(python)
    environment["V2D_JAVA_HEAP_BYTES"] = str(limits.java_heap_bytes)
    output = destination / "near_limit_248"
    log = destination / "near_limit_248.stdout.log"
    argv = [python, str(ROOT / "scripts/benchmark_layout.py"), "--output", str(output),
            "--cases", "near_limit_248", "--stress-timeout", str(layout_timeout), "--no-render"]
    started = time.monotonic()
    worker, reader, errors = None, None, []
    metrics, record = {}, {"case": "near_limit_248", "kind": "synthetic_native_layout_fixture", "valid": False}
    try:
        worker = ManagedWorker.start(argv, cwd=ROOT, env=environment,
            stderr_path=destination / "near_limit_248.stderr.log",
            limits=ProcessLimits(job_memory_bytes=limits.memory_bytes,
                                 process_memory_bytes=limits.process_memory_bytes,
                                 active_process_limit=limits.active_process_limit))
        def drain():
            try:
                size = 0
                with log.open("xb") as file:
                    while block := worker.stdout.read(8192):
                        size += len(block)
                        if size > 16 * 1024**2:
                            raise RuntimeError("Benchmark output exceeds its 16 MiB bound")
                        file.write(block)
            except Exception as error:
                errors.append(str(error))
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        while worker.poll() is None:
            if time.monotonic() - started > limits.deadline_seconds:
                raise TimeoutError("Native benchmark exceeded its owned process-tree deadline")
            if errors:
                raise RuntimeError(errors[0])
            time.sleep(.05)
        metrics = worker.metrics()
        grace = time.monotonic() + 1
        while metrics["active_processes"] and time.monotonic() < grace:
            if time.monotonic() - started > limits.deadline_seconds:
                raise TimeoutError("Native benchmark exceeded its owned process-tree deadline")
            time.sleep(.05)
            metrics = worker.metrics()
        if metrics["active_processes"]:
            raise RuntimeError("Native benchmark root exited with live descendants")
        reader.join(timeout=2)
        if errors or reader.is_alive():
            raise RuntimeError(errors[0] if errors else "Native benchmark output did not finish")
        summary = read_document(output / "summary.json")
        record.update(valid=worker.poll() == 0 and summary["valid"], exit_code=worker.poll(),
                      layout_report=str(output / "summary.json"), layout_case=summary["cases"][0])
    except Exception as error:
        record["error"] = {"code": type(error).__name__, "message": str(error)}
    finally:
        if worker is not None:
            worker.close()
            metrics = worker.metrics()
        if reader is not None:
            reader.join(timeout=2)
    return {**record, "wall_seconds": time.monotonic() - started, "metrics": metrics,
            "owned_processes_remaining": metrics.get("active_processes")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for isolated jobs, revisions and measurements")
    parser.add_argument("--cases", nargs="+", choices=list(CASES), default=list(CASES))
    parser.add_argument("--layout-timeout", type=float, default=2, help="Compact routing search seconds before bounded fallback")
    parser.add_argument("--stress-timeout", type=float, default=10, help="Native 248-component planning and routing budget; separate from small HDL jobs")
    parser.add_argument("--deadline", type=float, default=300)
    parser.add_argument("--memory-mib", type=int, default=6144)
    parser.add_argument("--java-heap-mib", type=int, default=1024)
    parser.add_argument("--include-stress", action="store_true")
    parser.add_argument("--warm-cache", action="store_true", help="Add cold/warm enabled-cache buffer jobs after uncached measurements")
    args = parser.parse_args(argv)
    require_venv(ROOT)
    limits = JobLimits(deadline_seconds=args.deadline, memory_bytes=args.memory_mib * 1024**2,
                       java_heap_bytes=args.java_heap_mib * 1024**2)
    limits.validate()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    previous = os.environ.get("V2D_JOB_ROOT")
    os.environ["V2D_JOB_ROOT"] = str(output / "jobs")
    fingerprint = runtime_fingerprint()
    report = {"schema_version": 1, "valid": True, "cases": [], "limits": asdict(limits),
              "layout_timeouts_seconds": {"hdl": args.layout_timeout, "native_stress": args.stress_timeout},
              "platform": platform.platform(), "python": sys.version, "memory_before": memory_status(),
              "runtime_sha256": fingerprint["sha256"],
              "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "notes": ["Single observations include native tool startup and original-HDL validation; these are not statistical latency guarantees.",
                        "Memory limits are committed-memory ceilings, not allocations or reservations of RAM or VRAM.",
                        "Windows peak commit counters can include denied allocation attempts; no limit violation is inferred from peaks alone.",
                        "Native stress validates routing and topology; it is not an original-HDL behavior conversion."]}
    def append(record):
        report["cases"].append(record)
        report["valid"] = report["valid"] and record["valid"] and record.get("owned_processes_remaining") == 0
        atomic_document(output / "summary.json", report)
        print(json.dumps({"case": record["case"], "valid": record["valid"], "wall_seconds": record["wall_seconds"],
                          "peak_job_memory_bytes": record.get("metrics", {}).get("peak_job_memory_bytes"),
                          "cache": record.get("cache"), "error": record.get("error")}), flush=True)
    try:
        for name in args.cases:
            append(conversion_case(name, output, limits, layout_timeout=args.layout_timeout))
        if args.include_stress:
            append(stress_case(output, limits, layout_timeout=args.stress_timeout))
        if args.warm_cache:
            append(conversion_case("buffer", output, limits, layout_timeout=args.layout_timeout, cache=True, label="buffer_cache_cold"))
            warm = conversion_case("buffer", output, limits, layout_timeout=args.layout_timeout, cache=True, label="buffer_cache_warm")
            if not warm.get("cache", {}).get("hit"):
                warm["valid"] = False
                warm["error"] = {"code": "CACHE_MISS", "message": "Expected the second identical buffer job to hit its verified cache"}
            append(warm)
        current = runtime_fingerprint()
        report["runtime_unchanged"] = current["sha256"] == fingerprint["sha256"]
        report["valid"] = report["valid"] and report["runtime_unchanged"]
        report["memory_after"] = memory_status()
        report["max_observed_peak_job_memory_bytes"] = max((record.get("metrics", {}).get("peak_job_memory_bytes", 0)
                                                           for record in report["cases"]), default=0)
        atomic_document(output / "summary.json", report)
    finally:
        if previous is None:
            os.environ.pop("V2D_JOB_ROOT", None)
        else:
            os.environ["V2D_JOB_ROOT"] = previous
    print(f"Resource report: {output / 'summary.json'}", flush=True)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
