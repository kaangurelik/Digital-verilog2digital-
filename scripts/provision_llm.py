"""Install only pinned official inference artifacts under the repository .tools."""
from pathlib import Path
import argparse
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.runtime import require_venv
from verilog2digital.llm.artifacts import load_lock, provision


def main():
    require_venv(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", choices=list(load_lock(ROOT)["models"]))
    parser.add_argument("--backend", action="append", choices=["cuda", "cpu"])
    parser.add_argument("--download-mode", choices=["stream", "ranges"], default="ranges")
    parser.add_argument("--source", choices=["hf", "modelscope", "both"], default="hf")
    parser.add_argument("--workers", type=int, choices=[1, 2, 4, 8, 16], default=8)
    args = parser.parse_args()
    models = args.model or ["qwen3-4b-q4_k_m"]
    result = provision(model_ids=models if args.download_mode == "stream" else [],
                       backends=args.backend or ["cuda", "cpu"], root=ROOT)
    if args.download_mode == "ranges":
        for model in models:
            subprocess.run([sys.executable, str(ROOT / "scripts/download_llm_ranges.py"), "--model", model,
                            "--workers", str(args.workers), "--source", args.source], check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result["models"] = models
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
