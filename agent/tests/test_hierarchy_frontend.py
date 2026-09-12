"""Specializations preserve occurrence identity before unmerged expansion."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.hierarchy import lower_hierarchy, validate_hierarchy
from verilog2digital.frontend.project import Project


def lower_text(tmp_path, source):
    path = tmp_path / "source.v"
    path.write_text(source, encoding="utf-8")
    return lower_hierarchy(Project([path], top="top"), tmp_path / "frontend")


def test_repeated_nested_parameter_specializations_and_safe_occurrence_ids(tmp_path):
    hierarchy = lower_text(tmp_path, """
module leaf #(parameter W=4, parameter B=4'd3)(input [W-1:0] a, output [W-1:0] y);
 assign y=a+B;
endmodule
module middle(input [3:0] a, output [3:0] y);
 leaf #(.W(4),.B(4'd2)) \\escaped.with.dots (.a(a),.y(y));
endmodule
module top(input [3:0] a,b, output [3:0] y,z, output [1:0] w);
 middle first(a,y); middle second(b,z);
 leaf #(.W(2),.B(2'd1)) tiny(a[1:0],w);
endmodule
""")
    assert len(hierarchy["modules"]) == 4
    assert len(hierarchy["occurrences"]) == 6
    assert len(hierarchy["expanded_design"]["cells"]) == 3
    assert len({cell["native_name"] for cell in hierarchy["expanded_design"]["cells"]}) == 3
    assert any(child["native_name"] == "escaped.with.dots" for module in hierarchy["modules"] for child in module["children"])
    assert all(atom.startswith("u") and "." not in atom for occurrence in hierarchy["occurrences"] for atom in occurrence["path"])
    leaves = [module for module in hierarchy["modules"] if module["hdl_name"] == "leaf"]
    assert len(leaves) == 2
    assert sorted(len(module["parameters_raw"]["B"]) for module in leaves) == [2, 4]
    assert json.loads((tmp_path / "frontend/source-inspection.json").read_text())["input_hashes"]


def test_repeated_state_with_unused_output_is_not_globally_eliminated(tmp_path):
    hierarchy = lower_text(tmp_path, """
module child(input clk,rst,d, output reg q, output y);
 always @(posedge clk) if(rst) q<=0; else q<=d;
 assign y=d;
endmodule
module top(input clk,rst,a,b, output q,y,z);
 child first(clk,rst,a,q,y); child second(clk,rst,b,,z);
endmodule
""")
    states = [cell for cell in hierarchy["expanded_design"]["cells"] if cell["type"] == "$dff"]
    assert len(states) == 2
    assert len({cell["id"] for cell in states}) == 2
    validate_hierarchy(hierarchy)


def test_passthrough_aliases_preserve_distinct_local_port_bindings(tmp_path):
    hierarchy = lower_text(tmp_path, """
module child(input [3:0] a, output [3:0] y); assign y={a[0],a[2:1],a[3]}; endmodule
module top(input [3:0] a, output [3:0] y); child node(a,y); endmodule
""")
    root = next(module for module in hierarchy["modules"] if module["id"] == hierarchy["top_module_id"])
    assert root["children"][0]["ports"]["a"] != root["children"][0]["ports"]["y"]
    assert not hierarchy["expanded_design"]["cells"]
    assert len(hierarchy["occurrences"]) == 2


def test_occurrence_cell_identity_tampering_rejected(tmp_path):
    hierarchy = lower_text(tmp_path, """
module child(input a,output y); assign y=~a; endmodule
module top(input a,b, output y,z); child first(a,y); child second(b,z); endmodule
""")
    bad = deepcopy(hierarchy)
    occurrences = [item for item in bad["occurrences"] if item["cell_map"]]
    occurrences[1]["cell_map"] = deepcopy(occurrences[0]["cell_map"])
    with pytest.raises(FrontendError, match="identity"):
        validate_hierarchy(bad)


def test_composed_parent_bit_lane_mutation_disagrees_with_expanded_reference(tmp_path):
    hierarchy = lower_text(tmp_path, """
module child(input [3:0] a, output [3:0] y); assign y=~a; endmodule
module top(input [3:0] a, output [3:0] y); child first(a,y); endmodule
""")
    root = next(module for module in hierarchy["modules"] if module["id"] == hierarchy["top_module_id"])
    root["children"][0]["ports"]["a"].reverse()
    with pytest.raises(FrontendError) as error:
        validate_hierarchy(hierarchy)
    assert error.value.code == "HIERARCHY_BINDING"


def test_expansion_budget_is_checked_before_native_flatten(tmp_path):
    source = "module leaf(input a,output y); assign y=~a; endmodule\n"
    source += "module top(input a,output [255:0] y);\n"
    source += "\n".join(f"leaf x{i}(a,y[{i}]);" for i in range(256))
    source += "\nendmodule\n"
    # Use scalar output ports because the existing frontend bounds buses at 64.
    source = source.replace("output [255:0] y", ",".join(f"output y{i}" for i in range(256)))
    for i in range(256):
        source = source.replace(f"y[{i}]", f"y{i}")
    with pytest.raises(FrontendError) as error:
        lower_text(tmp_path, source)
    assert error.value.code == "HIERARCHY_LIMIT"
    assert not (tmp_path / "frontend/expand.tcl").exists()
