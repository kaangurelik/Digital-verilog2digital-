"""Cache identity and corruption checks do not launch or trust native tools."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from verilog2digital.jobs.cache import CandidateCache, CacheError, cache_key
from verilog2digital.jobs.publication import atomic_document, bundle_bindings, digest, read_document
from verilog2digital.jobs.store import file_lease


def fixture(tmp_path):
    source, include = tmp_path / "original.v", tmp_path / "constants.vh"
    source.write_text('`include "constants.vh"\nmodule sample; endmodule\n')
    include.write_text('`define WIDTH 4\n')
    staged = str(tmp_path / "old-job" / "sources" / "f0000.v")
    project = {"sources": [str(source)], "top": "sample", "include_dirs": [str(tmp_path)],
               "defines": {}, "parameters": {}}
    configuration = {key: value for key, value in project.items() if key != "top"}
    snapshot = {"sources": {str(path): digest(path) for path in (source, include)},
                "binding": {"staged": [{"path": staged}]},
                "source_map": {"schema_version": 1, "files": {staged: {"path": str(source), "columns": []}},
                               "configuration": configuration}}
    directory = tmp_path / "candidate"
    directory.mkdir()
    behavior = {"valid": True, "coverage": "exhaustive_binary", "input_vectors": 4, "diagnostics": []}
    report = {"schema_version": 1, "valid": True, "behavior": behavior,
              **{key: {"valid": True, "diagnostics": []} for key in ("components", "model", "geometry", "topology", "bits")},
              "origin": {"path": staged, "line": 2, "column": 1},
              "source": staged + ":2.1-2.9"}
    contents = {"circuit": ("circuit.dig", b"native circuit bytes"),
                "sidecar": ("circuit.dig.ids.json", b'{"native":"identity bytes"}'),
                "preview": ("preview.png", b"PNG bytes"), "svg": ("preview.svg", b"<svg/>"),
                "validation": ("validation.json", json.dumps(report).encode())}
    for name, data in contents.values():
        (directory / name).write_bytes(data)
    manifest = {"schema_version": 1, "state": "ready", "top": "sample", "profile": "datapath-v1",
                "configuration": {**configuration, "sources": [staged]}, "sources": {staged: "a" * 64},
                "behavior": behavior, "artifacts": {key: {"path": name, "sha256": digest(directory / name)}
                                                     for key, (name, _) in contents.items()}}
    atomic_document(directory / "manifest.json", manifest)
    key = cache_key(project, {"spacing": 40}, snapshot["sources"], "a" * 64)
    return directory, project, snapshot, key


def test_hit_restores_original_provenance_and_copies_only_verified_artifacts(tmp_path):
    candidate, project, snapshot, key = fixture(tmp_path)
    (candidate / "work").mkdir()
    (candidate / "work" / "unpublished.json").write_text("secret scratch")
    original_manifest = (candidate / "manifest.json").read_bytes()
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    restored = tmp_path / "new-candidate"
    result = cache.restore(key, restored)
    assert result["directory"] == str(restored)
    assert result["state"] == "ready"
    assert not (restored / "work").exists()
    assert (candidate / "manifest.json").read_bytes() == original_manifest
    assert (restored / "circuit.dig").read_bytes() == (candidate / "circuit.dig").read_bytes()
    assert (restored / "circuit.dig.ids.json").read_bytes() == (candidate / "circuit.dig.ids.json").read_bytes()
    manifest = read_document(restored / "manifest.json")
    assert manifest["configuration"] == snapshot["source_map"]["configuration"]
    assert manifest["sources"] == snapshot["sources"]
    assert manifest["artifacts"]["validation"]["sha256"] != read_document(candidate / "manifest.json")["artifacts"]["validation"]["sha256"]
    assert bundle_bindings(restored, manifest)
    for path in cache.root.rglob("*.json"):
        assert "old-job" not in path.read_text(encoding="utf-8")
    assert read_document(restored / "validation.json")["origin"]["path"] == project["sources"][0]


@pytest.mark.parametrize("changed", ["source", "include", "tool", "code", "options", "order", "defines", "parameters", "include_order", "top"])
def test_every_semantic_dependency_changes_cache_key(tmp_path, changed):
    _, project, snapshot, key = fixture(tmp_path)
    options, sources, runtime = {"spacing": 40}, deepcopy(snapshot["sources"]), "a" * 64
    if changed in {"source", "include"}:
        sources[list(sources)[changed == "include"]] = "b" * 64
    elif changed in {"tool", "code"}:
        runtime = "c" * 64
    elif changed == "options":
        options["spacing"] = 80
    elif changed == "order":
        project["sources"] = list(reversed(list(sources)))
    elif changed == "defines":
        project["defines"]["NEW"] = "1"
    elif changed == "parameters":
        project["parameters"]["WIDTH"] = 8
    elif changed == "include_order":
        project["include_dirs"] = [str(tmp_path / "earlier"), str(tmp_path)]
    elif changed == "top":
        project["top"] = None
    assert cache_key(project, options, sources, runtime) != key


def test_fresh_staged_job_paths_do_not_change_identity(tmp_path):
    _, project, snapshot, key = fixture(tmp_path)
    fresh = deepcopy(snapshot)
    fresh["binding"]["staged"][0]["path"] = str(tmp_path / "new-job" / "sources" / "f0000.v")
    fresh["source_map"]["files"] = {}
    assert cache_key(project, {"spacing": 40}, fresh["sources"], "a" * 64) == key
    assert cache_key(dict(reversed(list(project.items()))), {"spacing": 40}, dict(reversed(list(fresh["sources"].items()))), "a" * 64) == key


@pytest.mark.parametrize("file", ["circuit.dig", "circuit.dig.ids.json", "validation.json", "manifest.json", "entry.json"])
def test_corrupt_entry_is_never_restored(tmp_path, file):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    directory = cache.entries / key
    target = directory / file if file == "entry.json" else directory / "bundle" / file
    data = target.read_bytes()
    target.write_bytes(b"!" + data[1:])  # Same length defeats size-only checking.
    destination = tmp_path / "miss"
    assert cache.restore(key, destination) is None
    assert not destination.exists()
    assert not directory.exists()


def test_unknown_key_and_existing_destination_preserve_user_files(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    assert cache.restore("0" * 64, tmp_path / "missing") is None
    destination = tmp_path / "user"
    destination.mkdir()
    (destination / "keep").write_text("important")
    assert cache.restore(key, destination) is None
    assert (destination / "keep").read_text() == "important"


def test_failed_validation_cannot_enter_cache_even_with_rehashed_artifact(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    report = read_document(candidate / "validation.json")
    report["model"]["valid"] = False
    atomic_document(candidate / "validation.json", report)
    manifest = read_document(candidate / "manifest.json")
    manifest["artifacts"]["validation"]["sha256"] = digest(candidate / "validation.json")
    atomic_document(candidate / "manifest.json", manifest)
    cache = CandidateCache(tmp_path / "cache")
    assert not cache.store(key, candidate, source_snapshot=snapshot)
    assert cache.restore(key, tmp_path / "miss") is None


def test_corrupt_copy_is_never_exposed_as_candidate(tmp_path, monkeypatch):
    from verilog2digital.jobs import cache as module
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    original = module._copy
    def corrupted(source, target, **kwargs):
        original(source, target, **kwargs)
        target.write_bytes(b"corrupted")
    monkeypatch.setattr(module, "_copy", corrupted)
    assert cache.restore(key, tmp_path / "miss") is None
    assert not (tmp_path / "miss").exists()
    assert not list(cache.entries.glob(".pending-*"))


def test_lru_evicts_oldest_unused_entry_under_count_bound(tmp_path):
    candidate, project, snapshot, first = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache", max_entries=2)
    second = cache_key(project, {"spacing": 80}, snapshot["sources"], "a" * 64)
    third = cache_key(project, {"spacing": 120}, snapshot["sources"], "a" * 64)
    assert cache.store(first, candidate, source_snapshot=snapshot)
    assert cache.store(second, candidate, source_snapshot=snapshot)
    assert cache.restore(first, tmp_path / "hit")
    assert cache.store(third, candidate, source_snapshot=snapshot)
    assert cache.restore(second, tmp_path / "miss") is None
    assert (cache.entries / first).exists() and (cache.entries / third).exists()
    assert len(list(cache.entries.iterdir())) == 2


def test_storage_and_restore_copy_headroom_are_bounded(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache", max_bytes=128 * 1024)
    assert cache.store(key, candidate, source_snapshot=snapshot)
    assert cache.restore(key, tmp_path / "hit")
    assert sum(path.stat().st_size for path in cache.root.rglob("*") if path.is_file()) <= cache.max_bytes
    (candidate / "preview.png").write_bytes(b"x" * (64 * 1024))
    manifest = read_document(candidate / "manifest.json")
    manifest["artifacts"]["preview"]["sha256"] = digest(candidate / "preview.png")
    atomic_document(candidate / "manifest.json", manifest)
    assert not cache.store("c" * 64, candidate, source_snapshot=snapshot)


def test_index_traversal_and_orphan_cleanup_cannot_delete_outside_cache(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("important")
    atomic_document(cache.root / "index.json", {"schema_version": 1, "generation": 0, "entries": {"../../outside": {}}})
    assert cache.restore(key, tmp_path / "miss") is None
    assert (outside / "keep").read_text() == "important"
    assert not (cache.entries / key).exists()


def test_busy_cache_is_a_miss_and_does_not_interfere_with_owner(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    with file_lease(cache.root / ".cache.lock"):
        assert cache.restore(key, tmp_path / "miss") is None
        assert not cache.store(key, candidate, source_snapshot=snapshot)
    assert cache.restore(key, tmp_path / "hit")


def test_unmanaged_cache_directory_is_not_adopted_or_cleaned(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    directory = tmp_path / "unmanaged"
    directory.mkdir()
    (directory / "keep").write_text("important")
    cache = CandidateCache(directory)
    assert not cache.store(key, candidate, source_snapshot=snapshot)
    assert (directory / "keep").read_text() == "important"


def test_failed_report_on_restore_is_rejected_even_if_storage_hashes_were_rebuilt(tmp_path):
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    entry_dir = cache.entries / key
    bundle = entry_dir / "bundle"
    report = read_document(bundle / "validation.json")
    report["topology"]["valid"] = False
    atomic_document(bundle / "validation.json", report)
    manifest = read_document(bundle / "manifest.json")
    manifest["artifacts"]["validation"]["sha256"] = digest(bundle / "validation.json")
    atomic_document(bundle / "manifest.json", manifest)
    entry = read_document(entry_dir / "entry.json")
    for item in entry["files"]:
        item["sha256"] = digest(bundle / item["path"])
        item["bytes"] = (bundle / item["path"]).stat().st_size
    atomic_document(entry_dir / "entry.json", entry)
    index = read_document(cache.root / "index.json")
    index["entries"][key]["entry_sha256"] = digest(entry_dir / "entry.json")
    index["entries"][key]["bytes"] = sum(path.stat().st_size for path in entry_dir.rglob("*") if path.is_file())
    atomic_document(cache.root / "index.json", index)
    assert cache.restore(key, tmp_path / "miss") is None
    assert not (tmp_path / "miss").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction containment")
def test_cache_junction_never_traverses_or_deletes_external_directory(tmp_path):
    import _winapi
    candidate, _, snapshot, key = fixture(tmp_path)
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, candidate, source_snapshot=snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("important")
    link = cache.entries / key / "bundle" / "redirected"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        assert cache.restore(key, tmp_path / "miss") is None
        assert (outside / "keep").read_text() == "important"
    finally:
        os.rmdir(link)  # Remove this test's junction itself; never recurse.


@pytest.mark.parametrize("field,value", [("max_bytes", 1), ("max_bytes", True), ("max_entries", 0), ("max_entries", 65)])
def test_invalid_cache_limits_are_rejected(tmp_path, field, value):
    with pytest.raises(CacheError, match="Cache"):
        CandidateCache(tmp_path / "cache", **{field: value})
