"""Parent-owned job records, atomic JSON writes and crash-released file leases."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from ..contracts import validate
from ..frontend.errors import FrontendError


JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVENTS_BYTES = 8 * 1024 * 1024
TERMINAL = frozenset({"ready", "failed", "cancelled", "interrupted"})
MAX_RETAINED_JOBS = 16
MAX_RETAINED_BYTES = 512 * 1024 * 1024
MAX_JOB_BYTES = 512 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field {key!r}")
        result[key] = value
    return result


def read_json(path: Path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise FrontendError("JOB_FILE_LIMIT", "Job JSON exceeds its 8 MiB bound", path=str(path))
    return decode_json(data)


def decode_json(data: bytes):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON number {value}")))


def atomic_json(path: Path, value):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise FrontendError("JOB_FILE_LIMIT", "Job JSON exceeds its 8 MiB bound", path=str(path))
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Retry transient Windows sharing errors; atomic replacement preserves
        # the old record until the write succeeds.
        delays = (0.01, 0.02, 0.04, 0.08, 0.1)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if (os.name != "nt" or getattr(error, "winerror", None) not in (5, 32)
                        or attempt == len(delays)):
                    raise
                time.sleep(delays[attempt])
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lease(path: Path, *, code="JOB_BUSY"):
    """The operating system releases this nonblocking lease on owner death."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise FrontendError(code, "Another process owns this active job or output", path=str(path)) from error
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class JobStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "job.json"
        self.events_path = self.directory / "events.jsonl"
        if not JOB_ID.fullmatch(self.directory.name):
            raise FrontendError("INVALID_JOB_ID", "Job IDs contain exactly 32 lowercase hexadecimal characters")

    @classmethod
    def create(cls, root: Path, request: dict):
        validate("job-request-v2", request)
        directory = Path(root) / request["job_id"]
        directory.mkdir()
        store = cls(directory)
        now = utc_now()
        manifest = {"schema_version": 2, "job_id": request["job_id"], "kind": request["kind"],
                    "state": "created", "stage": "created", "event_seq": 0,
                    "created_utc": now, "updated_utc": now, "request": deepcopy(request),
                    "resources": {"limits": deepcopy(request["limits"])}, "artifacts": {}, "diagnostics": []}
        atomic_json(directory / "request.json", request)
        validate("job-manifest-v2", manifest)
        atomic_json(store.path, manifest)
        return store

    def read(self):
        value = read_json(self.path)
        validate("job-manifest-v2", value)
        if value["job_id"] != self.directory.name:
            raise FrontendError("INVALID_JOB_ID", "Job directory and manifest identity disagree")
        return value

    def update(self, *, state=None, stage=None, **fields):
        value = self.read()
        if value["state"] in TERMINAL:
            raise FrontendError("JOB_TERMINAL", "A terminal job cannot be modified")
        if state is not None:
            if state not in TERMINAL | {"running"}:
                raise FrontendError("JOB_STATE", f"Unsupported transition to {state}")
            value["state"] = state
        if stage is not None:
            value["stage"] = stage
        forbidden = {"schema_version", "job_id", "kind", "state", "stage", "event_seq", "request", "created_utc", "updated_utc"}
        if forbidden & fields.keys():
            raise FrontendError("JOB_STATE", "Immutable job identity or request cannot be replaced")
        value.update(deepcopy(fields))
        value["event_seq"] += 1
        value["updated_utc"] = utc_now()
        if value["state"] in TERMINAL:
            value["finished_utc"] = value["updated_utc"]
        validate("job-manifest-v2", value)
        atomic_json(self.path, value)
        event = {"schema_version": 1, "job_id": value["job_id"], "sequence": value["event_seq"],
                 "state": value["state"], "stage": value["stage"], "utc": value["updated_utc"]}
        data = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        # job.json remains authoritative if power loss interrupts journal append.
        if not self.events_path.exists() or self.events_path.stat().st_size + len(data) <= MAX_EVENTS_BYTES:
            with self.events_path.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return value


def recover_unlocked(root: Path):
    recovered = []
    root = Path(root).resolve()
    for path in sorted(root.glob("*/job.json"))[:4096]:
        if not JOB_ID.fullmatch(path.parent.name):
            continue
        store, value = None, None
        try:
            resolved = path.parent.resolve(strict=True)
            if resolved != path.parent.absolute() or resolved.parent != root:
                continue  # A redirected record may belong to another manager's live job.
            store = JobStore(path.parent)
            value = store.read()
            if value["state"] not in TERMINAL:
                output = value["request"].get("output_root")
                if output:
                    from .publication import bundle_bindings, current_revision, contained_file, read_document
                    current, _ = current_revision(Path(output))
                    if current and current["job_id"] == value["job_id"]:
                        revision = Path(output).resolve() / current["revision"]
                        manifest = read_document(revision / "manifest.json")
                        bundle_bindings(revision, manifest)
                        result = {"schema_version": 2, "state": "ready", "job_id": value["job_id"],
                                  "directory": str(revision), "revision": str(revision), "output_root": str(Path(output).resolve()),
                                  "current": str(Path(output).resolve() / "current.json"), "manifest": str(revision / "manifest.json"),
                                  "job_manifest": str(store.path),
                                  **{key: str(contained_file(revision, item["path"])) for key, item in manifest["artifacts"].items()}}
                        recovered.append(store.update(state="ready", stage="ready", result=result,
                                                      recovery="committed_current_revision"))
                        continue
                recovered.append(store.update(state="interrupted", stage="interrupted", diagnostics=[
                    {"code": "JOB_INTERRUPTED", "message": "The owning manager stopped before this job completed"}]))
        except (OSError, ValueError, FrontendError) as error:
            # A damaged selected output cannot promote an unfinished job.
            if store is not None and value is not None and value["state"] not in TERMINAL:
                try:
                    recovered.append(store.update(state="interrupted", stage="interrupted", diagnostics=[
                        {"code": "JOB_INTERRUPTED", "message": f"Unfinished job recovery could not verify its output: {error}"}]))
                except (OSError, ValueError, FrontendError):
                    pass
            continue
    return recovered


def directory_bytes(directory: Path, *, limit=None, max_entries=20000):
    """Measure only a bounded, unredirected owned tree without following links."""
    supplied = Path(directory).absolute()
    root = supplied.resolve(strict=True)
    if root != supplied:
        raise FrontendError("JOB_STORAGE_PATH", "Owned job storage must not use a redirected root", path=str(supplied))
    pending, total, entries = [root], 0, 0
    while pending:
        with os.scandir(pending.pop()) as stream:
            for item in stream:
                entries += 1
                if entries > max_entries:
                    raise FrontendError("JOB_DISK_LIMIT", "The owned job exceeds its file-count bound")
                path = Path(item.path)
                try:
                    # DirEntry may cache stale sizes for growing Windows files.
                    # A fresh no-follow stat also works for locked JVM temporary files.
                    metadata = path.stat(follow_symlinks=False)
                    if (stat.S_ISLNK(metadata.st_mode)
                            or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                        raise FrontendError("JOB_STORAGE_PATH", "Owned job storage must not contain redirected paths", path=str(path))
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
                except FileNotFoundError:
                    continue  # A worker may atomically replace a temporary file.
                if limit is not None and total > limit:
                    raise FrontendError("JOB_DISK_LIMIT", "The owned job exceeds its 512 MiB disk budget")
    return total


def _remove_owned_tree(path: Path, root: Path):
    path, root = Path(path), Path(root).resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or resolved == root or not resolved.is_relative_to(root):
        raise FrontendError("JOB_STORAGE_PATH", "Refusing to remove a redirected or unowned job path", path=str(path))
    directory_bytes(resolved)  # Check the complete subtree before recursive removal.
    shutil.rmtree(resolved)


def prune_jobs(root: Path, *, max_jobs=MAX_RETAINED_JOBS, max_bytes=MAX_RETAINED_BYTES, keep_ids=()):
    """Caller holds manager lease; published output revisions are never visited."""
    if type(max_jobs) is not int or max_jobs < 1 or type(max_bytes) is not int or max_bytes < 1:
        raise FrontendError("JOB_RETENTION", "Retention requires positive job-count and byte bounds")
    root = Path(root).resolve(strict=True)
    records, diagnostics = [], []
    for path in sorted(root.iterdir()):
        if not JOB_ID.fullmatch(path.name) or not path.is_dir():
            continue
        try:
            if path.resolve(strict=True) != path.absolute() or path.resolve().parent != root:
                raise FrontendError("JOB_STORAGE_PATH", "Owned job directories must not be redirected")
            value = JobStore(path).read()
            records.append({"path": path, "value": value, "bytes": directory_bytes(path)})
        except (OSError, ValueError, FrontendError) as error:
            diagnostics.append({"job_id": path.name, "message": str(error)})
    records.sort(key=lambda item: (item["value"]["created_utc"], item["path"].name))
    removed, pruned = [], []
    terminal_count = sum(item["value"]["state"] in TERMINAL for item in records)
    for record in list(records):
        if terminal_count <= max_jobs:
            break
        if record["value"]["state"] not in TERMINAL or record["path"].name in keep_ids:
            continue
        _remove_owned_tree(record["path"], root)
        removed.append(record["path"].name)
        records.remove(record)
        terminal_count -= 1
    total = sum(item["bytes"] for item in records)
    for record in records:
        if total <= max_bytes:
            break
        if record["value"]["state"] not in TERMINAL:
            continue
        removed_paths = []
        for name in ("candidate", "sources", "tmp"):
            target = record["path"] / name
            if target.is_dir():
                _remove_owned_tree(target, record["path"])
                removed_paths.append(name)
        if removed_paths:
            value = record["value"]
            value["retention"] = {"heavy_artifacts_removed": removed_paths, "reason": "retained_bytes_limit",
                                  "retained_utc": utc_now(), "max_jobs": max_jobs, "max_bytes": max_bytes}
            atomic_json(record["path"] / "job.json", value)
            remaining = directory_bytes(record["path"])
            total += remaining - record["bytes"]
            record["bytes"] = remaining
            pruned.append(record["path"].name)
    # Prune old terminal records, retaining the just-completed job's diagnostics.
    for record in list(records):
        if total <= max_bytes:
            break
        if record["value"]["state"] in TERMINAL and record["path"].name not in keep_ids:
            _remove_owned_tree(record["path"], root)
            total -= record["bytes"]
            removed.append(record["path"].name)
            records.remove(record)
    return {"removed_jobs": removed, "pruned_jobs": pruned, "retained_bytes": total,
            "retained_jobs": len(records), "max_jobs": max_jobs, "max_bytes": max_bytes, "diagnostics": diagnostics}
