# Scalar conversion

The scalar profile converts one-bit Verilog logic into Digital circuits. It produces an editable native circuit, PNG/SVG previews, and separate component, connectivity, geometry, model, and behavioral results. The [datapath profile](datapath-conversion.md) adds buses and arithmetic while preserving this explicit scalar profile.

To convert a source file:

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert tests/fixtures/verilog/scalar/full_adder.v --output .runs/my-full-adder
```

The CLI uses managed output revisions and reports the selected circuit's exact path; `--replace` selects a new revision while preserving previous completed results. See [jobs-and-cli.md](jobs-and-cli.md) for project files, cancellation and revision selection. A successful circuit bundle contains `manifest.json` with `state: "ready"` and hashes of its final artifacts.

| Output | Contents |
| --- | --- |
| `circuit.dig` | The editable circuit; open this file in Digital. |
| `circuit.dig.ids.json` | Hash-bound native component identities for subsequent checks. |
| `preview.png`, `preview.svg` | Previews rendered by Digital. |
| `preview-metadata.json` | The native PNG transform and component geometry, bound to the preview and circuit hashes. |
| `baseline-preview.png` (only when layout refinement ran) | The validated canvas before refinement, for comparison. |
| `validation.json` | Native component, topology, geometry, model and Icarus comparison results. |
| `manifest.json` | Source/include hashes, tool versions, artifact hashes, timings and layout metrics. |
| `work/` (direct `conversion.convert()`; job storage for the CLI) | Inspected/lowered HDL, mapped electrical intent, candidate geometry, stimuli, both simulator results and logs. |

Multiple sources are positional arguments in compilation order. `--top` selects a top module when discovery is ambiguous. Repeat `-I` for include directories, `-D NAME=value` for macros, and `-P NAME=value` for top parameters. A parameter value is a decimal integer from 0 through 2147483647 or an unsigned sized literal such as `8'hff`; negative decimals and signed sized literals are rejected because the Yosys backend cannot preserve them, so declare the parameter signed and pass its bit pattern instead. `-D NAME` means `NAME=1`; `-D NAME=` explicitly defines an empty textual macro. The inspection, Yosys and Icarus preprocess one identical design: they share the explicit `YOSYS=1` and `SYNTHESIS=1` profile, and the tools' own identity macros (`__ICARUS__`, `__slang__`, `__slang_major__`, `__slang_minor__`) are undefined unless you define them, so no tool compiles code that the inspection skipped. `--spacing` accepts multiples of 20 from 40 through 400; an out-of-range value is rejected before a job starts. `--json --quiet` produces one machine-readable result on stdout.

The scalar mapping profile is stateless scalar logic: one-bit input/output ports, aliases, constants, bitwise gates, scalar logical operators/reductions, and one-bit multiplexers. Complete procedural logic is accepted when lowering produces that supported inventory. Parameterized modules and flattened hierarchy can be used within the same limits. The scalar bound is 12 input bits, 64 total mapped components, and at least one output. Use `--profile scalar-v1` to enforce it; the default `auto` can select the datapath or [sequential profile](sequential-conversion.md) when required.

Windows source, include and output paths may contain spaces and Unicode. The CLI stages sources inside its isolated worker. The lower-level `conversion.convert()` API uses the same verified snapshot whenever one of these paths is not ASCII: the native tools read private ASCII copies of the UTF-8 sources in a scratch directory under `V2D_JOB_ROOT` (default `.runs/jobs`, which must be ASCII), and that directory is removed afterwards. Original and staged copies are hash-checked before and after conversion. The requested directory receives the result, or `failure.json`, with original source paths, lines and columns; artifact hashes are recomputed for restored JSON. Its `work/` files keep the staged `f0000.v`-style names, and `work/source-snapshot.json` maps them to the original files. As in the CLI, an active `__FILE__` use outside an include filename is rejected because staging would change its value. ASCII paths, including paths with spaces, convert directly.

Mapping records each native pin's intended electrical bit before placement. The reserved router (`--layout reserved`) places components in evenly spaced columns, gives connected pins separate vertical channels and assigns nets distinct horizontal tracks. The default compact router and optional tunnels are described in [the user guide](../USER_GUIDE.md#supported-designs).

Before export, the geometry checker verifies grid positions, bounds/label clearance, pin approach directions and net ownership. After Digital saves and reloads the file, Python restores segment ownership from complete collinear coverage and checks the normalized geometry again. The independent connection oracle compares every native endpoint's actual peers with the original mapped intent. A strict interior crossing is distinguished from an electrical junction. Native component defaults, pin widths and the runnable model are checked separately.

Icarus compiles the original Verilog using the same project configuration, then every binary input combination is applied to both simulators. The report states the number of vectors and the combinational binary assumptions. Unknown outputs, missing samples and mismatches reject the result. The emitted circuit and sidecar must retain their validated hashes through rendering and publication. Progress callbacks are observational; a callback exception cannot turn an already published success into a failure.
