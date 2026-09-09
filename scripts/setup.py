"""Provision Python libraries in .venv and other tools in .tools."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import urllib.request
import venv
import zipfile

from toolchain import LOCK, ROOT, TOOLS, VENV, environment, executable, run


def provision(name: str) -> None:
    spec = LOCK[name]
    target = TOOLS / spec["directory"]
    marker = target / ".v2d-source.json"
    if marker.is_file() and json.loads(marker.read_text()) == spec:
        print(f"{name}: verified installation already present", flush=True)
        return
    cache = TOOLS / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["url"].rsplit("/", 1)[1]

    def digest(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    if not archive.is_file() or digest(archive) != spec["sha256"]:
        part = archive.with_suffix(archive.suffix + ".part")
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "verilog2digital-setup"})
        print(f"Downloading {name} {spec['version']}", flush=True)
        with urllib.request.urlopen(request, timeout=60) as source, part.open("wb") as dest:
            size = int(source.headers.get("Content-Length", 0))
            count = 0
            reported = 0
            while chunk := source.read(1024 * 1024):
                dest.write(chunk)
                count += len(chunk)
                if count - reported >= 50 * 1024 * 1024:
                    print(f"  {count // (1024 * 1024)} MiB / {size // (1024 * 1024)} MiB", flush=True)
                    reported = count
        if digest(part) != spec["sha256"]:
            raise RuntimeError(f"Checksum mismatch for {name}; download not installed")
        part.replace(archive)
    print(f"Extracting verified {name} archive", flush=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                if not (TOOLS / item.filename).resolve().is_relative_to(TOOLS.resolve()):
                    raise RuntimeError("Archive entry escapes tools directory")
            z.extractall(TOOLS)
    else:
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(TOOLS, filter="data")
    if not target.is_dir():
        raise RuntimeError(f"Expected extracted directory missing: {target}")
    marker.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"{name}: installed {spec['version']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["jdk", "hdl", "python"])
    args = parser.parse_args()
    TOOLS.mkdir(exist_ok=True)
    for item in ([args.only] if args.only else ["jdk", "hdl", "python"]):
        if item in ("jdk", "hdl"):
            provision(item)
        else:
            if not (VENV / "pyvenv.cfg").exists():
                # ensurepip inherits only process-local settings, including a writable temp directory.
                import os
                os.environ.update(environment())
                venv.EnvBuilder(with_pip=True, system_site_packages=False).create(VENV)
            run([executable("python"), "-m", "pip", "install", "-r", str(ROOT / "agent/requirements-dev.lock")], log="python-setup.log")
            run([executable("python"), "-m", "pip", "check"], log="python-check.log")
            run([executable("python"), "-m", "pip", "freeze"], log="python-freeze.txt")


if __name__ == "__main__":
    main()
