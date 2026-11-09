# Hierarchical circuits

Hierarchy is preserved by default when the selected Verilog top instantiates child modules. Each elaborated specialization becomes a native Digital subcircuit, reused by its occurrences. The parent uses the child symbol's actual Digital geometry and port widths. Use `--flatten` to request one canvas instead.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert tests/fixtures/verilog/hierarchy/stateful_children.v --top stateful_children --output .runs/my-hierarchy
```

Open the returned `circuit` path in Digital. The CLI puts it in a managed `revisions/<job_id>/` directory. Keep the sibling `m_*.dig` files with it: these contain its child circuits. Their deterministic filenames identify elaborated specializations; readable circuit captions show module names and parameters. Each instance retains its original HDL label. Copy the complete circuit revision to move the project, or copy all files listed by its `package.json` together with that manifest. Native Digital only needs the `.dig` files; verification additionally needs their `.ids.json` sidecars. See [jobs-and-cli.md](jobs-and-cli.md) for revision selection.

The generated package has:

- `circuit.dig` and its identity sidecar, plus one child circuit and sidecar per reused specialization.
- `package.json`, containing relative dependency names, circuit/sidecar hashes, specialization parameters and occurrence paths.
- PNG/SVG previews of the top canvas with `preview-metadata.json`, `validation.json`, and a hashed run manifest, plus `baseline-preview.png` only when layout refinement ran.
- `work/`, containing frontend records, local layouts and checks, expanded electrical intent, stimuli/trace and both simulation results for diagnosis.

Every specialization is checked independently after native serialization and reload. A second oracle composes the entire hierarchy, including direct aliases, constants, slices, repeated bits, and tunnels. It compares every expanded primitive component and pin-bit connection against Digital's own expanded model. Local geometry stays associated with its own canvas.

Behavior is compared with Icarus running the original source. Small combinational interfaces are exhaustive; larger ones use directed and seeded vectors. Sequential designs use the same proven input-only initialization and three observations per cycle described in [sequential-conversion.md](sequential-conversion.md). Repeated register modules have independent state identities. State represented in the native hierarchy must be initialized even when a child output is unused; no native default value is assumed. Explicit flattening can eliminate unobservable child state during synthesis.

Dependencies are resolved only from the selected package and checked by hash on each native request. Registered types are generated filenames; arbitrary filesystem paths, missing/mutated dependencies, mismatched sidecars, recursive hierarchy and ambiguous port interfaces are rejected. Package manifests store generated occurrence IDs separately from original HDL path arrays, so a dot inside an escaped Verilog name is preserved as text.

The current supported conversion limit remains 250 expanded native components including helpers, with at most 250 on any one canvas. There may be at most 64 specializations, 256 occurrences and 12 levels of nesting. Each specialization needs an output interface. Buses and clocks follow the existing [datapath](datapath-conversion.md) and sequential restrictions. Hierarchy does not expand the set of supported HDL operators or clock forms.

The Java adapter accepts an explicit `package` object on description, layout, emit, inspect, render and simulation calls. Its `root` is the current absolute directory; each `modules` record contains `type`, `sha256`, and `sidecar_sha256`. A new directory requires a new root value. The library is scoped to that request. Inspection returns the physical top canvas and an `expanded` electrical view with occurrence-qualified primitive IDs. Expanded coordinates are local to their source canvases and are not a global layout. Package emits and previews never replace a registered module or its sidecar; paths are compared with the platform's rules, so on Windows a case variant of a registered name is rejected. A preview never replaces the circuit's adjacent `.ids.json`, even when `sidecar_path` is omitted. A child input that its specialization leaves unused has no expanded connection.
