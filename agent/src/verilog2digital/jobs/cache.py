"""Cache verified bundles against current source and runtime fingerprints.

Hits reuse recorded coverage. Native bytes stay unchanged; JSON paths are
restored before hashing. Staging and cleanup stay under the cache root, and
an atomic rename exposes the restored candidate.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from ..frontend.errors import FrontendError
from .publication import bundle_bindings, contained_file, digest, read_document
from .store import file_lease
from .workspace import restore_provenance


HASH = re.compile(r"[0-9a-f]{64}\Z")
PENDING = re.compile(r"\.pending-[0-9a-f]{32}\Z")
MAX_DOCUMENT = 8 * 1024**2
MAX_INDEX = 64 * 1024
FORMAT = {"schema_version": 1, "kind": "verified-converter-candidates"}


class CacheError(FrontendError):
    pass


def _json(value, *, limit=MAX_DOCUMENT):
    try:
        data = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise CacheError("CACHE_INPUT", f"Cache inputs must be bounded JSON: {error}") from error
    if len(data) > limit:
        raise CacheError("CACHE_LIMIT", "Cache metadata exceeds its size bound")
    return data


def cache_key(project: dict, options: dict, sources: dict[str, str], runtime_sha256: str) -> str:
    """Call only after fresh include discovery; never include staged job paths."""
    required = {"sources", "top", "include_dirs", "defines", "parameters"}
    if (not isinstance(project, dict) or set(project) != required or not isinstance(options, dict)
            or not isinstance(project["sources"], list) or not project["sources"]
            or not isinstance(project["include_dirs"], list)
            or not isinstance(sources, dict) or not 1 <= len(sources) <= 1024
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   or not isinstance(sha, str) or not HASH.fullmatch(sha) for path, sha in sources.items())
            or any(not isinstance(path, str) or path not in sources for path in project["sources"])
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in project["include_dirs"])
            or not isinstance(runtime_sha256, str) or not HASH.fullmatch(runtime_sha256)):
        raise CacheError("CACHE_INPUT", "Cache identity requires the original project, complete source/include hashes and runtime hash")
    value = {"schema_version": 1, "project": project, "options": options,
             "sources": sources, "runtime_sha256": runtime_sha256}
    return hashlib.sha256(_json(value)).hexdigest()


def _ready_bundle(directory):
    manifest = read_document(directory / "manifest.json")
    bindings = bundle_bindings(directory, manifest)
    report = read_document(bindings["validation"][0])
    required = {"components", "geometry", "model", "topology", "behavior"}
    if (manifest.get("schema_version") != 1 or "job" in manifest or "job_id" in manifest
            or not isinstance(manifest.get("top"), str) or not manifest["top"]
            or report.get("schema_version") != 1
            or any(not isinstance(report.get(name), dict) or report[name].get("valid") is not True for name in required)
            or manifest.get("behavior") != report["behavior"]):
        raise CacheError("CACHE_VALIDATION", "Only complete verified converter candidates can enter the cache")
    def checks(value):
        if isinstance(value, dict):
            if "valid" in value and value["valid"] is not True:
                raise CacheError("CACHE_VALIDATION", "A recorded validation check did not pass")
            for item in value.values():
                checks(item)
        elif isinstance(value, list):
            for item in value:
                checks(item)
    checks(report)
    if "package" in bindings:
        from ..validation.hierarchy import verify_package_snapshot
        package = read_document(bindings["package"][0])
        if not verify_package_snapshot(directory, package.get("snapshot", {}))["valid"]:
            raise CacheError("CACHE_VALIDATION", "A hierarchy package dependency has changed")
        covered = {item["path"] for item in package["snapshot"]["files"]}
        native_files = {manifest["artifacts"][name]["path"] for name in bindings
                        if name in {"circuit", "sidecar"} or name.startswith("dependency_")}
        if covered != native_files:
            raise CacheError("CACHE_VALIDATION", "Hierarchy snapshot and published native artifacts differ")
    return manifest, bindings


def _copy(source, target, *, size, sha):
    """Never copy beyond a verified byte count, including during concurrent edits."""
    target.parent.mkdir(parents=True, exist_ok=True)
    value = hashlib.sha256()
    remaining = size
    with source.open("rb") as reader, target.open("xb") as writer:
        while remaining:
            block = reader.read(min(1024**2, remaining))
            if not block:
                raise CacheError("CACHE_CHANGED", "A cache artifact became shorter while copying")
            writer.write(block)
            value.update(block)
            remaining -= len(block)
        if reader.read(1) or value.hexdigest() != sha:
            raise CacheError("CACHE_CHANGED", "A cache artifact changed while copying")


class CandidateCache:
    def __init__(self, root: Path, *, max_bytes=256 * 1024**2, max_entries=8):
        if type(max_bytes) is not int or not 64 * 1024 <= max_bytes <= 1024**3:
            raise CacheError("CACHE_LIMIT", "Cache size must be from 64 KiB through 1 GiB")
        if type(max_entries) is not int or not 1 <= max_entries <= 64:
            raise CacheError("CACHE_LIMIT", "Cache entry count must be from one through 64")
        self.root = Path(root).resolve()
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries = self.root / "entries"

    def _check_root(self):
        if self.root.resolve(strict=True) != self.root:
            raise CacheError("CACHE_DIRECTORY", "Cache root was redirected")
        marker = self.root / "format.json"
        if not marker.exists():
            if any(item.name != ".cache.lock" for item in self.root.iterdir()):
                raise CacheError("CACHE_DIRECTORY", "Cache storage must be a new or previously managed cache directory")
            self._write(marker, FORMAT)
        elif read_document(marker, limit=1024) != FORMAT:
            raise CacheError("CACHE_DIRECTORY", "Cache storage has an unknown format")
        self.entries.mkdir(exist_ok=True)
        if self.entries.resolve(strict=True) != self.entries:
            raise CacheError("CACHE_DIRECTORY", "Cache entry storage was redirected")
        for path in (marker, self.root / "index.json"):
            if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise CacheError("CACHE_DIRECTORY", "Cache metadata cannot be redirected")

    @staticmethod
    def _write(path, value, *, limit=MAX_INDEX):
        data = _json(value, limit=limit)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _tree(self, directory):
        """Reject links before traversal or deletion, including Windows junctions."""
        if (directory.parent != self.entries or not (HASH.fullmatch(directory.name) or PENDING.fullmatch(directory.name))
                or directory.resolve(strict=True).parent != self.entries):
            raise CacheError("CACHE_DIRECTORY", "Cleanup target is outside owned cache entry storage")
        files = []
        stack = [directory]
        visited = 0
        while stack:
            path = stack.pop()
            visited += 1
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CacheError("CACHE_DIRECTORY", "Cache entries cannot contain links or reparse points")
            if not path.resolve(strict=True).is_relative_to(self.entries):
                raise CacheError("CACHE_DIRECTORY", "Cache entry path escaped owned storage")
            if path.is_dir():
                stack.extend(path.iterdir())
            elif path.is_file():
                files.append(path)
            else:
                raise CacheError("CACHE_DIRECTORY", "Cache entries contain an unsupported filesystem object")
            if visited + len(stack) > 512:
                raise CacheError("CACHE_LIMIT", "Cache entry contains too many filesystem objects")
        return files

    def _remove(self, directory):
        if directory.exists():
            self._tree(directory)  # Validate final absolute target before recursion.
            shutil.rmtree(directory)

    def _index(self):
        empty = {"schema_version": 1, "generation": 0, "entries": {}}
        try:
            value = read_document(self.root / "index.json", limit=MAX_INDEX)
            if (not isinstance(value, dict) or set(value) != set(empty) or value["schema_version"] != 1
                    or type(value["generation"]) is not int or not 0 <= value["generation"] < 2**63
                    or not isinstance(value["entries"], dict) or len(value["entries"]) > 64):
                raise ValueError("Invalid cache index")
            for key, item in value["entries"].items():
                if (not HASH.fullmatch(key) or not isinstance(item, dict)
                        or set(item) != {"entry_sha256", "bytes", "last_used"}
                        or not isinstance(item["entry_sha256"], str) or not HASH.fullmatch(item["entry_sha256"])
                        or type(item["bytes"]) is not int or not 0 < item["bytes"] <= 1024**3
                        or type(item["last_used"]) is not int or not 0 <= item["last_used"] <= value["generation"]):
                    raise ValueError("Invalid cache index record")
        except (OSError, ValueError, FrontendError):
            value = empty
        children = list(self.entries.iterdir())
        if len(children) > 256:
            raise CacheError("CACHE_LIMIT", "Cache storage contains too many entries")
        for path in children:
            if path.name not in value["entries"]:
                self._remove(path)
        # An index is only a selector: each record must bind its own manifest.
        for key, row in list(value["entries"].items()):
            path = self.entries / key
            try:
                files = self._tree(path)
                if (sum(item.stat().st_size for item in files) != row["bytes"]
                        or (path / "entry.json").stat().st_size > MAX_INDEX
                        or digest(path / "entry.json") != row["entry_sha256"]):
                    raise ValueError("Cache entry binding changed")
            except (OSError, ValueError, FrontendError):
                self._remove(path)
                del value["entries"][key]
        return value

    def _save(self, index):
        self._write(self.root / "index.json", index)

    @staticmethod
    def _touch(index, key):
        index["generation"] += 1
        index["entries"][key]["last_used"] = index["generation"]

    def _space(self, index, incoming, *, protect=None, new_entry=False):
        # Reserve bounded root metadata as well as any temporary restore copy.
        while (sum(row["bytes"] for row in index["entries"].values()) + incoming + MAX_INDEX + 1024 > self.max_bytes
               or len(index["entries"]) + int(new_entry) > self.max_entries):
            eligible = [(row["last_used"], key) for key, row in index["entries"].items() if key != protect]
            if not eligible:
                return False
            _, oldest = min(eligible)
            self._remove(self.entries / oldest)
            del index["entries"][oldest]
        return True

    def _verify(self, key, row):
        directory = self.entries / key
        entry = read_document(directory / "entry.json", limit=MAX_INDEX)
        if (digest(directory / "entry.json") != row["entry_sha256"] or not isinstance(entry, dict)
                or set(entry) != {"schema_version", "key", "files"} or entry["schema_version"] != 1
                or entry["key"] != key or not isinstance(entry["files"], list) or not 6 <= len(entry["files"]) <= 133):
            raise CacheError("CACHE_CHANGED", "Cache entry manifest does not match its index")
        bundle = directory / "bundle"
        names = set()
        for item in entry["files"]:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}
                    or not isinstance(item["path"], str) or item["path"].casefold() in names
                    or not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])
                    or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= 128 * 1024**2):
                raise CacheError("CACHE_CHANGED", "Invalid cache artifact binding")
            names.add(item["path"].casefold())
            path = contained_file(bundle, item["path"])
            if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                raise CacheError("CACHE_CHANGED", "Cache artifact hash or length changed")
        actual = {path.relative_to(directory).as_posix() for path in self._tree(directory)}
        if actual != {"entry.json", *("bundle/" + item["path"] for item in entry["files"])}:
            raise CacheError("CACHE_CHANGED", "Cache entry contains undeclared files")
        manifest, _ = _ready_bundle(bundle)
        if names != {"manifest.json", *(item["path"].casefold() for item in manifest["artifacts"].values())}:
            raise CacheError("CACHE_CHANGED", "Cache manifest and artifact inventory differ")
        return entry, manifest

    def store(self, key: str, candidate_dir: Path, *, source_snapshot: dict) -> bool:
        """An invalid, busy, full or unavailable cache never prevents conversion."""
        temporary = None
        try:
            if not isinstance(key, str) or not HASH.fullmatch(key):
                raise CacheError("CACHE_INPUT", "Cache key must be a SHA-256 digest")
            original = Path(candidate_dir).resolve(strict=True)
            manifest, bindings = _ready_bundle(original)
            sources, source_map = source_snapshot["sources"], source_snapshot["source_map"]
            if not isinstance(sources, dict) or not sources or any(not HASH.fullmatch(value) for value in sources.values()):
                raise CacheError("CACHE_INPUT", "Cache storage requires complete original source hashes")
            restored = restore_provenance(manifest, source_map)
            restored["sources"] = deepcopy(sources)
            restored["configuration"] = deepcopy(source_map["configuration"])
            data, source_files = {}, {}
            for label, (source, sha) in bindings.items():
                name = manifest["artifacts"][label]["path"]
                if label in {"validation", "package"}:
                    data[name] = _json(restore_provenance(read_document(source), source_map))
                    restored["artifacts"][label]["sha256"] = hashlib.sha256(data[name]).hexdigest()
                else:
                    source_files[name] = (source, source.stat().st_size, sha)
            data["manifest.json"] = _json(restored)
            # Refuse an unrecognized field that would preserve old staged paths.
            for content in data.values():
                text = content.decode("utf-8")
                for staged in source_map.get("files", {}):
                    if json.dumps(staged, ensure_ascii=False)[1:-1] in text or Path(staged).as_posix() in text:
                        raise CacheError("CACHE_PROVENANCE", "Known cache JSON still contains a staged source path")
            files = [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                     for name, content in data.items()]
            files += [{"path": name, "bytes": size, "sha256": sha} for name, (_, size, sha) in source_files.items()]
            entry = {"schema_version": 1, "key": key, "files": sorted(files, key=lambda item: item["path"])}
            entry_data = _json(entry, limit=MAX_INDEX)
            size = sum(item["bytes"] for item in files) + len(entry_data)
            # Copy headroom makes restore atomic without exceeding the cache cap.
            if 2 * size + MAX_INDEX + 1024 > self.max_bytes:
                return False
            with file_lease(self.root / ".cache.lock", code="CACHE_BUSY"):
                self._check_root()
                index = self._index()
                if key in index["entries"]:
                    try:
                        self._verify(key, index["entries"][key])
                        self._touch(index, key)
                        self._save(index)
                        return True
                    except (OSError, ValueError, FrontendError):
                        self._remove(self.entries / key)
                        del index["entries"][key]
                if not self._space(index, size, new_entry=True):
                    self._save(index)
                    return False
                temporary = self.entries / (".pending-" + uuid.uuid4().hex)
                temporary.mkdir()
                bundle = temporary / "bundle"
                bundle.mkdir()
                for name, content in data.items():
                    target = contained_file(bundle, name, must_exist=False)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                for name, (source, length, sha) in source_files.items():
                    _copy(source, contained_file(bundle, name, must_exist=False), size=length, sha=sha)
                _ready_bundle(bundle)
                (temporary / "entry.json").write_bytes(entry_data)
                os.replace(temporary, self.entries / key)
                temporary = None
                index["entries"][key] = {"entry_sha256": hashlib.sha256(entry_data).hexdigest(),
                                          "bytes": size, "last_used": 0}
                self._touch(index, key)
                self._save(index)
                return True
        except (OSError, ValueError, KeyError, TypeError, RecursionError, FrontendError):
            return False
        finally:
            if temporary is not None:
                try:
                    self._remove(temporary)
                except (OSError, FrontendError):
                    pass  # A future lease can retry cleanup within owned storage.

    def restore(self, key: str, destination: Path) -> dict | None:
        """Return a new candidate only after complete verification; otherwise miss."""
        temporary = None
        try:
            if not isinstance(key, str) or not HASH.fullmatch(key):
                raise CacheError("CACHE_INPUT", "Cache key must be a SHA-256 digest")
            destination = Path(destination).absolute()
            if destination.exists() or not destination.parent.is_dir():
                return None
            with file_lease(self.root / ".cache.lock", code="CACHE_BUSY"):
                self._check_root()
                index = self._index()
                if key not in index["entries"]:
                    self._save(index)
                    return None
                try:
                    entry, manifest = self._verify(key, index["entries"][key])
                except (OSError, ValueError, KeyError, TypeError, RecursionError, FrontendError):
                    self._remove(self.entries / key)
                    del index["entries"][key]
                    self._save(index)
                    return None
                if not self._space(index, index["entries"][key]["bytes"], protect=key):
                    self._save(index)
                    return None
                temporary = self.entries / (".pending-" + uuid.uuid4().hex)
                temporary.mkdir()
                bundle = temporary / "bundle"
                bundle.mkdir()
                for item in entry["files"]:
                    _copy(contained_file(self.entries / key / "bundle", item["path"]),
                          contained_file(bundle, item["path"], must_exist=False), size=item["bytes"], sha=item["sha256"])
                _ready_bundle(bundle)
                self._touch(index, key)
                self._save(index)
                # Both arguments are directories; Windows rename refuses an
                # existing destination. Different-volume caches safely miss.
                result = {"schema_version": 1, "state": "ready", "top": manifest["top"],
                          "directory": str(destination), "manifest": str(destination / "manifest.json"),
                          "progress_warnings": manifest.get("progress_warnings", []),
                          **{label: str(destination / Path(item["path"])) for label, item in manifest["artifacts"].items()}}
                bundle.rename(destination)
                return result
        except (OSError, ValueError, KeyError, TypeError, RecursionError, FrontendError):
            return None
        finally:
            if temporary is not None:
                try:
                    self._remove(temporary)
                except (OSError, FrontendError):
                    pass
