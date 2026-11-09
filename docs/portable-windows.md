# Portable Windows application

The portable application includes Python, Java, Digital and the HDL tools.

Extract the complete package into a writable directory and run
`VerilogToDigital.exe`. Keep the directory together: the launcher prepares a
private `.venv` using the packaged Python base and existing package files. It does
not use pip or install Python packages globally. Java, Digital, the HDL tools,
schemas, examples and required native DLLs are included.

Use the project controls described in [desktop.md](desktop.md) to select Verilog
sources and a top module, convert, inspect the native preview, export, and open
the circuit in Digital. Example projects are under `examples/fixtures/release`.
To experiment with them, copy the entire `examples/fixtures` directory to a
working folder outside the installation, then open a project from that copy.
This preserves relative source/include/trace paths. The bundled examples are
checked runtime files: saving edits over them makes later integrity checks fail.
See [supported designs](../USER_GUIDE.md#supported-designs),
[datapath conversion](datapath-conversion.md) and
[sequential conversion](sequential-conversion.md) for language and component limits.

The default conversion uses readable components and visible wires. Optional
Qwen refinement is off by default. The core package has no model weights; a
model bundle includes explicitly selected, pinned GGUF files and llama.cpp
runtimes. See [local-model.md](local-model.md) for model options and resource limits. Refinement preserves the best independently checked circuit when
a proposal is rejected or the model cannot run.

The launcher also exposes diagnostics and the existing CLI:

```powershell
.\VerilogToDigital.exe --doctor
.\VerilogToDigital.exe --diagnostics
.\VerilogToDigital.exe --cli inspect --project .\examples\fixtures\release\full_adder\project.json --json --quiet
```

## Use from an MCP host

`VerilogToDigital.exe --mcp` runs the converter as a Model Context Protocol server
over standard input and output. An MCP host such as Claude Desktop, Claude Code,
VS Code or Cursor then drives the converter with its own model. The launcher checks
the package and prepares the private runtime exactly as for `--cli`, then starts
`v2d mcp` with every argument after `--mcp` unchanged:

```text
VerilogToDigital.exe --mcp [--allow-dir DIR]... [--deadline S] [--memory-mib M] [--java-heap-mib H]
```

Repeat `--allow-dir` for each project directory the server may use. Relative
directories resolve from the working directory the host starts the launcher in, so
use absolute paths in host configuration. An empty or blank value stops the server
with `ALLOW_DIR_INVALID` instead of allowing the working directory, and so does a
folder inside the package or the job storage: those are never user paths.
`--deadline`, `--memory-mib` and `--java-heap-mib` take the same values as the
conversion options with those names in [jobs-and-cli.md](jobs-and-cli.md) and cap
every job, including those a project file asks more for.
`.\VerilogToDigital.exe --cli mcp --help` prints the server's own description of its
options.

Register the launcher as a stdio server. Use the full path of the extracted package.
Claude Desktop (`claude_desktop_config.json`) and Cursor (`.cursor/mcp.json`) use
this shape:

```json
{
  "mcpServers": {
    "verilog2digital": {
      "command": "C:\\Apps\\VerilogToDigital\\VerilogToDigital.exe",
      "args": ["--mcp", "--allow-dir", "C:\\Users\\me\\hdl"]
    }
  }
}
```

VS Code (`.vscode/mcp.json`) names the transport explicitly:

```json
{
  "servers": {
    "verilog2digital": {
      "type": "stdio",
      "command": "C:\\Apps\\VerilogToDigital\\VerilogToDigital.exe",
      "args": ["--mcp", "--allow-dir", "${workspaceFolder}"]
    }
  }
}
```

If the host cannot resolve `${workspaceFolder}`, for example with no folder open, the
server receives an empty value or a folder that does not exist and stops with
`ALLOW_DIR_INVALID`; open the project folder first or name the folder explicitly.

Claude Code registers the same command from a terminal:

```powershell
claude mcp add verilog2digital -- C:\Apps\VerilogToDigital\VerilogToDigital.exe --mcp --allow-dir C:\Users\me\hdl
```

Other hosts that start stdio servers take the same command and arguments. For a
Unicode installation path, add `V2D_JOB_ROOT` to the server's environment in the host
configuration (`"env": {"V2D_JOB_ROOT": "C:\\v2d-jobs"}` in the JSON files above, or
`-e V2D_JOB_ROOT=C:\v2d-jobs` before `--` for Claude Code), as described below. The
server receives `V2D_JOB_ROOT` and `V2D_NATIVE_TOOLS_ROOT` from the host. Other
`PYTHON*`, Java and `V2D_*` variables are removed, as for `--cli`.

Standard output carries only MCP messages. The launcher and the bootstrap hand the
host's pipes to the server unchanged and write their own diagnostics to standard
error, which hosts show as the server log. This mode never opens a message box: a
startup failure is one JSON line on standard error and exit code 1. Each start
hashes every packaged file before the server answers, which can take tens of
seconds; where a host lets you set a server startup timeout, allow at least a
minute. The first start after extraction also prepares the private environment,
and on a Unicode path its ASCII native copy. If a host gives up waiting for that
first start, run `.\VerilogToDigital.exe --doctor` once and restart the server
from the host.

The server runs as long as the host keeps it; there is no overall runtime cap, while
each conversion keeps its own deadline and memory limits. One Windows Job Object owns
the bootstrap, the server and its conversion workers, Java and HDL tools. Closing
standard input is the normal shutdown: the server finishes and exits, and the
launcher returns its exit code. If the server is still running 10 seconds after its
input closed, the launcher stops the whole tree. Terminating the launcher, which a
host does when shutdown takes too long, also ends every process in it.

Runtime files are checked against `release-manifest.json`. Missing or changed
dependencies cause an explicit failure. Extract a fresh copy to repair such a
package; copying DLLs from another installation changes the checked runtime.
Keep your project files and exports outside the immutable installation files;
the bundled examples are part of the inventory. Generated job state is excluded.
The package directory must be writable for its private environment and runtime
state. The pinned native HDL tools and Java launcher require ASCII installation
paths. For a Unicode package location, set `V2D_JOB_ROOT` to an absolute writable
ASCII directory before launching. Bootstrap does not search for an alternative
job directory automatically. Open PowerShell in the extracted package directory.
When your Windows profile path contains only ASCII characters, use its
local application-data directory:

```powershell
$env:V2D_JOB_ROOT = Join-Path $env:LOCALAPPDATA 'VerilogToDigital\jobs'
& '.\VerilogToDigital.exe'
```

Bootstrap creates a checked copy of the affected native tools and Java assets
beside that job root. Every copied file is bound to the release inventory. Python
and its libraries remain inside the package. Without an ASCII job root, a Unicode
installation reports `UNSUPPORTED_JOB_ROOT`. In an ASCII installation, the default
is the package's `.runs/jobs` directory and this setting is optional.
If your profile path itself contains non-ASCII characters, select another
absolute writable ASCII directory for `V2D_JOB_ROOT` instead.
Native copies are kept by release content identity and reused. A temporary
Windows sharing error during first-time publication receives a bounded retry
at the same destination. Persistent errors remain visible; the application does
not change permissions or security settings to bypass them. A damaged native
copy reports `RELEASE_CHANGED` with its path, and extracting the package again
does not replace it: close the application, delete that release's folder under
`native-tools` beside the job root, then start it to create a new checked copy.

Dependency notices and source availability are listed in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
