"""Recursive alias composition and relocatable package bindings reject corruption."""
from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from verilog2digital.validation.hierarchy import (
    HierarchyError, compose_hierarchy, scoped_id, snapshot_package, verify_hierarchy, verify_package_snapshot,
)
from hierarchy_fixtures import alias_hierarchy, register_hierarchy


@pytest.mark.parametrize("repeated", [False, True])
def test_declared_child_aliases_join_parent_nets_without_crossing_occurrences(repeated):
    hierarchy, mappings, native = alias_hierarchy(repeated)
    original = deepcopy((hierarchy, mappings))
    expected = compose_hierarchy(hierarchy, mappings)
    assert (hierarchy, mappings) == original
    report = verify_hierarchy(expected, {"expanded": native})
    assert report["valid"], report
    assert len(expected["expanded_mapping"]["nets"]) == (2 if repeated else 1)
    assert {item["id"] for item in expected["expanded_mapping"]["instances"]} == {item["id"] for item in native["instances"]}
    assert expected["occurrences"][1]["hdl_path"] == ["first.child"]


def test_native_open_connection_is_rejected_after_child_passthrough_composition():
    hierarchy, mappings, native = alias_hierarchy()
    native["unconnected"] = native["nets"].pop()["endpoints"]
    report = verify_hierarchy(compose_hierarchy(hierarchy, mappings), native)
    assert not report["valid"]
    assert not report["topology"]["valid"] and not report["bits"]["valid"]


def test_accidental_native_join_between_repeated_children_is_rejected():
    hierarchy, mappings, native = alias_hierarchy(True)
    native["nets"][0]["endpoints"].extend(native["nets"].pop()["endpoints"])
    report = verify_hierarchy(compose_hierarchy(hierarchy, mappings), native)
    assert not report["valid"]
    assert not report["topology"]["valid"] and not report["bits"]["valid"]


def test_component_mutation_is_not_hidden_by_correct_connectivity():
    hierarchy, mappings, native = alias_hierarchy()
    native["instances"][0]["attributes"]["Label"] = "other_input"
    report = verify_hierarchy(compose_hierarchy(hierarchy, mappings), native)
    assert not report["components"]["valid"]


def test_unknown_native_pin_width_requires_independent_explicit_width():
    hierarchy, mappings, native = alias_hierarchy()
    mappings["leaf"]["descriptors"][0]["pins"][0]["width"] = None
    assert verify_hierarchy(compose_hierarchy(hierarchy, mappings), native)["valid"]
    mappings["leaf"]["pin_bindings"][0]["width"] = 2
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code == "HIERARCHY_ENDPOINT"


def test_recursive_or_missing_module_is_rejected_before_expansion():
    hierarchy, mappings, _ = alias_hierarchy()
    hierarchy["modules"][0]["children"][0]["module_id"] = "missing"
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code == "HIERARCHY_MISSING_MODULE"
    hierarchy["modules"][0]["children"][0]["module_id"] = "top"
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code == "HIERARCHY_RECURSION"


def test_boundary_pin_bit_order_cannot_silently_override_elaborated_binding():
    hierarchy, mappings, _ = alias_hierarchy()
    hierarchy["modules"][0]["children"][0]["ports"]["a"] = [3]
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code == "HIERARCHY_BITS"


def test_root_port_cannot_be_rebound_while_child_bindings_remain_unchanged():
    hierarchy, mappings, _ = alias_hierarchy(True)
    hierarchy["modules"][0]["ports"][0]["bits"] = [4]
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code == "HIERARCHY_BITS"


def test_reused_child_registers_keep_distinct_native_ids_and_expanded_proof_ids():
    hierarchy, mappings = register_hierarchy()
    expected = compose_hierarchy(hierarchy, mappings)
    view = expected["behavior_mapping"]
    assert [(item["instance_id"], item["cell_id"], item["width"]) for item in view["registers"]] == [("u0.g0", "c0", 1), ("u1.g0", "c1", 1)]
    assert view["clock"]["instance_id"] == "in0"
    assert {item["id"]: item["attributes"]["Default"] for item in view["instances"] if item["type"] == "D_FF"} == {"u0.g0": "0x0", "u1.g0": "0x0"}
    assert all(item["type"] != "child.dig" for item in expected["expanded_mapping"]["instances"])
    assert not any(item["id"].startswith("u0.in") for item in expected["expanded_mapping"]["instances"])


@pytest.mark.parametrize("mutation", ["duplicate_cell", "missing_register", "wrong_width"])
def test_physical_state_cannot_be_omitted_or_bound_to_another_reference_occurrence(mutation):
    hierarchy, mappings = register_hierarchy()
    if mutation == "duplicate_cell": hierarchy["occurrences"][2]["cell_map"]["c0"] = "c0"
    elif mutation == "missing_register": mappings["leaf"]["registers"] = []
    else: mappings["leaf"]["registers"][0]["width"] = 2
    with pytest.raises(HierarchyError) as error:
        compose_hierarchy(hierarchy, mappings)
    assert error.value.code in {"HIERARCHY_PROVENANCE", "HIERARCHY_STATE_BINDING"}


def test_generated_scope_ids_do_not_parse_original_hdl_punctuation():
    assert scoped_id(["u0000", "u0002"], "g0003") == "u0000.u0002.g0003"
    for path, identifier in ((["real.child"], "g0"), (["u0"], "a/b"), (["u" * 125], "g0000")):
        with pytest.raises(HierarchyError):
            scoped_id(path, identifier)


def test_package_snapshot_remains_valid_after_relocation_and_binds_every_child(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    files = ["circuit.dig", "m_001.dig", "m_001.dig.ids.json"]
    for name in files:
        (original / name).write_text(name, encoding="utf-8")
    binding = snapshot_package(original, files)
    relocated = tmp_path / "relocated package"
    shutil.copytree(original, relocated)
    original.rename(tmp_path / "original_unavailable")
    assert verify_package_snapshot(relocated, binding)["valid"]
    (relocated / "m_001.dig").write_text("child changed while top remained identical", encoding="utf-8")
    assert not verify_package_snapshot(relocated, binding)["valid"]


@pytest.mark.parametrize("bad_paths", [["../outside.dig"], ["/absolute.dig"], ["C:/absolute.dig"], ["a\\child.dig"], ["a/../child.dig"], ["child.dig", "CHILD.dig"]])
def test_package_binding_rejects_escapes_and_native_case_collisions(tmp_path, bad_paths):
    with pytest.raises(HierarchyError):
        snapshot_package(tmp_path, bad_paths)


def test_missing_sidecar_invalidates_package_binding(tmp_path):
    path = tmp_path / "child.dig.ids.json"
    path.write_text("{}", encoding="utf-8")
    binding = snapshot_package(tmp_path, [path.name])
    path.unlink()
    assert not verify_package_snapshot(tmp_path, binding)["valid"]
