"""Standard-library-only integrity and path policy for a copied Windows release."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid

from .frontend.errors import FrontendError


MANIFEST = "release-manifest.json"
MAX_MANIFEST = 8 * 1024**2
MAX_FILES = 20000
MAX_BYTES = 4 * 1024**3
HASH = re.compile(r"[0-9a-f]{64}\Z")
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                   *(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789\u00b9\u00b2\u00b3")}
PRODUCTION = ("attrs", "jsonschema", "jsonschema-specifications", "pyslang", "referencing", "rpds-py")
GENERATED = {".venv/pyvenv.cfg", ".venv/Scripts/python.exe", ".venv/Scripts/pythonw.exe"}
GENERATED_SCRIPTS = GENERATED | {".venv/Scripts/" + name for name in (
    "activate", "activate.fish", "Activate.ps1", "activate.bat", "deactivate.bat")}


def _error(code, message, path=None):
    return FrontendError(code, message, path=str(path) if path is not None else None)


def is_release(root: Path) -> bool:
    root = Path(root)
    return (root / MANIFEST).exists() or (root / ".tools/python-runtime/python.exe").exists()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _error("RELEASE_MANIFEST", "Release manifest contains a repeated key")
        result[key] = value
    return result


def _relative_parts(name: str) -> tuple[str, ...]:
    if (not isinstance(name, str) or not name or len(name) > 512 or "\\" in name
            or ":" in name or "\0" in name or name != PurePosixPath(name).as_posix()
            or PurePosixPath(name).is_absolute() or any(p in {".", ".."} for p in name.split("/"))):
        raise _error("RELEASE_PATH", "Release inventory paths must be normalized relative paths")
    try:
        name.encode("utf-8")
    except UnicodeError as error:
        raise _error("RELEASE_PATH", "Release paths must contain valid Unicode") from error
    parts = PurePosixPath(name).parts
    # Reject Windows device names and trailing-dot/space aliases.
    if any(part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
           or part.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_DEVICES for part in parts):
        raise _error("RELEASE_PATH", "Release paths cannot contain Windows aliases, devices, or invalid characters")
    return parts


def _checked_file(root: Path, parts: tuple[str, ...]) -> Path:
    """Resolve validated path segments under a canonical root.

    Recheck the file and every ancestor on each call to detect redirected paths.
    """
    path = root.joinpath(*parts)
    item = path
    while True:
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise _error("RELEASE_PATH", "Release files cannot traverse links or junctions", item)
        if item == root:
            break
        item = item.parent
    return path


def _relative_file(root: Path, name: str) -> Path:
    """Internal path check after the caller resolves its root once per pass."""
    return _checked_file(root, _relative_parts(name))


def relative_file(root: Path, name: str) -> Path:
    parts = _relative_parts(name)
    root = Path(root).resolve(strict=True)
    path = _checked_file(root, parts)
    if not path.resolve().is_relative_to(root):
        raise _error("RELEASE_PATH", "Release file escaped its root", path)
    return path


def release_manifest(root: Path) -> dict | None:
    root = Path(root).resolve(strict=True)
    if not is_release(root):
        return None
    path = _relative_file(root, MANIFEST)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_MANIFEST + 1)
        if len(data) > MAX_MANIFEST:
            raise _error("RELEASE_MANIFEST", "Release manifest exceeds 8 MiB")
        document = json.loads(data, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, FrontendError):
            raise
        raise _error("RELEASE_MANIFEST", "Release manifest is missing or malformed", path) from error
    if (not isinstance(document, dict) or set(document) != {"schema_version", "product", "version", "files", "optional_models"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["product"] != "verilog2digital" or not isinstance(document["version"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", document["version"])
            or not isinstance(document["files"], dict) or not 1 <= len(document["files"]) <= MAX_FILES):
        raise _error("RELEASE_MANIFEST", "Release manifest has an unsupported shape")
    seen, total = set(), 0
    for name, item in document["files"].items():
        _relative_file(root, name)
        folded = name.casefold()
        if (folded in seen or name == MANIFEST or name in GENERATED
                or name.startswith(".runs/") and name != ".runs/digital-build/Digital.jar"
                or not isinstance(item, dict) or set(item) != {"sha256", "size"}
                or not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])
                or type(item["size"]) is not int or not 0 <= item["size"] <= MAX_BYTES):
            raise _error("RELEASE_MANIFEST", "Release inventory has duplicate, mutable, or invalid entries", name)
        seen.add(folded)
        total += item["size"]
    if total > MAX_BYTES:
        raise _error("RELEASE_MANIFEST", "Core release inventory exceeds 4 GiB")
    models = document["optional_models"]
    if not isinstance(models, list) or len(models) > 3:
        raise _error("RELEASE_MANIFEST", "Optional model inventory is invalid")
    ids = set()
    for model in models:
        if (not isinstance(model, dict) or set(model) != {"model_id", "backends"}
                or not isinstance(model["model_id"], str)
                or model["model_id"] not in {"qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"}
                or model["model_id"] in ids or not isinstance(model["backends"], list)
                or not 1 <= len(model["backends"]) <= 2
                or any(not isinstance(b, str) or b not in {"cpu", "cuda"} for b in model["backends"])
                or len(set(model["backends"])) != len(model["backends"])):
            raise _error("RELEASE_MANIFEST", "Optional model inventory is invalid")
        ids.add(model["model_id"])
    return document


def _hash_file(path: Path, expected: dict) -> None:
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected["size"]:
            raise _error("RELEASE_CHANGED", "A packaged runtime file is missing or has changed size", path)
        digest, remaining = hashlib.sha256(), expected["size"]
        with path.open("rb") as stream:
            while remaining:
                block = stream.read(min(1024**2, remaining))
                if not block:
                    raise _error("RELEASE_CHANGED", "A packaged runtime file became shorter", path)
                digest.update(block)
                remaining -= len(block)
            if stream.read(1) or digest.hexdigest() != expected["sha256"]:
                raise _error("RELEASE_CHANGED", "A packaged runtime file differs from its release hash", path)
    except OSError as error:
        raise _error("RELEASE_CHANGED", "A packaged runtime file is missing or unreadable", path) from error


def native_tools_root(root: Path) -> Path:
    root = Path(root).resolve(strict=True)
    selected = os.environ.get("V2D_NATIVE_TOOLS_ROOT")
    if not selected:
        return root / ".tools"
    path = Path(selected).resolve(strict=True)
    if not Path(selected).is_absolute() or not str(path).isascii():
        raise _error("NATIVE_TOOLS_PATH", "V2D_NATIVE_TOOLS_ROOT must be an absolute ASCII directory", path)
    if not is_release(root):
        raise _error("NATIVE_TOOLS_PATH", "A native tools override requires a verified release inventory")
    return path


def _native_directories(root):
    try:
        with (root / "scripts/toolchain-lock.json").open("rb") as stream:
            data = stream.read(1024**2 + 1)
        if len(data) > 1024**2:
            raise ValueError("large lock")
        lock = json.loads(data)
        names = tuple(lock[key]["directory"] for key in ("jdk", "hdl"))
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", name)
               or name in {".", ".."} for name in names):
            raise ValueError("unsafe directory")
        return names
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise _error("RELEASE_MANIFEST", "Packaged tool directory configuration is invalid") from error


def _reject_unlisted(root, directories, files, *, allowed=()):
    root = Path(root).resolve(strict=True)
    for directory in directories:
        for path in (root / directory).rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(root).as_posix()
            _relative_file(root, name)
            if path.is_file() and name not in files and name not in allowed:
                raise _error("RELEASE_CHANGED", "An unbound file is present in the private runtime", path)


def _native_entries(root, document):
    prefixes = tuple(".tools/" + name + "/" for name in _native_directories(root))
    result = {}
    for name, expected in document["files"].items():
        if name.startswith(prefixes) or name == ".tools/runtime-support.jar":
            result[name.removeprefix(".tools/")] = (name, expected)
        elif (name in {"digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar"}
              or name.startswith("src/main/dig/lib/")):
            result["_application/" + name] = (name, expected)
    return result


def _verify_mirror(root, directory, document):
    directory = Path(directory).resolve(strict=True)
    entries = _native_entries(root, document)
    for destination, (_, expected) in entries.items():
        _hash_file(_relative_file(directory, destination), expected)
    _reject_unlisted(directory, (*_native_directories(root), "_application"), entries)
    return len(entries)


def native_asset(root: Path, name: str) -> Path:
    """Path to a native-only application asset in the exact verified mirror."""
    root = Path(root).resolve(strict=True)
    if not is_release(root):
        return root / name
    tools = native_tools_root(root)
    return relative_file(root, name) if tools == root / ".tools" else relative_file(tools, "_application/" + name)


def _publish_native_mirror(root, parent, temporary, target, document):
    """Publish the same verified directory despite a bounded Windows sharing delay."""
    deadline, delay, last_error = time.monotonic() + 10.0, 0.05, None

    def checked_paths():
        # Keep the original canonical root to detect redirected parents.
        if parent.resolve(strict=True) != parent:
            raise _error("RELEASE_PATH", "Native mirror storage changed before publication", parent)
        return (_relative_file(parent, "native-tools/" + temporary.name),
                _relative_file(parent, "native-tools/" + target.name))

    while True:
        if last_error is not None and time.monotonic() >= deadline:
            raise last_error
        pending, destination = checked_paths()
        if destination.exists():
            # Another launcher may have published this exact release meanwhile.
            _verify_mirror(root, destination, document)
            return
        try:
            pending.rename(destination)
        except OSError as error:
            _, destination = checked_paths()
            if destination.exists():
                _verify_mirror(root, destination, document)
                return
            remaining = deadline - time.monotonic()
            if (os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33)
                    or remaining <= 0):
                raise
            last_error = error
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.5)
        else:
            _, destination = checked_paths()
            # Recheck pending content after waiting; it may have changed.
            _verify_mirror(root, destination, document)
            return


def ensure_native_mirror(root: Path, jobs: Path, verified: dict) -> Path:
    """Create a bounded, immutable ASCII copy for narrow-path native tools."""
    root = Path(root).resolve(strict=True)
    selected = native_tools_root(root)
    if selected != root / ".tools" or str(root).isascii():
        return selected
    document = release_manifest(root)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if hashlib.sha256(canonical).hexdigest() != verified["sha256"]:
        raise _error("RELEASE_CHANGED", "Release inventory changed before native mirror creation")
    parent = Path(jobs).resolve(strict=True).parent
    if not str(parent).isascii():
        raise _error("NATIVE_TOOLS_PATH", "Native mirror storage requires an ASCII path")
    cache = relative_file(parent, "native-tools")
    cache.mkdir(exist_ok=True)
    target = relative_file(cache, verified["sha256"])
    if target.exists():
        _verify_mirror(root, target, document)
        return target
    temporary = relative_file(cache, ".pending-" + uuid.uuid4().hex)
    temporary.mkdir(exist_ok=False)
    try:
        for name, (source, expected) in _native_entries(root, document).items():
            destination = relative_file(temporary, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with relative_file(root, source).open("rb") as reader, destination.open("xb") as writer:
                remaining = expected["size"]
                while remaining:
                    block = reader.read(min(1024**2, remaining))
                    if not block:
                        raise _error("RELEASE_CHANGED", "A native runtime file became shorter while copying")
                    writer.write(block)
                    remaining -= len(block)
                if reader.read(1):
                    raise _error("RELEASE_CHANGED", "A native runtime file grew while copying")
            _hash_file(destination, expected)
        _verify_mirror(root, temporary, document)
        _publish_native_mirror(root, parent, temporary, target, document)
        return target
    finally:
        if temporary.exists():
            # Check the original root before removing pending directories.
            try:
                if parent.resolve(strict=True) == parent:
                    checked = _relative_file(parent, "native-tools/" + temporary.name)
                    if checked.resolve(strict=True) == temporary and temporary.name.startswith(".pending-"):
                        shutil.rmtree(checked)
            except FrontendError:
                pass  # Preserve the publication failure; leave redirected paths untouched.


def verify_release(root: Path, *, require_complete=True) -> dict | None:
    root = Path(root).resolve(strict=True)
    document = release_manifest(root)
    if document is None:
        return None
    files = document["files"]
    required = {"scripts/portable_bootstrap.py", "scripts/v2d.py", "scripts/toolchain.py", "scripts/toolchain-lock.json",
                "agent/src/verilog2digital/release.py", "agent/src/verilog2digital/runtime.py",
                "gui/build/v2d-gui.exe", "digital-adapter/target/digital-adapter.jar", ".runs/digital-build/Digital.jar",
                ".tools/runtime-support.jar", ".tools/python-runtime/python.exe", ".tools/python-runtime/python314.dll",
                ".tools/python-runtime/Lib/venv/scripts/nt/venvlauncher.exe",
                ".tools/python-runtime/Lib/venv/scripts/nt/venvwlauncher.exe"}
    # Allow only the packaged Digital JAR under .runs in the release manifest.
    if require_complete and not required <= set(files):
        raise _error("RELEASE_MANIFEST", "Release inventory omits a required runtime file")
    for name, expected in files.items():
        _hash_file(_relative_file(root, name), expected)
    native_directories = _native_directories(root)
    if require_complete:
        jdk, hdl = native_directories
        native_required = {f".tools/{jdk}/bin/{name}.exe" for name in ("java", "javaw")}
        native_required |= {f".tools/{hdl}/bin/{name}.exe" for name in ("yosys", "iverilog", "vvp")}
        if not native_required <= set(files):
            raise _error("RELEASE_MANIFEST", "Release inventory omits a required native executable")
        _reject_unlisted(root, ("agent/src/verilog2digital", "schemas", "scripts", "gui/build",
            "digital-adapter/target", "src/main/dig/lib", ".venv/Lib/site-packages", ".venv/Scripts",
            ".tools/python-runtime", *(".tools/" + name for name in native_directories)), files, allowed=GENERATED_SCRIPTS)
    alternate = native_tools_root(root)
    mirrored = 0
    if alternate != root / ".tools":
        mirrored = _verify_mirror(root, alternate, document)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {"sha256": hashlib.sha256(canonical).hexdigest(), "files": len(files),
            "bytes": sum(item["size"] for item in files.values()), "native_mirror_files": mirrored}


def job_root(root: Path) -> Path:
    root = Path(root).resolve(strict=True)
    configured = os.environ.get("V2D_JOB_ROOT")
    candidate = Path(configured) if configured else root / ".runs/jobs"
    if not candidate.is_absolute():
        raise _error("UNSUPPORTED_JOB_ROOT", "V2D_JOB_ROOT must be an absolute ASCII path")
    candidate = candidate.resolve()
    if not str(candidate).isascii():
        raise _error("UNSUPPORTED_JOB_ROOT", "Choose a writable ASCII job directory and set V2D_JOB_ROOT; project and output paths may contain Unicode", candidate)
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate
