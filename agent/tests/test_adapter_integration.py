"""Exercise electrical correctness through emitted and reloaded native files."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from verilog2digital.adapter import Adapter, AdapterError
from verilog2digital.validation.topology import compare_components, compare_topology

from adapter_fixtures import (
    CASE_FACTORIES,
    and_gate,
    direct_wire,
    disconnected_pin,
    distinct_tunnels,
    matching_tunnels,
    nonjoining_crossing,
    splitter,
)


@pytest.fixture(scope="session")
def adapter():
    # Native startup is substantially more expensive than individual requests.
    with Adapter() as worker:
        yield worker


def emit(adapter, case, directory: Path):
    result = adapter.call("circuit.emit", {"circuit": case.circuit, "path": str(directory / f"{case.name}.dig")})
    assert Path(result["path"]).is_file()
    assert Path(result["sidecar_path"]).is_file()
    return {"path": result["path"], "sidecar_path": result["sidecar_path"]}


@pytest.mark.parametrize("make_case", CASE_FACTORIES, ids=lambda factory: factory.__name__)
def test_emitted_topology_matches_independent_intent(adapter, tmp_path, make_case):
    case = make_case(adapter)
    paths = emit(adapter, case, tmp_path)
    inspection = adapter.call("circuit.inspect", paths)

    components = compare_components(case.descriptors, inspection["instances"])
    assert components["valid"], components["diagnostics"]
    topology = compare_topology(case.expected_nets, inspection)
    assert topology["valid"] is case.topology_valid, topology["diagnostics"]
    assert inspection["topology_available"] is case.topology_available
    assert inspection["model"]["valid"] is case.model_valid, inspection["model"]["diagnostics"]
    if not case.topology_valid:
        assert topology["diagnostics"], "A rejected circuit must explain its wiring discrepancy"
        assert inspection["model"]["diagnostics"], "An invalid model must explain its electrical error"


SIMULATION_CASES = (
    (direct_wire, [{"a": 0}, {"a": 1}], [{"y": 0}, {"y": 1}]),
    (and_gate,
     [{"a": 0, "b": 0}, {"a": 0, "b": 1}, {"a": 1, "b": 0}, {"a": 1, "b": 1}],
     [{"y": 0}, {"y": 0}, {"y": 0}, {"y": 1}]),
    (nonjoining_crossing,
     [{"a": 0, "b": 1}, {"a": 1, "b": 0}],
     [{"y": 0, "z": 1}, {"y": 1, "z": 0}]),
    (splitter,
     [{"a": 0}, {"a": 1}, {"a": 2}, {"a": 3}],
     [{"lo": 0, "hi": 0}, {"lo": 1, "hi": 0}, {"lo": 0, "hi": 1}, {"lo": 1, "hi": 1}]),
    (matching_tunnels, [{"a": 0}, {"a": 1}], [{"y": 0}, {"y": 1}]),
    (distinct_tunnels,
     [{"a": 0, "b": 1}, {"a": 1, "b": 0}],
     [{"y": 0, "z": 1}, {"y": 1, "z": 0}]),
)


@pytest.mark.parametrize("make_case,inputs,expected", SIMULATION_CASES,
                         ids=[case[0].__name__ for case in SIMULATION_CASES])
def test_simulation_against_declared_truth_tables(adapter, tmp_path, make_case, inputs, expected):
    case = make_case(adapter)
    paths = emit(adapter, case, tmp_path)
    stimuli = [{"values": {identifier: hex(value) for identifier, value in values.items()}} for values in inputs]
    result = adapter.call("circuit.simulate", {**paths, "stimuli": stimuli})

    assert len(result["samples"]) == len(expected)
    for sample, values in zip(result["samples"], expected):
        assert {identifier: int(value, 0) for identifier, value in sample["values"].items()} == values
        assert set(sample["high_z"]) == set(values)
        assert all(int(value, 0) == 0 for value in sample["high_z"].values())


def test_width_mutation_is_detected_even_when_topology_matches(adapter, tmp_path):
    case = direct_wire(adapter)
    # Intended descriptors were queried for a one-bit connection. Mutating both
    # ends preserves connectivity and model validity, but changes its meaning.
    mutated = deepcopy(case)
    mutated.name = "attribute_mutation"
    for instance in mutated.circuit["instances"]:
        instance["attributes"]["Bits"] = 2
    inspection = adapter.call("circuit.inspect", emit(adapter, mutated, tmp_path))

    assert inspection["model"]["valid"], inspection["model"]["diagnostics"]
    assert compare_topology(case.expected_nets, inspection)["valid"]
    components = compare_components(case.descriptors, inspection["instances"])
    assert not components["valid"]
    assert components["diagnostics"]


def test_invalid_model_cannot_produce_simulation_samples(adapter, tmp_path):
    paths = emit(adapter, disconnected_pin(adapter), tmp_path)
    with pytest.raises(AdapterError):
        adapter.call("circuit.simulate", {**paths, "stimuli": [{"values": {"a": "0x1"}}]})

    # A circuit error must not terminate the persistent worker.
    description = adapter.call("component.describe", {"type": "In", "attributes": {},
                                                       "position": {"x": 0, "y": 0}, "rotation": 0})
    assert description["type"] == "In"


def test_edited_circuit_rejects_stale_sidecar(adapter, tmp_path):
    paths = emit(adapter, direct_wire(adapter), tmp_path)
    tree = ET.parse(paths["path"])
    endpoint = tree.getroot().find("./wires/wire/p2")
    assert endpoint is not None
    endpoint.set("x", "180")
    tree.write(paths["path"], encoding="utf-8", xml_declaration=True)

    with pytest.raises(AdapterError) as error:
        adapter.call("circuit.inspect", paths)
    assert error.value.data["code"] == "STALE_SIDECAR"


def test_reordered_serialized_components_cannot_reuse_index_associations(adapter, tmp_path):
    paths = emit(adapter, direct_wire(adapter), tmp_path)
    tree = ET.parse(paths["path"])
    elements = tree.getroot().find("visualElements")
    assert elements is not None and len(elements) == 2
    elements[:] = list(reversed(elements))
    tree.write(paths["path"], encoding="utf-8", xml_declaration=True)

    # Update only the outer digest to exercise component signatures independently
    # of the earlier stale-file check. The old index-to-instance map is wrong.
    sidecar_path = Path(paths["sidecar_path"])
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["sha256"] = hashlib.sha256(Path(paths["path"]).read_bytes()).hexdigest()
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(AdapterError) as error:
        adapter.call("circuit.inspect", paths)
    assert error.value.data["code"] == "INSTANCE_ASSOCIATION"


@pytest.mark.parametrize("mutation", ["reordered_records", "duplicate_ids"])
def test_corrupt_sidecar_associations_are_rejected(adapter, tmp_path, mutation):
    paths = emit(adapter, direct_wire(adapter), tmp_path)
    sidecar_path = Path(paths["sidecar_path"])
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if mutation == "reordered_records":
        sidecar["instances"].reverse()
    else:
        sidecar["instances"][1]["id"] = sidecar["instances"][0]["id"]
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(AdapterError):
        adapter.call("circuit.inspect", paths)


def test_sidecar_from_another_circuit_is_rejected(adapter, tmp_path):
    direct = emit(adapter, direct_wire(adapter), tmp_path)
    other = emit(adapter, and_gate(adapter), tmp_path)
    with pytest.raises(AdapterError) as error:
        adapter.call("circuit.inspect", {"path": direct["path"], "sidecar_path": other["sidecar_path"]})
    assert error.value.data["code"] == "STALE_SIDECAR"


@pytest.mark.parametrize("format", ["svg", "png"])
def test_rendered_artifact_is_readable(adapter, tmp_path, format):
    paths = emit(adapter, and_gate(adapter), tmp_path)
    output = tmp_path / f"circuit.{format}"
    adapter.call("circuit.render", {"path": paths["path"], "format": format, "output_path": str(output)})

    assert output.is_file()
    assert output.stat().st_size > 32
    if format == "png":
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    else:
        assert ET.parse(output).getroot().tag == "{http://www.w3.org/2000/svg}svg"
