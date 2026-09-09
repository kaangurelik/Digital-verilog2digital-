from copy import deepcopy

from verilog2digital.validation.topology import compare_components, compare_topology


def endpoint(identifier, direction="input", pin="D"):
    return {"instance_id": identifier, "direction": direction, "pin": pin}


def inspected(groups, disconnected=()):
    return {"topology_available": True,
            "nets": [{"id": str(index), "endpoints": group} for index, group in enumerate(groups)],
            "unconnected": list(disconnected)}


def test_equal_net_and_fanout_counts_do_not_hide_swapped_connections():
    a, b, c, d = [endpoint(identifier) for identifier in "abcd"]
    report = compare_topology([[a, b], [c, d]], inspected([[a, c], [b, d]]))
    assert not report["valid"]
    assert any(error["code"] == "connection_mismatch" for error in report["diagnostics"])


def test_order_and_native_net_names_do_not_affect_the_partition():
    a, b, c = [endpoint(identifier) for identifier in "abc"]
    assert compare_topology([[a, b, c]], inspected([[c, a, b]]))["valid"]


def test_separate_disconnected_pins_remain_distinct():
    a, b = endpoint("a"), endpoint("b")
    assert compare_topology([[a], [b]], inspected([], [a, b]))["valid"]
    assert not compare_topology([[a, b]], inspected([], [a, b]))["valid"]


def test_splitter_names_are_disambiguated_by_direction():
    splitter_in = endpoint("split", "input", "0-7")
    splitter_out = endpoint("split", "output", "0-7")
    a, b = endpoint("a"), endpoint("b")
    expected = [[a, splitter_in], [splitter_out, b]]
    assert compare_topology(expected, inspected(expected))["valid"]
    assert not compare_topology(expected, inspected([[a, splitter_out], [splitter_in, b]]))["valid"]


def test_missing_unused_pin_is_detected():
    a, b = endpoint("a"), endpoint("b")
    report = compare_topology([[a], [b]], inspected([], [a]))
    assert not report["valid"]
    assert report["diagnostics"][0]["code"] == "missing_endpoint"


def test_duplicate_endpoint_and_orphan_wire_are_invalid():
    a = endpoint("a")
    assert not compare_topology([[a]], inspected([[a], [a]]))["valid"]
    assert not compare_topology([[a]], inspected([[a], []]))["valid"]


def test_omitted_actual_pin_cannot_make_the_oracle_pass():
    a = endpoint("a")
    report = inspected([[a]])
    report["instances"] = [{"id": "a", "pins": [{"pin": "D", "direction": "input"},
                                                   {"pin": "Q", "direction": "output"}]}]
    result = compare_topology([[a]], report)
    assert not result["valid"]
    assert any(error["code"] == "incomplete_inspection" for error in result["diagnostics"])


def test_unavailable_topology_is_never_accepted():
    report = inspected([])
    report["topology_available"] = False
    assert not compare_topology([], report)["valid"]


def component():
    return {"id": "u", "type": "Const", "attributes": {"Bits": 8, "Value": "0x0"},
            "rotation": 0, "position": {"x": 0, "y": 0},
            "pins": [{"direction": "output", "pin": "out", "width": 8}]}


def test_same_topology_does_not_hide_changed_constant_value():
    expected = component()
    actual = deepcopy(expected)
    actual["attributes"]["Value"] = "0xff"
    assert not compare_components([expected], [actual])["valid"]


def test_geometry_can_move_without_changing_component_behavior():
    expected = component()
    actual = deepcopy(expected)
    actual["position"]["x"] = 400
    assert compare_components([expected], [actual])["valid"]


def test_resolved_width_and_splitter_bit_bindings_are_checked():
    expected = component()
    actual = deepcopy(expected)
    actual["pins"][0]["width"] = 4
    assert not compare_components([expected], [actual])["valid"]
    expected["splitter_bindings"] = [{"direction": "output", "pin": "out", "offset": 0, "width": 8}]
    actual = deepcopy(expected)
    actual["splitter_bindings"][0]["offset"] = 1
    assert not compare_components([expected], [actual])["valid"]
