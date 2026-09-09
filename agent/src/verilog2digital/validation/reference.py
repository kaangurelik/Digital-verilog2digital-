"""Independent 1–64-bit combinational and clocked results from original Verilog.

The normalized design supplies interface and profile information only. No cell
in the normalized design is evaluated to produce reference output values.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from ..frontend.errors import FrontendError
from ..frontend.project import Project
from ..frontend.yosys import _parameter
from ..ir import validate_design
from ..runtime import development_toolchain, repository_root
from .sequential import PHASES, prove_initialization, validate_trace


MAX_VECTORS = 4096
COMPILE_TIMEOUT = 30
SIMULATION_TIMEOUT = 15
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class ReferenceError(FrontendError):
    """A reference could not be produced; this is never an equivalence pass."""


def _identifier(name: str) -> str:
    # Escaped identifiers end at whitespace. Punctuation and Verilog keywords
    # therefore remain identifiers, never generated testbench syntax.
    if not isinstance(name, str) or not name or any(not 33 <= ord(char) <= 126 for char in name):
        raise ReferenceError("REFERENCE_IDENTIFIER", f"Unsupported Verilog identifier: {name!r}")
    return "\\" + name + " "


def _configuration(project: Project, design: dict) -> None:
    validate_design(design)
    if project.top is not None and project.top != design["top"]:
        raise ReferenceError("REFERENCE_CONFIGURATION", "The reference project and normalized design select different top modules")
    expected_configuration = {
        "sources": [str(path) for path in project.sources],
        "include_dirs": [str(path) for path in project.include_dirs],
        "defines": project.effective_defines(), "parameters": project.parameters,
    }
    if design["configuration"] != expected_configuration:
        raise ReferenceError("REFERENCE_CONFIGURATION", "The reference must use the same ordered sources, includes, defines, and parameters as lowering")


def _profile(project: Project, design: dict, stimuli: list[dict[str, int]]) -> tuple[list[str], list[str]]:
    _configuration(project, design)
    for cell in design["cells"]:
        if cell["type"] == "$dff":
            raise ReferenceError("REFERENCE_PROFILE", "The combinational reference does not simulate registers or clocks")
    inputs = [port["name"] for port in design["ports"] if port["direction"] == "input"]
    outputs = [port["name"] for port in design["ports"] if port["direction"] == "output"]
    widths = {port["name"]: len(port["bits"]) for port in design["ports"]}
    if not outputs:
        raise ReferenceError("REFERENCE_PROFILE", "A reference requires at least one output port")
    for name in [design["top"], *inputs, *outputs]:
        _identifier(name)
    if not isinstance(stimuli, list) or not 1 <= len(stimuli) <= MAX_VECTORS:
        raise ReferenceError("REFERENCE_STIMULI", f"Provide between 1 and {MAX_VECTORS} input vectors")
    for index, values in enumerate(stimuli):
        if not isinstance(values, dict) or set(values) != set(inputs):
            raise ReferenceError("REFERENCE_STIMULI", f"Vector {index} must specify every input exactly once: {inputs}")
        for name, value in values.items():
            if type(value) is not int or not 0 <= value < (1 << widths[name]):
                raise ReferenceError("REFERENCE_STIMULI", f"Vector {index} input {name} must be an unsigned {widths[name]}-bit integer pattern")
    return inputs, outputs


def _testbench(project: Project, design: dict, inputs: list[str], outputs: list[str],
               stimuli: list[dict[str, int]] | None, *, trace: dict | None = None) -> tuple[str, str, str]:
    widths = {port["name"]: len(port["bits"]) for port in design["ports"]}
    identity = json.dumps({"top": design["top"], "sources": [str(path) for path in project.sources],
                           "inputs": inputs, "outputs": outputs, "widths": widths}, sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    base = "v2d_reference_" + digest
    modules = {origin["module"] for origin in design["origins"]} | {design["top"]}
    module, suffix = base, 0
    while module in modules:
        suffix += 1
        module = f"{base}_{suffix}"
    marker = "V2D_REFERENCE_" + digest
    ports = inputs + outputs
    signals = {name: f"v2d_signal_{index}" for index, name in enumerate(ports)}
    lines = ["// Generated independent reference: original project sources are compiled separately.",
             "`timescale 1ns/1ps", f"module {module};"]
    lines += [f"  reg [{widths[name] - 1}:0] {signals[name]};" for name in inputs]
    lines += [f"  wire [{widths[name] - 1}:0] {signals[name]};" for name in outputs]
    parameters = ""
    if project.parameters:
        parameters = " #( " + ", ".join(
            f".{_identifier(name)}({_parameter(value)})" for name, value in sorted(project.parameters.items())
        ) + " )"
    connections = ", ".join(f".{_identifier(name)}({signals[name]})" for name in ports)
    lines += [f"  {_identifier(design['top'])}{parameters} v2d_dut ( {connections} );", "  initial begin"]
    display = " ".join("%h" for _ in outputs)
    arguments = ", ".join(signals[name] for name in outputs)
    if trace is None:
        for index, vector in enumerate(stimuli):
            lines += [f"    {signals[name]} = {widths[name]}'h{vector[name]:x};" for name in inputs]
            lines += ["    #1;", f'    $display("{marker} {index} {display}", {arguments});']
        count = len(stimuli)
    else:
        clock = trace["clock"]["port"]
        lines += [f"    {signals[clock]} = 1'b0;"]
        for rows, checked in ((trace["initialization"]["cycles"], False), (trace["cycles"], True)):
            for index, row in enumerate(rows):
                lines += [f"    {signals[name]} = {widths[name]}'h{row['values'][name]:x};" for name in inputs if name != clock]
                for phase, level in zip(PHASES, (0, 1, 0)):
                    lines += [f"    {signals[clock]} = 1'b{level};", "    #1;"]
                    if checked:
                        lines += [f'    $display("{marker} {index} {phase} {display}", {arguments});']
        count = 3 * len(trace["cycles"])
    lines += [f'    $display("{marker} DONE {count}");', "    $finish;", "  end", "endmodule", ""]
    return module, marker, "\n".join(lines)


def _run(args: list[str], directory: Path, log: Path, toolchain, *, timeout: int, stage: str) -> None:
    try:
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(args, cwd=directory, env=toolchain.environment(args[0]),
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ReferenceError("REFERENCE_TIMEOUT", f"Reference {stage} exceeded {timeout} seconds; see {log}", path=str(log)) from error
    except OSError as error:
        raise ReferenceError("REFERENCE_TOOL", f"Unable to run reference {stage}: {error}", path=str(log)) from error
    if result.returncode:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4096))
            details = stream.read().decode("utf-8", errors="replace").strip()
        raise ReferenceError("REFERENCE_COMPILE" if stage == "compilation" else "REFERENCE_SIMULATION",
                             f"Reference {stage} failed (exit {result.returncode}):\n{details}", path=str(log))


def _samples(log: Path, marker: str, outputs: dict[str, int], count: int, *, sequential: bool = False) -> list[dict]:
    if log.stat().st_size > MAX_OUTPUT_BYTES:
        raise ReferenceError("REFERENCE_OUTPUT_LIMIT", "Reference output exceeded the 2 MiB limit", path=str(log))
    samples = []
    finished = False
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(marker + " "):
            continue
        fields = line.split()
        if fields[1:] == ["DONE", str(count)] and len(samples) == count and not finished:
            finished = True
            continue
        labels = ([str(len(samples) // 3), PHASES[len(samples) % 3]] if sequential else [str(len(samples))])
        if (finished or len(fields) != 1 + len(labels) + len(outputs)
                or fields[1:1 + len(labels)] != labels or len(samples) >= count):
            raise ReferenceError("REFERENCE_OUTPUT", "Reference samples were malformed, repeated, or out of order", path=str(log))
        values = fields[1 + len(labels):]
        if any(set(value.lower()) & {"x", "z"} for value in values):
            raise ReferenceError("REFERENCE_UNKNOWN", f"Reference vector {len(samples)} produced an X/Z output", path=str(log))
        parsed = {}
        for (name, width), value in zip(outputs.items(), values):
            if len(value) != (width + 3) // 4 or set(value.lower()) - set("0123456789abcdef"):
                raise ReferenceError("REFERENCE_OUTPUT", f"Reference output {name} is not a {width}-bit hexadecimal pattern", path=str(log))
            number = int(value, 16)
            if number >= 1 << width:
                raise ReferenceError("REFERENCE_OUTPUT", f"Reference output {name} exceeds its declared width", path=str(log))
            parsed[name] = number
        sample = {"values": parsed}
        if sequential:
            sample.update(cycle=len(samples) // 3, phase=PHASES[len(samples) % 3])
        samples.append(sample)
    if len(samples) != count or not finished:
        raise ReferenceError("REFERENCE_INCOMPLETE", f"Reference completed {len(samples)} of {count} requested samples", path=str(log))
    return samples


def _simulate_reference(project: Project, design: dict, workdir: Path,
                        stimuli: list[dict[str, int]] | None = None, *, trace: dict | None = None) -> dict:
    project = project.resolved()
    proof = None
    if trace is None:
        inputs, outputs = _profile(project, design, stimuli)
    else:
        _configuration(project, design)
        trace = validate_trace(design, trace)
        proof = prove_initialization(design, trace)
        inputs = [port["name"] for port in design["ports"] if port["direction"] == "input"]
        outputs = [port["name"] for port in design["ports"] if port["direction"] == "output"]
        if not outputs:
            raise ReferenceError("REFERENCE_PROFILE", "A reference requires at least one output port")
        for name in [design["top"], *inputs, *outputs]:
            _identifier(name)
    directory = Path(workdir).resolve()
    for path in [*project.sources, *project.include_dirs, directory]:
        if os.name == "nt" and not str(path).isascii():
            raise ReferenceError("UNSUPPORTED_PATH", "The current Windows HDL tools require ASCII paths. "
                                 "Move sources/includes or choose a work directory whose full path contains only ASCII characters.", path=str(path))
    module, marker, source = _testbench(project, design, inputs, outputs, stimuli, trace=trace)
    directory.mkdir(parents=True, exist_ok=True)
    testbench = directory / f"{module}.v"
    program = directory / f"{module}.vvp"
    compile_log, simulation_log = directory / "reference.compile.log", directory / "reference.simulation.log"
    version_log, provenance = directory / "reference.versions.log", directory / "reference.provenance.json"
    artifacts = {"testbench": testbench, "program": program, "compile_log": compile_log,
                 "simulation_log": simulation_log, "version_log": version_log, "provenance": provenance}
    if any(path in project.sources for path in artifacts.values()):
        raise ReferenceError("REFERENCE_WORKSPACE", "Reference artifacts would overwrite a supplied source; choose another work directory")
    testbench.write_text(source, encoding="utf-8")
    toolchain = development_toolchain(repository_root())
    compiler, runtime = toolchain.executable("iverilog"), toolchain.executable("vvp")
    compile_args = [compiler, "-g2005", "-grelative-include", "-s", module, "-o", program.name]
    for name, value in sorted(project.effective_defines().items()):
        # Icarus interprets bare -DNAME as NAME=1. Empty replacement text must
        # remain empty, while still testing as defined under `ifdef.
        compile_args += [f"-D{name}={value}"]
    for include in project.include_dirs:
        compile_args += ["-I", str(include)]
    compile_args += [str(path) for path in project.sources] + [testbench.name]
    simulation_args = [runtime, program.name]
    versions = {}
    with version_log.open("w", encoding="utf-8") as log:
        for name, executable in (("iverilog", compiler), ("vvp", runtime)):
            try:
                result = subprocess.run([executable, "-V"], cwd=directory, env=toolchain.environment(executable),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10,
                                        encoding="utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ReferenceError("REFERENCE_TOOL", f"Cannot identify the reference {name}: {error}") from error
            log.write(result.stdout + "\n")
            if result.returncode or not result.stdout.strip():
                raise ReferenceError("REFERENCE_TOOL", f"Cannot identify the reference {name}; see {version_log}")
            versions[name] = result.stdout.splitlines()[0]
    input_widths = {port["name"]: len(port["bits"]) for port in design["ports"] if port["direction"] == "input"}
    output_widths = {port["name"]: len(port["bits"]) for port in design["ports"] if port["direction"] == "output"}
    scalar = all(width == 1 for width in [*input_widths.values(), *output_widths.values()])
    assumptions = {"profile": "combinational_scalar" if scalar else "combinational_datapath",
                   "input_values": [0, 1] if scalar else "unsigned_integer_bit_patterns",
                   "input_widths": input_widths, "output_widths": output_widths,
                   "signed_ports": "two_complement_bit_patterns",
                   "unknown_outputs": "rejected", "settle_time_ns": 1,
                   "timescale": "1ns/1ps", "language": "Verilog-2005"}
    if trace is not None:
        assumptions.update(profile="synchronous_sequential", clock=trace["clock"], phases=list(PHASES),
                           input_values="unsigned_integer_bit_patterns", initialization=proof,
                           checked_cycles=len(trace["cycles"]), sample_count=3 * len(trace["cycles"]),
                           state_updates="original_Verilog_updates_settled_before_observation",
                           native_default_state="not_used_as_reference_or_initialization")
    manifest = {"schema_version": 1, "top": design["top"], "testbench_module": module,
                "sources": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                            for path in project.sources],
                "include_dirs": [str(path) for path in project.include_dirs],
                "defines": project.effective_defines(), "parameters": project.parameters,
                "input_ports": inputs, "output_ports": outputs, "stimuli": stimuli,
                "tools": versions, "assumptions": assumptions,
                "commands": {"compile": compile_args, "simulate": simulation_args},
                "artifacts": {name: str(path) for name, path in artifacts.items()}}
    if trace is not None:
        manifest.pop("stimuli")
        manifest["trace"] = trace
    provenance.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    _run(compile_args, directory, compile_log, toolchain, timeout=COMPILE_TIMEOUT, stage="compilation")
    _run(simulation_args, directory, simulation_log, toolchain, timeout=SIMULATION_TIMEOUT, stage="simulation")
    count = len(stimuli) if trace is None else 3 * len(trace["cycles"])
    return {"samples": _samples(simulation_log, marker, output_widths, count, sequential=trace is not None), "tools": versions,
            "assumptions": assumptions, "artifacts": manifest["artifacts"]}


def simulate_reference(project: Project, design: dict, workdir: Path, stimuli: list[dict[str, int]]) -> dict:
    """Simulate original Verilog with complete combinational input vectors.

    Sample unsigned bit patterns one nanosecond after applying inputs.
    Reject X/Z outputs.
    """
    return _simulate_reference(project, design, workdir, stimuli)


def simulate_sequential_reference(project: Project, design: dict, workdir: Path, trace: dict) -> dict:
    """Apply a proven input prefix and sample before and after clock edges.

    Drive only top-level data and clock inputs; HDL registers start unknown.
    Wait one nanosecond before each observation for nonblocking updates to settle.
    """
    return _simulate_reference(project, design, workdir, trace=trace)
