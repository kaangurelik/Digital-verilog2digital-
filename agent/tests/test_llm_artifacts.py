from __future__ import annotations

import hashlib
from contextlib import ExitStack
import json
import os
from pathlib import Path
import zipfile

import pytest

from verilog2digital.llm import artifacts


def spec(path):
    return {"filename": path.name, "size": path.stat().st_size, "sha256": artifacts.sha256(path)}


def fixture(root):
    (root / "scripts").mkdir()
    tools = root / ".tools"
    downloads = tools / "downloads"
    downloads.mkdir(parents=True)
    model = tools / "models/model.gguf"
    model.parent.mkdir()
    model.write_bytes(b"GGUF-pinned-test-model")
    archive = downloads / "cpu.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("runtime/llama-server.exe", b"verified-test-program-not-executed")
        bundle.writestr("runtime/ggml.dll", b"verified-test-library")
    lock = {"schema_version": 1, "runtime": {"release": "v0.4.0", "nightly": "test", "commit": "abc",
                                             "artifacts": {"cpu": spec(archive)}},
            "models": {"test": {**spec(model), "revision": "def", "repository": "Qwen/test", "layers": 1}}}
    (root / "scripts/llm-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    artifacts.provision(model_ids=["test"], backends=["cpu"], root=root)
    return tools / "llama.cpp/test-cpu", model


def test_provision_and_load_bind_both_model_and_official_runtime(tmp_path):
    folder, model = fixture(tmp_path)
    result = artifacts.verify_installation("test", "cpu", tmp_path)
    assert result["executable"] == str(folder / "llama-server.exe")
    assert result["model_path"] == str(model)
    assert result["runtime"]["commit"] == "abc"


@pytest.mark.parametrize("target", ["model", "dll", "archive", "extra"])
def test_changed_artifacts_fail_before_loading(tmp_path, target):
    folder, model = fixture(tmp_path)
    paths = {"model": model, "dll": folder / "ggml.dll", "archive": tmp_path / ".tools/downloads/cpu.zip",
             "extra": folder / "foreign.dll"}
    paths[target].write_bytes(b"changed")
    with pytest.raises(artifacts.ModelError) as caught:
        artifacts.verify_installation("test", "cpu", tmp_path)
    assert caught.value.code == "MODEL_INTEGRITY"


def test_rewriting_local_inventory_cannot_authorize_changed_dll(tmp_path):
    folder, _ = fixture(tmp_path)
    dll = folder / "ggml.dll"
    dll.write_bytes(b"modified local program")
    marker = folder / "installation.json"
    manifest = json.loads(marker.read_text())
    manifest["files"][dll.name] = {"size": dll.stat().st_size, "sha256": artifacts.sha256(dll)}
    marker.write_text(json.dumps(manifest))
    with pytest.raises(artifacts.ModelError, match="official pinned archives"):
        artifacts.verify_installation("test", "cpu", tmp_path)


def test_reprovision_does_not_claim_corrupted_existing_installation_is_verified(tmp_path):
    folder, _ = fixture(tmp_path)
    (folder / "ggml.dll").write_bytes(b"changed")
    with pytest.raises(artifacts.ModelError):
        artifacts.provision(model_ids=["test"], backends=["cpu"], root=tmp_path)


def test_verification_checks_cancellation_between_hash_chunks(tmp_path):
    fixture(tmp_path)
    def stop():
        raise artifacts.ModelError("MODEL_CANCELLED", "cancelled")
    with pytest.raises(artifacts.ModelError) as caught:
        artifacts.verify_installation("test", "cpu", tmp_path, check=stop)
    assert caught.value.code == "MODEL_CANCELLED"


@pytest.mark.skipif(os.name != "nt", reason="Windows immutable artifact handles")
def test_loaded_artifacts_cannot_change_during_idle_until_owner_closes(tmp_path):
    directory, model = fixture(tmp_path)
    with ExitStack() as guards:
        artifacts.verify_installation("test", "cpu", tmp_path, guards=guards)
        for path in (model, directory / "ggml.dll", directory / "llama-server.exe"):
            with pytest.raises(PermissionError):
                path.write_bytes(b"changed while idle")
            with pytest.raises(PermissionError):
                path.unlink()
    model.write_bytes(b"owner released guard")


def test_missing_model_actionable(tmp_path):
    _, model = fixture(tmp_path)
    model.unlink()
    with pytest.raises(artifacts.ModelError) as caught:
        artifacts.verify_installation("test", "cpu", tmp_path)
    assert caught.value.code == "MODEL_UNAVAILABLE"


def test_download_wrong_hash_never_publishes_partial(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(artifacts.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"changed"))
    target = tmp_path / "model.gguf"
    with pytest.raises(artifacts.ModelError):
        artifacts._download("https://example.invalid/pinned", {"size": 7, "sha256": "0" * 64}, target)
    assert not target.exists() and not target.with_suffix(".gguf.part").exists()
    assert not list(tmp_path.glob(".*.part-*"))


@pytest.mark.parametrize("entry", ["../../outside.exe", "/absolute.exe", "C:/absolute.exe"])
def test_zip_traversal_never_extracts(tmp_path, entry):
    (tmp_path / "scripts").mkdir()
    downloads = tmp_path / ".tools/downloads"
    downloads.mkdir(parents=True)
    archive = downloads / "cpu.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(entry, b"not executable")
    lock = {"runtime": {"nightly": "test", "commit": "x", "artifacts": {"cpu": spec(archive)}}, "models": {}}
    (tmp_path / "scripts/llm-lock.json").write_text(json.dumps(lock))
    with pytest.raises(artifacts.ModelError) as caught:
        artifacts.provision(model_ids=[], backends=["cpu"], root=tmp_path)
    assert caught.value.code == "MODEL_ARCHIVE"
    assert not (tmp_path / "outside.exe").exists()
    assert not list((tmp_path / ".tools/llama.cpp").glob(".pending-*"))
