"""Prepare the private release venv using only its packaged standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent/src"))
from verilog2digital.release import (PRODUCTION, FrontendError, _hash_file, job_root,
                                    _verify_mirror, ensure_native_mirror, native_tools_root, relative_file, verify_release)

# Each production distribution is imported through a module that loads its native code, if any.
MODULES = {"attrs": "attrs", "jsonschema": "jsonschema", "jsonschema-specifications": "jsonschema_specifications",
           "pyslang": "pyslang", "referencing": "referencing", "rpds-py": "rpds", "annotated-types": "annotated_types",
           "anyio": "anyio", "cffi": "_cffi_backend", "click": "click", "cryptography": "cryptography.hazmat.bindings._rust",
           "h11": "h11", "httpcore2": "httpcore2", "httpx2": "httpx2", "idna": "idna", "mcp": "mcp.server.mcpserver",
           "mcp-types": "mcp_types", "opentelemetry-api": "opentelemetry.trace", "pycparser": "pycparser",
           "pydantic": "pydantic", "pydantic-core": "pydantic_core._pydantic_core", "pyjwt": "jwt",
           "python-multipart": "python_multipart", "pywin32": "win32job", "sse-starlette": "sse_starlette",
           "starlette": "starlette", "truststore": "truststore", "typing-extensions": "typing_extensions",
           "typing-inspection": "typing_inspection", "uvicorn": "uvicorn"}
# The MCP server's runtime closure, beyond the converter's own release.PRODUCTION.
MCP_RUNTIME = tuple(name for name in MODULES if name not in PRODUCTION)
DISTRIBUTIONS = tuple(dict.fromkeys((*PRODUCTION, *MCP_RUNTIME)))
RUNTIME_DLLS = ("python314.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                "msvcp140_atomic_wait.dll", "pywintypes314.dll", "pythoncom314.dll")


def _private_process(root, *, virtual=False):
    wanted = root / (".venv" if virtual else ".tools/python-runtime")
    base = root / ".tools/python-runtime"
    if Path(sys.prefix).resolve() != wanted or Path(sys.base_prefix).resolve() != base:
        raise FrontendError("PYTHON_ISOLATION", "Use the supplied launcher and its private Python runtime")


def _clean_environment(root, jobs, tools):
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith("PYTHON") or upper in {"VIRTUAL_ENV", "JAVA_HOME", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "TCL_LIBRARY", "TK_LIBRARY"}:
            env.pop(key)
        elif upper.startswith("V2D_") and upper not in {"V2D_JOB_ROOT", "V2D_NATIVE_TOOLS_ROOT"}:
            env.pop(key)
    system = Path(os.environ.get("SystemRoot", "C:/Windows"))
    env.update(V2D_ROOT=str(root), V2D_JOB_ROOT=str(jobs), VIRTUAL_ENV=str(root / ".venv"),
               PYTHONUTF8="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join(map(str, (root / ".venv/Scripts", root / ".tools/python-runtime", system / "System32", system))))
    if tools != root / ".tools":
        env["V2D_NATIVE_TOOLS_ROOT"] = str(tools)
    else:
        env.pop("V2D_NATIVE_TOOLS_ROOT", None)
    return env


def _packages(site):
    packages = {}
    for distribution in DISTRIBUTIONS:
        if distribution not in MODULES:
            raise FrontendError("PYTHON_ISOLATION", "No import probe is defined for production distribution " + distribution)
        module = importlib.import_module(MODULES[distribution])
        path = Path(module.__file__).resolve(strict=True)
        if not path.is_relative_to(site):
            raise FrontendError("PYTHON_ISOLATION", "A production package loaded outside the private venv", path=str(path))
        packages[distribution] = {"version": importlib.metadata.version(distribution), "path": str(path)}
    # The stdio transport loads pywin32's handle and job modules through pywin32.pth.
    importlib.import_module("mcp.server.stdio")
    return packages


def _probe(root):
    _private_process(root, virtual=True)
    site = root / ".venv/Lib/site-packages"
    packages = _packages(site)
    native = {}
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and Path(path).suffix.lower() in {".pyd", ".dll"}:
            path = Path(path).resolve(strict=True)
            if not path.is_relative_to(root / ".tools/python-runtime") and not path.is_relative_to(site):
                raise FrontendError("PYTHON_ISOLATION", "A native Python module loaded outside the private runtime", path=str(path))
            native[name] = str(path)
    loaded = {}
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetModuleFileNameW.restype = wintypes.DWORD
        for name in RUNTIME_DLLS:
            handle = kernel.GetModuleHandleW(name)
            if handle:
                buffer = ctypes.create_unicode_buffer(32768)
                count = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
                if not count or count == len(buffer):
                    raise FrontendError("PYTHON_ISOLATION", "Cannot identify a loaded private runtime DLL")
                path = Path(buffer.value).resolve(strict=True)
                if not path.is_relative_to(root):
                    raise FrontendError("PYTHON_ISOLATION", "A required runtime DLL loaded outside this package", path=str(path))
                loaded[name] = str(path)
    stdlib = Path(json.__file__).resolve(strict=True)
    if not stdlib.is_relative_to(root / ".tools/python-runtime/Lib"):
        raise FrontendError("PYTHON_ISOLATION", "Python standard library loaded outside the private base")
    return {"executable": str(Path(sys.executable).resolve()), "prefix": str(Path(sys.prefix).resolve()),
            "base_prefix": str(Path(sys.base_prefix).resolve()), "stdlib": str(stdlib), "packages": packages,
            "native_modules": native, "loaded_dlls": loaded}


def _run(root, args, environment, *, capture=False, timeout=None, cwd=None, stdin=subprocess.DEVNULL):
    streams = {"capture_output": True} if capture else {"stdout": sys.stdout, "stderr": sys.stderr}
    return subprocess.run([str(root / ".venv/Scripts/python.exe"), "-I", "-X", "utf8", "-B", *args],
                          cwd=cwd if cwd is not None else root, env=environment, stdin=stdin, **streams, text=True,
                          encoding="utf-8", errors="strict", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _serve(root, args, environment, cwd):
    """Hand the host's protocol streams to `v2d mcp` unchanged: no capture, buffering or deadline here."""
    return _run(root, [str(root / "scripts/v2d.py"), "mcp", *args], environment, cwd=cwd, stdin=sys.stdin).returncode


def _venv_ready(root, manifest):
    try:
        path = relative_file(root, ".venv/pyvenv.cfg")
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(8193)
        if len(text) > 8192:
            return False
        values = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = (item.strip() for item in line.split("=", 1))
                if key in values:
                    return False
                values[key] = value
        if (values.get("include-system-site-packages", "").lower() != "false"
                or Path(values.get("home", "")).resolve() != root / ".tools/python-runtime"):
            return False
        for name, source in (("python.exe", "venvlauncher.exe"), ("pythonw.exe", "venvwlauncher.exe")):
            _hash_file(relative_file(root, ".venv/Scripts/" + name),
                       manifest["files"][".tools/python-runtime/Lib/venv/scripts/nt/" + source])
        return True
    except (FrontendError, OSError, UnicodeError):
        return False


@contextmanager
def _bootstrap_lock(root):
    directory = relative_file(root, ".runs")
    directory.mkdir(exist_ok=True)
    path = relative_file(root, ".runs/bootstrap.lock")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise FrontendError("BOOTSTRAP_BUSY", "Another launcher is preparing this private runtime; retry shortly")
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prepare(root):
    _private_process(root)
    verified = verify_release(root)
    if verified is None:
        raise FrontendError("RELEASE_MANIFEST", "The supplied launcher requires a complete release manifest")
    jobs = job_root(root)
    tools = ensure_native_mirror(root, jobs, verified)
    environment = _clean_environment(root, jobs, tools)
    # Preserve packaged dependencies with EnvBuilder's default clear=False.
    for name in (".venv", ".venv/Scripts", ".venv/Lib", ".venv/Lib/site-packages", ".venv/pyvenv.cfg"):
        relative_file(root, name)
    manifest = importlib.import_module("verilog2digital.release").release_manifest(root)
    with _bootstrap_lock(root):
        # A running worker can hold its launcher executable open on Windows.
        # Reuse valid generated files; recreate only a new or relocated venv.
        if not _venv_ready(root, manifest):
            venv.EnvBuilder(with_pip=False, clear=False, symlinks=False, scm_ignore_files=frozenset()).create(root / ".venv")
    for name, source in (("python.exe", "venvlauncher.exe"), ("pythonw.exe", "venvwlauncher.exe")):
        expected = manifest["files"][".tools/python-runtime/Lib/venv/scripts/nt/" + source]
        _hash_file(relative_file(root, ".venv/Scripts/" + name), expected)
    result = _run(root, [str(root / "scripts/portable_bootstrap.py"), "--root", str(root), "--_probe"],
                  environment, capture=True, timeout=30)
    if result.returncode:
        try:
            diagnostic = json.loads(result.stdout)["diagnostic"]
        except (ValueError, KeyError):
            raise FrontendError("PYTHON_ISOLATION", "Private venv dependency probe failed: " + result.stderr[-2000:])
        raise FrontendError(diagnostic["code"], diagnostic["message"], path=diagnostic.get("path"))
    isolation = json.loads(result.stdout)
    # Recheck after creation/import before admitting a GUI or CLI operation.
    if verify_release(root)["sha256"] != verified["sha256"]:
        raise FrontendError("RELEASE_CHANGED", "Release inventory changed while preparing Python")
    if tools != root / ".tools":
        verified["native_mirror_files"] = _verify_mirror(root, tools, manifest)
    details = {"root": str(root), "release": verified, "isolation": isolation,
               "job_root": str(jobs), "native_tools_root": str(tools)}
    return details, environment


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--cwd", type=Path, help=argparse.SUPPRESS)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--doctor", action="store_true")
    group.add_argument("--diagnostics", action="store_true")
    group.add_argument("--_probe", action="store_true", help=argparse.SUPPRESS)
    group.add_argument("--cli", nargs=argparse.REMAINDER)
    group.add_argument("--mcp", nargs=argparse.REMAINDER, help="Serve `v2d mcp` over this process's stdin/stdout")
    options = parser.parse_args(argv)
    # In MCP mode stdout belongs to the protocol; every bootstrap diagnostic goes to stderr.
    report = sys.stderr if options.mcp is not None else sys.stdout
    try:
        root = options.root.resolve(strict=True)
        if root != ROOT:
            raise FrontendError("RELEASE_PATH", "The bootstrap must run from the selected release directory")
        delegated = options.cli is not None or options.mcp is not None
        if options.cwd is not None and not delegated:
            raise FrontendError("BOOTSTRAP_ARGUMENT", "--cwd is only accepted with --cli or --mcp")
        caller = options.cwd if options.cwd is not None else Path.cwd()
        if delegated and (not caller.is_absolute() or not caller.is_dir()):
            raise FrontendError("BOOTSTRAP_ARGUMENT", "CLI working directory must be an existing absolute directory")
        if options._probe:
            print(json.dumps(_probe(root), ensure_ascii=False, allow_nan=False))
            return 0
        details, environment = prepare(root)
        if options.mcp is not None:
            return _serve(root, options.mcp, environment, caller)
        if options.prepare:
            gui = relative_file(root, "gui/build/v2d-gui.exe")
            system = Path(os.environ.get("SystemRoot", "C:/Windows"))
            env = {"V2D_ROOT": str(root), "V2D_JOB_ROOT": details["job_root"],
                   "PATH": os.pathsep.join(map(str, (gui.parent, system / "System32", system)))}
            if "V2D_NATIVE_TOOLS_ROOT" in environment:
                env["V2D_NATIVE_TOOLS_ROOT"] = environment["V2D_NATIVE_TOOLS_ROOT"]
            result = {"schema_version": 1, "root": str(root), "gui": {"executable": str(gui)}, "environment": env}
        elif options.diagnostics:
            result = {"schema_version": 1, "state": "ready", "bootstrap": details}
        elif options.doctor:
            run = _run(root, [str(root / "scripts/v2d.py"), "doctor", "--json"], environment, capture=True, timeout=90)
            result = json.loads(run.stdout)
            result["bootstrap"] = details
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return run.returncode
        else:
            if not options.cli:
                raise FrontendError("BOOTSTRAP_ARGUMENT", "--cli requires a v2d command")
            return _run(root, [str(root / "scripts/v2d.py"), *options.cli], environment, cwd=caller).returncode
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (FrontendError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        diagnostic = error.as_dict() if isinstance(error, FrontendError) else {"code": "BOOTSTRAP_FAILED", "message": str(error)}
        print(json.dumps({"schema_version": 1, "state": "failed", "diagnostic": diagnostic}, ensure_ascii=False), file=report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
