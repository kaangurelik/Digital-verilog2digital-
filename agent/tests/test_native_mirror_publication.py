"""A short Windows publication lock cannot weaken immutable mirror admission."""
import ctypes
import os
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from verilog2digital import release
from verilog2digital.frontend.errors import FrontendError
from test_portable_release import manifest


@pytest.fixture
def mirror_input(tmp_path, monkeypatch):
    monkeypatch.delenv("V2D_NATIVE_TOOLS_ROOT", raising=False)
    root = tmp_path / "package \u03a9"
    root.mkdir()
    document = manifest(root)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    verified = release.verify_release(root)
    target = jobs.parent / "native-tools" / verified["sha256"]
    return root, jobs, document, verified, target


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(release, "time", clock)
    return clock


def sharing_error(code):
    error = OSError("Simulated native mirror publication failure")
    error.winerror = code
    return error


def create(mirror_input):
    root, jobs, _, verified, _ = mirror_input
    return release.ensure_native_mirror(root, jobs, verified)


def no_pending(target):
    assert list(target.parent.glob(".pending-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows publication retry policy")
@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_windows_denial_retries_exact_paths_and_verifies_final_bytes(mirror_input, fake_clock, monkeypatch, code):
    original_rename, original_verify = Path.rename, release._verify_mirror
    calls, verified_paths = [], []
    def rename(path, target):
        calls.append((path, target))
        if len(calls) <= 2:
            raise sharing_error(code)
        return original_rename(path, target)
    def verify(root, directory, document):
        verified_paths.append(directory)
        return original_verify(root, directory, document)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(release, "_verify_mirror", verify)
    target = create(mirror_input)
    assert len(calls) == 3 and len(set(calls)) == 1
    assert fake_clock.now == pytest.approx(0.15)
    assert verified_paths == [calls[0][0], target]
    assert target == mirror_input[-1]
    no_pending(target)


@pytest.mark.skipif(os.name != "nt", reason="Windows publication retry policy")
@pytest.mark.parametrize("code", [5, 32, 33])
def test_persistent_windows_denial_exhausts_ten_seconds_and_cleans_only_pending(mirror_input, fake_clock, monkeypatch, code):
    target = mirror_input[-1]
    sentinel = target.parent / "unrelated"
    sentinel.mkdir(parents=True)
    (sentinel / "keep.txt").write_text("keep")
    error, attempts = sharing_error(code), []
    def rename(path, destination):
        attempts.append((path, destination))
        raise error
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError) as caught:
        create(mirror_input)
    assert caught.value is error and fake_clock.now == pytest.approx(10.0)
    assert 2 < len(attempts) <= 25 and len(set(attempts)) == 1
    assert all(0 < value <= 0.5 for value in fake_clock.sleeps)
    assert not target.exists() and (sentinel / "keep.txt").read_text() == "keep"
    no_pending(target)


@pytest.mark.parametrize("code", [2, 3, 13, 112, None])
def test_unrelated_publication_error_is_not_retried(mirror_input, fake_clock, monkeypatch, code):
    error, attempts = sharing_error(code), []
    def rename(path, target):
        attempts.append((path, target))
        raise error
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError) as caught:
        create(mirror_input)
    assert caught.value is error and len(attempts) == 1 and fake_clock.sleeps == []
    no_pending(mirror_input[-1])


def test_non_windows_error_code_does_not_activate_windows_retry(mirror_input, fake_clock, monkeypatch):
    monkeypatch.setattr(release, "os", SimpleNamespace(name="posix", environ=os.environ))
    error = sharing_error(5)
    def rename(path, target):
        raise error
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError) as caught:
        create(mirror_input)
    assert caught.value is error and fake_clock.sleeps == []
    no_pending(mirror_input[-1])


@pytest.mark.parametrize("corruption", [None, "changed", "missing", "unlisted"])
def test_concurrent_winner_requires_complete_inventory_and_is_not_overwritten(mirror_input, fake_clock, monkeypatch, corruption):
    calls = []
    def rename(path, target):
        calls.append((path, target))
        shutil.copytree(path, target)
        if corruption == "changed":
            payload = target / "jdk/bin/java.exe"
            payload.write_bytes(b"!" * payload.stat().st_size)
        elif corruption == "missing":
            (target / "jdk/bin/java.exe").unlink()
        elif corruption == "unlisted":
            (target / "jdk/bin/unbound.dll").write_bytes(b"unbound")
        raise sharing_error(183)
    monkeypatch.setattr(Path, "rename", rename)
    if corruption is None:
        assert create(mirror_input) == mirror_input[-1]
    else:
        with pytest.raises(FrontendError) as caught:
            create(mirror_input)
        assert caught.value.code == "RELEASE_CHANGED"
    assert len(calls) == 1 and fake_clock.sleeps == []
    assert mirror_input[-1].is_dir()  # Never remove somebody else's published target.
    no_pending(mirror_input[-1])


@pytest.mark.skipif(os.name != "nt", reason="Windows publication retry policy")
@pytest.mark.parametrize("corruption", ["changed", "missing", "unlisted"])
def test_pending_mutation_during_retry_is_rejected_after_successful_rename(mirror_input, fake_clock, monkeypatch, corruption):
    original, calls = Path.rename, []
    def rename(path, target):
        calls.append((path, target))
        if len(calls) == 1:
            payload = path / "jdk/bin/java.exe"
            if corruption == "changed":
                payload.write_bytes(b"!" * payload.stat().st_size)
            elif corruption == "missing":
                payload.unlink()
            else:
                (path / "jdk/bin/unbound.dll").write_bytes(b"unbound")
            raise sharing_error(32)
        return original(path, target)
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(FrontendError) as caught:
        create(mirror_input)
    assert caught.value.code == "RELEASE_CHANGED" and len(calls) == 2
    no_pending(mirror_input[-1])


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction boundary")
def test_concurrent_target_junction_is_rejected_and_external_bytes_preserved(mirror_input, fake_clock, monkeypatch, tmp_path):
    import _winapi
    external = tmp_path / "unrelated-winner"
    target = mirror_input[-1]
    def rename(path, destination):
        shutil.copytree(path, external)
        _winapi.CreateJunction(str(external), str(destination))
        raise sharing_error(183)
    monkeypatch.setattr(Path, "rename", rename)
    try:
        with pytest.raises(FrontendError) as caught:
            create(mirror_input)
        assert caught.value.code == "RELEASE_PATH"
        assert target.is_junction() and (external / "jdk/bin/java.exe").is_file()
        no_pending(target)
    finally:
        if target.is_junction():
            target.rmdir()  # Remove only this fixture's junction, never its target.


@pytest.mark.skipif(os.name != "nt", reason="Windows publication retry policy")
@pytest.mark.parametrize("where", ["parent", "cache", "pending", "target"])
def test_retry_checks_paths_again_before_renaming(mirror_input, fake_clock, monkeypatch, where):
    original, changed, calls = Path.is_junction, [], []
    def rename(path, target):
        calls.append((path, target))
        raise sharing_error(32)
    original_sleep = fake_clock.sleep
    def sleep(seconds):
        original_sleep(seconds)
        path, target = calls[-1]
        changed.append({"parent": target.parent.parent, "cache": target.parent,
                        "pending": path, "target": target}[where])
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(fake_clock, "sleep", sleep)
    monkeypatch.setattr(Path, "is_junction", lambda path: path in changed or original(path))
    with pytest.raises(FrontendError) as caught:
        create(mirror_input)
    assert caught.value.code == "RELEASE_PATH" and len(calls) == 1
    # Simulated link metadata does not create real links. Remove the test-only
    # metadata before checking normal cleanup; production never follows it.
    changed.clear()
    for pending in mirror_input[-1].parent.glob(".pending-*"):
        assert pending.parent == mirror_input[-1].parent
        shutil.rmtree(pending)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction and cleanup boundary")
def test_cache_redirect_during_backoff_never_cleans_external_pending_tree(mirror_input, fake_clock, monkeypatch, tmp_path):
    import _winapi
    original_rename = Path.rename
    target = mirror_input[-1]
    cache, retained, external = target.parent, tmp_path / "retained-cache", tmp_path / "external-cache"
    external.mkdir()
    calls, redirected = [], []
    def rename(path, destination):
        calls.append((path, destination))
        raise sharing_error(32)
    original_sleep = fake_clock.sleep
    def sleep(seconds):
        original_sleep(seconds)
        pending = calls[-1][0]
        external_pending = external / pending.name
        external_pending.mkdir()
        (external_pending / "untouched.txt").write_text("unrelated user data")
        original_rename(cache, retained)
        _winapi.CreateJunction(str(external), str(cache))
        redirected.append(external_pending)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(fake_clock, "sleep", sleep)
    try:
        with pytest.raises(FrontendError) as caught:
            create(mirror_input)
        assert caught.value.code == "RELEASE_PATH" and len(calls) == 1
        assert (redirected[0] / "untouched.txt").read_text() == "unrelated user data"
        assert len(list(retained.glob(".pending-*"))) == 1
    finally:
        if cache.is_junction():
            cache.rmdir()  # Only the link, not the unrelated directory.
        if retained.exists():
            original_rename(retained, cache)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows sharing semantics")
def test_real_windows_directory_handle_released_after_two_seconds_allows_publication(mirror_input, monkeypatch):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    original, attempts, errors = Path.rename, [], []
    held, timer, released = [], [], threading.Event()
    def close_handle():
        if held:
            handle = held.pop()
            if not kernel.CloseHandle(handle):
                errors.append(ctypes.get_last_error())
        released.set()
    def rename(path, target):
        attempts.append((path, target))
        if len(attempts) == 1:
            # Sharing permits reads/writes but deliberately omits DELETE.
            handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            held.append(handle)
            timer.append(threading.Timer(2.2, close_handle))
            timer[0].start()
        try:
            return original(path, target)
        except OSError as error:
            assert error.winerror in {5, 32, 33}
            raise
    monkeypatch.setattr(Path, "rename", rename)
    started = time.monotonic()
    try:
        target = create(mirror_input)
        elapsed = time.monotonic() - started
        assert released.is_set() and not errors and 2 <= elapsed < 10
        assert len(attempts) >= 5 and len(set(attempts)) == 1
        assert target == mirror_input[-1]
        no_pending(target)
    finally:
        if timer:
            timer[0].cancel()
            timer[0].join(timeout=3)
        close_handle()
