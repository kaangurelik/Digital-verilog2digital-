"""Original HDL and expanded initialization retain each physical child occurrence."""
from pathlib import Path

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.hierarchy import lower_hierarchy
from verilog2digital.frontend.project import Project
from verilog2digital.mapping import map_hierarchy_module
from verilog2digital.validation.hierarchy import compose_hierarchy
from verilog2digital.validation.reference import simulate_reference, simulate_sequential_reference
from verilog2digital.validation.sequential import sequential_stimulus_plan
from verilog2digital.validation.stimuli import stimulus_plan
from test_hierarchy_mapping import ChildGeometry, catalog


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/hierarchy"
POSITIVE = ("repeated_logic", "parameter_specializations", "nested_arithmetic", "stateful_children",
            "controller_datapath", "port_aliases", "generated_children", "unused_state_output", "sized_parameters")


@pytest.fixture(scope="module")
def hierarchy_adapter():
    with Adapter() as adapter:
        yield adapter


@pytest.mark.parametrize("name", POSITIVE)
def test_original_hierarchy_composes_all_occurrences_and_preserves_reference_behavior(name, tmp_path, hierarchy_adapter):
    project = Project([FIXTURES / f"{name}.v"], top=name)
    hierarchy = lower_hierarchy(project, tmp_path / "frontend")
    worker = ChildGeometry(hierarchy_adapter, hierarchy)
    mappings = {module["id"]: map_hierarchy_module(hierarchy, module["id"], worker, catalog(hierarchy))
                for module in hierarchy["modules"]}
    composed = compose_hierarchy(hierarchy, mappings)
    mapped, design = composed["behavior_mapping"], hierarchy["expanded_design"]
    assert len(composed["occurrences"]) == len(hierarchy["occurrences"])
    assert all("." in item["id"] or item["type"] in {"In", "Out", "Splitter", "Const"}
               for item in composed["expanded_mapping"]["instances"])
    if mapped["registers"]:
        plan = sequential_stimulus_plan(design, random_count=4)
        reference = simulate_sequential_reference(project, design, tmp_path / "reference", plan["trace"])
        assert len(reference["samples"]) == len(plan["trace"]["cycles"]) * 3
        assert plan["initialization"]["register_count"] == len(mapped["registers"])
        if name == "unused_state_output":
            assert len(mapped["registers"]) == 4
            assert plan["initialization"]["known_register_bits"] == 32
        if name == "stateful_children":
            assert len({item["instance_id"] for item in mapped["registers"]}) == 2
    else:
        plan = stimulus_plan(mapped, random_count=4)
        reference = simulate_reference(project, design, tmp_path / "reference", plan["stimuli"])
        for values, row in zip(plan["stimuli"], reference["samples"]):
            observed = row["values"]
            if name in {"repeated_logic", "generated_children"}:
                expected = {"x": values["a"] ^ values["b"], "y": values["c"] ^ values["d"]} if name == "repeated_logic" else {"y": values["a"] ^ values["b"]}
            elif name == "parameter_specializations":
                expected = {"y": (values["a"] + values["b"]) & 15, "z": (values["c"] + values["d"]) & 255}
            elif name == "nested_arithmetic":
                expected = {"y": ((values["a"] + values["b"]) & 15) ^ values["c"]}
            elif name == "port_aliases":
                a = values["a"]
                expected = {"direct": a, "reversed": int(f"{a:04b}"[::-1], 2),
                            "repeated": ((a >> 3) * 12) | ((a & 1) * 3), "zeros": 0}
            else:
                expected = {"narrow": 127, "wide": 7}
            assert observed == expected


def test_unobserved_child_state_still_requires_real_initialization(tmp_path):
    project = Project([FIXTURES / "hidden_child_state.v"], top="hidden_child_state")
    hierarchy = lower_hierarchy(project, tmp_path / "frontend")
    assert sum(cell["type"] == "$dff" for cell in hierarchy["expanded_design"]["cells"]) == 2
    with pytest.raises(FrontendError) as error:
        sequential_stimulus_plan(hierarchy["expanded_design"], random_count=0)
    assert error.value.code == "UNINITIALIZED_STATE"
