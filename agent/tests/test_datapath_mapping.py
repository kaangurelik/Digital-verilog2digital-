"""Native datapath semantics checked against integer arithmetic, without routing."""
from copy import deepcopy
from itertools import product

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.mapping import MappingError, map_datapath, map_design, validate_datapath
from test_scalar_mapping import design as scalar_design, simulate_mapping


_UNARY = {"$not", "$pos", "$neg", "$logic_not", "$reduce_and", "$reduce_or", "$reduce_bool", "$reduce_xor", "$reduce_xnor"}


def design(kind="$add", a_width=4, b_width=4, y_width=4, a_signed=False, b_signed=False):
    source = scalar_design()
    cursor = 2
    vectors = {}
    source["ports"] = []
    for name, width, direction in (("a", a_width, "input"), ("b", b_width, "input"), ("s", 1, "input"), ("y", y_width, "output")):
        bits = list(range(cursor, cursor + width))
        cursor += width
        vectors[name] = bits
        source["ports"].append({"name": name, "bits": bits, "direction": direction,
                                "offset": 0, "upto": False, "signed": a_signed if name == "a" else b_signed if name == "b" else False, "source": "fixture.v:1"})
    pins = {"A": vectors["a"], "Y": vectors["y"]}
    params = {"A_SIGNED": int(a_signed), "A_WIDTH": a_width, "Y_WIDTH": y_width}
    if kind not in _UNARY:
        pins["B"] = vectors["b"]
        params.update(B_SIGNED=int(b_signed), B_WIDTH=b_width)
    if kind == "$mux":
        pins["S"] = vectors["s"]
        params = {"WIDTH": y_width}
    source["cells"] = [{"id": "c0", "native_name": "operation", "type": kind, "parameters": params,
                        "ports": pins, "port_directions": {pin: "output" if pin == "Y" else "input" for pin in pins},
                        "source": "fixture.v:2", "hierarchy": "top c0"}]
    source["nets"] = []
    return source


@pytest.fixture(scope="module")
def adapter():
    with Adapter() as worker:
        yield worker


def signed(value, width):
    return value - (1 << width) if value & (1 << (width - 1)) else value


def expected(kind, a, b, s, aw, bw, yw, sa, sb):
    common_signed = sa and sb
    left = signed(a, aw) if (sa if kind in _UNARY or kind in {"$shl", "$shr", "$sshl", "$sshr"} else common_signed) else a
    right = signed(b, bw) if common_signed else b
    if kind == "$not": result = ~left
    elif kind == "$pos": result = left
    elif kind == "$neg": result = -left
    elif kind == "$and": result = left & right
    elif kind == "$or": result = left | right
    elif kind == "$xor": result = left ^ right
    elif kind == "$xnor": result = ~(left ^ right)
    elif kind == "$add": result = left + right
    elif kind == "$sub": result = left - right
    elif kind == "$mux": result = b if s else a
    elif kind in {"$eq", "$eqx"}: result = int(left == right)
    elif kind in {"$ne", "$nex"}: result = int(left != right)
    elif kind == "$lt": result = int(left < right)
    elif kind == "$le": result = int(left <= right)
    elif kind == "$gt": result = int(left > right)
    elif kind == "$ge": result = int(left >= right)
    elif kind == "$logic_not": result = int(a == 0)
    elif kind == "$logic_and": result = int(bool(a) and bool(b))
    elif kind == "$logic_or": result = int(bool(a) or bool(b))
    elif kind == "$reduce_and": result = int(a == (1 << aw) - 1)
    elif kind in {"$reduce_or", "$reduce_bool"}: result = int(bool(a))
    elif kind == "$reduce_xor": result = a.bit_count() & 1
    elif kind == "$reduce_xnor": result = 1 ^ (a.bit_count() & 1)
    elif kind in {"$shl", "$sshl"}: result = 0 if b >= max(aw, yw) else left << b
    elif kind == "$sshr" and sa: result = (-1 if left < 0 else 0) if b >= max(aw, yw) else left >> b
    else:
        # Logical right shift uses the expression's context width before truncation.
        result = 0 if b >= max(aw, yw) else (left & ((1 << max(aw, yw)) - 1)) >> b
    return result & ((1 << yw) - 1)


@pytest.mark.parametrize("kind", [
    "$not", "$pos", "$neg", "$and", "$or", "$xor", "$xnor", "$add", "$sub", "$mux",
    "$eq", "$ne", "$eqx", "$nex", "$lt", "$le", "$gt", "$ge", "$logic_not", "$logic_and", "$logic_or",
    "$reduce_and", "$reduce_or", "$reduce_bool", "$reduce_xor", "$reduce_xnor", "$shl", "$shr", "$sshl", "$sshr",
])
def test_all_native_datapath_operations(adapter, tmp_path, kind):
    source = design(kind, a_signed=True, b_signed=True)
    before = deepcopy(source)
    mapped = map_datapath(source, adapter)
    assert source == before
    vectors = [dict(zip(("a", "b", "s"), values)) for values in product(range(16), range(16), (0, 1))]
    actual = simulate_mapping(mapped, adapter, tmp_path, vectors)
    assert actual == [{"y": expected(kind, **v, aw=4, bw=4, yw=4, sa=True, sb=True)} for v in vectors]


@pytest.mark.parametrize("kind,aw,bw,yw,sa,sb", [
    ("$add", 3, 5, 8, True, True), ("$sub", 3, 5, 2, True, True),
    ("$add", 3, 5, 8, True, False), ("$and", 3, 5, 8, True, True),
    ("$xnor", 3, 5, 8, True, False), ("$not", 3, 5, 8, True, False),
    ("$neg", 3, 5, 8, True, False), ("$lt", 3, 5, 8, True, True),
    ("$ge", 3, 5, 8, True, False), ("$shr", 3, 8, 7, True, False),
    ("$sshr", 7, 8, 3, True, False), ("$shl", 3, 8, 7, True, False),
    ("$sshr", 3, 8, 7, True, False), ("$sshr", 3, 8, 7, False, False),
    ("$reduce_xnor", 1, 1, 4, False, False),
])
def test_mixed_sizes_sign_extension_truncation_and_oversized_shifts(adapter, tmp_path, kind, aw, bw, yw, sa, sb):
    source = design(kind, aw, bw, yw, sa, sb)
    mapped = map_datapath(source, adapter)
    values_a = sorted({0, 1, (1 << aw) - 1, 1 << (aw - 1), (1 << (aw - 1)) - 1})
    values_b = sorted({0, 1, (1 << bw) - 1, 1 << (bw - 1), (1 << (bw - 1)) - 1})
    vectors = [{"a": a, "b": b, "s": 0} for a, b in product(values_a, values_b)]
    actual = simulate_mapping(mapped, adapter, tmp_path, vectors)
    assert actual == [{"y": expected(kind, **v, aw=aw, bw=bw, yw=yw, sa=sa, sb=sb)} for v in vectors]


@pytest.mark.parametrize("kind", ["$add", "$sub", "$lt", "$ge", "$shl", "$shr", "$sshr"])
def test_64bit_extremes(adapter, tmp_path, kind):
    mapped = map_datapath(design(kind, 64, 64, 64, True, True), adapter)
    values = [0, 1, 63, 64, 65, (1 << 63) - 1, 1 << 63, (1 << 64) - 1]
    vectors = [{"a": a, "b": b, "s": 0} for a, b in product(values, values)]
    actual = simulate_mapping(mapped, adapter, tmp_path, vectors)
    assert actual == [{"y": expected(kind, **v, aw=64, bw=64, yw=64, sa=True, sb=True)} for v in vectors]


def test_permutation_duplicate_bits_constants_and_unused_input(adapter, tmp_path):
    source = design()
    source["cells"] = []
    source["ports"][-1]["bits"] = [source["ports"][0]["bits"][3], "1", source["ports"][0]["bits"][3], "0"]
    mapped = map_datapath(source, adapter)
    vectors = [{"a": a, "b": 0, "s": 0} for a in range(16)]
    assert simulate_mapping(mapped, adapter, tmp_path, vectors) == [{"y": 2 | ((v["a"] >> 3) * 5)} for v in vectors]
    assert all(binding["width"] == len(binding["bits"]) for binding in mapped["pin_bindings"])


def test_datapath_is_deterministic_and_dispatch_preserves_scalar(adapter):
    source = design()
    mapped = map_datapath(source, adapter)
    source["ports"].reverse()
    assert map_datapath(source, adapter) == mapped
    assert map_design(scalar_design(), adapter)["profile"] == "scalar-v1"
    assert map_design(source, adapter)["profile"] == "datapath-v1"


@pytest.mark.parametrize("mutation", ["width", "pin_bits", "splitter_offset", "splitter_transfer", "missing_net", "missing_binding", "constant", "driver"])
def test_corrupt_datapath_contract_is_rejected(adapter, mutation):
    mapped = map_datapath(design(), adapter)
    if mutation == "width": mapped["pin_bindings"][0]["width"] = 64
    elif mutation == "pin_bits": next(binding for binding in mapped["pin_bindings"] if binding["width"] > 1)["bits"].reverse()
    elif mutation == "splitter_offset":
        next(d for d in mapped["descriptors"] if d["type"] == "Splitter")["splitter_bindings"][0]["offset"] += 1
    elif mutation == "splitter_transfer":
        split = next(d for d in mapped["descriptors"] if d["type"] == "Splitter")
        next(b for b in mapped["pin_bindings"] if b["instance_id"] == split["id"] and b["width"] > 1)["bits"].reverse()
    elif mutation == "missing_net": mapped["nets"].pop()
    elif mutation == "missing_binding": mapped["pin_bindings"].pop()
    elif mutation == "constant":
        const = next(i for i in mapped["instances"] if i["type"] == "Const")
        const["attributes"]["Value"] = "0x1"
        next(d for d in mapped["descriptors"] if d["id"] == const["id"])["attributes"]["Value"] = "0x1"
    else:
        net = next(n for n in mapped["nets"] if len(n["endpoints"]) > 1)
        net["endpoints"] = [e for e in net["endpoints"] if e["direction"] != "output"]
    with pytest.raises(MappingError):
        validate_datapath(mapped)


def test_unsupported_operations_fail_before_native_work():
    class NoCalls:
        def call(self, *_):
            pytest.fail("Unsupported cells must fail before native requests")
    with pytest.raises(MappingError, match="no datapath-v1 mapping"):
        map_datapath(design("$mul"), NoCalls())


def test_null_native_width_is_checked_against_independent_binding_contract(adapter):
    class NullInputs:
        def call(self, method, params):
            result = adapter.call(method, params)
            for pin in result["pins"]:
                if pin["direction"] == "input":
                    pin["width"] = None
            return result
    validate_datapath(map_datapath(design(), NullInputs()))
