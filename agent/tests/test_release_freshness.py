"""File/link metadata from one release check must never authorize a later one."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

import pytest

from verilog2digital import release
from verilog2digital.frontend.errors import FrontendError


@pytest.fixture
def small_release(tmp_path, monkeypatch):
    monkeypatch.delenv("V2D_NATIVE_TOOLS_ROOT", raising=False)
    root = tmp_path / "release"
    files = {}
    data = {"scripts/toolchain-lock.json": json.dumps({"jdk": {"directory": "jdk"}, "hdl": {"directory": "hdl"}}).encode(),
            "payload/first.bin": b"first bound file", "payload/second.bin": b"second bound file"}
    for name, body in data.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files[name] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    (root / "release-manifest.json").write_text(json.dumps({"schema_version": 1, "product": "verilog2digital", "version": "0.1.0",
        "files": files, "optional_models": []}), encoding="utf-8")
    return root


def verify(root):
    # These are inert data fixtures; complete production inventory behavior is
    # independently covered by test_portable_release.py.
    return release.verify_release(root, require_complete=False)


def test_same_size_mutation_with_restored_timestamp_is_rehashed(small_release):
    root = small_release
    expected = verify(root)
    target = root / "payload/second.bin"
    original = target.read_bytes()
    before = target.stat()
    target.write_bytes(b"X" * len(original))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(FrontendError) as caught:
        verify(root)
    assert caught.value.code == "RELEASE_CHANGED"
    target.write_bytes(original)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert verify(root) == expected


@contextmanager
def junction(directory):
    """Temporarily alias only one private fixture directory, without recursion."""
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    import _winapi
    moved = directory.with_name(directory.name + "-held")
    assert not moved.exists()
    directory.rename(moved)
    created = False
    try:
        _winapi.CreateJunction(str(moved), str(directory))
        created = True
        assert directory.is_junction()
        yield
    finally:
        if created:
            directory.rmdir()  # Removes this junction only; never its target.
        moved.rename(directory)


def test_junction_inserted_after_success_is_rejected_even_for_identical_bytes(small_release):
    root = small_release
    expected = verify(root)
    with junction(root / "payload"):
        assert (root / "payload/second.bin").read_bytes() == b"second bound file"
        with pytest.raises(FrontendError) as caught:
            verify(root)
        assert caught.value.code == "RELEASE_PATH"
    assert verify(root) == expected


def test_new_junction_between_files_in_one_verification_is_not_cached(small_release, monkeypatch):
    root = small_release
    expected = verify(root)
    original_hash = release._hash_file
    link = junction(root / "payload")
    entered = False
    def after_first(path, expected_hash):
        nonlocal entered
        original_hash(path, expected_hash)
        if path == root / "payload/first.bin":
            link.__enter__()
            entered = True
    monkeypatch.setattr(release, "_hash_file", after_first)
    try:
        with pytest.raises(FrontendError) as caught:
            verify(root)
        assert entered
        assert caught.value.code == "RELEASE_PATH"
    finally:
        if entered:
            link.__exit__(None, None, None)
    monkeypatch.setattr(release, "_hash_file", original_hash)
    assert verify(root) == expected


def test_file_symlink_inserted_after_success_is_rejected(small_release):
    root = small_release
    expected = verify(root)
    target = root / "payload/second.bin"
    moved = target.with_name("second-held.bin")
    target.rename(moved)
    created = False
    try:
        try:
            target.symlink_to(moved)
            created = True
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                pytest.skip("File symlink creation requires an unavailable Windows privilege")
            raise
        assert target.is_symlink() and target.read_bytes() == b"second bound file"
        with pytest.raises(FrontendError) as caught:
            verify(root)
        assert caught.value.code == "RELEASE_PATH"
    finally:
        if created:
            target.unlink()
        moved.rename(target)
    assert verify(root) == expected


def test_prechecked_canonical_root_is_checked_again_for_a_new_junction(small_release):
    root = small_release.resolve(strict=True)
    # An internal verification loop keeps one canonical root Path. Replacing
    # that actual directory later must still be detected by ancestry checking.
    assert release._relative_file(root, "payload/first.bin").read_bytes() == b"first bound file"
    with junction(root):
        with pytest.raises(FrontendError) as caught:
            release._relative_file(root, "payload/second.bin")
        assert caught.value.code == "RELEASE_PATH"
