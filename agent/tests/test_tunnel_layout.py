"""Optional tunnels preserve native connectivity and leave source mapping intact."""
from copy import deepcopy

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.conversion import validate_export
from verilog2digital.frontend.project import Project
from verilog2digital.frontend.yosys import lower
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.layout.scalar import LayoutError
from verilog2digital.layout.tunnels import add_long_tunnels
from verilog2digital.mapping import map_design


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as native:
        yield native


def candidate(tmp_path, adapter, width=1):
    source = tmp_path / "buffer.v"
    source.write_text(f"module buffer(input [{width-1}:0] a,output [{width-1}:0] y); assign y=a; endmodule\n", encoding="utf-8")
    mapped = map_design(lower(Project([source], top="buffer"), tmp_path / "frontend"), adapter)
    return mapped, layout_reserved(mapped, spacing=400)


@pytest.mark.parametrize("width", [1, 8])
def test_named_tunnels_preserve_scalar_and_bus_bits_after_reload(tmp_path, adapter, width):
    mapped, layout = candidate(tmp_path, adapter, width)
    original = deepcopy(mapped)
    tunneled = add_long_tunnels(layout, mapped, adapter, minimum_length=100, maximum_nets=2, timeout=20)
    assert mapped == original
    assert tunneled["tunnels"]["nets"], tunneled["tunnels"]
    assert tunneled["geometry"]["valid"]
    emitted = adapter.call("circuit.emit", {"circuit": tunneled["circuit"], "path": str(tmp_path / "tunneled.dig")})
    paths = {key: emitted[key] for key in ("path", "sidecar_path")}
    report, _ = validate_export(adapter, tunneled["validation_mapping"], tunneled["circuit"], paths)
    assert report["valid"], report
    simulated = adapter.call("circuit.simulate", {**paths, "stimuli": [
        {"values": {mapped["inputs"][0]["instance_id"]: hex(value)}} for value in (0, (1 << width) - 1)]})
    assert [int(row["values"][mapped["outputs"][0]["instance_id"]], 16) for row in simulated["samples"]] == [0, (1 << width) - 1]


def test_changed_tunnel_name_is_detected_by_native_oracle(tmp_path, adapter):
    mapped, layout = candidate(tmp_path, adapter)
    tunneled = add_long_tunnels(layout, mapped, adapter, minimum_length=100, maximum_nets=1, timeout=20)
    assert tunneled["tunnels"]["nets"]
    corrupt = deepcopy(tunneled["circuit"])
    next(item for item in corrupt["instances"] if item["type"] == "Tunnel")["attributes"]["NetName"] = "UnmatchedTunnel"
    emitted = adapter.call("circuit.emit", {"circuit": corrupt, "path": str(tmp_path / "corrupt.dig")})
    report, _ = validate_export(adapter, tunneled["validation_mapping"], tunneled["circuit"],
                               {key: emitted[key] for key in ("path", "sidecar_path")})
    assert not report["valid"]
    assert not report["topology"]["valid"]


def test_default_visible_layout_and_disabled_tunnel_budget_remain_unchanged(tmp_path, adapter):
    mapped, layout = candidate(tmp_path, adapter)
    tunneled = add_long_tunnels(layout, mapped, adapter, minimum_length=100, maximum_nets=0)
    assert tunneled["circuit"] == layout["circuit"]
    assert not tunneled["tunnels"]["nets"]


@pytest.mark.parametrize("timeout", [-1, True, float("nan"), float("inf"), 31])
def test_invalid_tunnel_deadlines_are_rejected_before_native_work(timeout):
    with pytest.raises(LayoutError) as error:
        add_long_tunnels({}, {}, None, timeout=timeout)
    assert error.value.code == "INVALID_TUNNEL_OPTIONS"


def test_zero_tunnel_budget_keeps_the_valid_visible_layout(tmp_path, adapter):
    mapped, layout = candidate(tmp_path, adapter)
    result = add_long_tunnels(layout, mapped, None, minimum_length=100, timeout=0)
    assert result["circuit"] == layout["circuit"]
    assert result["tunnels"]["budget_exhausted"]
