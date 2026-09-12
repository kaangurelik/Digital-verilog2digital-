"""Convert the ALU with compact native placement and validated visible routing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from toolchain import ROOT

sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.conversion import convert
from verilog2digital.frontend.project import Project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "tests/fixtures/verilog/datapath/alu4.v")
    parser.add_argument("--top")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tunnels", action="store_true")
    arguments = parser.parse_args()
    destination = arguments.output or ROOT / ".runs/phase5-demo"
    index = 0
    while not arguments.output and destination.exists():
        index += 1
        destination = ROOT / f".runs/phase5-demo-{index:03d}"
    result = convert(Project([arguments.source], top=arguments.top), destination, tunnels=arguments.tunnels,
                     progress=lambda event: print(f"{event['stage']} ({event['elapsed_seconds']:.2f}s)", flush=True))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
