"""Exercise original HDL through mapping, native serialization, and both simulators."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import product
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
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
