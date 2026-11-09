"""Start the development GUI with its pinned native DLL environment."""
from pathlib import Path
import subprocess
import sys

from toolchain import ROOT, environment, executable


def main():
    python = executable("python")
    if ROOT.joinpath(".venv").resolve() != Path(sys.prefix).resolve():
        raise RuntimeError(f"Run this launcher through {python}")
    gui = ROOT / "gui/build/v2d-gui.exe"
    if not gui.is_file():
        raise RuntimeError("Build the interface with .venv/Scripts/python.exe scripts/build.py gui")
    return subprocess.call([str(gui), "--root", str(ROOT), *sys.argv[1:]], cwd=ROOT, env=environment(gui))


if __name__ == "__main__":
    raise SystemExit(main())
