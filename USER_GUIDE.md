# Verilog to Digital

Convert Verilog into editable Digital circuits with native previews and
independent connectivity and simulation checks.

## Portable Windows app

Extract the complete package into a writable directory and run
`VerilogToDigital.exe`. Python, Java, Digital and the HDL tools are included.
Keep the package together and save projects and exports outside its runtime files.

Copy `examples/fixtures` to a working directory before editing the examples.
Open a project under the copied `release` folder; copying the entire fixtures
directory preserves relative source, include and trace paths.

1. Add Verilog files in compilation order or open a project JSON file.
2. Use **Inspect tops**, select the top module and choose a new output folder.
3. Select **Convert**, then inspect the native preview and validation results.
4. Use **Export package** to write a new folder or **Open in Digital** to edit it.

Scroll to zoom, drag to pan and use **Fit** to show the complete circuit. Click
a component to inspect its identity. Disable **Component overlays** if labels
crowd the view. Project settings include source paths, macros, parameters,
layout options and an optional sequential trace.

Successful conversions select a verified revision through `current.json`.
Failed or cancelled replacements keep the previous selection. **Reopen circuit
package** accepts `manifest.json` or `current.json`, verifies the saved files and
reloads the native model; it displays the HDL coverage recorded at conversion.

## Command line

Run these commands from the extracted package directory:

```powershell
.\VerilogToDigital.exe --doctor
.\VerilogToDigital.exe --diagnostics
.\VerilogToDigital.exe --cli inspect C:\circuits\top.v --json --quiet
.\VerilogToDigital.exe --cli convert C:\circuits\top.v --top top -o C:\circuits\output
.\VerilogToDigital.exe --cli validate C:\circuits\output --json --quiet
```

Use `--project project.json` for saved settings. Paths in a project resolve
relative to that file. Command-line options override saved settings. Use
`--replace` for a new revision in an existing managed output, `--no-cache` for
a fresh conversion and `--help` for available options.

## MCP hosts

`.\VerilogToDigital.exe --mcp --allow-dir C:\circuits` serves the converter over
stdio to MCP hosts such as Claude, VS Code or Cursor. Their model can inspect,
convert, validate and preview designs and propose layout refinements; every
result passes the same checks. Tool paths and included files must be inside the
`--allow-dir` folders; that is a path rule, not an operating-system sandbox. See
[docs/mcp.md](docs/mcp.md) for host configuration, the tools and the security
model.

## Supported designs

The converter supports synthesizable Verilog-2005 with 1-64-bit signals, scalar
logic, readable arithmetic and mux components, specialized module hierarchies,
and synchronous registers sharing one dedicated rising-edge clock. Reset and
enable logic follows the source. Hierarchy and visible wires are enabled by
default; `--flatten` selects one canvas and `--tunnels` permits named connections.

Registers must reach known state through top-level inputs. The converter tries
bounded initialization patterns; use `--trace trace.json` for a custom startup
sequence. Each cycle supplies every nonclock input as an unsigned bit pattern.
For a module with `clk` and an eight-bit `d` input:

```json
{
  "schema_version": 1,
  "clock": {"port": "clk", "edge": "rising"},
  "initialization": {
    "kind": "input_sequence",
    "cycles": [{"values": {"d": 17}}]
  },
  "cycles": [{"values": {"d": 128}}, {"values": {"d": 255}}]
}
```

Memories, latches, asynchronous reset, multiple or gated clocks, source
initializers, timing controls, bidirectional nets and X/Z literals are unsupported.
Diagnostics identify unsupported source constructs before synthesis removes them.
Small combinational interfaces are checked exhaustively; larger interfaces and
sequential designs use bounded samples. Reports state the actual coverage.

## Local models and resource limits

Model refinement is off by default. A model bundle or an explicit model install
is required to enable it. Use the desktop model controls or `--llm 4b` / `--llm 14b`
to request layout proposals. Every proposal is checked; rejected proposals and
model failures preserve the best validated circuit.

The default job deadline is 300 seconds with a 6144 MiB committed-memory ceiling
and a 1024 MiB Java heap ceiling. Adjust these through the Resources tab or
`--deadline`, `--memory-mib` and `--java-heap-mib`. Limits do not reserve memory;
model admission also checks available RAM and VRAM.

## Paths and troubleshooting

Project and output paths may contain Unicode. Native tools need an ASCII job
directory. For a Unicode package location, set `V2D_JOB_ROOT` to an absolute,
writable ASCII path before launching. When your Windows profile path is ASCII:

```powershell
$env:V2D_JOB_ROOT = Join-Path $env:LOCALAPPDATA 'VerilogToDigital\jobs'
.\VerilogToDigital.exe
```

The launcher creates verified native-tool copies beside that directory. Without
an ASCII job root, a Unicode installation reports `UNSUPPORTED_JOB_ROOT`.
ASCII installations default to `.runs/jobs` inside the package.

Use **Run → Check tools and memory** or `--doctor` for dependency diagnostics.
If a runtime file differs from `release-manifest.json`, extract a fresh package.
Dependency notices and source availability are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Run from source

From the repository root, set up the pinned dependencies and build:

```powershell
python scripts/setup.py --only python
.\.venv\Scripts\python.exe scripts/setup.py
.\.venv\Scripts\python.exe scripts/doctor.py
.\.venv\Scripts\python.exe scripts/build.py all
.\.venv\Scripts\python.exe scripts/run_gui.py
```

Setup uses the local CMake, Ninja, UCRT64 compiler/wxWidgets and Maven tools.
Run Python commands through `.venv`; provisioned tools live in `.tools`.
For CLI use, replace `VerilogToDigital.exe --cli` with
`.\.venv\Scripts\python.exe scripts/v2d.py`.
