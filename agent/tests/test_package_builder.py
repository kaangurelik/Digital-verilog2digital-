"""Release publication preserves exact inputs and never packages mutable extras."""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
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


def test_distributable_archive_requires_license_texts_of_statically_linked_components(tmp_path, monkeypatch):
    inventory = json.loads((ROOT / "scripts/release-licenses.json").read_text(encoding="utf-8"))
    # Recorded gaps: OpenSSL and Rust crates in cryptography, libffi in cffi, Rust crates in pydantic_core and rpds-py.
    assert {"cffi 2.1.1: libffi", "cryptography 50.0.2: OpenSSL 4.0.3"} <= set(builder.missing_notices(inventory))
    root = tmp_path / "workspace"
    (root / "scripts").mkdir(parents=True)
    package = {"name": "example", "version": "1.0", "bundled_components": [
        {"name": "zlib", "linkage": "static", "notice": {"kind": "not_in_distribution", "note": "no text"}}]}
    (root / "scripts/release-licenses.json").write_text(json.dumps({"python_packages": [package]}), encoding="utf-8")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(root / ".venv"))
    with pytest.raises(RuntimeError, match="example 1.0: zlib"):
        builder.assemble(root / "dist/release", archive=True)
    assert sorted((root / "dist").iterdir()) == []
    package["bundled_components"][0]["notice"] = {"kind": "file", "target": "licenses/zlib/LICENSE"}
    assert builder.missing_notices({"python_packages": [package]}) == []


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


def _inventory(root, **sections):
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/release-licenses.json").write_text(json.dumps(sections), encoding="utf-8")


def test_vc_runtime_copies_only_the_inventory_hashed_redistributables(tmp_path, monkeypatch):
    redist = tmp_path / "redist"
    redist.mkdir()
    files = []
    for index in range(10):
        library = redist / f"runtime{index}.dll"
        library.write_bytes(b"vc runtime %d" % index)
        files.append({"path": "C:\\VC\\Redist\\" + library.name.upper(), "sha256": builder.sha256(library), "bytes": library.stat().st_size})
    _inventory(tmp_path, msvc={"files": files})
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "VC_DIRECTORY", redist)
    copied = tmp_path / "first"; copied.mkdir()
    builder.vc_runtime(copied)
    assert {path.name: path.read_bytes() for path in copied.iterdir()} == {path.name: path.read_bytes() for path in redist.iterdir()}
    (redist / "runtime3.dll").write_bytes(b"vc runtime X")
    with pytest.raises(ValueError, match="license inventory"):
        builder.vc_runtime(tmp_path / "first")
    (redist / "runtime3.dll").write_bytes(b"vc runtime 3")
    (redist / "runtime3.dll").rename(redist / "unaudited.dll")
    with pytest.raises(ValueError, match="ten-file"):
        builder.vc_runtime(tmp_path / "first")


def test_hdl_companion_dlls_must_be_the_inventory_mapped_binaries(tmp_path, monkeypatch):
    hdl = tmp_path / ".tools/oss-cad-suite"
    recorded = []
    for name in builder.HDL_FILES:
        path = hdl / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        if name.startswith("lib/"):
            recorded.append({"binary": {"path": ".tools/oss-cad-suite/" + name, "sha256": builder.sha256(path), "bytes": path.stat().st_size}})
    _inventory(tmp_path, oss_cad_runtime_dependencies=recorded)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    contents = builder.Contents()
    builder.hdl_runtime(contents, hdl)
    assert set(contents.files) == {".tools/oss-cad-suite/" + name for name in builder.HDL_FILES}
    (hdl / "lib/libffi-8.dll").write_bytes(b"another build of lib/libffi-8.dll")
    with pytest.raises(ValueError, match="recorded hash"):
        builder.hdl_runtime(builder.Contents(), hdl)
    _inventory(tmp_path, oss_cad_runtime_dependencies=[item for item in recorded if not item["binary"]["path"].endswith("libffi-8.dll")])
    with pytest.raises(ValueError, match="no license inventory mapping"):
        builder.hdl_runtime(builder.Contents(), hdl)


def test_source_archive_keeps_documentation_and_build_configuration(tmp_path, monkeypatch):
    for name in ("docs/desktop.md", "sun_checks_neemann.xml", "pom.xml", "README.md", "src/main/Example.java",
                 "gui/src/main.cpp", "gui/build/v2d-gui.exe", "agent/src/__pycache__/x.pyc"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    archive = tmp_path / "out/source.zip"
    archive.parent.mkdir()
    builder.source_archive(archive)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert {"docs/desktop.md", "sun_checks_neemann.xml", "pom.xml", "README.md", "src/main/Example.java", "gui/src/main.cpp"} <= names
    assert not {"gui/build/v2d-gui.exe", "agent/src/__pycache__/x.pyc"} & names


def test_archive_times_follow_content_so_stale_bytecode_cannot_match_an_edited_source(tmp_path):
    sources = {"old.py": b"LIMIT = 300\n", "edited.py": b"LIMIT = 600\n", "copy.py": b"LIMIT = 300\n"}
    contents = builder.Contents()
    for name, data in sources.items():
        (tmp_path / name).write_bytes(data)
        contents.add(tmp_path / name, "agent/" + name)
    package = tmp_path / "private-package"
    contents.copy(package)
    builder.write_document(package / "release-manifest.json", {"version": "test"})
    builder.write_archive(tmp_path / "release.zip", package, contents, "release")
    with zipfile.ZipFile(tmp_path / "release.zip") as archive:
        times = {info.filename: info.date_time for info in archive.infolist()}
    # Same size, different bytes: an earlier release's timestamp-validated pyc must not apply.
    assert times["release/agent/old.py"] == times["release/agent/copy.py"] != times["release/agent/edited.py"]
    assert all(time[:3] == (2026, 1, 1) for time in times.values())


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def test_production_packages_are_the_installed_lock_pinned_runtime_closure():
    closure = builder.runtime_closure()
    assert list(closure) == sorted(builder.PRODUCTION_PACKAGES) and len(closure) == 30
    # pyjwt[crypto] and the win32 marker are what pull these in.
    assert {"cryptography", "cffi", "pycparser", "pywin32"} <= set(closure) and closure["mcp"] == "2.3.0"
    pins = builder.locked_versions()
    assert all(pins[name] == version for name, version in closure.items())


def test_dependency_lock_pins_exactly_the_installed_environment():
    installed = {canonical(item.metadata["Name"]): item.version for item in importlib.metadata.distributions()}
    for name in ("pip", "verilog2digital"):
        installed.pop(name, None)
    assert builder.locked_versions() == installed


def _project(root, dependencies, lock):
    (root / "agent").mkdir(parents=True, exist_ok=True)
    (root / "agent/pyproject.toml").write_text("[project]\nname = \"probe\"\ndependencies = " + json.dumps(dependencies) + "\n", encoding="utf-8")
    (root / "agent/requirements-dev.lock").write_text(lock, encoding="utf-8")


def test_runtime_closure_rejects_missing_unexpected_and_unpinned_distributions(tmp_path, monkeypatch):
    lock = (ROOT / "agent/requirements-dev.lock").read_text(encoding="utf-8")
    declared = ["jsonschema==4.26.0", "mcp==2.3.0", "pyslang==11.0.0"]
    _project(tmp_path, declared, lock)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    assert set(builder.runtime_closure()) == set(builder.PRODUCTION_PACKAGES)
    production = builder.PRODUCTION_PACKAGES
    monkeypatch.setattr(builder, "PRODUCTION_PACKAGES", tuple(name for name in production if name != "pywin32"))
    with pytest.raises(ValueError, match="missing: pywin32;"):
        builder.runtime_closure()
    monkeypatch.setattr(builder, "PRODUCTION_PACKAGES", (*production, "pytest"))
    with pytest.raises(ValueError, match="unexpected: pytest"):
        builder.runtime_closure()
    monkeypatch.setattr(builder, "PRODUCTION_PACKAGES", production)
    for changed, message in ((lock.replace("pywin32==312\n", ""), "pywin32 312 differs"), (lock.replace("mcp==2.3.0", "mcp==2.2.0"), "mcp 2.3.0 differs"),
                             (lock + "mcp==2.3.0\n", "unique exact pins"), (lock.replace("anyio==4.15.1", "anyio>=4"), "unique exact pins")):
        _project(tmp_path, declared, changed)
        with pytest.raises(ValueError, match=message):
            builder.runtime_closure()
    _project(tmp_path, ["jsonschema==4.26.0", "mcp==2.2.0", "pyslang==11.0.0"], lock)
    with pytest.raises(ValueError, match="does not satisfy mcp==2.2.0"):
        builder.runtime_closure()


def test_only_the_reviewed_pth_startup_hook_is_packaged(tmp_path, monkeypatch):
    site = tmp_path / ".venv/Lib/site-packages"
    for name, data in (("hooked/__init__.py", b""), ("pywin32.pth", b"win32\nimport pywin32_bootstrap\n"), ("injected.pth", b"import os\n")):
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_bytes(data)
    records = ["hooked/__init__.py", "pywin32.pth"]
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "PRODUCTION_PACKAGES", ("hooked",))
    monkeypatch.setattr(builder.importlib.metadata, "distribution", lambda name:
        SimpleNamespace(locate_file=lambda record: site / record, files=list(records)))
    contents = builder.Contents()
    builder.dependencies(contents)
    assert ".venv/Lib/site-packages/pywin32.pth" in contents.files
    records.append("injected.pth")
    with pytest.raises(ValueError, match="startup hooks differ from the reviewed set: injected.pth"):
        builder.dependencies(builder.Contents())
    records[:] = ["hooked/__init__.py"]
    with pytest.raises(ValueError, match="reviewed set: pywin32.pth"):
        builder.dependencies(builder.Contents())


def _inventory_document():
    return json.loads((ROOT / "scripts/release-licenses.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", builder.PRODUCTION_PACKAGES)
def test_every_installed_notice_of_a_production_distribution_is_inventoried(name):
    inventory = _inventory_document()
    distribution = importlib.metadata.distribution(name)
    metadata = next(record for record in distribution.files if record.name == "METADATA" and record.parent.name.endswith(".dist-info"))
    info = Path(distribution.locate_file(metadata)).parent
    entry = next(item for item in inventory["python_packages"] if canonical(item["name"]) == canonical(name))
    assert entry["version"] == distribution.version
    records = {record["source"].get("path"): record for record in inventory["files"]}
    notices = []
    for path in (path for folder in ("licenses", "sboms") for path in (info / folder).rglob("*") if path.is_file()):
        record = records[path.relative_to(ROOT).as_posix()]
        relative = path.relative_to(info).as_posix()
        assert record["target"] == f"licenses/python/{entry['name']}/{info.name}/{relative}"
        assert record["sha256"] == builder.sha256(path) and record["bytes"] == path.stat().st_size
        assert record["kind"] == ("notice" if relative.startswith("licenses/") else "provenance")
        staged = ROOT / record["staged_path"]
        if staged.exists():
            assert staged.read_bytes() == path.read_bytes()
        if record["kind"] == "notice":
            notices.append(record["target"])
    assert notices and sorted(entry["notice_files"]) == sorted(notices) and len(set(notices)) == len(notices)
    targets = {record["target"] for record in inventory["files"]}
    for component in entry.get("bundled_components", ()):
        assert component["notice"]["kind"] == "not_in_distribution" and set(component.get("evidence", ())) <= targets


@pytest.mark.parametrize("failures,code,published", [(2, 32, True), (3, 5, True), (1, 2, False), (99, 33, False)])
def test_publication_retries_only_bounded_windows_sharing_errors(tmp_path, monkeypatch, failures, code, published):
    source = tmp_path / "private"
    source.mkdir()
    (source / "native.dll").write_bytes(b"verified")
    destination, original, calls, clock = tmp_path / "release", Path.rename, [], [0.0]
    def rename(path, target):
        calls.append(target)
        if len(calls) <= failures:
            error = PermissionError("held by a scanner")
            error.winerror = code
            raise error
        return original(path, target)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(builder, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(builder, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    if published:
        builder.publish(source, destination)
        assert (destination / "native.dll").read_bytes() == b"verified" and len(calls) == failures + 1
    else:
        with pytest.raises(PermissionError):
            builder.publish(source, destination, seconds=5)
        assert source.is_dir() and not destination.exists()
        assert len(calls) == 1 if code == 2 else clock[0] >= 5


def test_third_party_notices_name_every_inventoried_python_distribution():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for entry in _inventory_document()["python_packages"]:
        assert entry["name"] in text and entry["version"] in text, entry["name"]
