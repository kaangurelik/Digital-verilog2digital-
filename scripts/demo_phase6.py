"""Convert and verify a synchronous counter using its natural reset sequence."""
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
    parser.add_argument("--source", type=Path, default=ROOT / "tests/fixtures/verilog/sequential/counter.v")
    parser.add_argument("--top")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tunnels", action="store_true")
    options = parser.parse_args()
    destination = options.output or ROOT / ".runs/phase6-demo"
    index = 0
    while not options.output and destination.exists():
        index += 1
        destination = ROOT / f".runs/phase6-demo-{index:03d}"
    result = convert(Project([options.source], top=options.top), destination, tunnels=options.tunnels,
                     progress=lambda event: print(f"{event['stage']} ({event['elapsed_seconds']:.2f}s)", flush=True))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
