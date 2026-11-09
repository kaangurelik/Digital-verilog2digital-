"""Replacement, interruption, and tampering must preserve the selected bundle."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil

import pytest

from verilog2digital.jobs import publication
from verilog2digital.jobs.publication import (PublicationError, atomic_document, bundle_bindings,
    current_revision, digest, output_lease, publish_revision, read_document)


def candidate(tmp_path, name="candidate"):
    directory = tmp_path / name
    directory.mkdir()
    contents = {"circuit": ("circuit.dig", "circuit bytes"), "sidecar": ("circuit.dig.ids.json", "{}"),
                "preview": ("preview.png", "preview"), "svg": ("preview.svg", "<svg/>"),
                "validation": ("validation.json", json.dumps({"valid": True}))}
    for filename, text in contents.values():
        (directory / filename).write_text(text, encoding="utf-8")
    manifest = {"schema_version": 1, "state": "ready", "top": "sample", "profile": "scalar-v1",
                "configuration": {"sources": ["staged.v"], "include_dirs": []},
                "artifacts": {label: {"path": filename, "sha256": digest(directory / filename)}
                              for label, (filename, _) in contents.items()}}
    atomic_document(directory / "manifest.json", manifest)
    source = tmp_path / "original.v"
    source.write_text("module sample; endmodule", encoding="utf-8")
    return directory, {str(source): digest(source)}


def publish(directory, sources, output, *, job_id="a" * 32, replace=False, checkpoint=lambda: None):
    with output_lease(output, replace=replace) as lease:
        return publish_revision(directory, output, job_id, expected_current_sha256=lease["current_sha256"],
                                replace=replace, original_hashes=sources,
                                job_metadata={"request": {"project": {"sources": list(sources)}}}, checkpoint=checkpoint)


def test_unicode_output_and_replacement_keep_prior_revision_bytes(tmp_path):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "çıktı alanı Ω"
    first = publish(directory, sources, output)
    original = Path(first["circuit"]).read_bytes()
    second = publish(directory, sources, output, job_id="b" * 32, replace=True)
    assert Path(first["circuit"]).read_bytes() == original
    assert first["revision"] != second["revision"]
    assert current_revision(output)[0]["job_id"] == "b" * 32
    manifest = read_document(Path(second["manifest"]))
    assert manifest["schema_version"] == 2 and manifest["sources"] == sources
    assert manifest["configuration"]["sources"] == list(sources)
    assert Path(second["circuit"]).is_relative_to(output)


def test_existing_selection_requires_explicit_replace(tmp_path):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    publish(directory, sources, output)
    selected = (output / "current.json").read_bytes()
    with pytest.raises(PublicationError) as error:
        publish(directory, sources, output, job_id="b" * 32)
    assert error.value.code == "OUTPUT_EXISTS"
    assert (output / "current.json").read_bytes() == selected


def test_cancellation_during_copy_preserves_current_selection(tmp_path):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    first = publish(directory, sources, output)
    selected = (output / "current.json").read_bytes()
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        publish(directory, sources, output, job_id="b" * 32, replace=True, checkpoint=cancel)
    assert (output / "current.json").read_bytes() == selected
    assert Path(first["circuit"]).read_text() == "circuit bytes"
    assert current_revision(output)[0]["job_id"] == "a" * 32
    assert not (output / "revisions" / ("b" * 32)).exists()


def test_source_change_at_last_checkpoint_never_selects_revision(tmp_path):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    calls = 0
    def change():
        nonlocal calls
        calls += 1
        if calls == 7:  # initial, five copies, and the check before final source hashing
            Path(next(iter(sources))).write_text("changed", encoding="utf-8")
    with pytest.raises(PublicationError) as error:
        publish(directory, sources, output, checkpoint=change)
    assert error.value.code == "SOURCE_CHANGED"
    assert not (output / "current.json").exists()
    assert not (output / "revisions" / ("a" * 32)).exists()


def test_cancellation_after_final_source_hashing_still_prevents_selection(tmp_path, monkeypatch):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    publish(directory, sources, output)
    selected = (output / "current.json").read_bytes()
    verified = []
    real_verify = publication.verify_sources
    monkeypatch.setattr(publication, "verify_sources", lambda hashes: verified.append(1) or real_verify(hashes))
    def cancel():
        if len(verified) == 2:  # Both source checks have finished; only the commit remains.
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        publish(directory, sources, output, job_id="b" * 32, replace=True, checkpoint=cancel)
    assert (output / "current.json").read_bytes() == selected
    assert not (output / "revisions" / ("b" * 32)).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic replacement sharing regression")
def test_selection_commit_retries_while_a_reader_denies_delete_sharing(tmp_path, monkeypatch):
    from test_jobs import _reader_without_delete_sharing
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    publish(directory, sources, output)
    selected = (output / "current.json").read_bytes()
    real_replace, denials = os.replace, []
    with _reader_without_delete_sharing(output / "current.json") as release:
        def replace(source, destination):
            try:
                return real_replace(source, destination)
            except OSError as error:
                if Path(destination).name == "current.json" and not denials:
                    denials.append(error.winerror)
                    assert (output / "current.json").read_bytes() == selected
                    release()
                raise
        monkeypatch.setattr(publication.os, "replace", replace)
        second = publish(directory, sources, output, job_id="b" * 32, replace=True)
    assert denials and denials[0] in (5, 32)
    assert current_revision(output)[0]["job_id"] == "b" * 32
    assert Path(second["circuit"]).read_text() == "circuit bytes"
    assert not [path for path in output.iterdir() if path.name.endswith(".tmp")]


def test_changed_copy_cannot_replace_valid_revision(tmp_path, monkeypatch):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    publish(directory, sources, output)
    selected = (output / "current.json").read_bytes()
    copy = shutil.copyfile
    def broken_copy(source, target):
        result = copy(source, target)
        Path(target).write_bytes(b"corrupted")
        return result
    monkeypatch.setattr(shutil, "copyfile", broken_copy)
    with pytest.raises(PublicationError) as error:
        publish(directory, sources, output, job_id="b" * 32, replace=True)
    assert error.value.code == "ARTIFACT_CHANGED"
    assert (output / "current.json").read_bytes() == selected


def test_lost_current_binding_prevents_overwriting_another_selection(tmp_path):
    directory, sources = candidate(tmp_path)
    output = tmp_path / "output"
    publish(directory, sources, output)
    with output_lease(output, replace=True):
        with pytest.raises(PublicationError) as error:
            publish_revision(directory, output, "b" * 32, expected_current_sha256="0" * 64,
                             replace=True, original_hashes=sources, job_metadata={}, checkpoint=lambda: None)
    assert error.value.code == "OUTPUT_CHANGED"
    assert current_revision(output)[0]["job_id"] == "a" * 32


def test_output_lease_excludes_another_owner_and_releases_after_failure(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(RuntimeError):
        with output_lease(output, replace=False):
            with pytest.raises(PublicationError) as error:
                with output_lease(output, replace=False):
                    pytest.fail("Two owners acquired the output")
            assert error.value.code == "OUTPUT_BUSY"
            raise RuntimeError("owner failed")
    with output_lease(output, replace=False) as lease:
        assert lease["current_sha256"] is None


def test_unmanaged_directory_is_preserved(tmp_path):
    output = tmp_path / "user-files"
    output.mkdir()
    (output / "notes.txt").write_text("keep")
    with pytest.raises(PublicationError) as error:
        with output_lease(output, replace=True):
            pytest.fail("Unexpected ownership")
    assert error.value.code == "UNMANAGED_OUTPUT"
    assert list(output.iterdir()) == [output / "notes.txt"]


@pytest.mark.parametrize("path", ("../original.v", "C:/Windows/file", "circuit.dig/../circuit.dig", "/absolute"))
def test_manifest_cannot_escape_bundle(tmp_path, path):
    directory, _ = candidate(tmp_path)
    manifest = read_document(directory / "manifest.json")
    manifest["artifacts"]["circuit"]["path"] = path
    with pytest.raises(PublicationError) as error:
        bundle_bindings(directory, manifest)
    assert error.value.code == "ARTIFACT_PATH"


def test_manifest_artifact_cannot_override_result_state(tmp_path):
    directory, _ = candidate(tmp_path)
    manifest = read_document(directory / "manifest.json")
    manifest["artifacts"]["state"] = deepcopy(manifest["artifacts"]["circuit"])
    with pytest.raises(PublicationError) as error:
        bundle_bindings(directory, manifest)
    assert error.value.code == "ARTIFACT_BINDING"


def test_duplicate_document_fields_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"nested":{"valid":false,"valid":true}}', encoding="utf-8")
    with pytest.raises(PublicationError) as error:
        read_document(path)
    assert error.value.code == "INVALID_DOCUMENT"
