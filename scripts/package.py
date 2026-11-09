"""Assemble a new portable package from verified local inputs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION = ("USER_GUIDE.md", "THIRD_PARTY_NOTICES.md")
PRODUCTION_PACKAGES = ("attrs", "jsonschema", "jsonschema-specifications", "pyslang", "referencing", "rpds-py")
PYTHON_ARCHIVE = "python.3.14.0.nupkg"
PYTHON_SHA256 = "620fb3527428fb354f093b0b8b634dfb8e3023115df68608fba7e91db69b4f4d"
GUI_DLLS = ("wxmsw32u_core_gcc_custom.dll", "wxbase32u_gcc_custom.dll", "zlib1.dll", "libstdc++-6.dll",
            "libwinpthread-1.dll", "libgcc_s_seh-1.dll", "libpcre2-16-0.dll", "liblzma-5.dll", "libtiff-6.dll",
            "libzstd.dll", "libwebp-7.dll", "libsharpyuv-0.dll", "libLerc.dll", "libjpeg-8.dll", "libjbig-0.dll",
            "libdeflate.dll", "libpng16-16.dll")
HDL_FILES = ("bin/yosys.exe", "bin/iverilog.exe", "bin/vvp.exe", "bin/libvvp-1.dll", "lib/libstdc++-6.dll",
             "lib/libwinpthread-1.dll", "lib/libgcc_s_seh-1.dll", "lib/libreadline8.dll", "lib/libtermcap-0.dll",
             "lib/zlib1.dll", "lib/tcl86.dll", "lib/libffi-8.dll", "lib/libbz2-1.dll")
VC_DIRECTORY = Path("C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_name(name):
    if not isinstance(name, str) or not name or len(name) > 512 or any(ord(char) < 32 for char in name) or "\\" in name or ":" in name:
        raise ValueError("Package destinations must be relative POSIX names")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(part in {".", ".."} for part in path.parts):
        raise ValueError("Package destination escapes its root")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
    if any(part.endswith((".", " ")) or any(char in part for char in '<>"|?*') or part.split(".")[0].upper() in reserved for part in path.parts):
        raise ValueError("Package destination is not a regular Windows filename")
    return name


def reject_links(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError(f"Linked package input is not supported: {part}")


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_document(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


@dataclass(frozen=True)
class Input:
    source: Path
    sha256: str
    size: int
    core: bool = True


class Contents:
    def __init__(self):
        self.files: dict[str, Input] = {}
        self.names: set[str] = set()

    def add(self, source, target=None, *, expected=None, core=True):
        source = Path(source)
        reject_links(source)
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing or linked package input: {source}")
        name = relative_name(target or source.relative_to(ROOT).as_posix())
        if name.casefold() in self.names:
            old = self.files.get(name)
            if old and old.source.resolve() == source.resolve() and old.core == core:
                return
            raise ValueError(f"Duplicate package destination: {name}")
        size, digest = source.stat().st_size, sha256(source)
        if expected and (digest != expected["sha256"] or size != expected.get("bytes", expected.get("size"))):
            raise ValueError(f"Release input differs from its recorded hash/size: {source}")
        self.files[name] = Input(source, digest, size, core)
        self.names.add(name.casefold())

    def tree(self, source, target=None, *, core=True, predicate=lambda path: True):
        source = Path(source)
        reject_links(source)
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"Missing or linked package directory: {source}")
        target = target or source.relative_to(ROOT).as_posix()
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError(f"Linked package input is not supported: {path}")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and predicate(path):
                self.add(path, target + "/" + path.relative_to(source).as_posix(), core=core)

    def copy(self, destination):
        for name, item in sorted(self.files.items()):
            target = destination / Path(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = item.size
            with item.source.open("rb") as source, target.open("xb") as output:
                while remaining:
                    block = source.read(min(4 * 1024**2, remaining))
                    if not block:
                        raise RuntimeError(f"Package input became shorter: {name}")
                    output.write(block)
                    remaining -= len(block)
                if source.read(1):
                    raise RuntimeError(f"Package input grew: {name}")
            if sha256(target) != item.sha256:
                raise RuntimeError(f"Package input changed during copying: {name}")

    def verify_inputs(self):
        for name, item in self.files.items():
            if item.source.stat().st_size != item.size or sha256(item.source) != item.sha256:
                raise RuntimeError(f"Package input changed before final selection: {name}")


def private_python(directory):
    archive = ROOT / ".tools/downloads" / PYTHON_ARCHIVE
    if sha256(archive) != PYTHON_SHA256:
        raise ValueError("Private Python archive does not match the pinned official NuGet package")
    extracted = directory / "python-runtime"
    total, seen = 0, set()
    with zipfile.ZipFile(archive) as package:
        if len(package.infolist()) > 2000:
            raise ValueError("Private Python archive has too many entries")
        for item in package.infolist():
            if not item.filename.startswith("tools/") or item.is_dir():
                continue
            name = relative_name(item.filename[6:])
            if name.casefold() in seen or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Invalid private Python archive entry")
            seen.add(name.casefold())
            if name.startswith("Lib/site-packages/") or "__pycache__" in PurePosixPath(name).parts or name.endswith(".pyc"):
                continue
            total += item.file_size
            if total > 64 * 1024**2 or item.file_size > 16 * 1024**2:
                raise ValueError("Private Python archive exceeds its extraction bounds")
            target = extracted / Path(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(item) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
    for name in ("python.exe", "python314.dll", "Lib/venv/scripts/nt/venvlauncher.exe", "LICENSE.txt"):
        if not (extracted / name).is_file():
            raise ValueError(f"Private Python archive lacks {name}")
    # Use the complete identified redistributable family, not a System32 copy.
    libraries = sorted(VC_DIRECTORY.glob("*.dll"))
    if len(libraries) != 10:
        raise ValueError("Expected the audited ten-file VC143 x64 redistributable directory")
    for library in libraries:
        shutil.copyfile(library, extracted / library.name)
    return extracted


def dependencies(contents):
    site = (ROOT / ".venv/Lib/site-packages").resolve(strict=True)
    for name in PRODUCTION_PACKAGES:
        distribution = importlib.metadata.distribution(name)
        if Path(distribution.locate_file("")).resolve(strict=True) != site:
            raise ValueError(f"Production dependency metadata is outside the repository .venv: {name}")
        if not distribution.files:
            raise ValueError(f"Production dependency has no installed file inventory: {name}")
        for record in distribution.files:
            path = Path(distribution.locate_file(record)).resolve()
            if not path.is_relative_to(site) or path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue
            contents.add(path, ".venv/Lib/site-packages/" + path.relative_to(site).as_posix())


def notices(contents):
    inventory = document(ROOT / "scripts/release-licenses.json")
    for item in inventory["files"]:
        contents.add(ROOT / item["staged_path"], item["target"], expected=item)
    for dependency in inventory["java_dependencies"]:
        source = dependency.get("source_archive", {})
        if source.get("status") == "retrieved":
            contents.add(ROOT / source["path"], "licenses/" + source["name"], expected=source)
    contents.add(ROOT / "scripts/release-licenses.json", "licenses/inventory.json")


def source_archive(path):
    files = []
    for folder in ("agent", "digital-adapter", "gui", "schemas", "scripts", "src", "tests", "distribution"):
        for file in (ROOT / folder).rglob("*"):
            if (file.is_file() and not file.is_symlink() and not {"target", "build", "__pycache__", ".pytest_cache"}.intersection(file.relative_to(ROOT).parts)
                    and file.suffix not in {".pyc", ".log"}):
                reject_links(file)
                files.append(file)
    files.extend(ROOT / name for name in ("LICENSE", "pom.xml", ".gitignore", "README.md", *DOCUMENTATION) if (ROOT / name).is_file())
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(set(files)):
            info = zipfile.ZipInfo(file.relative_to(ROOT).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes())


def write_archive(path, package, contents, prefix):
    """Archive only recorded inputs while the complete package is still private."""
    records = {name: {"size": item.size, "sha256": item.sha256} for name, item in contents.files.items()}
    manifest = package / "release-manifest.json"
    records[manifest.name] = {"size": manifest.stat().st_size, "sha256": sha256(manifest)}
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for name, expected in sorted(records.items()):
            info = zipfile.ZipInfo(prefix + "/" + name, (2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            remaining, digest = expected["size"], hashlib.sha256()
            with (package / name).open("rb") as source, bundle.open(info, "w", force_zip64=True) as target:
                while remaining:
                    block = source.read(min(4 * 1024**2, remaining))
                    if not block:
                        raise RuntimeError(f"Package file became shorter during archiving: {name}")
                    remaining -= len(block)
                    digest.update(block)
                    target.write(block)
                if source.read(1) or digest.hexdigest() != expected["sha256"]:
                    raise RuntimeError(f"Package file changed during archiving: {name}")
    return sha256(path)


def model_contents(contents, model_ids, backends):
    if not model_ids:
        return []
    sys.path.insert(0, str(ROOT / "agent/src"))
    from verilog2digital.llm.artifacts import load_lock, model_path, verify_installation
    lock = load_lock(ROOT)
    for model in model_ids:
        for backend in backends:
            verify_installation(model, backend, ROOT)
        contents.add(model_path(model, ROOT), core=False)
    for backend in backends:
        contents.tree(ROOT / ".tools/llama.cpp" / (lock["runtime"]["nightly"] + "-" + backend), core=False)
    for name in set(backends) | ({"cudart"} if "cuda" in backends else set()):
        spec = lock["runtime"]["artifacts"][name]
        contents.add(ROOT / ".tools/downloads" / spec["filename"], expected=spec, core=False)
    return [{"model_id": model, "backends": sorted(backends)} for model in model_ids]


def assemble(destination, *, model_ids=(), backends=("cpu", "cuda"), archive=False, include_native_tests=False):
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise RuntimeError("Build packages using the repository .venv")
    destination = Path(destination).absolute()
    parent = destination.parent.resolve()
    if not parent.is_relative_to(ROOT) or parent == ROOT:
        raise ValueError("Build packages under a workspace output folder such as dist")
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / destination.name
    if destination.exists():
        raise FileExistsError("Choose a new package directory; existing outputs are preserved")
    zip_path = destination.parent / (destination.name + ".zip")
    if archive and zip_path.exists():
        raise FileExistsError("Choose a new package archive; existing outputs are preserved")
    temporary = parent / (".v2d-package-" + uuid.uuid4().hex)
    temporary.mkdir(exist_ok=False)
    inputs = temporary / "inputs"; inputs.mkdir()
    package = temporary / "package"; package.mkdir()
    try:
        from toolchain import executable, LOCK
        contents = Contents()
        python = private_python(inputs)
        contents.tree(python, ".tools/python-runtime")
        dependencies(contents)
        for directory in ("agent/src/verilog2digital", "schemas", "src/main/dig/lib"):
            contents.tree(ROOT / directory)
        for name in ("v2d.py", "toolchain.py", "toolchain-lock.json", "llm-lock.json", "portable_bootstrap.py"):
            contents.add(ROOT / "scripts" / name)
        for name in (".tools/runtime-support.jar", "digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar", "LICENSE"):
            contents.add(ROOT / name)
        contents.add(ROOT / "gui/build/v2d-gui.exe")
        contents.add(ROOT / "gui/build/VerilogToDigital.exe", "VerilogToDigital.exe")
        if include_native_tests:
            for name in ("v2d-main-frame-tests.exe", "v2d-preview-tests.exe", "v2d-project-tests.exe"):
                contents.add(ROOT / "gui/build" / name)
        compiler_bin = Path(executable("g++")).parent
        for name in GUI_DLLS:
            contents.add(compiler_bin / name, "gui/build/" + name)
        hdl = ROOT / ".tools" / LOCK["hdl"]["directory"]
        for name in HDL_FILES:
            contents.add(hdl / name)
        for name in ("lib/ivl", "share/yosys", "lib/tcl8.6", "license"):
            contents.tree(hdl / name)
        contents.tree(ROOT / ".tools" / LOCK["jdk"]["directory"])
        notices(contents)
        for name in DOCUMENTATION:
            contents.add(ROOT / name)
        contents.tree(ROOT / "tests/fixtures", "examples/fixtures")
        # Include the source and build files corresponding to these binaries.
        sources = inputs / "verilog2digital-source.zip"; source_archive(sources)
        contents.add(sources, "sources/verilog2digital-source.zip")
        optional = model_contents(contents, model_ids, backends)
        print(f"Copying {len(contents.files)} files ({sum(item.size for item in contents.files.values()) / 1024**3:.2f} GiB)...", flush=True)
        contents.copy(package)
        manifest = {"schema_version": 1, "product": "verilog2digital", "version": "0.1.0",
                    "files": {name: {"sha256": item.sha256, "size": item.size} for name, item in sorted(contents.files.items()) if item.core},
                    "optional_models": optional}
        write_document(package / "release-manifest.json", manifest)
        sys.path.insert(0, str(ROOT / "agent/src"))
        from verilog2digital.release import verify_release
        verify_release(package)
        archived = temporary / "release.zip"
        archive_sha256 = write_archive(archived, package, contents, destination.name) if archive else None
        contents.verify_inputs()
        package.rename(destination)
        result = {"schema_version": 1, "directory": str(destination), "files": len(contents.files),
                  "core_files": len(manifest["files"]), "bytes": sum(item.size for item in contents.files.values()),
                  "manifest_sha256": sha256(destination / "release-manifest.json"), "optional_models": optional}
        if archive:
            archived.rename(zip_path)
            result["archive"] = str(zip_path)
            result["archive_sha256"] = archive_sha256
        return result
    finally:
        resolved = temporary.resolve(strict=True)
        if not temporary.is_symlink() and resolved.parent == parent and resolved.name.startswith(".v2d-package-"):
            shutil.rmtree(resolved)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", action="append", choices=("qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"))
    parser.add_argument("--without-models", action="store_true")
    parser.add_argument("--backend", action="append", choices=("cpu", "cuda"))
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--include-native-tests", action="store_true")
    args = parser.parse_args()
    if args.without_models and args.model:
        parser.error("Choose --without-models or explicit --model entries")
    result = assemble(args.output, model_ids=() if args.without_models else sorted(set(args.model or ["qwen3-4b-q4_k_m"])),
                      backends=tuple(sorted(set(args.backend or ["cpu", "cuda"]))), archive=args.archive,
                      include_native_tests=args.include_native_tests)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
