# Projects and command-line conversion

The CLI runs conversion, inspection and saved-result validation in isolated Python workers. Every worker uses this repository's `.venv`. On Windows, one Job Object owns the worker and its Java/HDL children from process creation, so cancellation or supervisor termination cleans up that specific process tree. The GUI uses the same commands and progress protocol.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py doctor --json
.\.venv\Scripts\python.exe scripts/v2d.py inspect design.v --json
.\.venv\Scripts\python.exe scripts/v2d.py convert design.v --top top -o output --json
.\.venv\Scripts\python.exe scripts/v2d.py validate output --json
```

`inspect` reports possible top modules without requiring conversion. `convert` accepts the existing source/include/macro/parameter, layout, tunnel, profile and sequential trace options. `--flatten` requests one canvas. `--events` writes UTF-8 JSON lines with `type: progress`, followed by a terminal `result` or `error` record, also after an unexpected internal failure; this is the desktop protocol. `--json --quiet` produces one result document. Diagnostic objects retain stable codes and original source paths, with line/column locations when available.

Output is a managed directory. Each successful conversion creates `revisions/<job_id>/` containing the circuit, child circuits, identity sidecars, previews, validation report and manifest. The small `current.json` file selects the active revision. Its atomic replacement is the publication point. The returned `circuit` path points directly to the selected `.dig`; open that file in Digital.

Run the same command with `--replace` to select a new revision. Earlier completed revisions remain available. A failed or cancelled replacement preserves the previous selection. Partial copies from failed publication are discarded within that job's newly created revision directory. The application does not take ownership of unrelated nonempty output directories; use a fresh directory for the first managed conversion.

The lower-level Python `conversion.convert()` API still creates a new standalone output directory, without job supervision, revisions or caching. It accepts Windows Unicode source, include and output paths through the same verified source snapshot in a removable ASCII scratch directory under `V2D_JOB_ROOT`; see [scalar-conversion.md](scalar-conversion.md). The durable API is `jobs.run_job(project, output_root, options=..., replace=..., cache=...)`. `run_inspection()` and `run_validation()` use the same worker ownership and supervision.

Project configuration is a versioned JSON file. Relative paths resolve against that file, regardless of the shell's working directory. For example:

```json
{
  "schema_version": 1,
  "sources": ["rtl/top.v", "rtl/child.v"],
  "top": "top",
  "include_dirs": ["include"],
  "defines": {"MODE": "1"},
  "parameters": {"WIDTH": 8},
  "conversion": {"spacing": 40, "tunnels": false, "flatten": false},
  "resources": {"deadline_seconds": 300, "memory_mib": 6144, "java_heap_mib": 1024},
  "output": "generated"
}
```

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert --project project.json --json
```

Command-line layout and resource settings override project defaults. Additional include directories are appended; explicit macros and parameters override matching project values. Project files reject unknown fields, duplicate JSON fields and invalid values before starting native work. `--spacing`, `--layout-timeout` and `--random-vectors` use the same bounds as the project file and are also rejected before a job or output directory is created.

Windows source/include/output paths may contain spaces and Unicode. The worker snapshots UTF-8 source files into a private ASCII directory, preserving compilation order, macro definitions, source line counts and active include resolution. Each include occurrence gets its own copy, including macro-selected filenames. Native slang identifies the precise include use before it is redirected. The original files and the staged copies have separate hash bindings; edits to either invalidate the job. Unrepresentable include forms produce located diagnostics. Original paths are restored only in recognized provenance fields, leaving HDL labels and parameter strings intact.

Active `__FILE__` uses outside include filenames are rejected as path-dependent behavior; changing a temporary filename must not change an HDL constant. Include uses retain their original file identity, and `__LINE__` keeps the original line number. Comments and unused macro definitions remain unchanged.

The resource defaults are a 300-second total deadline, a 6 GiB aggregate committed-memory ceiling, and a 1 GiB Java heap ceiling within that aggregate limit. These are caps, not allocations; Java starts with a small heap. Use `--deadline`, `--memory-mib` and `--java-heap-mib` to adjust them. Reports include the configured caps and measured Windows job counters. Committed-memory accounting differs from resident RAM; Windows may include a denied allocation attempt in its peak counter. Local-model admission also checks available GPU memory.

The supervisor checks deadlines and cancellation independently of native progress. Progress observers run through a bounded queue, so a blocked observer cannot prevent process cleanup. Worker messages, logs, source snapshots and caches have size limits. Job records preserve state, stage, timestamps, sequence numbers, limits, measurements, diagnostics and artifact references. Adapter and native temporary files belong to the job.

Worker storage has a 512 MiB per-job budget. Terminal-job retention defaults to 16 records and 512 MiB total, removing old owned jobs or their heavy intermediate artifacts while retaining explicit retention metadata. Active jobs and published output revisions are excluded from that cleanup. Record identity and resolved path containment are checked before deletion or recovery, so a junction or symbolic link cannot redirect either into another directory.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py job status .runs/jobs/<job_id> --json
.\.venv\Scripts\python.exe scripts/v2d.py job cancel .runs/jobs/<job_id> --json
.\.venv\Scripts\python.exe scripts/v2d.py job recover --json
```

Cancellation remains effective until the output selection commits; the last check runs immediately before `current.json` is replaced. Cancelling an already completed job reports `accepted: false` with its terminal status, including a job that completes while the request is written. A job whose selection has committed but whose record is still being finalized reports `already_committed` and `ready`. Jobs that share a job-storage directory run one at a time: a second concurrent command, or `job recover` while a job is active, reports `JOB_BUSY` instead of waiting. Recovery marks abandoned active records as interrupted; it never promotes partial candidates or kills processes by executable name. `V2D_JOB_ROOT` can select an ASCII job-storage directory when the installation path is unsuitable for the pinned native tools.

The candidate cache binds the original project settings, current active source/include hashes, conversion options and exact runtime/converter fingerprint. Cache entries contain verified artifact bundles; altered or incomplete entries are discarded. Its default budget is eight entries and 256 MiB, including temporary-copy headroom. It is stored in the `cache` directory beside the job-storage directory (`.runs/cache` by default, or the sibling of `V2D_JOB_ROOT`). `--no-cache` forces a fresh conversion. Cached JSON metadata uses original provenance, so pruning an old worker directory does not leave stale source paths in a new result.

`validate` checks every bound output file, current source/include hashes, runtime dependencies and converter code, then reloads the native model. It reports the original behavior coverage separately and does not claim to replay those tests. Changed inputs, tools or artifacts invalidate freshness. Use `--artifacts-only` to verify a moved bundle without the original source installation; its report explicitly limits that validation scope.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py benchmark design.v --top top -o layout-measurement --json
```

The benchmark runs compact and reserved layouts with the same source and conversion settings, disables cache reuse, and records full job time, conversion time, layout metrics and process measurements. Its `-o` directory must not exist yet; each run gets its own managed subdirectory and `benchmark.json` summarizes them. `--repeats` accepts one through five runs per strategy. Every benchmark result passes the ordinary electrical and behavior checks before being included.
