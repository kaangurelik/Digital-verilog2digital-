"""Native overlays remain bound through replacement, relocation and caching."""
from copy import deepcopy
from pathlib import Path
import shutil
import struct
import zlib

import pytest

from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.jobs.cache import CandidateCache
from verilog2digital.jobs.publication import atomic_document, bundle_bindings, digest, read_document
from verilog2digital.preview import render_preview, validate_metadata, write_metadata
from test_conversion import conversion_adapter
from test_job_cache import fixture as cache_fixture


ROOT = Path(__file__).resolve().parents[2]


def png(width=40, height=30):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * (width * 3)) * height)) + chunk(b"IEND", b""))


@pytest.fixture
def bundle(tmp_path):
    directory, project, snapshot, key = cache_fixture(tmp_path)
    manifest = read_document(directory / "manifest.json")
    (directory / "preview.png").write_bytes(png())
    atomic_document(directory / "circuit.dig.ids.json", {"schema_version": 1, "sha256": digest(directory / "circuit.dig"),
                    "instances": [{"id": "source", "index": 0, "signature": "0" * 64}]})
    for label in ("preview", "sidecar"):
        manifest["artifacts"][label]["sha256"] = digest(directory / manifest["artifacts"][label]["path"])
    bounds = {"min": {"x": 0, "y": 0}, "max": {"x": 20, "y": 20}}
    view = {"module_id": None, "comparison_scope": "selected_top_canvas",
            "image": {"artifact": "preview", "sha256": manifest["artifacts"]["preview"]["sha256"], "width_px": 40, "height_px": 30},
            "circuit": {"sha256": manifest["artifacts"]["circuit"]["sha256"], "sidecar_sha256": manifest["artifacts"]["sidecar"]["sha256"]},
            "circuit_to_pixel": {"a": 1, "b": 0, "c": 0, "d": 1, "tx": 4, "ty": 4},
            "instances": [{"id": "source", "type": "In", "label": "Işık Ω <data>", "position": {"x": 0, "y": 0},
                "rotation": 0, "bounds": bounds, "body_bounds": deepcopy(bounds),
                "pins": [{"direction": "output", "pin": "out", "position": {"x": 20, "y": 10}, "width": 1}]}]}
    write_metadata(directory / "preview-metadata.json", "sample", view)
    manifest["artifacts"]["preview_metadata"] = {"path": "preview-metadata.json", "sha256": digest(directory / "preview-metadata.json")}
    atomic_document(directory / "manifest.json", manifest)
    return directory, project, snapshot, key


def test_metadata_survives_cache_restore_and_unicode_relocation(bundle, tmp_path):
    directory, _, snapshot, key = bundle
    before = (directory / "preview-metadata.json").read_bytes()
    cache = CandidateCache(tmp_path / "cache")
    assert cache.store(key, directory, source_snapshot=snapshot)
    restored = tmp_path / "çıktı_日本語"
    result = cache.restore(key, restored)
    assert result and Path(result["preview_metadata"]).read_bytes() == before
    assert bundle_bindings(restored, read_document(restored / "manifest.json"))
    relocated = tmp_path / "taşınmış_Ω"
    shutil.copytree(restored, relocated)
    assert bundle_bindings(relocated, read_document(relocated / "manifest.json"))


@pytest.mark.parametrize("mutation", ["image", "circuit", "sidecar", "dimensions", "identity", "module", "transform", "duplicate", "partial-baseline"])
def test_association_corruption_cannot_be_hidden_by_rehashing_metadata(bundle, mutation):
    directory, _, _, _ = bundle
    path = directory / "preview-metadata.json"
    metadata = read_document(path)
    selected = metadata["views"]["selected"]
    if mutation == "image": selected["image"]["sha256"] = "a" * 64
    elif mutation == "circuit": selected["circuit"]["sha256"] = "a" * 64
    elif mutation == "sidecar": selected["circuit"]["sidecar_sha256"] = "a" * 64
    elif mutation == "dimensions": selected["image"]["width_px"] += 1
    elif mutation == "identity": selected["instances"][0]["id"] = "different"
    elif mutation == "module": metadata["top"] = "different"
    elif mutation == "transform": selected["circuit_to_pixel"]["a"] = -1
    elif mutation == "duplicate": selected["instances"].append(deepcopy(selected["instances"][0]))
    else:
        baseline = deepcopy(selected)
        baseline["comparison_scope"], baseline["image"]["artifact"] = "before_refinement", "baseline_preview"
        metadata["views"]["baseline"] = baseline
    atomic_document(path, metadata)
    manifest = read_document(directory / "manifest.json")
    manifest["artifacts"]["preview_metadata"]["sha256"] = digest(path)
    with pytest.raises(FrontendError):
        bundle_bindings(directory, manifest)


def test_legacy_bundle_remains_image_only(bundle):
    directory, _, _, _ = bundle
    manifest = read_document(directory / "manifest.json")
    manifest["artifacts"].pop("preview_metadata")
    assert bundle_bindings(directory, manifest)


@pytest.mark.parametrize("hierarchical", [False, True])
def test_native_conversion_captures_selected_and_baseline_without_expanded_coordinates(tmp_path, conversion_adapter, hierarchical):
    family, name = ("hierarchy", "repeated_logic") if hierarchical else ("scalar", "full_adder")
    project = Project([ROOT / "tests/fixtures/verilog" / family / (name + ".v")], top=name)
    result = convert(project, tmp_path / name, adapter=conversion_adapter,
                     refinement={"mode": "replay", "responses": ["{malformed"], "max_attempts": 1})
    metadata = read_document(Path(result["preview_metadata"]))
    validate_metadata(metadata)
    assert set(metadata["views"]) == {"selected", "baseline"}
    selected, baseline = metadata["views"]["selected"], metadata["views"]["baseline"]
    assert (selected["module_id"] is not None) is hierarchical
    assert baseline["comparison_scope"] == ("top_after_child_refinement_before_top_refinement" if hierarchical else "before_refinement")
    assert [item["id"] for item in selected["instances"]] == [item["id"] for item in baseline["instances"]]
    assert all("." not in item["id"] for item in selected["instances"])
    assert bundle_bindings(Path(result["directory"]), read_document(Path(result["manifest"])))


def test_native_baseline_metadata_remains_correct_after_original_files_are_replaced(tmp_path, conversion_adapter):
    from adapter_fixtures import direct_wire
    adapter = conversion_adapter
    case = direct_wire(adapter)
    path = tmp_path / "circuit.dig"
    def emit():
        emitted = adapter.call("circuit.emit", {"circuit": case.circuit, "path": str(path)})
        paths = {key: emitted[key] for key in ("path", "sidecar_path")}
        return emitted, paths, adapter.call("circuit.inspect", paths)
    emitted, paths, inspection = emit()
    baseline = render_preview(adapter, paths, tmp_path / "baseline.png", inspection=inspection, emitted=emitted,
                              scale=.37, comparison_scope="before_refinement", artifact="baseline_preview")
    for instance in case.circuit["instances"]:
        instance["position"]["x"] -= 100
    for wire in case.circuit["wires"]:
        wire["from"]["x"] -= 100
        wire["to"]["x"] -= 100
    emitted, paths, inspection = emit()
    selected = render_preview(adapter, paths, tmp_path / "selected.png", inspection=inspection, emitted=emitted, scale=.37)
    write_metadata(tmp_path / "metadata.json", "direct", selected, baseline)
    assert baseline["circuit"]["sha256"] != selected["circuit"]["sha256"]
    assert baseline["circuit_to_pixel"] != selected["circuit_to_pixel"]
    assert baseline["image"]["sha256"] == digest(tmp_path / "baseline.png")
