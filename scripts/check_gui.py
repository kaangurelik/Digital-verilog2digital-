"""Run native desktop checks with the isolated development DLL environment."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from toolchain import ROOT, environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflows", action="store_true", help="Also exercise real conversion/model workflows in the native window")
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise RuntimeError("Use .venv/Scripts/python.exe to run desktop checks")
    build = ROOT / "gui/build"
    directory = ROOT / ".runs/gui-checks" / str(time.time_ns())
    directory.mkdir(parents=True, exist_ok=False)
    commands = [("startup", [build / "v2d-gui.exe", "--root", ROOT, "--smoke-test"]),
                ("preview", [build / "v2d-preview-tests.exe", directory]),
                ("projects", [build / "v2d-project-tests.exe", directory]),
                ("defaults", [build / "v2d-main-frame-tests.exe", ROOT, "--project-defaults-only"]),
                ("transport", [build / "v2d-bridge-tests.exe", ROOT])]
    if args.workflows:
        commands.append(("workflows", [build / "v2d-main-frame-tests.exe", ROOT]))
    results = []
    env = environment(build / "v2d-gui.exe")
    for name, command in commands:
        started = time.monotonic()
        with (directory / (name + ".log")).open("w", encoding="utf-8") as log:
            try:
                code = subprocess.run(list(map(str, command)), cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=240 if name == "workflows" else 60).returncode
            except subprocess.TimeoutExpired:
                code = None  # subprocess.run has already killed the hung check.
        results.append({"name": name, "exit_code": code, "timed_out": code is None, "seconds": time.monotonic() - started,
                        "log": str(directory / (name + ".log"))})
        print(f"{name}: {'timed out' if code is None else f'exit {code}'}, {results[-1]['seconds']:.2f}s", flush=True)
        if code != 0:
            print((directory / (name + ".log")).read_text(encoding="utf-8"))
            break
    valid = all(result["exit_code"] == 0 for result in results) and len(results) == len(commands)
    (directory / "summary.json").write_text(json.dumps({"valid": valid, "checks": results}, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence: {directory}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
