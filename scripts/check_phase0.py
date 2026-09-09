"""Check compatibility across Digital, ELK and the HDL tools."""
import json
from toolchain import ROOT, executable, run


def main():
    fixture = ROOT / "tests/fixtures/verilog"
    output = ROOT / ".runs/setup"
    output.mkdir(parents=True, exist_ok=True)
    report = output / "phase0-checks.json"
    report.unlink(missing_ok=True)
    # The controlled script uses relative fixture paths; user paths never enter shell syntax.
    ys = output / "smoke.ys"
    ys.write_text("read_verilog tests/fixtures/verilog/smoke.v\nhierarchy -check -top smoke\nproc\ncheck -assert\nwrite_json .runs/setup/smoke.json\n", encoding="utf-8")
    run([executable("yosys"), "-s", str(ys)], log="yosys-smoke.log")
    netlist = json.loads((output / "smoke.json").read_text())
    assert len(netlist["modules"]["smoke"]["cells"]) == 1
    run([executable("iverilog"), "-g2012", "-s", "smoke_tb", "-o", str(output / "smoke.vvp"),
         str(fixture / "smoke.v"), str(fixture / "smoke_tb.v")], log="iverilog-smoke.log")
    run([executable("vvp"), str(output / "smoke.vvp")], log="vvp-smoke.log")
    assert "PASS: all four AND inputs" in (output / "vvp-smoke.log").read_text()
    run([executable("java"), "-Djava.awt.headless=true", "-jar", str(ROOT / "digital-adapter/target/digital-adapter.jar"), "--smoke-test"], log="java-compatibility.log")
    run([str(ROOT / "gui/build/v2d-gui.exe"), "--smoke-test"], log="wx-smoke.log", timeout=30)
    report.write_text(json.dumps({"schema_version": 1, "hdl": True, "java_elk": True, "wxwidgets": True}, indent=2), encoding="utf-8")
    print("Cross-tool compatibility checks passed")


if __name__ == "__main__":
    main()
