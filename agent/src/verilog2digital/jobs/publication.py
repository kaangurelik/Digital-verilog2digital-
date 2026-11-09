"""Publish immutable bundles by atomically selecting a verified revision."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import uuid

from ..frontend.errors import FrontendError


MAX_DOCUMENT = 8 * 1024 * 1024
MAX_BUNDLE = 128 * 1024 * 1024
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


class PublicationError(FrontendError):
    pass


def digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate JSON field: {name[:80]}")
        result[name] = value
    return result


def read_document(path: Path, *, limit: int = MAX_DOCUMENT):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PublicationError("DOCUMENT_LIMIT", f"JSON document exceeds its {limit}-byte limit", path=str(path))
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError) as error:
        raise PublicationError("INVALID_DOCUMENT", f"Invalid JSON: {error}", path=str(path)) from error


def atomic_document(path: Path, document) -> None:
    """A reader sees either complete version; an interrupted write stays private."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # A Windows reader without delete sharing briefly denies replacement of
        # current.json; retry while the old complete selection stays in place.
        delays = (0.01, 0.02, 0.04, 0.08, 0.1)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32) or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Secondary cleanup must not undo a completed pointer replacement.


def contained_file(root: Path, name: str, *, must_exist: bool = True) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise PublicationError("ARTIFACT_PATH", "Artifact names must be relative POSIX paths")
    relative = PurePosixPath(name)
    if relative.is_absolute() or relative.as_posix() != name or any(part in {".", ".."} for part in relative.parts):
        raise PublicationError("ARTIFACT_PATH", "Artifact paths cannot escape their bundle")
    root = Path(root).resolve()
    path = (root / Path(*relative.parts)).resolve(strict=must_exist)
    if not path.is_relative_to(root) or path == root or (must_exist and not path.is_file()):
        raise PublicationError("ARTIFACT_PATH", "Artifact must be a regular file inside its bundle")
    return path


def current_revision(output_root: Path) -> tuple[dict | None, str | None]:
    root = Path(output_root).resolve()
    path = root / "current.json"
    if not path.exists():
        return None, None
    current = read_document(path, limit=64 * 1024)
    required = {"schema_version", "state", "job_id", "revision", "manifest_sha256", "published_utc"}
    if (not isinstance(current, dict) or set(current) != required
            or current["schema_version"] != 2 or current["state"] != "ready"
            or not isinstance(current["job_id"], str) or not JOB_ID.fullmatch(current["job_id"])
            or current["revision"] != "revisions/" + current["job_id"]
            or not isinstance(current["manifest_sha256"], str) or not HASH.fullmatch(current["manifest_sha256"])):
        raise PublicationError("INVALID_CURRENT", "The selected revision record is invalid")
    manifest = contained_file(root, current["revision"] + "/manifest.json")
    if digest(manifest) != current["manifest_sha256"]:
        raise PublicationError("ARTIFACT_CHANGED", "The selected revision manifest has changed")
    return current, digest(path)


@contextmanager
def output_lease(output_root: Path, *, replace: bool):
    """An OS-held lock disappears on owner exit without stale PID assumptions."""
    if type(replace) is not bool:
        raise PublicationError("INVALID_REPLACE", "The replace option must be Boolean")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    allowed = {".v2d-output.lock", "revisions", "current.json", "failure.json"}
    if any(path.name not in allowed and not re.fullmatch(r"(?:\.current|failure)\.json\.[0-9a-f]{32}\.tmp", path.name)
           for path in root.iterdir()):
        raise PublicationError("UNMANAGED_OUTPUT", "Choose an empty directory or an existing managed output directory", path=str(root))
    lock = (root / ".v2d-output.lock").open("a+b")
    locked = False
    try:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise PublicationError("OUTPUT_BUSY", "Another job owns this output directory", path=str(root)) from error
        current, sha = current_revision(root)
        if current is not None and not replace:
            raise PublicationError("OUTPUT_EXISTS", "This output has a valid revision; use --replace to select a new one", path=str(root))
        yield {"root": str(root), "current_sha256": sha,
               "current_revision": current["revision"] if current else None}
    finally:
        if locked:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def verify_sources(expected: dict[str, str]) -> None:
    if not isinstance(expected, dict) or not expected:
        raise PublicationError("SOURCE_BINDING", "Publication requires the original source and include hashes")
    for name, wanted in expected.items():
        try:
            if not isinstance(wanted, str) or not HASH.fullmatch(wanted) or digest(Path(name)) != wanted:
                raise PublicationError("SOURCE_CHANGED", "An original source or include changed during the job", path=name)
        except OSError as error:
            raise PublicationError("SOURCE_CHANGED", "An original source or include is no longer readable", path=name) from error


def bundle_bindings(directory: Path, manifest: dict) -> dict[str, tuple[Path, str]]:
    """Validate declared files before treating a worker's ready result as data."""
    if not isinstance(manifest, dict):
        raise PublicationError("INVALID_CANDIDATE", "The candidate manifest must be a JSON object")
    artifacts = manifest.get("artifacts")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in {1, 2}
            or manifest.get("state") != "ready" or not isinstance(manifest.get("top"), str)
            or not isinstance(manifest.get("configuration"), dict)
            or not isinstance(manifest.get("job", {}), dict)
            or not isinstance(artifacts, dict) or not 5 <= len(artifacts) <= 136
            or not {"circuit", "sidecar", "preview", "svg", "validation"} <= set(artifacts)):
        raise PublicationError("INVALID_CANDIDATE", "Worker must supply a bounded, complete ready manifest")
    result, names, total = {}, set(), 0
    for label, item in artifacts.items():
        if not isinstance(label, str) or not re.fullmatch(r"(?:circuit|sidecar|preview|svg|validation|package|baseline_preview|preview_metadata|dependency_[0-9]{4})", label):
            raise PublicationError("ARTIFACT_BINDING", "The manifest contains an unknown artifact label")
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])):
            raise PublicationError("ARTIFACT_BINDING", "Every published artifact requires its path and SHA-256")
        path = contained_file(directory, item["path"])
        if item["path"].casefold() in names or item["path"].casefold() == "manifest.json":
            raise PublicationError("ARTIFACT_PATH", "Artifact paths must be unique and cannot replace the manifest")
        names.add(item["path"].casefold())
        total += path.stat().st_size
        if total > MAX_BUNDLE:
            raise PublicationError("BUNDLE_LIMIT", "Published artifacts exceed the 128 MiB bundle limit")
        if digest(path) != item["sha256"]:
            raise PublicationError("ARTIFACT_CHANGED", "A worker artifact changed after validation", path=str(path))
        result[label] = (path, item["sha256"])
    report = read_document(result["validation"][0])
    if not isinstance(report, dict) or report.get("valid") is not True:
        raise PublicationError("INVALID_CANDIDATE", "The candidate did not pass its validation checks")
    from ..preview import verify_preview_bindings
    verify_preview_bindings(manifest, result)
    return result


@contextmanager
def _owned_revision(root: Path, job_id: str):
    revision = root / "revisions" / job_id
    revision.mkdir(exist_ok=False)
    try:
        yield revision
    except BaseException:
        try:
            selection, _ = current_revision(root)
            resolved = revision.resolve(strict=True)
            # Recheck containment before removing this job's unselected revision.
            if ((selection is None or selection["job_id"] != job_id) and not revision.is_symlink()
                    and resolved.is_relative_to(root) and resolved != root
                    and resolved.name == job_id and resolved.parent == (root / "revisions").resolve()):
                shutil.rmtree(resolved)
        except (OSError, ValueError):
            pass  # Preserve the original failure and any inaccessible diagnostics.
        raise


def publish_revision(candidate_dir: Path, output_root: Path, job_id: str, *, expected_current_sha256: str | None,
                     replace: bool, job_metadata: dict, original_hashes: dict[str, str], checkpoint) -> dict:
    """Caller holds output_lease; current.json replacement is the commit point."""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise PublicationError("INVALID_JOB_ID", "A revision needs its 32-character hexadecimal job ID")
    candidate = Path(candidate_dir).resolve(strict=True)
    root = Path(output_root).resolve(strict=True)
    current, current_sha = current_revision(root)
    if current_sha != expected_current_sha256 or (current is not None and not replace):
        raise PublicationError("OUTPUT_CHANGED", "The selected output changed while this job was running")
    checkpoint()
    verify_sources(original_hashes)
    manifest = read_document(candidate / "manifest.json")
    bindings = bundle_bindings(candidate, manifest)
    revisions = root / "revisions"
    revisions.mkdir(exist_ok=True)
    if not revisions.resolve().is_relative_to(root):
        raise PublicationError("ARTIFACT_PATH", "Revision storage must remain inside the output directory")
    with _owned_revision(root, job_id) as revision:
        for label, (source, wanted) in bindings.items():
            checkpoint()
            target = contained_file(revision, manifest["artifacts"][label]["path"], must_exist=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if digest(target) != wanted:
                raise PublicationError("ARTIFACT_CHANGED", "An artifact changed while its revision was copied")
        published = deepcopy(manifest)
        source_map = job_metadata.get("source_snapshot", {}).get("source_map")
        if source_map:
            from .workspace import restore_provenance
            published = restore_provenance(published, source_map)
            for label in ("validation", "package"):
                if label in published["artifacts"]:
                    target = contained_file(revision, published["artifacts"][label]["path"])
                    original = read_document(target)
                    restored = restore_provenance(original, source_map)
                    if restored != original:
                        atomic_document(target, restored)
                        published["artifacts"][label]["sha256"] = digest(target)
        published.update(schema_version=2, sources=dict(original_hashes), job=deepcopy(job_metadata), job_id=job_id)
        original_project = job_metadata.get("request", {}).get("project", {})
        if original_project:
            configuration = published["configuration"]
            configuration["sources"] = list(original_project["sources"])
            configuration["include_dirs"] = list(original_project.get("include_dirs", []))
        atomic_document(revision / "manifest.json", published)
        # Verify copies and original inputs again after all potentially slow I/O.
        bundle_bindings(revision, published)
        checkpoint()
        verify_sources(original_hashes)
        if current_revision(root)[1] != expected_current_sha256:
            raise PublicationError("OUTPUT_CHANGED", "The selected output changed before publication")
        selection = {"schema_version": 2, "state": "ready", "job_id": job_id,
                     "revision": f"revisions/{job_id}", "manifest_sha256": digest(revision / "manifest.json"),
                     "published_utc": datetime.now(timezone.utc).isoformat()}
        result = {"schema_version": 2, "state": "ready", "job_id": job_id, "top": published["top"],
                "output_root": str(root), "directory": str(revision), "revision": str(revision),
                "current": str(root / "current.json"), "manifest": str(revision / "manifest.json"),
                **{label: str(contained_file(revision, item["path"])) for label, item in published["artifacts"].items()}}
        checkpoint()  # A cancellation or deadline reached during final hashing still applies.
        atomic_document(root / "current.json", selection)
        # No cancellation check or artifact I/O follows the selection.
        return result
