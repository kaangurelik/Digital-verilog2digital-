# MCP server

`v2d mcp` exposes the converter to any [Model Context Protocol](https://modelcontextprotocol.io)
host: Claude, VS Code, Cursor, local-model hosts and others. The host's model chooses the tools; the server
runs the same isolated jobs, native checks, original-Verilog behavior comparison and hash-bound publication
as the command line. A model can inspect and convert designs, read and validate results, look at previews and
propose layout refinements. It cannot publish a circuit that has not passed those checks, change a circuit's
components or connections, run shell commands, or read or write files outside the folders you allow.

## Register the server

The server speaks MCP over stdio. A host needs a command, its arguments and, optionally, environment
variables. From a source checkout:

```json
{
  "mcpServers": {
    "verilog2digital": {
      "command": "C:\\src\\Digital-verilog2digital\\.venv\\Scripts\\python.exe",
      "args": ["C:\\src\\Digital-verilog2digital\\scripts\\v2d.py", "mcp", "--allow-dir", "C:\\work\\circuits"]
    }
  }
}
```

From the portable package:

```json
{
  "mcpServers": {
    "verilog2digital": {
      "command": "C:\\Apps\\VerilogToDigital\\VerilogToDigital.exe",
      "args": ["--mcp", "--allow-dir", "C:\\work\\circuits"]
    }
  }
}
```

Hosts name this file and its top-level key differently (for example `mcpServers` or `servers`); the
`command`/`args` pair is the same. Use absolute paths. For a Unicode installation path, add
`"env": {"V2D_JOB_ROOT": "C:\\Users\\me\\AppData\\Local\\VerilogToDigital\\jobs"}` with any writable ASCII folder,
as described in [portable-windows.md](portable-windows.md).

```text
v2d mcp [--allow-dir DIR]... [--deadline SECONDS] [--memory-mib MIB] [--java-heap-mib MIB]
```

The server serves until its stdin closes and then exits with code 0. Closing stdin cancels running jobs, and
their process trees are cleaned up before exit. Stdout carries only MCP messages; the startup line, warnings
and logs go to stderr. A configuration error (an `--allow-dir` value that is empty or blank, missing, not a
folder or inside the server's own storage or installation, or invalid limits) prints a diagnostic code on stderr
and exits with code 1 before serving. The resource flags have the same meaning and bounds as for `v2d convert`.
They, or their defaults (300 seconds, 6144 MiB and 1024 MiB, which `doctor` reports as `default_limits`), are
the ceiling of every job: a project file's `resources`, or the limits recorded with an output that is refined,
can lower them but never raise them. `convert_design` adds a `PROJECT_RESOURCES_CAPPED` note when it lowered a
project's request.

Both protocol generations work: 2025-11-25 clients use the `initialize` handshake, and 2026-07-28 clients send
the protocol version and capabilities with every request and use `server/discover`. Neither era advertises list
change notifications or resource subscriptions, because the lists never change. The server does not use
sampling, roots or protocol logging. Long tools send progress notifications for their own request: stage
messages, with the job ID once a job exists. `convert_design` reports its preflight inspection as `preflight …`
stages before the conversion job's own stages. The final stages reach the
host before the result. Cancelling a tool call cancels its job; a call cancelled before its job started gives up
its place at once.

## Security model

`--allow-dir` (repeatable) lists the only folders whose contents tools may read or write; give absolute paths.
Every path argument is resolved to its real path, following junctions and symbolic links, and must stay inside
an allowed folder. Relative paths resolve against the first allowed folder. A path outside the allowed folders
is refused with `PATH_NOT_ALLOWED` whether or not it exists, so refusals reveal nothing about other files.
Without `--allow-dir` every path is refused with `NO_ALLOWED_DIRECTORIES`, and `doctor` explains how to
configure the server.

The rule covers source files, include folders, project files and the paths inside them, sequential traces,
outputs and results. A project file's own output folder is checked only when the call uses it, but its trace is
always checked because loading the project reads it. Include files are checked too, including those selected
by macros: the jobs snapshot every file the preprocessor actually includes, and a result that read a file
outside the allowed folders is withheld with `PATH_NOT_ALLOWED`. `convert_design` runs inspection jobs first, so
such an include is refused before anything is written to the output. A diagnostic located in a file outside
the allowed folders keeps its code and location but not its message. Outputs must be subfolders, not an
allowed folder itself. Server-owned storage and the installation are never valid user paths, even inside an
allowed folder: the job storage (`V2D_JOB_ROOT`, by default `.runs/jobs`), the candidate cache beside it (the
`cache` folder next to the job storage folder) and the program folders (`agent/src/verilog2digital`, `schemas`,
`scripts`, `.venv`, `.tools`, `gui/build`, `digital-adapter/target` and `src/main/dig/lib`).

The inspection, Yosys and the independent Icarus Verilog reference preprocess one identical design: they share
the defines plus `YOSYS=1` and `SYNTHESIS=1`, and each tool's own identity macros (`__ICARUS__`, and `__slang__`,
`__slang_major__` and `__slang_minor__` of the inspection) are undefined unless you define them, which defines
them for every tool. Code guarded by such a macro is therefore skipped by every tool, and the reference never
compiles code that the inspection did not check. The inspection rejects `initial` blocks and every system task
or function outside a small set of constant and cast functions, so design sources cannot open, write or dump
files while the reference simulates them.

`--allow-dir` confines path arguments and the files the preprocessor includes; it is not an operating-system
sandbox, and the HDL tools run with the server's own file access. A conversion inspects, lowers and simulates
its own snapshot of the sources, so a source edited after the preflight inspection is still inspected before
anything runs it. Such an edit can, however, include a file outside the allowed folders: the conversion's
snapshot is checked afterwards and the violation is reported, but the revision may already have been
published. Keep untrusted writers out of the allowed folders while the server runs. Every refinement replay
first checks that the inspected files are unchanged.

Strings derived from HDL — module, port and instance names, labels, parameter values and messages that quote
source text — are untrusted data, not instructions. Tool descriptions and results say so. Every result is
bounded: large reports stay in files and the result returns their paths.

## Tools

Tools are listed in this order. Paths can be absolute or relative to the first allowed folder.

| Tool | Purpose | Notes |
| --- | --- | --- |
| `doctor` | Tool and memory readiness, allowed folders, job storage, default limits | Read-only |
| `inspect_design` | Top candidates, modules, origins and located diagnostics | `sources` or `project`, plus `top`, `include_dirs`, `defines`, `parameters`; read-only |
| `convert_design` | Run a managed conversion job | Returns the selected `.dig`, revision folder, validation summary and preview availability |
| `validate_result` | Re-check a saved result in a job | `artifacts_only` skips the source/tool freshness checks; read-only |
| `get_result` | Hash-verified manifest summary, recorded validation and file list | No native work; read-only |
| `get_preview` | PNG preview as MCP image content plus metadata | `which`: `selected` or `before_refinement`; read-only |
| `job_status` | State, stage, limits, measurements and diagnostics of a job this server started | Omit `job_id` to list recent jobs; read-only |
| `cancel_job` | Request cancellation of a job this server started | Effective until the output selection commits |
| `refinement_start` | Start a refinement session for a converted output | Returns a session handle and the first prompt |
| `refinement_propose` | Submit one proposal | Returns the outcome, metric deltas and the next prompt |
| `refinement_finish` | Publish or discard the session | Publishes only from a complete verified job |

`convert_design` accepts the command line's conversion options with the same bounds: `profile`, `spacing`
(multiples of 20 from 40 through 400), `layout` (`auto` or `reserved`), `layout_timeout` (1–120 seconds),
`tunnels`, `flatten`, `seed`, `random_vectors` (0–2048), `trace`, `no_cache` and `replace`. It does not accept
model or refinement options, and a project file's refinement settings are ignored with a
`PROJECT_REFINEMENT_IGNORED` note. Use the refinement tools after converting. With `replace=true` an existing
output selects a new revision; earlier revisions are kept, and a failed or cancelled job keeps the previous
selection.

Errors are tool results with `isError: true` and a structured `diagnostic` with a stable code, for example
`INVALID_ARGUMENTS`, `PATH_NOT_ALLOWED`, `OUTPUT_EXISTS`, `SLANG_ERROR`, `BEHAVIOR_VALIDATION` or `CANCELLED`.
Job diagnostics keep their source path, line and column and the job ID. A failing tool never stops the server;
an unexpected internal error is reported as `SERVER_ERROR` with details on stderr.

Tool calls that run jobs share one job runner. Parallel calls wait their turn instead of failing with
`JOB_BUSY`; up to eight calls can wait, and more report `JOB_QUEUE_FULL`. When another process, such as the
desktop app or a `v2d convert`, uses the same job storage, the server retries for up to 30 seconds and then
reports `JOB_BUSY`.

## Layout refinement with your own model

The host's model is the refinement model. The workflow reuses the converter's refinement loop exactly: the
same prompt, the same response parser and action validator, rerouting, native verification of the candidate,
the strict Pareto rule and, before publication, the full original-Verilog behavior comparison.

1. `refinement_start(output)` reruns the output's recorded conversion as a private replay job with no
   responses. The loop writes the next attempt's prompt and then stops because no response exists. The tool
   returns a session handle and that prompt: `prompt.messages` (system and user messages),
   `prompt.response_schema`, `prompt.candidate_id`, the visible component and net IDs, and the current metrics.
2. Write one JSON object that follows the schema and pass it to `refinement_propose(session, response)`. The
   server reruns the replay with every kept response plus the new one and reports `accepted`,
   `retained_incumbent`, `rejected` or `no_change`, with metric deltas (candidate minus current; negative is
   better) and diagnostics, and returns the next prompt while attempts remain.
3. `refinement_finish(session)` reruns the conversion with all kept responses into the original output with
   `replace=true`. The published revision therefore comes from one complete verified job, and the previous
   revision is kept. `reproduced: true` confirms that the final job selected the same layouts as the session.
   With no accepted proposal, or with `publish=false`, nothing is published.

A proposal can move visible components on the 20-unit grid (at most 2000 units per axis) or prioritize the
rerouting of visible nets. It cannot change component types, attributes, rotations, widths or connections. A
proposal is accepted only when the rerouted candidate passes every native check and its bounding area, wire
length, bends and crossings are each less than or equal to the current values with at least one strictly lower.
Worse, equal, tradeoff, malformed, out-of-scope, colliding and timed-out proposals keep the current layout. See
[local-model.md](local-model.md) for the action bounds and the tunnel and hierarchy details.

Responses map to attempts in order across all canvases; `max_attempts` (1–8, default 3) is the shared limit.
Every response that reaches verification consumes an attempt, including rejected ones, and stays in the replay
sequence: each later prompt includes feedback from earlier attempts, and the final job must reproduce the same
attempt numbering. Invalid JSON, a schema violation or a stale `candidate_id` is rejected before it consumes
an attempt, up to 16 times per session. Each accepted proposal changes the candidate hash, so a proposal built
from an older prompt is stale. An empty `actions` list ends the current canvas; a hierarchy design then moves to
its next canvas, children first and the top last. `budget_seconds` (1–300, default 120) bounds verification
time in each replay job; the time your model spends thinking does not count. `request` adds a short layout
preference to the prompt, and `context_tokens` (1024–8192) shrinks the prompt for a small-context model.

Refinement starts from the deterministic layout of the output's recorded project and options; an earlier
refinement of that output is not carried over. When the selected revision was already refined,
`refinement_start` says so with `selected_revision` (its per-canvas metrics) and a
`REFINEMENT_STARTS_FROM_BASELINE` note, and `prompt.current_metrics` describe the deterministic layout. Such a
session publishes only a layout that is no worse than the selected revision on every canvas and metric and
better on at least one; otherwise `refinement_finish` reports `REFINEMENT_NOT_BETTER`, publishes nothing and
keeps the session open for further proposals or `publish=false`.

The recorded sources must be unchanged and inside the allowed folders, and `refinement_start` runs the same
preflight inspection as `convert_design`. Sessions are in-memory handles of this server process: at most four
are open, and starting a fifth ends the least recently used idle one. A session also ends after 30 idle minutes,
four hours after it started, when the server stops, when `refinement_finish` publishes or discards it, or when
its output or sources change. Unknown or ended handles report `REFINEMENT_SESSION_UNKNOWN`. Replay jobs use a
private scratch folder under the job storage folder, which the server removes. `refinement_propose` and
`refinement_finish` check, while they hold the job runner and so after any conversion queued before them, that
the sources are unchanged and, for `refinement_finish`, that the output still selects the revision the session
started from. Otherwise they report `REFINEMENT_SOURCES_CHANGED` or `REFINEMENT_OUTPUT_CHANGED`, publish
nothing and end the session. Another process, such as the desktop app, could still select a revision in the
moment between that check and the final job's own output lock.

## Resources

Read-only resources describe the rules a model must follow: `v2d://docs/user-guide` (supported Verilog),
`v2d://docs/scalar-conversion`, `v2d://docs/datapath-conversion`, `v2d://docs/sequential-conversion`,
`v2d://docs/hierarchy`, `v2d://docs/local-model` (refinement rules), `v2d://docs/mcp` and
`v2d://schemas/layout-actions`. A resource is listed only when its file is installed.

## Limits

| Limit | Value |
| --- | --- |
| Path argument | 4096 characters |
| Sources, include folders | 64 each |
| Defines, parameters | 256 each; values up to 4096 characters |
| Result JSON | 64 KiB; the longest lists are shortened to a common size and count what they lost in `<field>_omitted`, and other large fields are dropped and named in `omitted_for_size` |
| Diagnostics per result | 32; messages up to 2000 characters |
| Embedded preview | 2 MiB and 8000 pixels per side; larger previews return the file path |
| Waiting tool calls | 8 |
| Refinement | 4 sessions, 1–8 attempts, 1–300 seconds per job, 16 invalid responses, 30 idle minutes, 4 hours per session |
| Resource text | 256 KiB |

Job limits (deadline, committed memory and Java heap) and the per-job storage budget are those of the job
manager described in [jobs-and-cli.md](jobs-and-cli.md), capped by the server's flags or their defaults.

## Troubleshooting

- **`NO_ALLOWED_DIRECTORIES`**: add `--allow-dir` to the server's arguments in the host configuration and restart
  the server.
- **`PATH_NOT_ALLOWED`**: the path, an include file, a project path or a recorded source resolves outside the
  allowed folders or inside the server's own storage or installation, or the output is an allowed folder
  itself. For `validate_result`, use `artifacts_only=true` when the result's sources are elsewhere.
- **`UNSUPPORTED_INITIALIZATION` or `UNSUPPORTED_SYSTEM_CALL`**: the sources contain simulation-only code, such as
  an `initial` block or `$display`. Keep testbench code out of the design sources. Code guarded by a tool's own
  macro, such as `` `ifdef __ICARUS__ ``, is skipped by every tool unless you define that macro.
- **`REFINEMENT_NOT_BETTER`**: the output was already refined and the session's layout does not beat it yet.
  Propose further changes, or discard the session with `publish=false`.
- **The host reports that the server failed to start**: run the configured command in a terminal. A
  configuration error prints its code on stderr, for example `ALLOW_DIR_INVALID` (also for an empty value, such
  as an unset host variable) or `INVALID_LIMITS`. `UNSUPPORTED_JOB_ROOT` means `V2D_JOB_ROOT` must be set to an
  ASCII folder.
- **`JOB_BUSY`**: another application uses the same job storage folder. Wait for it, or give the server its own
  `V2D_JOB_ROOT`.
- **`TOOL_CHANGED`**: converter files or tools changed during a job. Retry after updates finish.
- **`OUTPUT_EXISTS`**: the output already has a revision; pass `replace=true`.
- **A long conversion is stopped**: raise `--deadline` (and the host's tool-call timeout, if it has one).
- **Progress is not shown**: hosts display progress only when they send a progress token; results are unaffected.
