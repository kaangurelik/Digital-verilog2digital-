"""Convert scalar Verilog and verify the exported Digital circuit exhaustively."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from toolchain import ROOT

sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project


def fresh_destination() -> Path:
    base = ROOT / ".runs/phase3-demo"
    destination = base
    index = 0
    while destination.exists():
        index += 1
        destination = base.with_name(f"{base.name}-{index:03d}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "tests/fixtures/verilog/scalar/full_adder.v")
    parser.add_argument("--top", help="Select a module; the default fixture is full_adder")
    parser.add_argument("--output", type=Path, help="A new output directory; existing results are preserved")
    arguments = parser.parse_args()
    destination = arguments.output if arguments.output is not None else fresh_destination()
    try:
        result = convert(Project(sources=[arguments.source], top=arguments.top), destination,
                         progress=lambda event: print(f"{event['stage']} ({event['elapsed_seconds']:.2f}s)", flush=True))
    except FrontendError as error:
        print(json.dumps({"state": "failed", "diagnostic": error.as_dict(), "directory": str(destination.resolve())}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
