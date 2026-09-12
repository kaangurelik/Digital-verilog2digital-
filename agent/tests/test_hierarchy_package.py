"""Reopen published-only packages with fresh native state and reject child damage."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from verilog2digital.adapter import Adapter, AdapterError
from verilog2digital.conversion import convert, compare_behavior, compare_sequential_behavior
from verilog2digital.frontend.project import Project
from verilog2digital.validation.hierarchy import verify_hierarchy, verify_package_snapshot
from test_conversion import conversion_adapter
from test_hierarchy_reference import FIXTURES


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def context(directory, package):
    return {"root": str(directory), "modules": deepcopy(package["modules"])}


def replay(adapter, directory, package, mapped, reference, validation, vectors):
    params = {"path": str(directory / package["root_file"]),
              "sidecar_path": str(directory / (package["root_file"] + ".ids.json")),
              "package": context(directory, package)}
    inputs = {item["port"]: item["instance_id"] for item in mapped["inputs"]}
    if mapped["registers"]:
        def rows(cycles):
            return [{"values": {inputs[name]: hex(value) for name, value in row["values"].items()}} for row in cycles]
        native = adapter.call("circuit.simulate_cycles", {**params, "clock_instance_id": mapped["clock"]["instance_id"],
            "initialization": {"kind": "input_sequence", "cycles": rows(vectors["initialization"]["cycles"])},
            "cycles": rows(vectors["cycles"])})
        compared = compare_sequential_behavior(mapped, vectors, reference, native,
            coverage=validation["behavior"], initialization=validation["behavior"]["initialization"])
    else:
        native = adapter.call("circuit.simulate", {**params, "stimuli": [
            {"values": {inputs[name]: hex(value) for name, value in row.items()}} for row in vectors]})
        compared = compare_behavior(mapped, vectors, reference, native, coverage=validation["behavior"])
    assert compared["valid"], compared


@pytest.mark.parametrize("name", ["port_aliases", "stateful_children"])
def test_published_files_relocate_without_hdl_or_original_work_and_replay_in_fresh_adapter(name, tmp_path, conversion_adapter):
    source = tmp_path / "original source.v"
    shutil.copyfile(FIXTURES / f"{name}.v", source)
    original = tmp_path / "original conversion"
    result = convert(Project([source], top=name), original, adapter=conversion_adapter, random_count=4)
    package, validation = document(result["package"]), document(result["validation"])
    expected = document(original / "work/hierarchy-mapped.json")
    mapped = expected["behavior_mapping"]
    reference = document(original / "work/reference-samples.json")
    vectors = document(original / ("work/trace.json" if mapped["registers"] else "work/stimuli.json"))
    copied = tmp_path / "relocated package"
    copied.mkdir()
    names = [item["path"] for item in package["snapshot"]["files"]] + ["package.json"]
    for filename in names:
        shutil.copyfile(original / filename, copied / filename)
    assert {path.name for path in copied.iterdir()} == set(names)
    assert all(path.is_file() for path in copied.iterdir())
    original.rename(tmp_path / "unavailable_original")
    source.rename(tmp_path / "unavailable_source.txt")
    assert not original.exists() and not source.exists()
    assert verify_package_snapshot(copied, package["snapshot"])["valid"]
    with Adapter() as fresh:
        params = {"path": str(copied / "circuit.dig"), "sidecar_path": str(copied / "circuit.dig.ids.json"),
                  "package": context(copied, package)}
        inspected = fresh.call("circuit.inspect", params)
        verified = verify_hierarchy(expected, inspected)
        assert verified["valid"], verified
        assert inspected["expanded"]["wires"] == []
        preview = tmp_path / "relocated.svg"
        fresh.call("circuit.render", {"path": params["path"], "package": params["package"],
                   "format": "svg", "output_path": str(preview)})
        assert preview.stat().st_size > 100
        replay(fresh, copied, package, mapped, reference, validation, vectors)


@pytest.fixture(scope="module")
def alias_package(tmp_path_factory, conversion_adapter):
    directory = tmp_path_factory.mktemp("alias-package") / "result"
    result = convert(Project([FIXTURES / "port_aliases.v"], top="port_aliases"), directory,
                     adapter=conversion_adapter, layout_strategy="reserved", random_count=0)
    return directory, document(result["package"]), document(directory / "work/hierarchy-mapped.json")


@pytest.mark.parametrize("suffix", ["", ".ids.json"])
def test_changed_child_file_is_rejected_even_when_root_circuit_is_identical(suffix, tmp_path, alias_package, conversion_adapter):
    original, package, _ = alias_package
    copied = tmp_path / "copy"
    copied.mkdir()
    for item in package["snapshot"]["files"]:
        shutil.copyfile(original / item["path"], copied / item["path"])
    top_before = (copied / "circuit.dig").read_bytes()
    target = copied / (package["modules"][0]["type"] + suffix)
    target.write_bytes(target.read_bytes() + b" ")
    assert (copied / "circuit.dig").read_bytes() == top_before
    assert not verify_package_snapshot(copied, package["snapshot"])["valid"]
    with pytest.raises(AdapterError) as error:
        conversion_adapter.call("circuit.inspect", {"path": str(copied / "circuit.dig"),
            "sidecar_path": str(copied / "circuit.dig.ids.json"), "package": context(copied, package)})
    assert error.value.data["code"] == "PACKAGE_HASH"


def test_real_child_wire_damage_with_fresh_identity_is_rejected_by_expanded_oracle(tmp_path, alias_package, conversion_adapter):
    original, package, expected = alias_package
    copied = tmp_path / "copy"
    copied.mkdir()
    for item in package["snapshot"]["files"]:
        shutil.copyfile(original / item["path"], copied / item["path"])
    module = package["modules"][0]
    identifier = module["type"][:-4]
    circuit = document(original / "work/modules" / identifier / "candidate.json")
    assert circuit["wires"]
    circuit["wires"] = []
    # Refresh identity hashes so the electrical check catches the corruption.
    emitted = conversion_adapter.call("circuit.emit", {"path": str(copied / module["type"]),
        "circuit": circuit, "package": {"root": str(copied), "modules": []}})
    replacement = {**module, "sha256": emitted["sha256"], "sidecar_sha256": emitted["sidecar_sha256"]}
    params = {"path": str(copied / "circuit.dig"), "sidecar_path": str(copied / "circuit.dig.ids.json"),
              "package": {"root": str(copied), "modules": [replacement]}}
    inspected = conversion_adapter.call("circuit.inspect", params)
    checked = verify_hierarchy(expected, inspected)
    assert not checked["valid"]
    assert not checked["topology"]["valid"] and not checked["bits"]["valid"]


def test_native_bus_lane_swap_is_detected_independently_of_component_inventory(alias_package, conversion_adapter):
    directory, package, expected = alias_package
    inspection = conversion_adapter.call("circuit.inspect", {"path": str(directory / "circuit.dig"),
        "sidecar_path": str(directory / "circuit.dig.ids.json"), "package": context(directory, package)})
    assert verify_hierarchy(expected, inspection)["valid"]
    changed = deepcopy(inspection)
    bindings = {(item["instance_id"], item["direction"], item["pin"]): item["bits"]
                for item in expected["expanded_mapping"]["pin_bindings"]}
    def bits(endpoint):
        return bindings[endpoint["instance_id"], endpoint["direction"], endpoint["pin"]]
    scalar = [(net, endpoint) for net in changed["expanded"]["nets"] for endpoint in net["endpoints"]
              if endpoint["instance_id"].count(".") and endpoint["direction"] == "input" and len(bits(endpoint)) == 1]
    first_net, first = scalar[0]
    second_net, second = next((net, endpoint) for net, endpoint in scalar
                              if net is not first_net and bits(endpoint) != bits(first))
    first_net["endpoints"].remove(first)
    second_net["endpoints"].remove(second)
    first_net["endpoints"].append(second)
    second_net["endpoints"].append(first)
    report = verify_hierarchy(expected, changed)
    assert report["components"]["valid"]
    assert not report["topology"]["valid"] and not report["bits"]["valid"]


def test_nested_bus_and_clock_tunnels_preserve_expanded_stateful_behavior(tmp_path, conversion_adapter):
    result = convert(Project([FIXTURES / "stateful_children.v"], top="stateful_children"),
                     tmp_path / "tunneled", adapter=conversion_adapter, layout_strategy="reserved",
                     tunnels=True, random_count=4)
    expected = document(Path(result["directory"]) / "work/hierarchy-mapped.json")
    manifest, validation = document(result["manifest"]), document(result["validation"])
    assert any(module["tunnels"]["nets"] for module in manifest["layout"]["modules"].values())
    assert any(item["type"] == "Tunnel" and "." in item["id"]
               for item in expected["expanded_mapping"]["instances"])
    assert validation["behavior"]["valid"] and validation["bits"]["valid"]
    package = document(result["package"])
    inspected = conversion_adapter.call("circuit.inspect", {"path": result["circuit"],
        "sidecar_path": result["sidecar"], "package": context(Path(result["directory"]), package)})
    assert verify_hierarchy(expected, inspected)["valid"]
