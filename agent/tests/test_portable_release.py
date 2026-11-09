"""Portable runtime closure, private bootstrap and relocation boundaries."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from verilog2digital import environment, release, runtime
from verilog2digital.frontend.errors import FrontendError

ROOT = Path(__file__).resolve().parents[2]
CORE = ["scripts/portable_bootstrap.py", "scripts/v2d.py", "scripts/toolchain.py", "scripts/toolchain-lock.json",
        "agent/src/verilog2digital/release.py", "agent/src/verilog2digital/runtime.py", "gui/build/v2d-gui.exe",
        "digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar", ".tools/runtime-support.jar",
        ".tools/python-runtime/python.exe", ".tools/python-runtime/python314.dll",
        ".tools/python-runtime/Lib/venv/scripts/nt/venvlauncher.exe",
        ".tools/python-runtime/Lib/venv/scripts/nt/venvwlauncher.exe",
        ".tools/jdk/bin/java.exe", ".tools/jdk/bin/javaw.exe", ".tools/oss-cad-suite/bin/yosys.exe",
        ".tools/oss-cad-suite/bin/iverilog.exe", ".tools/oss-cad-suite/bin/vvp.exe",
        ".tools/oss-cad-suite/bin/companion.dll", ".venv/Lib/site-packages/example/__init__.py"]


def manifest(root):
    files = {}
    for name in CORE:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"jdk": {"directory": "jdk"}, "hdl": {"directory": "oss-cad-suite"}}).encode()
                         if name == "scripts/toolchain-lock.json" else name.encode())
        files[name] = {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    value = {"schema_version": 1, "product": "verilog2digital", "version": "0.1.0", "files": files, "optional_models": []}
    save(root, value)
    return value


def save(root, value):
    (root / release.MANIFEST).write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.delenv("V2D_NATIVE_TOOLS_ROOT", raising=False)
    monkeypatch.delenv("V2D_JOB_ROOT", raising=False)
    root = tmp_path / "package"
    root.mkdir()
    return root, manifest(root)


def test_identical_release_relocation_preserves_content_identity(package, tmp_path):
    root, _ = package
    before = release.verify_release(root)
    moved = tmp_path / "copied release \u03a9"
    shutil.copytree(root, moved)
    assert release.verify_release(moved) == before


@pytest.mark.parametrize("action", ["delete", "same_size", "growth"])
def test_companion_runtime_dll_changes_fail_before_admission(package, action):
    root, _ = package
    path = root / ".tools/oss-cad-suite/bin/companion.dll"
    if action == "delete":
        path.unlink()
    elif action == "same_size":
        path.write_bytes(b"!" * path.stat().st_size)
    else:
        path.write_bytes(path.read_bytes() + b"!")
    with pytest.raises(FrontendError, match="runtime file") as caught:
        release.verify_release(root)
    assert caught.value.code == "RELEASE_CHANGED"


@pytest.mark.parametrize("name", ["../escape", "C:/escape", "a/../b", "a//b", "a\\b", ".venv/pyvenv.cfg", ".runs/jobs/job.json"])
def test_inventory_rejects_escaping_or_mutable_paths(package, name):
    root, value = package
    value["files"][name] = {"sha256": "0" * 64, "size": 0}
    save(root, value)
    with pytest.raises(FrontendError):
        release.verify_release(root)


@pytest.mark.parametrize("name", [".. /escape", "child./file", "child /file", "CON", "nul.txt",
    "COM1/file", "LPT9.txt", "COM\u00b9.txt", "CONOUT$", "AUX .txt", "file:stream", "file\x01",
    "file\nname", 'file"name', "file?name", "file*name", "file|name", "<file>"])
def test_public_and_canonical_paths_reject_windows_aliases(package, name):
    root, _ = package
    for check in (release.relative_file, release._relative_file):
        with pytest.raises(FrontendError) as caught:
            check(root, name)
        assert caught.value.code == "RELEASE_PATH"


def test_canonical_paths_keep_root_and_unicode_names_without_resolving_each_file(package, monkeypatch):
    root, _ = package
    root = root.resolve(strict=True)
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("canonical path resolved again"))
    assert release._relative_file(root, "data/\u03a9.txt") == root / "data/\u03a9.txt"
    assert release._relative_file(root, "missing/child") == root / "missing/child"


def test_public_paths_still_canonicalize_relative_root(package, monkeypatch):
    root, _ = package
    monkeypatch.chdir(root.parent)
    assert release.relative_file(Path("package"), "scripts/v2d.py") == root / "scripts/v2d.py"


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
@pytest.mark.parametrize("where", ["root", "ancestor", "file"])
def test_canonical_paths_check_link_status_fresh_on_every_call(package, monkeypatch, kind, where):
    root, _ = package
    name = "scripts/v2d.py"
    target = {"root": root, "ancestor": root / "scripts", "file": root / name}[where]
    changed = [False]
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: changed[0] and path == target or original(path))
    assert release._relative_file(root, name) == root / name
    changed[0] = True
    with pytest.raises(FrontendError) as caught:
        release._relative_file(root, name)
    assert caught.value.code == "RELEASE_PATH" and caught.value.path == str(target)


@pytest.mark.parametrize("value", [None, [], {"model_id": [], "backends": ["cpu"]},
    {"model_id": "qwen3-4b-q4_k_m", "backends": [{}]},
    {"model_id": "qwen3-4b-q4_k_m", "backends": ["cpu", "cpu"]}])
def test_bad_optional_model_descriptors_are_structured(package, value):
    root, document = package
    document["optional_models"] = [value]
    save(root, document)
    with pytest.raises(FrontendError) as caught:
        release.verify_release(root)
    assert caught.value.code == "RELEASE_MANIFEST"


def test_missing_manifest_does_not_turn_private_runtime_into_development(package):
    root, _ = package
    (root / release.MANIFEST).unlink()
    with pytest.raises(FrontendError) as caught:
        release.verify_release(root)
    assert caught.value.code == "RELEASE_MANIFEST"


def test_missing_required_inventory_entry_is_rejected(package):
    root, document = package
    del document["files"][".tools/python-runtime/python314.dll"]
    save(root, document)
    with pytest.raises(FrontendError, match="required"):
        release.verify_release(root)


def test_unlisted_private_package_code_rejected_but_generated_cache_ignored(package):
    root, _ = package
    cache = root / ".venv/Lib/site-packages/example/__pycache__/x.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"compiled")
    assert release.verify_release(root)
    injected = root / ".venv/Lib/site-packages/extra.pth"
    injected.write_text("import unexpected")
    with pytest.raises(FrontendError, match="unbound"):
        release.verify_release(root)


def test_release_manifest_duplicate_keys_and_nonfinite_are_rejected(package):
    root, _ = package
    for text in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}'):
        (root / release.MANIFEST).write_text(text)
        with pytest.raises(FrontendError):
            release.release_manifest(root)


def test_ascii_job_root_override_supports_unicode_runtime(package, tmp_path, monkeypatch):
    root, _ = package
    unicode_root = tmp_path / "runtime \u03a9"
    shutil.copytree(root, unicode_root)
    with pytest.raises(FrontendError) as caught:
        release.job_root(unicode_root)
    assert caught.value.code == "UNSUPPORTED_JOB_ROOT"
    jobs = tmp_path / "ascii-jobs"
    monkeypatch.setenv("V2D_JOB_ROOT", str(jobs))
    assert release.job_root(unicode_root) == jobs and jobs.is_dir()


def test_native_mirror_must_match_original_inventory(package, tmp_path, monkeypatch):
    root, value = package
    lock = root / "scripts/toolchain-lock.json"
    lock.write_text(json.dumps({"jdk": {"directory": "jdk"}, "hdl": {"directory": "oss-cad-suite"}}))
    value["files"]["scripts/toolchain-lock.json"] = {"size": lock.stat().st_size, "sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}
    save(root, value)
    before = release.verify_release(root)
    mirror = tmp_path / "native-tools"
    shutil.copytree(root / ".tools", mirror)
    for source in ("digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar"):
        destination = mirror / "_application" / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, destination)
    monkeypatch.setenv("V2D_NATIVE_TOOLS_ROOT", str(mirror))
    after = release.verify_release(root)
    assert after["sha256"] == before["sha256"] and after["native_mirror_files"] == 9
    (mirror / "oss-cad-suite/bin/companion.dll").write_bytes(b"modified")
    with pytest.raises(FrontendError) as caught:
        release.verify_release(root)
    assert caught.value.code == "RELEASE_CHANGED"


@pytest.mark.parametrize("mirrored", [False, True])
def test_unlisted_native_dll_is_rejected_in_package_or_ascii_mirror(package, tmp_path, monkeypatch, mirrored):
    root, _ = package
    tools = root / ".tools"
    if mirrored:
        tools = tmp_path / "native-mirror"
        shutil.copytree(root / ".tools", tools)
        for source in ("digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar"):
            destination = tools / "_application" / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / source, destination)
        monkeypatch.setenv("V2D_NATIVE_TOOLS_ROOT", str(tools))
    (tools / "oss-cad-suite/bin/unlisted.dll").write_bytes(b"unexpected dependency")
    with pytest.raises(FrontendError, match="unbound") as caught:
        release.verify_release(root)
    assert caught.value.code == "RELEASE_CHANGED"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_tools_ignore_external_override_and_never_fall_back_to_path(package, monkeypatch):
    root, _ = package
    shutil.copyfile(ROOT / "scripts/toolchain.py", root / "scripts/toolchain.py")
    shutil.copyfile(ROOT / "scripts/toolchain-lock.json", root / "scripts/toolchain-lock.json")
    toolchain = load_script("release_toolchain_probe", root / "scripts/toolchain.py")
    (root / ".tools/oss-cad-suite/bin/yosys.exe").unlink()
    monkeypatch.setenv("V2D_YOSYS", str(ROOT / ".tools/oss-cad-suite/bin/yosys.exe"))
    monkeypatch.setattr(toolchain.shutil, "which", lambda _: pytest.fail("release used PATH fallback"))
    with pytest.raises(FileNotFoundError, match="Packaged tool"):
        toolchain.executable("yosys")
    private = root / ".tools/oss-cad-suite/bin/yosys.exe"
    private.write_bytes(b"packaged")
    assert toolchain.executable("yosys") == str(private)


def test_release_fingerprint_binds_companion_files_and_ignores_location(package, tmp_path, monkeypatch):
    root, _ = package
    moved = tmp_path / "copy"
    shutil.copytree(root, moved)
    selected = [root]
    monkeypatch.setattr(environment, "repository_root", lambda: selected[0])
    monkeypatch.setattr(environment, "development_toolchain", lambda p: SimpleNamespace(executable=lambda _: str(p / ".tools/python-runtime/python.exe")))
    first = environment.runtime_fingerprint()
    selected[0] = moved
    second = environment.runtime_fingerprint()
    assert first["sha256"] == second["sha256"]
    assert first["artifacts"]["python"]["path"] != second["artifacts"]["python"]["path"]
    assert first["release"]["sha256"] == release.verify_release(root)["sha256"]
    (moved / ".tools/oss-cad-suite/bin/companion.dll").write_bytes(b"changed")
    with pytest.raises(FrontendError):
        environment.runtime_fingerprint()


def test_release_doctor_rejects_changed_file_before_any_native_process(package, monkeypatch):
    root, _ = package
    monkeypatch.setattr(environment, "repository_root", lambda: root)
    monkeypatch.setattr(environment.subprocess, "run", lambda *a, **k: pytest.fail("changed release executed a tool"))
    (root / ".tools/oss-cad-suite/bin/companion.dll").write_bytes(b"changed")
    with pytest.raises(FrontendError) as caught:
        environment.doctor()
    assert caught.value.code == "RELEASE_CHANGED"


@pytest.fixture
def bootstrap():
    return load_script("portable_bootstrap_test", ROOT / "scripts/portable_bootstrap.py")


def test_bootstrap_sanitizes_tool_and_python_injection_environment(bootstrap, package, monkeypatch):
    root, _ = package
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "V2D_YOSYS", "V2D_ROOT"):
        monkeypatch.setenv(name, "unrelated")
    result = bootstrap._clean_environment(root, root / ".runs/jobs", root / ".tools")
    assert result["V2D_ROOT"] == str(root)
    assert result["VIRTUAL_ENV"] == str(root / ".venv")
    assert all(name not in result for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "V2D_YOSYS"))
    assert "unrelated" not in result["PATH"]


def test_bootstrap_rebases_without_pip_and_preserves_bundled_dependencies(bootstrap, package, monkeypatch):
    root, _ = package
    monkeypatch.setattr(bootstrap, "_private_process", lambda *a, **k: None)
    calls = []
    class Builder:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def create(self, path):
            (path / "Scripts").mkdir()
            (path / "pyvenv.cfg").write_text("private")
            for target, source in (("python.exe", "venvlauncher.exe"), ("pythonw.exe", "venvwlauncher.exe")):
                shutil.copyfile(root / ".tools/python-runtime/Lib/venv/scripts/nt" / source, path / "Scripts" / target)
    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"prefix":"private"}'))
    dependency = root / ".venv/Lib/site-packages/example/__init__.py"
    before = dependency.read_bytes()
    details, env = bootstrap.prepare(root)
    assert calls == [{"with_pip": False, "clear": False, "symlinks": False, "scm_ignore_files": frozenset()}]
    assert dependency.read_bytes() == before
    assert details["isolation"] == {"prefix": "private"}
    assert env["V2D_ROOT"] == str(root)


def test_bootstrap_reuses_valid_venv_and_detects_relocated_home(bootstrap, package, monkeypatch):
    root, document = package
    scripts = root / ".venv/Scripts"
    scripts.mkdir()
    for target, source in (("python.exe", "venvlauncher.exe"), ("pythonw.exe", "venvwlauncher.exe")):
        shutil.copyfile(root / ".tools/python-runtime/Lib/venv/scripts/nt" / source, scripts / target)
    cfg = root / ".venv/pyvenv.cfg"
    cfg.write_text(f"home = {root / '.tools/python-runtime'}\ninclude-system-site-packages = false\n")
    assert bootstrap._venv_ready(root, document)
    monkeypatch.setattr(bootstrap, "_private_process", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", lambda **k: pytest.fail("valid live venv was overwritten"))
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}'))
    bootstrap.prepare(root)
    cfg.write_text("home = C:/UnrelatedOldPackage/python\ninclude-system-site-packages = false\n")
    assert not bootstrap._venv_ready(root, document)


@pytest.mark.parametrize("mode", ["--prepare", "--diagnostics", "--doctor"])
def test_bootstrap_public_json_shapes(bootstrap, package, monkeypatch, capsys, mode):
    root, _ = package
    details = {"root": str(root), "release": {}, "isolation": {}, "job_root": str(root / ".runs/jobs"), "native_tools_root": str(root / ".tools")}
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "prepare", lambda _: (details, {}))
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"state":"ready"}'))
    assert bootstrap.main(["--root", str(root), mode]) == 0
    output = json.loads(capsys.readouterr().out)
    if mode == "--prepare":
        assert set(output) == {"schema_version", "root", "gui", "environment"}
        assert set(output["environment"]) == {"V2D_ROOT", "V2D_JOB_ROOT", "PATH"}
    else:
        assert output["bootstrap"] == details


def test_bootstrap_cli_preserves_argument_vector(bootstrap, package, monkeypatch):
    root, _ = package
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "prepare", lambda _: ({}, {"private": "yes"}))
    calls = []
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: calls.append((a,k)) or SimpleNamespace(returncode=7))
    args = ["inspect", "--project", "source with spaces \u03a9.json", "--json"]
    assert bootstrap.main(["--root", str(root), "--cli", *args]) == 7
    assert calls[0][0][1] == [str(root / "scripts/v2d.py"), *args]


def test_bootstrap_cli_uses_explicit_caller_directory(bootstrap, package, tmp_path, monkeypatch):
    root, _ = package
    caller = tmp_path / "caller \u03a9"
    caller.mkdir()
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "prepare", lambda _: ({}, {}))
    calls = []
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0))
    assert bootstrap.main(["--root", str(root), "--cwd", str(caller), "--cli", "inspect", "relative.v"]) == 0
    assert calls[0][1]["cwd"] == caller
    assert calls[0][0][1][-1] == "relative.v"


def test_bootstrap_explicit_standard_handles_reach_hidden_cli_child(bootstrap, package, monkeypatch):
    root, _ = package
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: calls.append((a,k)))
    bootstrap._run(root, ["cli.py", "--json"], {})
    assert calls[0][1]["stdout"] is bootstrap.sys.stdout
    assert calls[0][1]["stderr"] is bootstrap.sys.stderr
    assert calls[0][1]["stdin"] == bootstrap.subprocess.DEVNULL


def test_unicode_runtime_creates_verified_ascii_tools_and_asset_mirror(package, tmp_path, monkeypatch):
    original, _ = package
    root = tmp_path / "package \u03a9"
    shutil.copytree(original, root)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    verified = release.verify_release(root)
    mirrored = release.ensure_native_mirror(root, jobs, verified)
    assert str(mirrored).isascii() and mirrored.parent.name == "native-tools"
    assert (mirrored / "jdk/bin/java.exe").read_bytes() == (root / ".tools/jdk/bin/java.exe").read_bytes()
    monkeypatch.setenv("V2D_NATIVE_TOOLS_ROOT", str(mirrored))
    jar = release.native_asset(root, "digital-adapter/target/digital-adapter.jar")
    assert jar == mirrored / "_application/digital-adapter/target/digital-adapter.jar"
    assert release.verify_release(root)["sha256"] == verified["sha256"]
    jar.write_bytes(b"changed")
    monkeypatch.delenv("V2D_NATIVE_TOOLS_ROOT")
    with pytest.raises(FrontendError):
        release.ensure_native_mirror(root, jobs, verified)


def test_release_tool_temp_paths_use_ascii_job_root(package, tmp_path, monkeypatch):
    original, _ = package
    root = tmp_path / "package \u03a9"
    shutil.copytree(original, root)
    shutil.copyfile(ROOT / "scripts/toolchain.py", root / "scripts/toolchain.py")
    jobs = tmp_path / "jobs"
    monkeypatch.setenv("V2D_JOB_ROOT", str(jobs))
    toolchain = load_script("release_unicode_temp_probe", root / "scripts/toolchain.py")
    env = toolchain.environment(str(root / ".venv/Scripts/python.exe"))
    assert env["TMP"] == env["TEMP"] == env["TMPDIR"] == str(jobs / "_runtime_tmp")


def test_hidden_bootstrap_subprocess_really_forwards_utf8_stdout_and_stderr(tmp_path):
    helper = tmp_path / "nested.py"
    helper.write_text("import importlib.util, os\nfrom pathlib import Path\n"
        f"root=Path({str(ROOT)!r})\n"
        "spec=importlib.util.spec_from_file_location('bootstrap',root/'scripts/portable_bootstrap.py')\n"
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "code=\"import sys; print('forwarded-\\u03a9'); print('diagnostic-\\u03c0', file=sys.stderr)\"\n"
        "raise SystemExit(module._run(root,['-c',code],dict(os.environ)).returncode)\n", encoding="utf-8")
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(helper)], capture_output=True,
        encoding="utf-8", errors="strict", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "forwarded-\u03a9"
    assert completed.stderr.strip() == "diagnostic-\u03c0"


def test_bootstrap_rejects_global_base_even_when_prefix_claims_venv(bootstrap, package, monkeypatch):
    root, _ = package
    monkeypatch.setattr(bootstrap.sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(bootstrap.sys, "base_prefix", "C:/GlobalPython")
    with pytest.raises(FrontendError) as caught:
        bootstrap._private_process(root, virtual=True)
    assert caught.value.code == "PYTHON_ISOLATION"
