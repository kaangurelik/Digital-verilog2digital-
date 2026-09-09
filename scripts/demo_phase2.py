"""Elaborate a parameterized Verilog module into checked electrical JSON."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import sys

from toolchain import ROOT

sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.ir import validate_design


def main() -> None:
    source = ROOT / "tests/fixtures/verilog/frontend/parameterized.v"
    destination = ROOT / ".runs/phase2-demo"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "report.json").unlink(missing_ok=True)
    design = lower(Project(sources=[source], top="parameterized",
                           parameters={"WIDTH": 8, "BIAS": "8'h15"}), destination)
    validate_design(design)
    report = {
        "schema_version": 1,
        "frontend_valid": True,
        "top": design["top"],
        "ports": [{"name": port["name"], "direction": port["direction"],
                   "width": len(port["bits"]), "signed": port["signed"]}
                  for port in design["ports"]],
        "cell_types": dict(Counter(cell["type"] for cell in design["cells"])),
        "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "design_sha256": hashlib.sha256((destination / "design.json").read_bytes()).hexdigest(),
        "tools": design["tools"],
        "configuration": design["configuration"],
        "origins": design["origins"],
    }
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Frontend demonstration passed: {destination}")


if __name__ == "__main__":
    main()
