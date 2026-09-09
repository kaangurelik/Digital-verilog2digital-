"""Read-only runtime inventory and dependency hashes without native Python imports."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess

from .jobs.publication import digest
from .runtime import development_toolchain, repository_root


def runtime_fingerprint() -> dict:
    root = repository_root()
    from .release import verify_release
    release = verify_release(root)
    toolchain = development_toolchain(root)
    paths = {name: Path(toolchain.executable(name)) for name in ("python", "java", "yosys", "iverilog", "vvp")}
    paths.update(adapter=root / "digital-adapter/target/digital-adapter.jar",
                 toolchain_lock=root / "scripts/toolchain-lock.json", runtime_support=root / ".tools/runtime-support.jar")
    artifacts = {name: {"path": str(path.resolve(strict=True)), "sha256": digest(path)} for name, path in sorted(paths.items())}
    packages = {}
    for name in ("pyslang", "jsonschema"):
        distribution = importlib.metadata.distribution(name)
        native = {str(path): digest(distribution.locate_file(path)) for path in distribution.files or ()
                  if str(path).lower().endswith((".pyd", ".dll", ".so"))}
        packages[name] = {"version": distribution.version, "native_files": native}
    code_paths = sorted((root / "agent/src/verilog2digital").rglob("*.py"))
    code_paths += sorted((root / "schemas").glob("*.schema.json"))
    code_paths += [root / "scripts/toolchain.py"]
    if (root / "scripts/llm-lock.json").is_file():
        code_paths.append(root / "scripts/llm-lock.json")
    code = {path.relative_to(root).as_posix(): digest(path) for path in code_paths}
    content = {"artifacts": artifacts, "packages": packages, "code": code}
    if release is not None:
        # Paths and use of an identical ASCII mirror are operational details.
        content["release"] = {key: release[key] for key in ("sha256", "files", "bytes")}
    bound_content = {**content, "artifacts": {name: item["sha256"] for name, item in artifacts.items()}}
    key = hashlib.sha256(json.dumps(bound_content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "sha256": key, **content}


def doctor() -> dict:
    root = repository_root()
    from .release import verify_release
    # Verify packaged tools before executing them to query versions.
    verify_release(root)
    toolchain = development_toolchain(root)
    tools = {}
    for name, flags in {"python": ["--version"], "java": ["-version"], "yosys": ["-V"],
                        "iverilog": ["-V"], "vvp": ["-V"]}.items():
        try:
            path = toolchain.executable(name)
            result = subprocess.run([path, *flags], env=toolchain.environment(path), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = [line for line in (result.stdout + result.stderr).splitlines()
                     if line.strip() and not line.startswith("Picked up JAVA_TOOL_OPTIONS:")]
            tools[name] = {"path": path, "available": result.returncode == 0,
                           "version": lines[0] if lines else "", "exit_code": result.returncode}
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            tools[name] = {"available": False, "error": str(error)}
    jar = root / "digital-adapter/target/digital-adapter.jar"
    tools["adapter"] = {"path": str(jar), "available": jar.is_file()}
    fingerprint = None
    try:
        fingerprint = runtime_fingerprint()
    except (OSError, RuntimeError, importlib.metadata.PackageNotFoundError) as error:
        tools["dependencies"] = {"available": False, "error": str(error)}
    ready = all(item["available"] for item in tools.values())
    return {"schema_version": 2, "state": "ready" if ready else "incomplete", "tools": tools,
            "memory": toolchain.memory_info(), "fingerprint": fingerprint,
            "python_isolation": {"venv": str(root / ".venv"), "global_packages": False}}
