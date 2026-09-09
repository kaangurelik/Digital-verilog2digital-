"""Explicit local tool paths and reproducible command execution."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
VENV = ROOT / ".venv"
LOCK = json.loads((ROOT / "scripts/toolchain-lock.json").read_text(encoding="utf-8"))
RELEASE = (ROOT / "release-manifest.json").exists() or (TOOLS / "python-runtime/python.exe").exists()
if RELEASE:
    sys.path.insert(0, str(ROOT / "agent/src"))
    from verilog2digital.release import native_tools_root
    TOOLS = native_tools_root(ROOT)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def executable(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    if name == "python":
        candidate = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        config = VENV / "pyvenv.cfg"
        if not candidate.is_file() or not config.is_file():
            raise FileNotFoundError("Missing project .venv; run python scripts/setup.py --only python")
        settings = dict(line.split("=", 1) for line in config.read_text(encoding="utf-8").splitlines() if "=" in line)
        if {k.strip(): v.strip().lower() for k, v in settings.items()}.get("include-system-site-packages") != "false":
            raise RuntimeError("Project .venv must disable system site packages")
        return str(candidate)
    if name in ("java", "javac"):
        candidate = TOOLS / LOCK["jdk"]["directory"] / "bin" / (name + suffix)
    elif name in ("yosys", "iverilog", "vvp"):
        candidate = TOOLS / LOCK["hdl"]["directory"] / "bin" / (name + suffix)
    else:
        candidate = Path("__not_configured__")
    override = os.environ.get("V2D_" + name.upper().replace("+", "X"))
    if override and not RELEASE:
        return str(Path(override).resolve(strict=True))
    if candidate.is_file():
        return str(candidate)
    if RELEASE:
        raise FileNotFoundError(f"Packaged tool {name} is missing; restore the verified release package")
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"Missing {name}; run python scripts/setup.py or set V2D_{name.upper()}")


def environment(program: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if RELEASE:
        for key in list(env):
            if key.upper() in {"PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONINSPECT", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "TCL_LIBRARY", "TK_LIBRARY"}:
                env.pop(key)
        system = Path(os.environ.get("SystemRoot", "C:/Windows"))
        env["PATH"] = os.pathsep.join(map(str, (system / "System32", system)))
        env["V2D_ROOT"] = str(ROOT)
    jdk = TOOLS / LOCK["jdk"]["directory"]
    hdl = TOOLS / LOCK["hdl"]["directory"]
    bins = [VENV / ("Scripts" if os.name == "nt" else "bin"), jdk / "bin", hdl / "bin", hdl / "lib"]
    if RELEASE and program and Path(program).name.lower().removesuffix(".exe") == "v2d-gui":
        bins.insert(0, ROOT / "gui/build")
    elif program and Path(program).name.lower().removesuffix(".exe") in {"cmake", "ninja", "g++", "gcc", "v2d-gui"}:
        # OSS CAD Suite and UCRT64 ship incompatible DLLs with identical filenames.
        # Native build/UI processes must load the compiler's runtime first.
        bins.insert(0, Path(executable("g++")).resolve().parent)
    if jdk.is_dir():
        env["JAVA_HOME"] = str(jdk)
    env["PATH"] = os.pathsep.join(map(str, bins)) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    if RELEASE:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["VIRTUAL_ENV"] = str(VENV)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(ROOT / "agent/src")
    if env.get("V2D_JOB_TMP"):
        temporary = Path(env["V2D_JOB_TMP"]).resolve()
    elif RELEASE:
        from verilog2digital.release import job_root
        temporary = job_root(ROOT) / "_runtime_tmp"
    else:
        temporary = ROOT / ".runs/tmp"
    if env.get("V2D_JOB_TMP") and not str(temporary).isascii():
        raise RuntimeError("Native job temporary paths must be ASCII")
    temporary.mkdir(parents=True, exist_ok=True)
    env["TMP"] = str(temporary)
    env["TEMP"] = str(temporary)
    env["TMPDIR"] = str(temporary)
    env["PIP_CACHE_DIR"] = str(TOOLS / "pip-cache")
    env["PIP_NO_INPUT"] = "1"
    env["PIP_REQUIRE_VIRTUALENV"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    for setting in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER", "PIP_PYTHON", "PYTHONUSERBASE"):
        env.pop(setting, None)
    support = TOOLS / "runtime-support.jar"
    if support.is_file():
        java_home = temporary / "java-home" if env.get("V2D_JOB_TMP") or RELEASE else ROOT / ".runs/java-home"
        java_home.mkdir(parents=True, exist_ok=True)
        library = ROOT / "src/main/dig/lib"
        if RELEASE:
            from verilog2digital.release import native_asset
            library = native_asset(ROOT, "src/main/dig/lib")
        env["JAVA_TOOL_OPTIONS"] = (
            f'-Xbootclasspath/a:"{support}" '
            '-Djava.util.prefs.PreferencesFactory=v2d.runtime.MemoryPreferencesFactory '
            f'-Ddigital.library.path="{library}" '
            f'-Duser.home="{java_home}"'
        )
    return env


def build_runtime_support() -> None:
    source = ROOT / "scripts/java/v2d/runtime/MemoryPreferencesFactory.java"
    jar = TOOLS / "runtime-support.jar"
    if jar.is_file() and jar.stat().st_mtime >= source.stat().st_mtime:
        return
    classes = TOOLS / "runtime-classes"
    classes.mkdir(parents=True, exist_ok=True)
    run([executable("javac"), "-source", "8", "-target", "8", "-d", str(classes), str(source)], log="runtime-compile.log")
    jar_tool = Path(executable("java")).parent / ("jar.exe" if os.name == "nt" else "jar")
    run([str(jar_tool), "--create", "--file", str(jar), "-C", str(classes), "."], log="runtime-jar.log")


def maven_args(*args: str, project: Path = ROOT) -> list[str]:
    maven = Path(executable("mvn")).resolve().parents[1]
    boot = next((maven / "boot").glob("plexus-classworlds-*.jar"))
    return [
        executable("java"), f"-Dmaven.home={maven}",
        f"-Dclassworlds.conf={maven / 'bin/m2.conf'}",
        f"-Dmaven.multiModuleProjectDirectory={project}",
        "-classpath", str(boot), "org.codehaus.plexus.classworlds.launcher.Launcher",
        "-B", "-ntp", "-Dstyle.color=never", f"-Dmaven.repo.local={TOOLS / 'm2'}",
        "-Djava.awt.headless=true", *args,
    ]


def run(args: list[str], *, cwd: Path = ROOT, log: str | None = None,
        timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess:
    started = time.monotonic()
    path = ROOT / ".runs/setup" / (log or "command.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Running {Path(args[0]).name}: {' '.join(args[1:])}", flush=True)
    with path.open("w", encoding="utf-8") as stream:
        result = subprocess.run(args, cwd=cwd, env=environment(args[0]), stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    elapsed = time.monotonic() - started
    with (path.parent / "commands.jsonl").open("a", encoding="utf-8") as metrics:
        metrics.write(json.dumps({"schema_version": 1, "finished_at": datetime.now(timezone.utc).isoformat(),
                                  "executable": str(args[0]), "exit_code": result.returncode,
                                  "elapsed_seconds": round(elapsed, 3), "peak_memory_bytes": None,
                                  "log": path.relative_to(ROOT).as_posix(), "log_bytes": path.stat().st_size}) + "\n")
    print(f"Exit {result.returncode}, {elapsed:.1f}s; log: {path.relative_to(ROOT)}", flush=True)
    if check and result.returncode:
        print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-35:]))
        raise subprocess.CalledProcessError(result.returncode, args)
    return result


def memory_info() -> dict:
    result: dict = {"system": None, "gpus": []}
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            result["system"] = {"total_bytes": status.total, "available_bytes": status.available,
                                "load_percent": status.load}
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            for line in p.stdout.strip().splitlines():
                name, total, free = [v.strip() for v in line.split(",")]
                result["gpus"].append({"name": name, "total_mib": int(total), "free_mib": int(free)})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(memory_info(), indent=2))
