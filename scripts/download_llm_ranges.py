"""Bounded range fallback for slow official GGUF delivery; final SHA is mandatory."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import shutil
from pathlib import Path
import sys
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.runtime import require_venv
from verilog2digital.llm.artifacts import load_lock, model_path, sha256, _no_links
from verilog2digital.jobs.store import file_lease
from verilog2digital.jobs.publication import atomic_document, read_document


def main():
    require_venv(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(load_lock()["models"]), required=True)
    parser.add_argument("--workers", type=int, choices=[1, 2, 4, 8, 16, 32], default=8)
    parser.add_argument("--source", choices=["hf", "modelscope", "both"], default="hf")
    parser.add_argument("--transport", choices=["urllib", "winhttp"], default="urllib")
    parser.add_argument("--recover", type=Path)
    parser.add_argument("--trial-chunks", type=int, default=0)
    args = parser.parse_args()
    spec = load_lock()["models"][args.model]
    target = model_path(args.model)
    _no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 3600
    stop = threading.Event()
    with file_lease(target.with_suffix(".range.lock")):
        if target.is_file() and target.stat().st_size == spec["size"] and sha256(target) == spec["sha256"]:
            print("Pinned model already verified", flush=True)
            return
        progress = target.with_suffix(".ranges.json")
        _no_links(progress)
        state = read_document(progress, limit=2 * 1024**2) if progress.is_file() else {}
        if state and (state.get("sha256") != spec["sha256"] or state.get("size") != spec["size"]):
            raise ValueError("Range metadata belongs to a different artifact")
        if state:
            temporary = target.parent / state["temporary"]
        elif args.recover:
            temporary = args.recover.resolve()
        else:
            temporary = target.with_name(".range-" + uuid.uuid4().hex)
        if temporary.resolve().parent != target.parent.resolve() or not temporary.name.startswith(".range-"):
            raise ValueError("Range staging must be an owned model-directory file")
        _no_links(temporary)
        try:
            if not temporary.exists():
                if shutil.disk_usage(target.parent).free < spec["size"] + 512 * 1024**2:
                    raise ValueError("Insufficient free disk space for pinned model staging")
                with temporary.open("xb") as stream:
                    stream.truncate(spec["size"])
            if temporary.stat().st_size != spec["size"]:
                raise ValueError("Range staging size differs from the pinned artifact")
            chunk_size = 8 * 1024**2
            ranges = []
            state = state or {"sha256": spec["sha256"], "size": spec["size"], "temporary": temporary.name, "complete": {}, "trials": []}
            with temporary.open("rb") as stream:
                for first in range(0, spec["size"], chunk_size):
                    last = min(first + chunk_size, spec["size"]) - 1
                    stream.seek(first)
                    block = stream.read(last - first + 1)
                    if str(first) in state["complete"]:
                        if hashlib.sha256(block).hexdigest() != state["complete"][str(first)]["sha256"]:
                            raise ValueError("A previously completed range changed")
                        continue
                    # The legacy writer filled ranges in order, so a nonzero tail proves the
                    # prefix was written. Re-fetch the tail and verify the complete file hash.
                    recover_tail = bool(args.recover and any(block[-1024**2:]))
                    request_first = max(first, last + 1 - 1024**2) if recover_tail else first
                    ranges.append((first, last, request_first))
            url = f"https://huggingface.co/{spec['repository']}/resolve/{spec['revision']}/{spec['filename']}?download=true"
            mirrors = {
                "qwen3-4b-q4_k_m": "https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF/resolve/8cca206c792af04f8d452368bd30b43ec18007f1/Qwen3-4B-Q4_K_M.gguf",
                "qwen3-4b-q5_k_m": "https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF/resolve/8cca206c792af04f8d452368bd30b43ec18007f1/Qwen3-4B-Q5_K_M.gguf",
                "qwen3-14b-q4_k_m": "https://modelscope.cn/models/Qwen/Qwen3-14B-GGUF/resolve/4195fe4e5e948e7d4c63e2eaa672a3a8338b4b8b/Qwen3-14B-Q4_K_M.gguf",
            }
            if args.source != "hf" and args.model not in mirrors:
                raise ValueError("No independently confirmed official mirror revision is registered for this model")
            def download(bounds):
                first, last, request_first = bounds
                for attempt in range(3):
                    if stop.is_set() or time.monotonic() > deadline:
                        raise TimeoutError("Model download deadline or cancellation")
                    source = "modelscope" if args.source == "modelscope" or args.source == "both" and (first // chunk_size + attempt) % 2 else "hf"
                    endpoint = mirrors[args.model] if source == "modelscope" else url
                    request_url = endpoint + ("&" if "?" in endpoint else "?") + "v2d_range=" + uuid.uuid4().hex
                    try:
                        started = time.monotonic()
                        chunk_deadline = min(deadline, time.monotonic() + 180)
                        transport_metrics = {}
                        with temporary.open("r+b", buffering=0) as writer:
                            writer.seek(request_first)
                            if args.transport == "winhttp":
                                from winhttp_range import download_range
                                transport_metrics = download_range(request_url, request_first, last, spec["size"],
                                    writer, deadline=chunk_deadline, stop=stop)
                            else:
                                request = urllib.request.Request(request_url, headers={
                                    "Range": f"bytes={request_first}-{last}", "User-Agent": "verilog2digital-pinned-provisioner"})
                                with urllib.request.urlopen(request, timeout=20) as response:
                                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {request_first}-{last}/{spec['size']}":
                                        raise ValueError("Official CDN did not return the exact requested range")
                                    remaining = last - request_first + 1
                                    while remaining:
                                        if stop.is_set() or time.monotonic() > chunk_deadline:
                                            raise TimeoutError("Model range exceeded its deadline")
                                        block = response.read1(min(1024**2, remaining))
                                        if not block:
                                            raise ValueError("Official model range ended early")
                                        if writer.write(block) != len(block):
                                            raise OSError("Model range write was incomplete")
                                        remaining -= len(block)
                            writer.seek(first)
                            data = writer.read(last - first + 1)
                            return first, {"sha256": hashlib.sha256(data).hexdigest(), "source": source,
                                "range": [first, last], "downloaded_range": [request_first, last],
                                "seconds": time.monotonic() - started, "downloaded_bytes": last - request_first + 1,
                                "recovered_prefix": request_first != first, "transport": args.transport,
                                "transport_metrics": transport_metrics}
                    except (OSError, TimeoutError, ValueError) as error:
                        if attempt == 2:
                            raise
            completed = sum(row["range"][1] - row["range"][0] + 1 for row in state["complete"].values())
            selected = ranges[:args.trial_chunks] if args.trial_chunks else ranges
            trial_start = time.monotonic()
            transferred = 0
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(download, item) for item in selected]
                try:
                    for future in as_completed(futures):
                        first, row = future.result()
                        state["complete"][str(first)] = row
                        completed += row["range"][1] - row["range"][0] + 1
                        transferred += row["downloaded_bytes"]
                        atomic_document(progress, state)
                        print(f"{args.model}: {completed // 1024**2}/{spec['size'] // 1024**2} MiB", flush=True)
                except BaseException:
                    stop.set()
                    for future in futures:
                        future.cancel()
                    raise
            elapsed = time.monotonic() - trial_start
            state["trials"].append({"workers": args.workers, "source": args.source, "transport": args.transport, "seconds": elapsed,
                                    "downloaded_bytes": transferred, "mib_per_second": transferred / 1024**2 / elapsed})
            atomic_document(progress, state)
            print(json.dumps(state["trials"][-1]), flush=True)
            if completed < spec["size"]:
                print("Trial complete; durable ranges retained for resumption", flush=True)
                return
            if temporary.stat().st_size != spec["size"] or sha256(temporary) != spec["sha256"]:
                raise ValueError("Completed ranges failed the pinned official model SHA256")
            os.replace(temporary, target)
            print(f"Verified {args.model}: {spec['sha256']}", flush=True)
        finally:
            # Retain failed staging data; publish only after the whole-file hash matches.
            pass


if __name__ == "__main__":
    main()
