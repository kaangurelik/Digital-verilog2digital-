"""Original HDL, mapped buses, native reload, and independently simulated values."""
from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/verilog/datapath"
CASES = ("ascending_bus", "bus_permutations", "bus_repeats64", "constants64", "add_sub4", "add_sub8",
         "signed_math8", "arithmetic64", "comparisons4", "signed_compare8", "unsigned_compare8",
         "mixed_compare8", "mux8", "reductions8", "shifts8", "shifts64", "alu4")


@pytest.fixture(scope="module")
def native_adapter():
    with Adapter() as adapter:
        yield adapter


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CASES)
def test_datapath_matches_original_hdl_and_preserves_native_bit_topology(tmp_path, native_adapter, name):
    result = convert(Project([FIXTURES / f"{name}.v"], top=name), tmp_path / name, adapter=native_adapter)
    manifest, validation = read(result["manifest"]), read(result["validation"])
    assert manifest["profile"] == "datapath-v1"
    assert validation["valid"], validation
    for check in ("topology", "bits", "components", "geometry", "model", "behavior"):
        assert validation[check]["valid"], validation[check]
    mapped = read(tmp_path / name / "work/mapped.json")
    input_bits = sum(port["width"] for port in mapped["inputs"])
    if input_bits <= 12:
        assert validation["behavior"]["coverage"] == "exhaustive_binary"
        assert validation["behavior"]["input_vectors"] == 2 ** input_bits
    else:
        assert validation["behavior"]["coverage"] != "exhaustive_binary"
        assert 256 <= validation["behavior"]["input_vectors"] <= 4096
    inspection = native_adapter.call("circuit.inspect", {"path": result["circuit"], "sidecar_path": result["sidecar"]})
    assert inspection["model"]["valid"]
    assert ET.parse(result["svg"]).getroot().tag.endswith("svg")
    assert Path(result["preview"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_selected_readable_blocks_survive_mapping(tmp_path, native_adapter):
    result = convert(Project([FIXTURES / "add_sub4.v"], top="add_sub4"), tmp_path / "blocks", adapter=native_adapter)
    mapped = read(Path(result["directory"]) / "work/mapped.json")
    assert {"Add", "Sub", "Splitter"} <= {item["type"] for item in mapped["instances"]}


def test_matching_operand_buses_are_shared_through_native_reload(tmp_path, native_adapter):
    result = convert(Project([FIXTURES / "add_sub8.v"], top="add_sub8"), tmp_path / "shared", adapter=native_adapter)
    validation = read(result["validation"])
    assert validation["valid"] and validation["bits"]["valid"] and validation["topology"]["valid"], validation
    mapped = read(Path(result["directory"]) / "work/mapped.json")
    types = {item["id"]: item["type"] for item in mapped["instances"]}
    assert "Splitter" not in types.values()
    operands = [sorted(types[endpoint["instance_id"]] for endpoint in net["endpoints"])
                for net in mapped["nets"] if net["width"] == 8 and any(types[item["instance_id"]] == "In" for item in net["endpoints"])]
    assert operands == [["Add", "In", "Sub"], ["Add", "In", "Sub"]]


def test_high_output_bit_corruption_cannot_be_published(tmp_path, native_adapter):
    class CorruptHighBit:
        capabilities = native_adapter.capabilities
        output_id = None

        def call(self, method, params, **options):
            response = native_adapter.call(method, params, **options)
            if method == "circuit.inspect":
                self.output_id = next(item["id"] for item in response["instances"]
                                      if item["type"] == "Out" and item["attributes"]["Bits"] == 64)
            if method == "circuit.simulate":
                response = deepcopy(response)
                values = response["samples"][0]["values"]
                identifier = self.output_id
                assert identifier is not None
                values[identifier] = hex(int(values[identifier], 16) ^ (1 << 63))
            return response

    destination = tmp_path / "corrupt_high_bit"
    with pytest.raises(FrontendError) as error:
        convert(Project([FIXTURES / "arithmetic64.v"], top="arithmetic64"), destination, adapter=CorruptHighBit())
    assert error.value.code == "BEHAVIOR_VALIDATION"
    assert not (destination / "manifest.json").exists()
    assert not (destination / "circuit.dig").exists()


def test_malformed_native_label_stays_text_in_svg(tmp_path, native_adapter):
    source = tmp_path / "escaped_label.v"
    label = "{<metadata>example</metadata>"
    source.write_text("module escaped_label(input \\" + label + " ,output y); assign y=\\" + label + " ; endmodule\n", encoding="utf-8")
    result = convert(Project([source], top="escaped_label"), tmp_path / "escaped", adapter=native_adapter)
    root = ET.parse(result["svg"]).getroot()
    assert not any(element.tag.endswith("metadata") for element in root.iter())
    assert label in "".join(root.itertext())
