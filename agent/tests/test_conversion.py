"""Exercise original HDL through mapping, native serialization, and both simulators."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import pytest

from verilog2digital import verification
from verilog2digital.adapter import Adapter
from verilog2digital.conversion import convert, simulate_combinational
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from verilog2digital.jobs.publication import bundle_bindings
from verilog2digital.validation.hierarchy import verify_package_snapshot
from test_reference import CASES, FIXTURES


@pytest.fixture(scope="session")
def conversion_adapter():
    with Adapter() as adapter:
        yield adapter


def project(name):
    return Project(sources=[FIXTURES / f"{name}.v"], top=name)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hashes(directory):
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def assert_no_published_result(directory):
    assert not (directory / "manifest.json").exists()
    assert not (directory / "circuit.dig").exists()
    assert not (directory / "validation.json").exists()
    failure = read_json(directory / "failure.json")
    assert failure["state"] == "failed"
    return failure


@pytest.mark.parametrize("name", CASES)
def test_scalar_verilog_is_published_only_after_exhaustive_native_equivalence(tmp_path, conversion_adapter, name):
    destination = tmp_path / name
    stages = []
    result = convert(project(name), destination, adapter=conversion_adapter,
                     progress=lambda event: stages.append(event["stage"]))
    assert result["state"] == "ready"
    assert Path(result["directory"]) == destination
    assert stages[-1] == "ready" and "validating_connections" in stages and "validating_behavior" in stages
    report = read_json(result["validation"])
    assert report["valid"] is True
    for check in ("topology", "components", "geometry", "model", "behavior"):
        assert report[check]["valid"] is True, report[check]
        assert not report[check]["diagnostics"]

    names, expected = CASES[name]
    stimuli = read_json(destination / "work/stimuli.json")
    assert len(stimuli) == 2 ** len(names)
    assert {tuple(vector[key] for key in names) for vector in stimuli} == set(product((0, 1), repeat=len(names)))
    wanted = [{"values": expected(vector)} for vector in stimuli]
    reference = read_json(destination / "work/reference-samples.json")
    assert reference["samples"] == wanted
    native = read_json(destination / "work/digital-samples.json")
    mapped = read_json(destination / "work/mapped.json")
    outputs = {port["port"]: port["instance_id"] for port in mapped["outputs"]}
    observed = [{"values": {name: int(sample["values"][identifier], 16) for name, identifier in outputs.items()}}
                for sample in native["samples"]]
    assert observed == wanted
    assert all(int(sample["high_z"][identifier], 16) == 0 for sample in native["samples"] for identifier in outputs.values())
    assert report["behavior"]["coverage"] == "exhaustive_binary"
    assert report["behavior"]["input_vectors"] == len(stimuli)

    # Check the final published file after native artifacts have moved.
    inspection = conversion_adapter.call("circuit.inspect", {"path": result["circuit"], "sidecar_path": result["sidecar"]})
    assert inspection["model"]["valid"] is True
    assert {instance["id"] for instance in inspection["instances"]} == {instance["id"] for instance in mapped["instances"]}
    assert Path(result["preview"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert ET.parse(result["svg"]).getroot().tag.endswith("svg")
    manifest = read_json(result["manifest"])
    assert manifest["state"] == "ready" and manifest["profile"] == "scalar-v1"
    assert manifest["top"] == name
    assert all(key in manifest["tools"] for key in ("yosys", "pyslang", "iverilog", "vvp", "digital_revision"))
    for label, artifact in manifest["artifacts"].items():
        path = destination / artifact["path"]
        assert path == Path(result[label]) and path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def test_full_adder_native_artifacts_are_byte_deterministic(tmp_path, conversion_adapter):
    first = convert(project("full_adder"), tmp_path / "first", adapter=conversion_adapter)
    second = convert(project("full_adder"), tmp_path / "second", adapter=conversion_adapter)
    for artifact in ("circuit", "sidecar", "preview", "svg"):
        assert Path(first[artifact]).read_bytes() == Path(second[artifact]).read_bytes(), artifact


class ForwardingAdapter:
    def __init__(self, native):
        self.native = native
        self.mutated = False

    @property
    def capabilities(self):
        return self.native.capabilities

    def call(self, method, params, **options):
        return self.native.call(method, params, **options)


class DisconnectedLeadAdapter(ForwardingAdapter):
    """Corrupt the actual emitted candidate after the layout check has passed."""

    def call(self, method, params, **options):
        if method == "circuit.emit":
            params = deepcopy(params)
            lead = params["circuit"]["wires"][0]
            old, bend = lead["from"], lead["to"]
            assert old != bend
            # Reflect the pin endpoint through its bend: the lead stays on-grid
            # and axis-aligned, but no longer reaches its intended native pin.
            lead["from"] = {axis: 2 * bend[axis] - old[axis] for axis in ("x", "y")}
            self.mutated = True
        return super().call(method, params, **options)


def test_accidental_wire_endpoint_mutation_cannot_publish_a_result(tmp_path, conversion_adapter):
    destination = tmp_path / "disconnected"
    corrupted = DisconnectedLeadAdapter(conversion_adapter)
    with pytest.raises(FrontendError) as failure:
        convert(project("buffer"), destination, adapter=corrupted)
    assert corrupted.mutated
    assert failure.value.code == "CIRCUIT_VALIDATION"
    failure_report = assert_no_published_result(destination)
    assert failure_report["stage"] == "validating_connections"
    validation = read_json(destination / "work/validation.json")
    assert validation["valid"] is False and validation["topology"]["valid"] is False
    assert validation["topology"]["diagnostics"]


def test_native_rejection_retries_a_visible_reserved_layout(tmp_path, conversion_adapter):
    class CorruptFirstCandidate(DisconnectedLeadAdapter):
        def call(self, method, params, **options):
            if self.mutated:
                return self.native.call(method, params, **options)
            return super().call(method, params, **options)

    destination = tmp_path / "recovered_layout"
    result = convert(project("full_adder"), destination,
                     adapter=CorruptFirstCandidate(conversion_adapter))
    attempts = read_json(destination / "work/layout-attempts.json")
    assert len(attempts) == 2
    assert attempts[0]["strategy"] == "elk-orthogonal-v1"
    assert attempts[0]["validation"]["valid"] is False
    assert attempts[1]["validation"]["valid"] is True
    manifest = read_json(result["manifest"])
    assert manifest["layout"]["fallback_used"] is True
    assert manifest["layout"]["fallback_reason"] == "native_layout_validation"
    assert manifest["layout"]["strategy"] == "scalar-reserved-tracks-v1"
    assert manifest["behavior"]["valid"] is True


class IncorrectSimulationAdapter(ForwardingAdapter):
    def call(self, method, params, **options):
        response = super().call(method, params, **options)
        if method == "circuit.simulate" and not self.mutated:
            response = deepcopy(response)
            values = response["samples"][0]["values"]
            identifier = next(iter(values))
            values[identifier] = hex(int(values[identifier], 16) ^ 1)
            self.mutated = True
        return response


def test_incorrect_native_output_is_rejected_despite_valid_connections(tmp_path, conversion_adapter):
    destination = tmp_path / "incorrect_output"
    corrupted = IncorrectSimulationAdapter(conversion_adapter)
    with pytest.raises(FrontendError) as failure:
        convert(project("buffer"), destination, adapter=corrupted)
    assert corrupted.mutated and failure.value.code == "BEHAVIOR_VALIDATION"
    report = assert_no_published_result(destination)
    assert report["stage"] == "validating_behavior"
    validation = read_json(destination / "work/validation.json")
    assert all(validation[check]["valid"] for check in ("topology", "components", "geometry", "model"))
    assert validation["valid"] is False and validation["behavior"]["valid"] is False
    assert any(diagnostic["code"] == "behavior_mismatch" for diagnostic in validation["behavior"]["diagnostics"])


def test_existing_valid_result_is_preserved_byte_for_byte(tmp_path, conversion_adapter):
    destination = tmp_path / "existing"
    convert(project("and_gate"), destination, adapter=conversion_adapter)
    before = file_hashes(destination)
    with pytest.raises(FrontendError) as failure:
        convert(project("inverter"), destination, adapter=conversion_adapter)
    assert failure.value.code == "OUTPUT_EXISTS"
    assert file_hashes(destination) == before
    assert read_json(destination / "manifest.json")["state"] == "ready"


def test_existing_empty_directory_is_also_preserved(tmp_path, conversion_adapter):
    destination = tmp_path / "existing_empty"
    destination.mkdir()
    with pytest.raises(FrontendError) as failure:
        convert(project("buffer"), destination, adapter=conversion_adapter)
    assert failure.value.code == "OUTPUT_EXISTS"
    assert list(destination.iterdir()) == []


def test_unsupported_bus_leaves_diagnostics_without_a_published_circuit(tmp_path, conversion_adapter):
    source = tmp_path / "bus.v"
    source.write_text("module bus(input [1:0]a,output [1:0]y); assign y=a; endmodule", encoding="utf-8")
    destination = tmp_path / "unsupported_bus"
    with pytest.raises(FrontendError) as failure:
        convert(Project(sources=[source], top="bus"), destination, adapter=conversion_adapter, profile="scalar-v1")
    assert failure.value.code == "UNSUPPORTED_MAPPING_WIDTH"
    report = assert_no_published_result(destination)
    assert report["stage"] == "mapping"
    assert report["diagnostic"]["code"] == "UNSUPPORTED_MAPPING_WIDTH"


def test_ready_progress_observer_failure_does_not_invalidate_conversion(tmp_path, conversion_adapter):
    destination = tmp_path / "observer_failure"
    observed = []

    def progress(event):
        observed.append(event["stage"])
        if event["stage"] == "ready":
            raise RuntimeError("observer failed after validation")

    result = convert(project("buffer"), destination, adapter=conversion_adapter, progress=progress)
    assert "ready" in observed
    assert result["state"] == "ready"
    assert read_json(result["manifest"])["state"] == "ready"
    assert read_json(result["validation"])["valid"] is True
    assert not (destination / "failure.json").exists()
    assert result["progress_warnings"]
    assert "observer failed after validation" in json.dumps(result["progress_warnings"])


class ChangedAfterRenderingAdapter(ForwardingAdapter):
    def __init__(self, native, artifact):
        super().__init__(native)
        self.artifact = artifact
        self.changed_path = None

    def call(self, method, params, **options):
        result = super().call(method, params, **options)
        if method == "circuit.render" and params["format"] == "png" and not self.mutated:
            path = Path(params["path"])
            if self.artifact == "sidecar":
                path = path.with_name(path.name + ".ids.json")
            # Whitespace preserves semantics but must invalidate the recorded file hashes.
            with path.open("ab") as stream:
                stream.write(b"\n ")
            self.changed_path = path
            self.mutated = True
        return result


@pytest.mark.parametrize("artifact", ["circuit", "sidecar"])
def test_native_artifact_changed_after_rendering_cannot_be_published(tmp_path, conversion_adapter, artifact):
    destination = tmp_path / ("changed_" + artifact)
    corrupted = ChangedAfterRenderingAdapter(conversion_adapter, artifact)
    with pytest.raises(FrontendError) as failure:
        convert(project("buffer"), destination, adapter=corrupted)
    assert corrupted.mutated and corrupted.changed_path.is_file()
    assert failure.value.code == "ARTIFACT_CHANGED"
    report = assert_no_published_result(destination)
    assert report["diagnostic"]["code"] == "ARTIFACT_CHANGED"
    if artifact == "circuit":
        assert ET.parse(corrupted.changed_path).getroot().tag == "circuit"
    else:
        assert isinstance(read_json(corrupted.changed_path), dict)


def test_source_changed_during_rendering_cannot_be_published(tmp_path, conversion_adapter):
    source = tmp_path / "changed_source.v"
    source.write_text("module changed_source(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    destination = tmp_path / "source_changed"

    class ChangeSource(ForwardingAdapter):
        def call(self, method, params, **options):
            result = super().call(method, params, **options)
            if method == "circuit.render" and not self.mutated:
                source.write_text("module changed_source(input a,output y); assign y=~a; endmodule\n", encoding="utf-8")
                self.mutated = True
            return result

    with pytest.raises(FrontendError) as failure:
        convert(Project([source], top="changed_source"), destination, adapter=ChangeSource(conversion_adapter))
    assert failure.value.code == "SOURCE_CHANGED"
    report = assert_no_published_result(destination)
    assert report["stage"] == "publishing"


windows_staging = pytest.mark.skipif(os.name != "nt", reason="Only the pinned Windows HDL tools need ASCII staging")


@pytest.fixture
def staging_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setenv("V2D_JOB_ROOT", str(root))
    return root


def assert_scratch_removed(root):
    assert not (root / "_direct").exists() or not list((root / "_direct").iterdir())


def assert_original_bundle(result, destination, sources):
    manifest = read_json(result["manifest"])
    assert manifest["state"] == "ready" and Path(result["directory"]) == destination
    assert manifest["sources"] == {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    assert bundle_bindings(destination, manifest)
    for label, artifact in manifest["artifacts"].items():
        assert Path(result[label]) == destination / artifact["path"]
        assert hashlib.sha256((destination / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    for name in ("manifest.json", "validation.json", *(["package.json"] if "package" in manifest["artifacts"] else [])):
        assert "_direct" not in (destination / name).read_text(encoding="utf-8")
    snapshot = read_json(destination / "work/source-snapshot.json")
    assert set(snapshot["sources"]) == set(manifest["sources"])
    return manifest


@windows_staging
def test_unicode_and_space_paths_are_staged_with_original_provenance(tmp_path, conversion_adapter, staging_root):
    inputs = tmp_path / "kaynak alanı Ω"
    includes = tmp_path / "başlık 日本語"
    header = includes / "işlem tanımı.vh"
    source = inputs / "devre ğ.v"
    includes.mkdir()
    inputs.mkdir()
    header.write_text("`define OP(a,b) ((a)^(b))\n", encoding="utf-8")
    source.write_text('`include "işlem tanımı.vh"\nmodule top(input [1:0] a,b,output [1:0] y); assign y=`OP(a,b); endmodule\n',
                      encoding="utf-8")
    destination = tmp_path / "çıktı alanı Ω"
    stages = []

    def observe(event):
        stages.append(event["stage"])
        if event["stage"] == "ready":
            raise RuntimeError("observer failed after publication")

    result = convert(Project([source], top="top", include_dirs=[includes]), destination,
                     adapter=conversion_adapter, progress=observe)
    assert stages[0] == "snapshotting_sources" and stages[-1] == "ready" and stages.count("ready") == 1
    assert "validating_behavior" in stages and not (destination / "failure.json").exists()
    assert [item["message"] for item in result["progress_warnings"]] == ["observer failed after publication"]
    manifest = assert_original_bundle(result, destination, [source, header])
    assert manifest["progress_warnings"] == []
    assert manifest["configuration"]["sources"] == [str(source.resolve())]
    assert manifest["configuration"]["include_dirs"] == [str(includes.resolve())]
    assert manifest["behavior"]["valid"] and manifest["behavior"]["coverage"] == "exhaustive_binary"
    assert "snapshotting_sources" in manifest["timings_seconds"]
    checked = verification.validate_output(destination, adapter=conversion_adapter)
    assert checked["valid"], checked["diagnostics"]
    assert set(checked["checks"]) == {"artifacts", "sources", "runtime", "native_model"}
    assert checked["checks"]["runtime"]["scope"] == "legacy_recorded_tools"
    assert_scratch_removed(staging_root)


@windows_staging
def test_unicode_hierarchy_is_staged_as_a_relocatable_native_package(tmp_path, conversion_adapter, staging_root):
    source = tmp_path / "hiyerarşi Ω" / "durumlu çocuklar.v"
    source.parent.mkdir()
    shutil.copyfile(FIXTURES.parent / "hierarchy/stateful_children.v", source)
    destination = tmp_path / "paket çıktısı ışık"
    result = convert(Project([source], top="stateful_children"), destination, adapter=conversion_adapter)
    manifest = assert_original_bundle(result, destination, [source])
    assert manifest["profile"] == "hierarchy-v1"
    package = read_json(result["package"])
    children = [item["path"] for item in package["snapshot"]["files"] if not item["path"].startswith("circuit.dig")]
    assert children and all(name.startswith("m_") and (destination / name).is_file() for name in children)
    assert verify_package_snapshot(destination, package["snapshot"])["valid"]
    checked = verification.validate_output(destination, adapter=conversion_adapter)
    assert checked["valid"], checked["diagnostics"]
    assert_scratch_removed(staging_root)


@windows_staging
def test_unicode_staged_failure_writes_original_location_at_destination(tmp_path, conversion_adapter, staging_root):
    includes = tmp_path / "başlık Ω"
    includes.mkdir()
    header = includes / "hatalı ğ.vh"
    header.write_text("\n\nwire t = missing_name;\n", encoding="utf-8")
    source = tmp_path / "devre ışık.v"
    source.write_text('module top(input a,output y);\n`include "hatalı ğ.vh"\nassign y = a;\nendmodule\n', encoding="utf-8")
    destination = tmp_path / "çıktı hata Ω"
    with pytest.raises(FrontendError) as failure:
        convert(Project([source], top="top", include_dirs=[includes]), destination, adapter=conversion_adapter)
    assert failure.value.code == "SLANG_ERROR"
    assert (failure.value.path, failure.value.line, failure.value.column) == (str(header.resolve()), 3, 10)
    report = assert_no_published_result(destination)
    assert report["stage"] == "lowering"
    assert {key: report["diagnostic"][key] for key in ("code", "path", "line")} == {
        "code": "SLANG_ERROR", "path": str(header.resolve()), "line": 3}
    assert set(read_json(destination / "work/source-snapshot.json")["sources"]) == {str(source.resolve()), str(header.resolve())}
    assert_scratch_removed(staging_root)


@windows_staging
def test_unicode_snapshot_failure_and_existing_output_keep_their_semantics(tmp_path, conversion_adapter, staging_root):
    source = tmp_path / "eksik ğ.v"
    source.write_text('`include "yok.vh"\nmodule top; endmodule\n', encoding="utf-8")
    destination = tmp_path / "çıktı Ω"
    with pytest.raises(FrontendError) as failure:
        convert(Project([source], top="top"), destination, adapter=conversion_adapter)
    assert failure.value.code == "MISSING_INCLUDE" and failure.value.path == str(source.resolve())
    report = assert_no_published_result(destination)
    assert report["stage"] == "snapshotting_sources" and report["diagnostic"]["line"] == 1
    before = file_hashes(destination)
    with pytest.raises(FrontendError) as existing:
        convert(project("buffer"), destination, adapter=conversion_adapter)
    assert existing.value.code == "OUTPUT_EXISTS" and file_hashes(destination) == before
    assert_scratch_removed(staging_root)


@windows_staging
def test_unicode_replay_refinement_publishes_its_baseline_preview(tmp_path, conversion_adapter, staging_root):
    source = tmp_path / "iyileştirme Ω.v"
    source.write_text("module top(input a,b,output y); assign y=a^b; endmodule\n", encoding="utf-8")
    destination = tmp_path / "çıktı iyileştirme"
    result = convert(Project([source], top="top"), destination, adapter=conversion_adapter, layout_strategy="reserved",
                     refinement={"mode": "replay", "responses": ["{malformed"], "max_attempts": 1, "budget_seconds": 10})
    manifest = assert_original_bundle(result, destination, [source])
    assert manifest["refinement"]["mode"] == "replay" and manifest["refinement"]["accepted"] == 0
    assert Path(result["baseline_preview"]) == destination / "baseline-preview.png"
    assert Path(result["baseline_preview"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert_scratch_removed(staging_root)


@windows_staging
def test_original_unicode_source_changed_during_staged_conversion_is_not_published(tmp_path, conversion_adapter, staging_root):
    source = tmp_path / "değişen kaynak Ω.v"
    source.write_text("module top(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    destination = tmp_path / "çıktı değişim"

    def change(event):
        if event["stage"] == "rendering":
            source.write_text("module top(input a,output y); assign y=~a; endmodule\n", encoding="utf-8")

    with pytest.raises(FrontendError) as failure:
        convert(Project([source], top="top"), destination, adapter=conversion_adapter, progress=change)
    assert failure.value.code == "SOURCE_CHANGED" and failure.value.path == str(source.resolve())
    report = assert_no_published_result(destination)
    assert report["stage"] == "publishing" and report["diagnostic"]["path"] == str(source.resolve())
    assert_scratch_removed(staging_root)


@windows_staging
def test_unretained_staged_work_files_do_not_reject_a_verified_result(tmp_path, conversion_adapter, staging_root, monkeypatch):
    from verilog2digital import conversion
    def unavailable(source, target):
        raise PermissionError("diagnostic copy denied")
    monkeypatch.setattr(conversion.shutil, "copytree", unavailable)
    source = tmp_path / "kaynak Ω.v"
    source.write_text("module top(input a,output y); assign y=~a; endmodule\n", encoding="utf-8")
    result = convert(Project([source], top="top"), tmp_path / "çıktı Ω", adapter=conversion_adapter)
    manifest = read_json(result["manifest"])
    assert manifest["state"] == "ready" and not (tmp_path / "çıktı Ω" / "work").exists()
    assert [item["message"] for item in manifest["progress_warnings"]] == [
        "Intermediate work files were not retained: diagnostic copy denied"]
    assert_scratch_removed(staging_root)


def test_invalid_layout_timeout_is_rejected_with_a_readable_message_before_output(tmp_path):
    with pytest.raises(FrontendError) as failure:
        convert(project("buffer"), tmp_path / "unused", layout_timeout=500)
    assert failure.value.code == "INVALID_LAYOUT_OPTIONS"
    assert failure.value.message == "Layout timeout must be between 1 and 120 seconds"
    assert not (tmp_path / "unused").exists()


def test_combinational_simulation_batches_stay_inside_the_adapter_line_limit():
    class Recorder:
        def __init__(self):
            self.batches = []

        def call(self, method, params):
            assert method == "circuit.simulate" and params["path"] == "c.dig"
            self.batches.append(len(params["stimuli"]))
            return {"schema_version": 1, "samples": [{"values": {name: item["values"]["a"] for name in outputs},
                                                      "high_z": {name: "0x0" for name in outputs}}
                                                     for item in params["stimuli"]]}

    outputs = [f"wide_output_port_{index:03d}" for index in range(64)]
    stimuli = [{"values": {"a": hex(index)}} for index in range(4096)]
    native = Recorder()
    result = simulate_combinational(native, {"path": "c.dig", "sidecar_path": "c.dig.ids.json"}, stimuli, outputs)
    assert [sample["values"][outputs[-1]] for sample in result["samples"]] == [hex(index) for index in range(4096)]
    widest = {name: "0x" + "f" * 16 for name in outputs}
    sample_bytes = len(json.dumps({"values": widest, "high_z": widest}).encode("utf-8")) + 1
    assert len(native.batches) > 1 and sum(native.batches) == 4096
    assert max(native.batches) * sample_bytes < 8 * 1024 * 1024
    small = Recorder()
    outputs = ["y"]
    simulate_combinational(small, {"path": "c.dig", "sidecar_path": "c.dig.ids.json"}, stimuli, outputs)
    assert small.batches == [4096]
