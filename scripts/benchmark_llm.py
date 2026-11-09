"""Measure pinned profiles with owned processes; no circuit validation is implied."""
from __future__ import annotations

import argparse
import ctypes
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.runtime import require_venv
from verilog2digital.llm.runtime import LocalModelRuntime, ModelError
from verilog2digital.llm.resources import probe_resources, select_profile
from verilog2digital.jobs.publication import atomic_document
from verilog2digital.jobs.processes import ManagedWorker, ProcessLimits


def implementation_hashes():
    paths = [ROOT / "agent/src/verilog2digital/llm" / name for name in ("runtime.py", "resources.py", "artifacts.py")]
    paths += [ROOT / "scripts/llm-lock.json", Path(__file__).resolve()]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", "target", "delta", "reason"],
          "properties": {"action": {"enum": ["keep", "move"]}, "target": {"enum": ["gate_a", "gate_b"]},
                         "delta": {"type": "integer", "enum": [-40, -20, 0, 20, 40]},
                         "reason": {"type": "string", "maxLength": 100}}}
MESSAGES = [{"role": "system", "content": "Propose only geometry. Do not change components or connectivity. Return the required JSON."},
            {"role": "user", "content": "gate_a at x=100,y=100 and gate_b at x=400,y=100 are connected. Both are 40 units wide. There are no obstacles. Choose one legal 20-unit-grid movement reducing distance. Explain briefly."}]


def run_profile(name, profile, *, idle=False):
    client = LocalModelRuntime(profile)
    row = {"name": name, "profile": deepcopy(client.profile), "valid": False}
    start = time.monotonic()
    try:
        row["startup"] = client.start(timeout=120)
        row["responses"] = [client.generate_json(MESSAGES, SCHEMA, max_tokens=128, timeout=60, seed=1) for _ in range(2)]
        row["loaded"] = client.status()
        if idle:
            deadline = time.monotonic() + profile["idle_seconds"] + 8
            while time.monotonic() < deadline:
                status = client.status()
                if status.get("server", {}).get("is_sleeping") is True:
                    row["sleeping"] = status
                    row["resources_sleeping"] = probe_resources()
                    break
                time.sleep(.2)
            if "sleeping" not in row:
                raise ModelError("MODEL_IDLE", "Native idle unloading did not occur within the measured window")
            row["resumed_response"] = client.generate_json(MESSAGES, SCHEMA, max_tokens=128, timeout=60)
        row["valid"] = True
    except BaseException as error:
        row["diagnostic"] = error.as_dict() if hasattr(error, "as_dict") else {"code": type(error).__name__, "message": str(error)}
        if getattr(error, "allocation_evidence", None) is not None:
            row["allocation_evidence"] = error.allocation_evidence
    finally:
        client.close()
        row["closed"] = client.status()
        logfile = Path(row["closed"].get("log_path", ""))
        if logfile.is_file():
            lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
            row["native_allocations"] = [line[:500] for line in lines if any(term in line.lower()
                for term in ("model buffer size", "kv buffer size", "compute buffer size", "offloaded", "cuda_host"))][:128]
        row["wall_seconds"] = time.monotonic() - start
        row["valid"] = row["valid"] and row["closed"].get("process", {}).get("active_processes") == 0
    print(json.dumps({"case": name, "valid": row["valid"], "seconds": round(row["wall_seconds"], 3),
                      "peak_commit_mib": round(row["closed"].get("process", {}).get("peak_job_memory_bytes", 0) / 1024**2, 2),
                      "diagnostic": row.get("diagnostic")}), flush=True)
    return row


def denial_case(profile):
    tiny = deepcopy(profile)
    tiny.update(backend="cpu", gpu_index=None, gpu_layers=0, context_tokens=2048)
    original_start = ManagedWorker.start
    def limited(*args, **kwargs):
        kwargs["limits"] = ProcessLimits(job_memory_bytes=128 * 1024**2, active_process_limit=4)
        return original_start(*args, **kwargs)
    # Inject the allocation failure after normal admission checks.
    with patch.object(ManagedWorker, "start", side_effect=limited):
        denied = run_profile("host-commit-denial", tiny)
    denied["fault_injection"] = {"applied_job_memory_bytes": 128 * 1024**2,
                                 "normal_profile_admission_preserved": True}
    log = Path(denied["closed"].get("log_path", ""))
    excerpt = log.read_text(encoding="utf-8", errors="replace")[-8192:] if log.is_file() else ""
    denied["expected_failure"] = True
    denied["native_log_tail"] = excerpt
    denied["denial_observed"] = (not denied["valid"] and denied["closed"].get("process", {}).get("active_processes") == 0
        and any(term in excerpt.lower() for term in ("bad_alloc", "failed to allocate", "not enough memory", "out of memory", "cannot allocate")))
    denied["classification"] = "host Job committed-memory denial; not a forced GPU OOM"
    recovery = run_profile("after-host-denial-recovery", profile)
    return denied, recovery


def main():
    require_venv(ROOT)
    if os.name == "nt":
        # Suppress Windows crash dialogs for this benchmark and its child processes.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3-4b-q4_k_m")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--without-q5", action="store_true", help="Run Q4 comparisons while Q5 is still provisioning")
    parser.add_argument("--denial", action="store_true")
    parser.add_argument("--large", action="store_true")
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--host-memory-mib", type=int, default=6144)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".runs"):
        parser.error("Benchmark output must be below the repository .runs directory")
    output.mkdir(parents=True, exist_ok=False)
    os.environ["V2D_JOB_TMP"] = str(output / "runtime")
    report = {"schema_version": 1, "resources_before": probe_resources(), "cases": [],
              "implementation": implementation_hashes(),
              "notes": ["Two short constrained requests per profile; latency is observational, not a statistical estimate.",
                        "Host peak is owned Job committed bytes and may include denied requests. GPU samples are device-wide.",
                        "A valid response satisfies its JSON schema; this benchmark does not certify circuit quality."]}
    try:
        profile = select_profile(args.model, report["resources_before"], context_tokens=args.context_tokens,
                                 host_memory_bytes=args.host_memory_mib * 1024**2)
    except ModelError as error:
        report.update(valid=False, admission={"model_id": args.model, "measured_inference": False,
                                             "diagnostic": error.as_dict()})
        atomic_document(output / "summary.json", report)
        print(json.dumps(report["admission"]), flush=True)
        return 1
    profile["idle_seconds"] = 2
    plans = [(args.model + f"-ctx{profile['context_tokens']}", profile, True)]
    if args.compare:
        small = {**profile, "context_tokens": 2048, "idle_seconds": 30}
        partial = {**profile, "gpu_layers": 18, "idle_seconds": 30}
        cpu = {**small, "backend": "cpu", "gpu_index": None, "gpu_layers": 0}
        f16 = {**profile, "kv_type": "f16", "idle_seconds": 30}
        plans += [("q4-ctx2048", small, False), ("q4-partial18", partial, False), ("q4-cpu", cpu, False),
                  ("q4-kv-f16", f16, False)]
        if not args.without_q5:
            plans.append(("q5-ctx4096", select_profile("qwen3-4b-q5_k_m", report["resources_before"]), False))
    if args.large:
        plans.append(("qwen3-14b-q4", select_profile("qwen3-14b-q4_k_m", report["resources_before"]), False))
    for name, selected, idle in plans:
        report["cases"].append(run_profile(name, selected, idle=idle))
        atomic_document(output / "summary.json", report)
    if args.denial:
        report["cases"].extend(denial_case({**profile, "idle_seconds": 30}))
    report["resources_after"] = probe_resources()
    report["implementation_unchanged"] = report["implementation"] == implementation_hashes()
    report["valid"] = all(row.get("denial_observed", False) if row.get("expected_failure") else row["valid"] for row in report["cases"])
    report["valid"] = report["valid"] and report["implementation_unchanged"]
    atomic_document(output / "summary.json", report)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
