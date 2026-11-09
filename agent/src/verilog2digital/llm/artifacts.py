"""Pinned official local inference artifacts; provisioning never executes downloads."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import time
import urllib.request
import uuid
import zipfile

from ..runtime import repository_root
from ..frontend.errors import FrontendError
from ..jobs.publication import atomic_document
from ..jobs.store import file_lease


class ModelError(FrontendError):
    # Keep rejected output separate from diagnostics and replayable actions.
    rejected_response: dict | None = None
    # Attach the requested profile only after selection succeeds.
    requested_profile: dict | None = None
    allocation_evidence: dict | None = None


def load_lock(root=None):
    root = Path(root or repository_root()).resolve()
    return json.loads((root / "scripts/llm-lock.json").read_text(encoding="utf-8"))


def sha256(path, check=None):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        while block := stream.read(4 * 1024**2):
            if check:
                check()
            digest.update(block)
        return digest.hexdigest()


def model_path(model_id, root=None):
    root = Path(root or repository_root()).resolve()
    spec = load_lock(root)["models"].get(model_id)
    if spec is None:
        raise ModelError("MODEL_PROFILE", f"Unknown pinned model: {model_id}")
    return root / ".tools/models" / spec["filename"]


def _regular(path, root):
    path = Path(path)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ModelError("MODEL_PATH", "Artifact escaped its local tools directory")
    for item in (path, *path.parents):
        if item == Path(root):
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ModelError("MODEL_PATH", "Artifact paths cannot contain links or junctions")
    if not path.is_file():
        raise ModelError("MODEL_UNAVAILABLE", f"Pinned local artifact is missing: {path.name}; install it with the desktop Install button or v2d models install")
    return path


def _no_links(path):
    for item in (Path(path), *Path(path).parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ModelError("MODEL_PATH", "Artifact paths cannot contain links or junctions")


def verify_file(path, spec, *, root, check=None):
    path = _regular(path, root)
    if path.stat().st_size != spec["size"] or sha256(path, check) != spec["sha256"]:
        raise ModelError("MODEL_INTEGRITY", f"Pinned artifact hash or size differs: {path.name}")
    return path


@contextmanager
def read_guard(path):
    """Deny writes/replacement while native idle sleep may release its own map."""
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(Path(path)), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        raise ModelError("MODEL_INTEGRITY", f"Cannot hold immutable local artifact: {Path(path).name} (Windows {error})")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        yield stream


def verify_installation(model_id, backend, root=None, *, check=None, guards=None):
    """Rehash model at explicit load, never in the ordinary converter fingerprint."""
    root = Path(root or repository_root()).resolve()
    lock = load_lock(root)
    if model_id not in lock["models"]:
        raise ModelError("MODEL_PROFILE", "Unknown pinned model")
    path = _regular(model_path(model_id, root), root / ".tools")
    if guards is not None:
        guards.enter_context(read_guard(path))
    model = verify_file(path, lock["models"][model_id], root=root / ".tools", check=check)
    return {**verify_runtime(backend, root, check=check, guards=guards), "model_path": str(model),
            "model": {"id": model_id, **lock["models"][model_id]}}


def verify_runtime(backend, root=None, *, check=None, guards=None):
    root = Path(root or repository_root()).resolve()
    lock = load_lock(root)
    if backend not in {"cuda", "cpu"}:
        raise ModelError("MODEL_PROFILE", "Backend must be cuda or cpu")
    directory = root / ".tools/llama.cpp" / (lock["runtime"]["nightly"] + "-" + backend)
    marker = _regular(directory / "installation.json", root / ".tools")
    if marker.stat().st_size > 1024**2:
        raise ModelError("MODEL_INTEGRITY", "Runtime installation manifest exceeds its bound")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    expected = {key: lock["runtime"]["artifacts"][key]["sha256"] for key in (["cuda", "cudart"] if backend == "cuda" else ["cpu"])}
    if manifest.get("archives") != expected or manifest.get("commit") != lock["runtime"]["commit"]:
        raise ModelError("MODEL_INTEGRITY", "Runtime installation does not match the pinned release")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not 1 <= len(files) <= 256 or "llama-server.exe" not in files:
        raise ModelError("MODEL_INTEGRITY", "Runtime installation inventory is invalid")
    # Verify extracted files against pinned archives, not mutable install markers.
    official = {}
    for key in expected:
        spec = lock["runtime"]["artifacts"][key]
        archive = verify_file(root / ".tools/downloads" / spec["filename"], spec, root=root / ".tools", check=check)
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                with bundle.open(item) as stream:
                    digest = hashlib.sha256()
                    while block := stream.read(4 * 1024**2):
                        if check:
                            check()
                        digest.update(block)
                name = PurePosixPath(item.filename.replace("\\", "/")).name
                if name in official:
                    raise ModelError("MODEL_INTEGRITY", "Duplicate file in pinned runtime archives")
                official[name] = {"size": item.file_size, "sha256": digest.hexdigest()}
    if files != official:
        raise ModelError("MODEL_INTEGRITY", "Runtime inventory differs from its official pinned archives")
    for name, spec in files.items():
        if Path(name).name != name or name in {".", ".."}:
            raise ModelError("MODEL_PATH", "Runtime manifest contains an invalid file name")
        path = _regular(directory / name, directory)
        if guards is not None:
            guards.enter_context(read_guard(path))
        verify_file(path, spec, root=directory, check=check)
    actual = {p.name for p in directory.iterdir() if p.name != "installation.json"}
    if actual != set(files):
        raise ModelError("MODEL_INTEGRITY", "Runtime directory contains files outside the verified inventory")
    return {"executable": str(directory / "llama-server.exe"),
            "runtime": {"release": lock["runtime"]["release"], "nightly": lock["runtime"]["nightly"],
                        "commit": lock["runtime"]["commit"], "archives": expected,
                        "server_sha256": files["llama-server.exe"]["sha256"]}}


def _download(url, spec, target):
    target = Path(target)
    _no_links(target)
    if target.is_file() and target.stat().st_size == spec["size"] and sha256(target) == spec["sha256"]:
        return
    if shutil.disk_usage(target.parent).free < spec["size"] + 512 * 1024**2:
        raise ModelError("MODEL_DISK", "Insufficient free disk space for the pinned artifact")
    part = target.with_name("." + target.name + ".part-" + uuid.uuid4().hex)
    start = time.monotonic()
    count, last = 0, 0
    request = urllib.request.Request(url, headers={"User-Agent": "verilog2digital-pinned-provisioner"})
    try:
        with urllib.request.urlopen(request, timeout=60) as source, part.open("xb") as output:
            digest = hashlib.sha256()
            while block := source.read(1024**2):
                count += len(block)
                if count > spec["size"] or time.monotonic() - start > 3600:
                    raise ModelError("MODEL_DOWNLOAD", "Download exceeded its pinned size or one-hour deadline")
                digest.update(block)
                output.write(block)
                if count - last >= 256 * 1024**2:
                    print(f"{target.name}: {count // 1024**2}/{spec['size'] // 1024**2} MiB", flush=True)
                    last = count
        if count != spec["size"] or digest.hexdigest() != spec["sha256"]:
            raise ModelError("MODEL_INTEGRITY", f"Downloaded artifact failed official hash verification: {target.name}")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def provision(*, model_ids=("qwen3-4b-q4_k_m",), backends=("cuda", "cpu"), root=None):
    root = Path(root or repository_root()).resolve()
    lock = load_lock(root)
    tools = root / ".tools"
    _no_links(tools)
    tools.mkdir(exist_ok=True)
    for name in ("downloads", "models", "llama.cpp"):
        _no_links(tools / name)
        (tools / name).mkdir(exist_ok=True)
    with file_lease(tools / "llama.cpp/provision.lock"):
        for backend in backends:
            if backend not in {"cuda", "cpu"}:
                raise ModelError("MODEL_PROFILE", "Unknown backend")
            directory = tools / "llama.cpp" / (lock["runtime"]["nightly"] + "-" + backend)
            marker = directory / "installation.json"
            keys = ["cuda", "cudart"] if backend == "cuda" else ["cpu"]
            archives = []
            for key in keys:
                spec = lock["runtime"]["artifacts"][key]
                archive = tools / "downloads" / spec["filename"]
                _download(f"https://github.com/ggml-org/llama.cpp/releases/download/{lock['runtime']['nightly']}/{spec['filename']}", spec, archive)
                archives.append(archive)
            if marker.is_file():
                verify_runtime(backend, root)
                continue
            staging = tools / "llama.cpp" / (".pending-" + uuid.uuid4().hex)
            staging.mkdir()
            try:
                total = 0
                for archive in archives:
                    with zipfile.ZipFile(archive) as bundle:
                        for item in bundle.infolist():
                            path = PurePosixPath(item.filename.replace("\\", "/"))
                            if path.is_absolute() or ".." in path.parts or ":" in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                                raise ModelError("MODEL_ARCHIVE", "Unsafe runtime archive entry")
                            if item.is_dir():
                                continue
                            total += item.file_size
                            if total > 3 * 1024**3 or item.file_size > 1024**3:
                                raise ModelError("MODEL_ARCHIVE", "Runtime archive exceeds extraction bounds")
                            target = staging / path.name
                            if target.exists():
                                raise ModelError("MODEL_ARCHIVE", "Runtime archives contain duplicate flattened file names")
                            with bundle.open(item) as reader, target.open("xb") as writer:
                                shutil.copyfileobj(reader, writer, length=1024**2)
                files = {p.name: {"size": p.stat().st_size, "sha256": sha256(p)} for p in staging.iterdir()}
                if "llama-server.exe" not in files:
                    raise ModelError("MODEL_ARCHIVE", "Verified runtime has no llama-server.exe")
                atomic_document(staging / "installation.json", {"commit": lock["runtime"]["commit"],
                    "archives": {key: lock["runtime"]["artifacts"][key]["sha256"] for key in keys}, "files": files})
                if directory.exists():
                    raise ModelError("MODEL_INSTALL", "Incomplete runtime installation already exists; inspect it before retrying")
                staging.rename(directory)
            finally:
                if staging.exists() and staging.resolve().parent == (tools / "llama.cpp").resolve():
                    shutil.rmtree(staging)
        for model_id in model_ids:
            if model_id not in lock["models"]:
                raise ModelError("MODEL_PROFILE", "Unknown model ID")
            spec = lock["models"][model_id]
            _download(f"https://huggingface.co/{spec['repository']}/resolve/{spec['revision']}/{spec['filename']}?download=true", spec, model_path(model_id, root))
    return {"models": list(model_ids), "backends": list(backends), "verified": True}
