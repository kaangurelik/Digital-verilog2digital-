"""Conservative admission estimates, followed by measured runtime accounting."""
from __future__ import annotations

import os
from copy import deepcopy
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess

from ..jobs.processes import memory_status
from .artifacts import load_lock, ModelError

MIB = 1024**2
GIB = 1024**3


class _ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in (
        "peak_working_set", "working_set", "peak_paged_pool", "paged_pool", "peak_nonpaged_pool", "nonpaged_pool",
        "commit", "peak_commit", "private")]


def owned_process_memory(worker) -> dict:
    """Read memory counters through the owned handle under its lifecycle lock.

    Working set includes shared pages; PrivateUsage measures private commitment.
    """
    function = ctypes.WinDLL("kernel32", use_last_error=True).K32GetProcessMemoryInfo
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemory), wintypes.DWORD]
    function.restype = wintypes.BOOL
    with worker._lock:
        if worker._closed or not worker._process:
            return {"process_exited": True, "working_set_bytes": 0, "private_committed_bytes": 0}
        memory = _ProcessMemory()
        memory.cb = ctypes.sizeof(memory)
        if not function(worker._process, ctypes.byref(memory), memory.cb):
            raise OSError(ctypes.get_last_error(), "Cannot sample the owned server memory handle")
        return {"source": "Windows.K32GetProcessMemoryInfo.PROCESS_MEMORY_COUNTERS_EX",
                "scope": "owned root server process; working set includes mapped/shared resident pages",
                "pid": worker.pid, "working_set_bytes": memory.working_set,
                "peak_working_set_bytes": memory.peak_working_set,
                "private_committed_bytes": memory.private, "peak_private_committed_bytes": memory.peak_commit,
                "process_exited": False}


def probe_resources() -> dict:
    result = {"host": memory_status(), "gpus": [], "gpu_probe": "unavailable"}
    utility = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/nvidia-smi.exe"
    if not utility.is_file():
        return result
    try:
        output = subprocess.run([str(utility), "--query-gpu=index,name,driver_version,memory.total,memory.free,memory.used,compute_cap",
                                 "--format=csv,noheader,nounits"], capture_output=True, timeout=3,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout
        if len(output) > 16384:
            raise ValueError("GPU response exceeds size bound")
        for line in output.decode("utf-8", errors="strict").splitlines():
            index, name, driver, total, free, used, capability = (item.strip() for item in line.split(","))
            result["gpus"].append({"index": int(index), "name": name, "driver": driver,
                                   "total_bytes": int(total) * MIB, "free_bytes": int(free) * MIB,
                                   "used_bytes": int(used) * MIB, "compute_capability": capability})
        result["gpu_probe"] = "nvidia-smi; device-wide usage, not process-exclusive allocation"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def memory_requirements(profile, model=None):
    """Estimate memory for admission, including repacking and mapped weights.

    Estimates include margin but reserve nothing; native counters measure usage.
    """
    model = model or load_lock()["models"][profile["model_id"]]
    kv = model["layers"] * profile["context_tokens"] * 8 * 128 * 2
    kv = kv * 2 if profile["kv_type"] == "f16" else kv * 34 // 32
    fraction = profile["gpu_layers"] / (model["layers"] + 1)
    commitment = int(model["size"] * (.8 + .2 * fraction)) + kv + int(.9 * GIB)
    cpu_weights = int(model["size"] * (1 - fraction))
    gpu = int(model["size"] * fraction) + kv + 768 * MIB if profile["backend"] == "cuda" else 0
    return {"estimated_host_commit_bytes": commitment, "required_available_host_commit_bytes": commitment + GIB,
            "required_available_physical_bytes": max(3 * GIB, cpu_weights + commitment) + GIB,
            "estimated_gpu_bytes": gpu, "required_available_gpu_bytes": gpu + profile["vram_margin_bytes"] if gpu else 0,
            "basis": "conservative Q4 measured host/working-set calibration plus 1GiB host and configured GPU headroom"}


def select_profile(model_id="qwen3-4b-q4_k_m", resources=None, *, context_tokens=None,
                   host_memory_bytes=6 * GIB, idle_seconds=30) -> dict:
    lock = load_lock()
    if model_id not in lock["models"]:
        raise ModelError("MODEL_PROFILE", f"Unknown pinned model: {model_id}")
    resources = probe_resources() if resources is None else resources
    spec = lock["models"][model_id]
    host = resources["host"]
    # Admission estimates do not reserve memory.
    if context_tokens is not None and (type(context_tokens) is not int or not 1024 <= context_tokens <= 8192):
        raise ModelError("MODEL_PROFILE", "Explicit context must be an integer from 1024 through 8192")
    if type(host_memory_bytes) is not int or not 64 * MIB <= host_memory_bytes <= 12 * GIB:
        raise ModelError("MODEL_PROFILE", "Explicit host cap must be an integer from 64 MiB through 12 GiB")
    if type(idle_seconds) is not int or not 1 <= idle_seconds <= 300:
        raise ModelError("MODEL_PROFILE", "Idle timeout must be an integer from 1 through 300 seconds")
    context = context_tokens or 4096
    kv = spec["layers"] * context * 8 * 128 * 2 * 34 // 32
    margin = 1536 * MIB
    estimate = spec["size"] + kv + 768 * MIB
    fitting = [gpu for gpu in resources.get("gpus", []) if gpu["free_bytes"] >= estimate + margin
               and int(gpu["driver"].split(".")[0]) >= 552]
    backend = "cuda" if fitting else "cpu"
    if backend == "cpu" and (spec["layers"] > 36 or host["available_physical_bytes"] < spec["size"] + 2 * GIB):
        raise ModelError("MODEL_MEMORY", "Requested model does not fit the conservative GPU or CPU admission profile")
    profile = {"model_id": model_id, "backend": backend, "context_tokens": context if backend == "cuda" else (context_tokens or 2048),
            "gpu_layers": spec["layers"] + 1 if backend == "cuda" else 0,
            "gpu_index": fitting[0]["index"] if fitting else None,
            "kv_type": "q8_0", "batch_tokens": 256, "micro_batch_tokens": 128,
            "host_memory_bytes": host_memory_bytes, "vram_margin_bytes": margin,
            "estimated_gpu_bytes": estimate if backend == "cuda" else 0,
            "idle_seconds": idle_seconds, "threads": max(1, min(8, (os.cpu_count() or 4) // 2)),
            "admission": "estimates only; host Job committed-memory cap, no hard GPU quota"}
    needs = memory_requirements(profile, spec)
    profile["memory_requirements"] = needs
    if (host["available_commit_bytes"] < needs["required_available_host_commit_bytes"]
            or host["available_physical_bytes"] < needs["required_available_physical_bytes"]
            or host_memory_bytes < needs["estimated_host_commit_bytes"]):
        error = ModelError("MODEL_MEMORY", "Measured host-memory estimate plus headroom does not fit current availability or the configured Job cap")
        error.requested_profile = deepcopy(profile)
        raise error
    return profile


def recovery_profile(profile, resources=None) -> dict | None:
    """Select a lower-memory profile after a fresh admission check.

    Preserve caps, GPU and model/backend selection. The caller manages cleanup
    and the retry count.
    """
    # Lazy import avoids the runtime -> resources module dependency at import.
    from .runtime import validate_profile

    candidate = validate_profile(profile)
    model = load_lock()["models"][candidate["model_id"]]
    if (candidate["model_id"] != "qwen3-4b-q4_k_m" or candidate["backend"] != "cuda"
            or candidate["context_tokens"] != 4096 or candidate["gpu_layers"] != model["layers"] + 1
            or candidate["kv_type"] != "q8_0"):
        return None
    candidate["context_tokens"] = 2048
    candidate = validate_profile(candidate)
    resources = probe_resources() if resources is None else resources
    host = resources["host"]
    needs = candidate["memory_requirements"]
    gpu = next((item for item in resources.get("gpus", []) if item["index"] == candidate["gpu_index"]), None)
    if (host["available_commit_bytes"] < needs["required_available_host_commit_bytes"]
            or host["available_physical_bytes"] < needs["required_available_physical_bytes"]
            or candidate["host_memory_bytes"] < needs["estimated_host_commit_bytes"]
            or gpu is None or int(gpu["driver"].split(".")[0]) < 552
            or gpu["free_bytes"] < needs["required_available_gpu_bytes"]):
        return None
    return candidate
