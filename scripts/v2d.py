"""Run the development CLI with the repository's isolated Python environment."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))

from verilog2digital.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
