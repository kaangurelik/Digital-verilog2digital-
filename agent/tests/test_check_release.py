"""Reject incomplete release validation results."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

from verilog2digital.project_file import load_project

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_release", ROOT / "scripts/check_release.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


@pytest.mark.parametrize("body", [b'{"state":"ready","state":"failed"}', b'{"nested":{"x":1,"x":2}}',
    b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}', b'{"\\udfff":1}', b'{"x":"\xff"}',
    b'[' * 65 + b'0' + b']' * 65, b''])
def test_incomplete_or_ambiguous_protocol_evidence_is_rejected(body):
    with pytest.raises((ValueError, RuntimeError, UnicodeError)):
        check.strict_json(body)


def test_valid_unicode_evidence_is_preserved_and_size_bounded(monkeypatch):
    assert check.strict_json(b'{"x":"\\ud83d\\ude00"}') == {"x": "😀"}
    monkeypatch.setattr(check, "MAX_JSON", 3)
    with pytest.raises(RuntimeError, match="byte limit"):
        check.strict_json(b'{"x":1}')


@pytest.mark.parametrize("name", [".", "../outside", "/outside", "C:/outside", "a//b", "a/./b", "a\\b",
    "a:stream", "a/NUL.txt", "a/COM1", "a/file.", "a/file ", "a/*.dll", "a\nb"])
def test_copied_artifacts_cannot_escape_or_alias_windows_paths(tmp_path, name):
    with pytest.raises(RuntimeError):
        check.contained(tmp_path, name, exists=False)


def test_exact_copy_detects_input_mutation_and_preserves_existing_output(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    source = tmp_path / "source"; source.mkdir()
    (source / "one.dll").write_bytes(b"original")
    recorded = {"one.dll": {"size": 8, "sha256": check.digest(source / "one.dll")}}
    target = check.copy_files(source, tmp_path / "first", recorded)
    assert (target / "one.dll").read_bytes() == b"original"
    (source / "one.dll").write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="changed"):
        check.copy_files(source, tmp_path / "second", recorded)
    with pytest.raises(RuntimeError, match="already exists"):
        check.copy_files(source, target, recorded)
    assert (target / "one.dll").read_bytes() == b"original"


def test_clean_environment_excludes_developer_and_python_injection(tmp_path, monkeypatch):
    for name in ("PYTHONPATH", "PYTHONHOME", "JAVA_TOOL_OPTIONS", "V2D_ROOT", "V2D_JAVA", "V2D_NATIVE_TOOLS_ROOT"):
        monkeypatch.setenv(name, "developer-injection")
    environment = check.clean_environment(tmp_path)
    assert not set(environment).intersection({"PYTHONPATH", "PYTHONHOME", "JAVA_TOOL_OPTIONS", "V2D_ROOT", "V2D_JAVA", "V2D_NATIVE_TOOLS_ROOT"})
    assert environment["PIP_NO_INDEX"] == environment["HF_HUB_OFFLINE"] == "1"
    assert "developer-injection" not in environment.values()
    assert str(ROOT / ".tools") not in environment["PATH"]


def test_clean_environment_preserves_windows_directories_needed_by_driver_discovery(tmp_path, monkeypatch):
    windows_directories = {"PROGRAMFILES": "C:/Program Files", "PROGRAMFILES(X86)": "C:/Program Files (x86)",
        "PROGRAMW6432": "C:/Program Files", "COMMONPROGRAMFILES": "C:/Program Files/Common Files",
        "COMMONPROGRAMFILES(X86)": "C:/Program Files (x86)/Common Files", "COMMONPROGRAMW6432": "C:/Program Files/Common Files"}
    for key, value in windows_directories.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "C:/developer-tools")
    monkeypatch.setenv("CUDA_PATH", "C:/developer-cuda")
    monkeypatch.setenv("V2D_JAVA", "C:/developer-java.exe")
    environment = check.clean_environment(tmp_path)
    assert {key: environment[key] for key in windows_directories} == windows_directories
    assert "C:/developer-tools" not in environment["PATH"]
    assert "CUDA_PATH" not in environment and "V2D_JAVA" not in environment


def test_independent_isolation_audit_requires_all_distributions_and_private_audited_dlls(tmp_path):
    package = tmp_path / "package"; package.mkdir()
    def file(relative):
        path = package / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"private")
        return str(path)
    executable = file(".venv/Scripts/python.exe")
    stdlib = file(".tools/python-runtime/Lib/json/__init__.py")
    native = file(".venv/Lib/site-packages/pyslang/_pyslang.pyd")
    python_dll = file(".tools/python-runtime/python314.dll")
    vc_dll = file(".tools/python-runtime/vcruntime140.dll")
    pywin32_dll = file(".venv/Lib/site-packages/pywin32_system32/pywintypes314.dll")
    packages = {name: {"version": "test", "path": file(".venv/Lib/site-packages/" + name + "/__init__.py")}
                for name in check.PRODUCTION_PACKAGES}
    bootstrap = {"root": str(package), "release": {"sha256": "a" * 64}, "job_root": str(tmp_path / "jobs"),
        "native_tools_root": str(package / ".tools"), "isolation": {"executable": executable, "prefix": str(package / ".venv"),
            "base_prefix": str(package / ".tools/python-runtime"), "stdlib": stdlib, "packages": packages,
            "native_modules": {"pyslang": native, "pywintypes": pywin32_dll},
            "loaded_dlls": {"python314.dll": python_dll, "vcruntime140.dll": vc_dll, "pywintypes314.dll": pywin32_dll}}}
    doctor = {"state": "ready", "bootstrap": bootstrap, "tools": {"python": {"path": executable, "available": True}},
              "fingerprint": {"sha256": "b" * 64}}
    check.audit_isolation(doctor, package)
    for name in ("attrs", "mcp", "pywin32", "cryptography"):
        missing = deepcopy(doctor); del missing["bootstrap"]["isolation"]["packages"][name]
        with pytest.raises(RuntimeError, match="production distribution"):
            check.audit_isolation(missing, package)
    unbound = deepcopy(doctor); del unbound["bootstrap"]["isolation"]["loaded_dlls"]["pywintypes314.dll"]
    with pytest.raises(RuntimeError, match="pywin32's private DLL"):
        check.audit_isolation(unbound, package)
    external = tmp_path / "external-vcruntime140.dll"; external.write_bytes(b"outside")
    doctor["bootstrap"]["isolation"]["loaded_dlls"]["vcruntime140.dll"] = str(external)
    with pytest.raises(RuntimeError, match="External audited DLL"):
        check.audit_isolation(doctor, package)


def _published(tmp_path):
    folder = tmp_path / "revision"; folder.mkdir()
    validation = {"valid": True, **{name: {"valid": True} for name in ("components", "topology", "bits", "model", "geometry")},
                  "behavior": {"valid": True, "coverage": "exhaustive_binary", "input_vectors": 8}}
    check.write_json(folder / "validation.json", validation)
    (folder / "circuit.dig").write_bytes(b"bound native circuit")
    manifest = {"state": "ready", "top": "test", "layout": {"metrics": {"wire_length": 20}},
                "artifacts": {key: {"path": name, "sha256": check.digest(folder / name)}
                              for key, name in (("validation", "validation.json"), ("circuit", "circuit.dig"))}}
    check.write_json(folder / "manifest.json", manifest)
    job = {"state": "ready", "resources": {"metrics": {"active_processes": 0}}, "runtime_fingerprint": {"sha256": "a" * 64}}
    check.write_json(tmp_path / "job.json", job)
    return {"state": "ready", "manifest": str(folder / "manifest.json"), "job_manifest": str(tmp_path / "job.json")}, validation


def _replace_validation(result, validation):
    manifest_path = Path(result["manifest"])
    manifest = check.read_json(manifest_path)
    path = manifest_path.parent / "validation.json"
    path.write_text(json.dumps(validation), encoding="utf-8")
    manifest["artifacts"]["validation"]["sha256"] = check.digest(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_release_validation_binds_bytes_and_every_independent_check(tmp_path):
    result, validation = _published(tmp_path)
    assert check.audit_conversion(result, {"vectors": 8})["behavior"]["input_vectors"] == 8
    for name in ("components", "topology", "bits", "model", "geometry", "behavior"):
        bad = deepcopy(validation); bad[name]["valid"] = False
        _replace_validation(result, bad)
        with pytest.raises(RuntimeError, match="independent check"):
            check.audit_conversion(result, {"vectors": 8})
    _replace_validation(result, validation)
    (Path(result["manifest"]).parent / "circuit.dig").write_bytes(b"altered")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        check.audit_conversion(result, {"vectors": 8})


def test_release_validation_rejects_incomplete_coverage_and_owned_processes(tmp_path):
    result, validation = _published(tmp_path)
    with pytest.raises(RuntimeError, match="coverage"):
        check.audit_conversion(result, {"vectors": 16})
    result["cache"] = {"hit": True}
    with pytest.raises(RuntimeError, match="cached"):
        check.audit_conversion(result, {"vectors": 8})
    result["cache"] = {"hit": False}
    job = check.read_json(result["job_manifest"]); job["resources"]["metrics"]["active_processes"] = 1
    Path(result["job_manifest"]).write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(RuntimeError, match="owned process"):
        check.audit_conversion(result, {"vectors": 8})


def test_release_sequential_checks_use_real_report_phase_and_sample_fields(tmp_path):
    result, validation = _published(tmp_path)
    validation["behavior"] = {"valid": True, "coverage": "sampled_cycles", "checked_cycles": 2, "input_vectors": 6,
        "assumptions": {"sample_count": 6}, "phase_order": ["before_rise", "after_rise", "after_fall"],
        "initialization": {"register_count": 2}}
    _replace_validation(result, validation)
    check.audit_conversion(result, {"cycles": 2, "registers": 2})
    validation["behavior"]["phase_order"].reverse()
    _replace_validation(result, validation)
    with pytest.raises(RuntimeError, match="phase coverage"):
        check.audit_conversion(result, {"cycles": 2, "registers": 2})


@pytest.mark.parametrize("change", ["bytes", "missing", "extra"])
def test_relocation_comparison_includes_every_hierarchy_child_and_sidecar(change):
    first = {"layout": {"metrics": {"wire_length": 10}}, "artifact_hashes": {
        "circuit": {"sha256": "a" * 64}, "dependency_0001": {"sha256": "b" * 64},
        "dependency_0002": {"sha256": "c" * 64}}}
    second = deepcopy(first)
    check.compare_native(first, second)
    if change == "bytes":
        second["artifact_hashes"]["dependency_0002"]["sha256"] = "d" * 64
    elif change == "missing":
        del second["artifact_hashes"]["dependency_0001"]
    else:
        second["artifact_hashes"]["dependency_0003"] = {"sha256": "e" * 64}
    with pytest.raises(RuntimeError, match="changed after"):
        check.compare_native(first, second)


@pytest.mark.parametrize("case", check.read_json(ROOT / "tests/fixtures/release/index.json")["cases"], ids=lambda case: case["id"])
def test_fixed_release_projects_resolve_their_original_sources_and_traces(case):
    project = load_project(ROOT / "tests/fixtures/release" / case["project"])
    assert project["project"].top == case["id"]
    assert all(path.is_file() for path in project["project"].sources)
    assert project["options"]["refinement"]["mode"] == "off"
    assert project["options"]["seed"] == 0
    if "cycles" in case:
        trace = project["options"]["sequential_trace"]
        assert len(trace["cycles"]) == case["cycles"]
        assert trace["initialization"]["kind"] == "input_sequence"
        assert all(trace["clock"]["port"] not in row["values"] for row in trace["cycles"] + trace["initialization"]["cycles"])


@pytest.mark.parametrize("body,stderr,active,expected", [
    (b'{"state":"ready"}', b'', 0, None),
    (b'', b'child failed before response', 0, ValueError),
    (b'{"state":"ready","state":"failed"}', b'', 0, ValueError),
    (b'{"state":"ready"}', b'x' * 33, 0, RuntimeError),
    (b'{"state":"ready"}', b'', 1, RuntimeError),
])
def test_owned_command_closes_on_bad_protocol_log_overflow_and_leaked_children(tmp_path, monkeypatch, body, stderr, active, expected):
    from verilog2digital.jobs import processes
    class Worker:
        def __init__(self):
            self.closed = False
            self.stdout = io.BytesIO(body)
        def poll(self): return 0
        def wait(self, timeout): return 0
        def close(self): self.closed = True
        def metrics(self): return {"active_processes": 0 if self.closed else active, "closed": self.closed}
    worker = Worker()
    def start(*args, **kwargs):
        kwargs["stderr_path"].write_bytes(stderr)
        return worker
    monkeypatch.setattr(processes.ManagedWorker, "start", start)
    monkeypatch.setattr(check, "MAX_LOG", 32)
    runner = check.Runner(tmp_path)
    package = tmp_path / "runtime"; package.mkdir()
    if expected is None:
        assert runner.command(package, "fake", ["--cli", "test"]) == {"state": "ready"}
    else:
        with pytest.raises(expected):
            runner.command(package, "fake", ["--cli", "test"])
    assert worker.closed
    record = check.read_json(tmp_path / "000-fake/command.json")
    assert record["closed_metrics"]["active_processes"] == 0
    assert (tmp_path / "000-fake/stdout.log").read_bytes() == body
    assert record["valid"] == (expected is None)


@pytest.mark.parametrize("override,passes", [(None, False), (900, True)])
def test_combined_journey_deadline_override_keeps_owned_cleanup(tmp_path, monkeypatch, override, passes):
    from verilog2digital.jobs import processes
    clock = [0.0]
    class Worker:
        closed = False
        stdout = io.BytesIO(b'{"state":"ready"}')
        def poll(self): return 0 if clock[0] >= 400 else None
        def wait(self, timeout): return 0
        def close(self): self.closed = True
        def metrics(self): return {"active_processes": 0 if self.closed or self.poll() == 0 else 1, "closed": self.closed}
    worker = Worker()
    def start(*args, **kwargs):
        kwargs["stderr_path"].write_bytes(b'')
        return worker
    monkeypatch.setattr(processes.ManagedWorker, "start", start)
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 100))
    runner = check.Runner(tmp_path, timeout=360)
    package = tmp_path / "runtime"; package.mkdir()
    if passes:
        assert runner.command(package, "journey", [], timeout=override) == {"state": "ready"}
    else:
        # Keep the worker alive past the first default deadline check.
        worker.poll = lambda: 0 if clock[0] >= 500 else None
        with pytest.raises(RuntimeError, match="owned deadline"):
            runner.command(package, "journey", [], timeout=override)
    record = check.read_json(tmp_path / "000-journey/command.json")
    assert record["harness_deadline_seconds"] == (override or 360)
    assert record["valid"] is passes
    assert record["closed_metrics"]["active_processes"] == 0


FAKE_MCP = r'''import json, sys, time
mode, calls = sys.argv[1], 0
tools = [{"name": "inspect", "inputSchema": {"type": "object"}}, {"name": "convert", "inputSchema": {"type": "object"}}]
if mode == "banner":
    print("server ready", flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method, modern = message["method"], "_meta" in message.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "server/discover":
        result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "cacheScope": "public", "ttlMs": 0}
    else:
        calls += 1
        if mode == "ping":
            print(json.dumps({"jsonrpc": "2.0", "id": "server-ping", "method": "ping"}), flush=True)
            if json.loads(sys.stdin.readline()) != {"jsonrpc": "2.0", "id": "server-ping", "result": {}}:
                raise SystemExit(9)
        result = {"tools": tools[::-1] if mode == "shuffle" and calls % 2 == 0 else tools}
    if modern:
        result["resultType"] = "complete"
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
if mode == "linger":
    time.sleep(60)
'''

SDK_MCP = '''from mcp.server.mcpserver import MCPServer
server = MCPServer("release-check-probe")

@server.tool()
def inspect_design(path: str) -> str:
    """Inspect a design."""
    return path

@server.tool()
def convert_design(path: str, top: str) -> str:
    """Convert a design."""
    return top

server.run("stdio")
'''


def _mcp_server(tmp_path, source, *arguments):
    script = tmp_path / "server.py"
    script.write_text(source, encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return [sys.executable, "-I", str(script), *arguments], evidence


@pytest.mark.parametrize("mode", ["ok", "ping"])
def test_mcp_acceptance_covers_both_eras_and_both_host_shutdown_paths(tmp_path, mode):
    command, evidence = _mcp_server(tmp_path, FAKE_MCP, mode)
    report = check.mcp_acceptance(command, evidence, startup=30, step=30, shutdown=20)
    assert report["valid"] and report["sessions"]["2025-11-25"]["tools"] == report["sessions"]["2026-07-28"]["tools"] == ["inspect", "convert"]
    assert report["sessions"]["2025-11-25"]["exit_code"] == report["sessions"]["2026-07-28"]["exit_code"] == 0
    assert report["sessions"]["terminated"]["exit_code"] != 0


def test_mcp_acceptance_requests_match_the_installed_sdk_server(tmp_path):
    command, evidence = _mcp_server(tmp_path, SDK_MCP)
    report = check.mcp_acceptance(command, evidence, startup=60, step=30, shutdown=20)
    sessions = report["sessions"]
    assert sessions["2025-11-25"]["tools"] == sessions["2026-07-28"]["tools"] == ["inspect_design", "convert_design"]
    assert "2026-07-28" in sessions["2026-07-28"]["supported_versions"] and sessions["2025-11-25"]["server"]["name"] == "release-check-probe"


@pytest.mark.parametrize("mode,message", [("banner", "MCP stdout closed: .*JSON"), ("shuffle", "changed between identical calls"),
                                          ("linger", "did not exit within 2 s after its host closed stdin")])
def test_mcp_acceptance_rejects_stdout_noise_unstable_tools_and_lingering_servers(tmp_path, mode, message):
    command, evidence = _mcp_server(tmp_path, FAKE_MCP, mode)
    with pytest.raises(RuntimeError, match=message):
        check.mcp_acceptance(command, evidence, startup=30, step=30, shutdown=2)


def test_hidden_mcp_host_always_reports_a_verdict_document(tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    arguments = ["--_mcp-host", "--launcher", str(tmp_path / "missing launcher.exe"), "--allow-dir", str(tmp_path), "--evidence", str(evidence)]
    assert check.main(arguments) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["valid"] is False and verdict["error"]["type"] == "FileNotFoundError"
    monkeypatch.setattr(check, "mcp_acceptance", lambda command, evidence: {"valid": True, "command": list(map(str, command))})
    assert check.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["command"][1:] == ["--mcp", "--allow-dir", str(tmp_path)]


def test_mcp_check_runs_the_host_as_an_owned_harness_worker(tmp_path):
    calls = []
    class Runner:
        verdict = {"valid": True, "sessions": {}}
        def command(self, package, name, arguments, **options):
            calls.append((package, name, arguments, options))
            return self.verdict
    root = tmp_path / "Kurulum \u03c0"
    root.mkdir()
    assert check.mcp_check(Runner(), root, "unicode", tmp_path / "projects", tmp_path)["valid"]
    package, name, arguments, options = calls[0]
    assert package == root and name == "unicode-mcp" and options["executable"] == sys.executable and "--_mcp-host" in arguments
    assert arguments[arguments.index("--launcher") + 1] == root / "VerilogToDigital.exe"
    Runner.verdict = {"valid": False, "error": {"message": "stdout carried noise"}}
    with pytest.raises(RuntimeError, match="stdout carried noise"):
        check.mcp_check(Runner(), root, "ascii", tmp_path / "projects", tmp_path)
