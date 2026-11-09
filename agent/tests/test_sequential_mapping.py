"""Register mapping and explicit clock/reset/enable semantics through native pins."""
from copy import deepcopy

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.layout.general import layout_general
from verilog2digital.layout.scalar import layout_reserved
from verilog2digital.mapping import MappingError, map_design, map_sequential, validate_sequential
from verilog2digital.validation.bits import compare_bit_topology
from verilog2digital.validation.topology import compare_topology
from test_scalar_mapping import design as scalar_design, simulate_mapping


def register_design(width=4, *, enable=False, reset=False):
    design = scalar_design()
    cursor = 2
    vectors = {}
    design["ports"] = []
    for name, bits, direction in (("clk", 1, "input"), ("d", width, "input"), ("en", 1, "input"), ("rst", 1, "input"), ("q", width, "output")):
        vector = list(range(cursor, cursor+bits))
        cursor += bits
        vectors[name] = vector
        design["ports"].append({"name": name, "bits": vector, "direction": direction, "offset": 0,
                                "upto": False, "signed": False, "source": "register.v:1"})
    design["nets"], design["cells"] = [], []
    def cell(identifier, kind, ports, parameters, output):
        design["cells"].append({"id": identifier, "native_name": identifier, "type": kind, "ports": deepcopy(ports),
                                "parameters": parameters, "port_directions": {pin: "output" if pin == output else "input" for pin in ports},
                                "source": "register.v:2", "hierarchy": "top "+identifier})
    data = vectors["d"]
    if enable:
        chosen = list(range(cursor, cursor+width))
        cursor += width
        cell("enable", "$mux", {"A": vectors["q"], "B": data, "S": vectors["en"], "Y": chosen}, {"WIDTH": width}, "Y")
        data = chosen
    if reset:
        chosen = list(range(cursor, cursor+width))
        cursor += width
        reset_bits = [str((5 >> index) & 1) for index in range(width)]
        cell("reset", "$mux", {"A": data, "B": reset_bits, "S": vectors["rst"], "Y": chosen}, {"WIDTH": width}, "Y")
        data = chosen
    cell("state", "$dff", {"CLK": vectors["clk"], "D": data, "Q": vectors["q"]}, {"WIDTH": width, "CLK_POLARITY": 1}, "Q")
    return design


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as worker:
        yield worker


@pytest.mark.parametrize("width", [1, 4, 64])
@pytest.mark.parametrize("enable,reset", [(False, False), (True, False), (False, True), (True, True)])
def test_native_register_edges_hold_and_nonzero_reset_priority(adapter, tmp_path, width, enable, reset):
    source = register_design(width, enable=enable, reset=reset)
    before = deepcopy(source)
    mapped = map_sequential(source, adapter)
    assert source == before
    assert mapped["clock"] == {"port": "clk", "instance_id": "in0000", "bit": 2, "edge": "rising"}
    mask = (1 << width)-1
    cycles = [(3 & mask, 1, 0), (mask, 0, 0), (0, 0, 1), (1, 1, 1), (mask, 1, 0), (0, 0, 0)]
    vectors, expected = [], []
    state = 0  # Tests native bootstrap explicitly; converter separately proves HDL warmup.
    for data, en, rst in cycles:
        values = {"d": data, "en": en, "rst": rst}
        vectors.append({**values, "clk": 0})
        expected.append({"q": state})
        if reset and rst:
            state = 5 & mask
        elif not enable or en:
            state = data
        vectors.append({**values, "clk": 1})
        expected.append({"q": state})
        vectors.append({**values, "clk": 0})
        expected.append({"q": state})
    assert simulate_mapping(mapped, adapter, tmp_path, vectors) == expected
    register = mapped["registers"][0]
    complement = next(net for net in mapped["nets"] if any(endpoint["instance_id"] == register["instance_id"] and endpoint["pin"] == "~Q" for endpoint in net["endpoints"]))
    assert complement["width"] == width and len(complement["endpoints"]) == 1
    assert set(complement["bits"]).isdisjoint(register["q_bits"])


@pytest.mark.parametrize("router", ["reserved", "general"])
def test_register_feedback_layout_preserves_reloaded_native_topology(adapter, tmp_path, router):
    mapped = map_sequential(register_design(enable=True, reset=True), adapter)
    layout = layout_reserved(mapped) if router == "reserved" else layout_general(mapped, adapter)
    assert layout["geometry"]["valid"]
    if router == "reserved":
        assert layout["strategy"] == "sequential-reserved-tracks-v1"
    emitted = adapter.call("circuit.emit", {"circuit": layout["circuit"], "path": str(tmp_path / "feedback.dig")})
    inspection = adapter.call("circuit.inspect", {key: emitted[key] for key in ("path", "sidecar_path")})
    assert inspection["model"]["valid"], inspection["model"]
    assert compare_topology([net["endpoints"] for net in mapped["nets"]], inspection)["valid"]
    assert compare_bit_topology(mapped, inspection)["valid"]


def test_register_feedback_shares_q_bus_and_splits_only_reset_constants(adapter):
    mapped = map_sequential(register_design(enable=True, reset=True), adapter)
    types = {item["id"]: item["type"] for item in mapped["instances"]}
    register = mapped["registers"][0]
    q_bus = next(net for net in mapped["nets"] if net["bits"] == register["q_bits"])
    assert sorted((types[item["instance_id"]], item["pin"]) for item in q_bus["endpoints"]) == [
        ("D_FF", "Q"), ("Multiplexer", "in_0"), ("Out", "in")]
    splitters = [origin for origin in mapped["origins"] if types[origin["instance_id"]] == "Splitter"]
    assert [origin["purpose"] for origin in splitters] == ["merge"]
    merged = next(net for net in mapped["nets"] if any(item["instance_id"] == splitters[0]["instance_id"] for item in net["endpoints"]) and net["width"] > 1)
    assert merged["bits"] == ["1", "0", "1", "0"]


def test_sequential_dispatch_and_metadata_are_deterministic(adapter):
    source = register_design(enable=True)
    mapped = map_design(source, adapter)
    assert mapped["profile"] == "sequential-v1"
    source["ports"].reverse()
    source["cells"].reverse()
    assert map_sequential(source, adapter) == mapped


@pytest.mark.parametrize("mutation", ["clock_width", "clock_as_data", "clock_output", "clock_edge", "multiple_clocks"])
def test_unsupported_clock_forms_fail_before_native_requests(mutation):
    source = register_design()
    register = source["cells"][-1]
    if mutation == "clock_width":
        source["ports"][0]["bits"].append(100)
    elif mutation == "clock_as_data":
        register["ports"]["D"][0] = 2
    elif mutation == "clock_output":
        source["ports"].append({**deepcopy(source["ports"][0]), "name": "clock_alias", "direction": "output"})
    elif mutation == "clock_edge":
        register["parameters"]["CLK_POLARITY"] = 0
    else:
        other = deepcopy(register)
        other.update(id="other", native_name="other")
        other["ports"]["CLK"] = source["ports"][2]["bits"]
        other["ports"]["Q"] = [100, 101, 102, 103]
        source["cells"].append(other)
    class NoCalls:
        def call(self, *_args, **_kwargs):
            pytest.fail("Unsupported clock must fail before native work")
    with pytest.raises(MappingError) as error:
        map_sequential(source, NoCalls())
    assert error.value.code in {"UNSUPPORTED_CLOCK", "UNSUPPORTED_CLOCK_USE"}


@pytest.mark.parametrize("mutation", ["clock_bit", "clock_port", "register_missing", "q_metadata", "default", "complement_alias", "complement_driver"])
def test_sequential_metadata_corruption_is_rejected(adapter, mutation):
    mapped = map_sequential(register_design(), adapter)
    register = mapped["registers"][0]
    if mutation == "clock_bit": mapped["clock"]["bit"] = 3
    elif mutation == "clock_port": mapped["clock"]["port"] = "d"
    elif mutation == "register_missing": mapped["registers"] = []
    elif mutation == "q_metadata": register["q_bits"].reverse()
    elif mutation == "default":
        for collection in ("instances", "descriptors"):
            next(item for item in mapped[collection] if item["id"] == register["instance_id"])["attributes"]["Default"] = "0x1"
    elif mutation == "complement_alias":
        next(binding for binding in mapped["pin_bindings"] if binding["instance_id"] == register["instance_id"] and binding["pin"] == "~Q")["bits"] = list(register["q_bits"])
    else:
        net = next(net for net in mapped["nets"] if any(endpoint["instance_id"] == register["instance_id"] and endpoint["pin"] == "~Q" for endpoint in net["endpoints"]))
        net["endpoints"].append(deepcopy(net["endpoints"][0]))
    with pytest.raises(MappingError):
        validate_sequential(mapped)
