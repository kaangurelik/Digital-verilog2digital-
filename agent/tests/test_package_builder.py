"""Release publication preserves exact inputs and never packages mutable extras."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("v2d_package_builder", ROOT / "scripts/package.py")
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


@pytest.mark.parametrize("name", ["../escape", "C:/file", "a//b", "a/./b", "a\\b", "a\x00b", "a\nb", "a/NUL.txt", "a/COM1", "a/file.", "a/file ", "a:stream", "a/*.dll"])
def test_windows_destination_aliases_are_rejected(name):
    with pytest.raises(ValueError):
        builder.relative_name(name)


@pytest.mark.parametrize("mutation", [b"", b"changes", b"longer than the original"])
def test_input_mutation_cannot_produce_a_selected_snapshot(tmp_path, mutation):
    source = tmp_path / "input.dll"
    source.write_bytes(b"initial")
    contents = builder.Contents()
    contents.add(source, "gui/native.dll")
    source.write_bytes(mutation)
    with pytest.raises(RuntimeError):
        contents.copy(tmp_path / "private-output")
    with pytest.raises(RuntimeError):
        contents.verify_inputs()


def test_copy_is_independent_and_destination_collisions_preserve_existing_bytes(tmp_path):
    source = tmp_path / "input.dll"
    source.write_bytes(b"verified")
    contents = builder.Contents()
    contents.add(source, "gui/native.dll")
    with pytest.raises(ValueError, match="Duplicate"):
        contents.add(source, "GUI/NATIVE.DLL")
    package = tmp_path / "private-package"
    contents.copy(package)
    source.write_bytes(b"modified")
    assert (package / "gui/native.dll").read_bytes() == b"verified"
    with pytest.raises(FileExistsError):
        contents.copy(package)
    assert (package / "gui/native.dll").read_bytes() == b"verified"


def test_archive_records_exact_snapshot_without_mutable_extras_and_is_reproducible(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"release bytes")
    contents = builder.Contents()
    contents.add(source, "gui/native.dll")
    package = tmp_path / "private-package"
    contents.copy(package)
    builder.write_document(package / "release-manifest.json", {"version": "test"})
    (package / "unexpected-job.txt").write_text("not part of release", encoding="utf-8")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    assert builder.write_archive(first, package, contents, "release") == builder.write_archive(second, package, contents, "release")
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"release/gui/native.dll", "release/release-manifest.json"}
        assert archive.read("release/gui/native.dll") == b"release bytes"
        assert archive.testzip() is None
    with pytest.raises(FileExistsError):
        builder.write_archive(first, package, contents, "release")


def test_archive_detects_same_size_copy_corruption(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"release bytes")
    contents = builder.Contents()
    contents.add(source, "gui/native.dll")
    package = tmp_path / "private-package"
    contents.copy(package)
    builder.write_document(package / "release-manifest.json", {"version": "test"})
    (package / "gui/native.dll").write_bytes(b"!" * len(b"release bytes"))
    with pytest.raises(RuntimeError, match="changed during archiving"):
        builder.write_archive(tmp_path / "private.zip", package, contents, "release")


def test_existing_output_refused_before_build_inputs_are_read(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    output = root / "dist/release"
    output.mkdir(parents=True)
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"existing user output")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(root / ".venv"))
    with pytest.raises(FileExistsError):
        builder.assemble(output)
    assert sentinel.read_bytes() == b"existing user output"
    assert sorted(output.parent.iterdir()) == [output]


def test_existing_archive_refused_before_directory_publication(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    parent = root / "dist"
    parent.mkdir(parents=True)
    archive = parent / "release.zip"
    archive.write_bytes(b"existing user archive")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(root / ".venv"))
    with pytest.raises(FileExistsError):
        builder.assemble(parent / "release", archive=True)
    assert archive.read_bytes() == b"existing user archive"
    assert sorted(parent.iterdir()) == [archive]


def test_global_dependency_metadata_is_rejected_before_copy(tmp_path, monkeypatch):
    site = tmp_path / ".venv/Lib/site-packages"
    site.mkdir(parents=True)
    external = tmp_path / "global-site"
    external.mkdir()
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.importlib.metadata, "distribution", lambda name:
        SimpleNamespace(locate_file=lambda record: external / record, files=["external.py"]))
    contents = builder.Contents()
    with pytest.raises(ValueError, match="outside the repository .venv"):
        builder.dependencies(contents)
    assert not contents.files


def test_dependency_without_file_inventory_is_not_silently_omitted(tmp_path, monkeypatch):
    site = tmp_path / ".venv/Lib/site-packages"
    site.mkdir(parents=True)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.importlib.metadata, "distribution", lambda name:
        SimpleNamespace(locate_file=lambda record: site / record, files=None))
    with pytest.raises(ValueError, match="no installed file inventory"):
        builder.dependencies(builder.Contents())
