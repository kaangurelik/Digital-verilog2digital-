"""Local child interfaces are independent of native geometry and remain immutable."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.frontend.hierarchy import lower_hierarchy
from verilog2digital.frontend.project import Project
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.mapping import MappingError, map_hierarchy_module, validate_mapped
from verilog2digital.validation.hierarchy import compose_hierarchy, verify_hierarchy


SOURCE = """
module child #(parameter W=4)(input [W-1:0] a, output [W-1:0] y);
 assign y=~a;
endmodule
module top(input [3:0] a,b, output [3:0] y,z);
 child first(a,y); child second(b,z);
endmodule
"""


@pytest.fixture
def hierarchy(tmp_path):
    source = tmp_path / "source.v"
    source.write_text(SOURCE, encoding="utf-8")
    return lower_hierarchy(Project([source], top="top"), tmp_path / "frontend")


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as worker:
        yield worker


def test_leaf_mapping_uses_native_bus_geometry_and_common_router(hierarchy, adapter):
    before = deepcopy(hierarchy)
    leaf = next(module for module in hierarchy["modules"] if not module["children"])
    mapped = map_hierarchy_module(hierarchy, leaf["id"], adapter, {})
    assert mapped["profile"] == "hierarchy-v1"
    assert mapped["children"] == []
    assert mapped["registers"] == []
    assert mapped["child_types"] == []
    assert layout_reserved(mapped)["strategy"] == "hierarchy-reserved-tracks-v1"
    assert hierarchy == before
    validate_mapped(mapped)


def test_parent_requires_registered_specialization_before_any_native_call(hierarchy):
    class NoCalls:
        def call(self, *args, **kwargs):
            raise AssertionError("Missing catalog must fail before native geometry")
    with pytest.raises(MappingError) as error:
        map_hierarchy_module(hierarchy, hierarchy["top_module_id"], NoCalls(), {})
    assert error.value.code == "MISSING_CHILD_COMPONENT"


class ChildGeometry:
    """Only child geometry is a test double; all primitive geometry stays native."""
    def __init__(self, adapter, hierarchy, *, change=None):
        self.adapter, self.requests, self.change = adapter, [], change
        self.modules = {f"{module['id']}.dig": module for module in hierarchy["modules"]}

    def call(self, method, params, **kwargs):
        self.requests.append(deepcopy(params))
        if params.get("type") not in self.modules:
            clean = {key: value for key, value in params.items() if key != "package"}
            return self.adapter.call(method, clean, **kwargs)
        module = self.modules[params["type"]]
        pins = []
        for index, port in enumerate(module["ports"]):
            direction = port["direction"]
            pins.append({"direction": direction, "pin": port["name"], "width": len(port["bits"]),
                         "position": {"x": 0 if direction == "input" else 80, "y": index*20},
                         "escape": {"x": -1 if direction == "input" else 1, "y": 0}})
        result = {key: deepcopy(params[key]) for key in ("type", "attributes", "position", "rotation")}
        result["schema_version"] = 1
        result["unsupported_attributes"] = []
        result.update(pins=pins, bounds={"min": {"x": 0, "y": -20}, "max": {"x": 80, "y": len(pins)*20}},
                      body_bounds={"min": {"x": 0, "y": -20}, "max": {"x": 80, "y": len(pins)*20}})
        if self.change:
            self.change(result)
        return result


def catalog(hierarchy):
    return {module["id"]: {"type": f"{module['id']}.dig", "sha256": "a"*64, "sidecar_sha256": "b"*64}
            for module in hierarchy["modules"]}


def test_custom_native_pins_bind_exact_source_vectors_and_package_context(hierarchy, adapter):
    worker = ChildGeometry(adapter, hierarchy)
    package = {"root": "C:/test/package", "modules": []}
    before = deepcopy(hierarchy)
    mapped = map_hierarchy_module(hierarchy, hierarchy["top_module_id"], worker, catalog(hierarchy), package=package)
    assert len(mapped["children"]) == 2
    assert len(mapped["child_types"]) == 1
    assert all(request["package"] == package for request in worker.requests)
    assert hierarchy == before
    for child in mapped["children"]:
        for pin, bits in child["ports"].items():
            binding = next(item for item in mapped["pin_bindings"] if item["instance_id"] == child["instance_id"] and item["pin"] == pin)
            assert binding["bits"] == bits
    # Ports and child pins with equal vectors share buses; nothing needs regrouping.
    assert "Splitter" not in {item["type"] for item in mapped["instances"]}
    assert all(len(net["endpoints"]) == 2 for net in mapped["nets"])
    assert layout_reserved(mapped)["geometry"]["valid"]


@pytest.mark.parametrize("change,code", [
    (lambda d: d["pins"][0].update(pin="wrong"), "NATIVE_PIN_MISMATCH"),
    (lambda d: d["pins"][0].update(width=3), "NATIVE_PIN_WIDTH"),
    (lambda d: d["attributes"].update(Label="wrong"), "NATIVE_ATTRIBUTE_MISMATCH"),
])
def test_native_child_inventory_disagreement_is_rejected(hierarchy, adapter, change, code):
    worker = ChildGeometry(adapter, hierarchy, change=change)
    with pytest.raises(MappingError) as error:
        map_hierarchy_module(hierarchy, hierarchy["top_module_id"], worker, catalog(hierarchy))
    assert error.value.code == code


@pytest.mark.parametrize("change", [
    lambda m: m["children"][0]["ports"]["a"].reverse(),
    lambda m: m["child_types"][0]["ports"][0].update(width=3),
    lambda m: m["children"].pop(),
    lambda m: m["child_types"][0].update(module_id="m_"+"0"*24),
])
def test_child_metadata_mutations_are_rejected(hierarchy, adapter, change):
    mapped = map_hierarchy_module(hierarchy, hierarchy["top_module_id"], ChildGeometry(adapter, hierarchy), catalog(hierarchy))
    change(mapped)
    with pytest.raises(MappingError):
        validate_mapped(mapped)


def test_native_emitted_child_geometry_and_parent_bus_behavior(hierarchy, adapter, tmp_path):
    """An actual emitted specialization supplies the parent symbol and execution."""
    directory = tmp_path / "package"
    directory.mkdir()
    leaf = next(module for module in hierarchy["modules"] if not module["children"])
    leaf_mapping = map_hierarchy_module(hierarchy, leaf["id"], adapter, {})
    leaf_path = directory / f"{leaf['id']}.dig"
    emitted = adapter.call("circuit.emit", {"path": str(leaf_path), "circuit": layout_reserved(leaf_mapping)["circuit"]})
    entry = {"type": leaf_path.name, "sha256": emitted["sha256"], "sidecar_sha256": emitted["sidecar_sha256"]}
    assert entry["sidecar_sha256"] == hashlib.sha256((directory / f"{leaf_path.name}.ids.json").read_bytes()).hexdigest()
    package = {"root": str(directory), "modules": [entry]}
    mapped = map_hierarchy_module(hierarchy, hierarchy["top_module_id"], adapter, {leaf["id"]: entry}, package=package)
    root_path = directory / f"{hierarchy['top_module_id']}.dig"
    root_emit = adapter.call("circuit.emit", {"path": str(root_path), "circuit": layout_reserved(mapped)["circuit"], "package": package})
    package["modules"].append({"type": root_path.name, "sha256": root_emit["sha256"], "sidecar_sha256": root_emit["sidecar_sha256"]})
    inputs = {port["port"]: port["instance_id"] for port in mapped["inputs"]}
    outputs = {port["port"]: port["instance_id"] for port in mapped["outputs"]}
    vectors = [{"a": 0, "b": 15}, {"a": 9, "b": 6}, {"a": 15, "b": 0}]
    actual = adapter.call("circuit.simulate", {"path": str(root_path), "sidecar_path": root_emit["sidecar_path"], "package": package,
                          "stimuli": [{"values": {inputs[name]: hex(value) for name, value in vector.items()}} for vector in vectors]})
    assert [{name: int(sample["values"][identifier], 16) for name, identifier in outputs.items()} for sample in actual["samples"]] == [
        {"y": vector["a"] ^ 15, "z": vector["b"] ^ 15} for vector in vectors]
    assert all(all(int(value, 16) == 0 for value in sample["high_z"].values()) for sample in actual["samples"])


@pytest.mark.parametrize("name", ["repeated_logic", "parameter_specializations", "generated_children"])
def test_native_specialized_mapper_corpus(adapter, tmp_path, name):
    source = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/hierarchy" / f"{name}.v"
    hierarchy = lower_hierarchy(Project([source], top=name), tmp_path / "frontend")
    modules = {module["id"]: module for module in hierarchy["modules"]}
    directory = tmp_path / "package"
    directory.mkdir()
    catalog, mappings, emitted = {}, {}, {}
    package = {"root": str(directory), "modules": []}

    def build(identifier):
        if identifier in mappings:
            return
        for child in modules[identifier]["children"]:
            build(child["module_id"])
        mapped = map_hierarchy_module(hierarchy, identifier, adapter, catalog, package=package)
        mappings[identifier] = mapped
        path = directory / f"{identifier}.dig"
        result = adapter.call("circuit.emit", {"path": str(path), "package": package,
                              "circuit": layout_reserved(mapped)["circuit"]})
        emitted[identifier] = result
        entry = {"type": path.name, "sha256": result["sha256"], "sidecar_sha256": result["sidecar_sha256"]}
        catalog[identifier] = entry
        package["modules"].append(entry)

    build(hierarchy["top_module_id"])
    expected = compose_hierarchy(hierarchy, mappings)
    root_emit = emitted[hierarchy["top_module_id"]]
    paths = {key: root_emit[key] for key in ("path", "sidecar_path")}
    inspected = adapter.call("circuit.inspect", {**paths, "package": package})
    validation = verify_hierarchy(expected, inspected)
    assert validation["valid"], validation
    root = mappings[hierarchy["top_module_id"]]
    rows = [{port["port"]: ((index+1)*pattern) & ((1 << port["width"])-1)
             for index, port in enumerate(root["inputs"])} for pattern in (0, 1, 5, 9, 85, 255)]
    native = adapter.call("circuit.simulate", {**paths, "package": package, "stimuli": [
        {"values": {port["instance_id"]: hex(row[port["port"]]) for port in root["inputs"]}} for row in rows]})
    actual = [{port["port"]: int(sample["values"][port["instance_id"]], 16) for port in root["outputs"]} for sample in native["samples"]]
    if name == "repeated_logic":
        wanted = [{"x": row["a"] ^ row["b"], "y": row["c"] ^ row["d"]} for row in rows]
    elif name == "parameter_specializations":
        wanted = [{"y": (row["a"]+row["b"]) & 15, "z": (row["c"]+row["d"]) & 255} for row in rows]
    else:
        wanted = [{"y": row["a"] ^ row["b"]} for row in rows]
    assert actual == wanted
    assert all(int(value, 16) == 0 for sample in native["samples"] for value in sample["high_z"].values())
