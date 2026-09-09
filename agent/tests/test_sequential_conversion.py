"""Original HDL, native feedback wiring and edge simulation must agree together."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from test_conversion import conversion_adapter, ForwardingAdapter, assert_no_published_result


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/sequential"
POSITIVE = ("d_register", "enabled_register", "counter", "shift_register", "fsm", "simultaneous",
            "enable_before_reset", "active_low_reset", "wide_register64", "constant_register")


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def project(name):
    return Project([FIXTURES / f"{name}.v"], top=name)


@pytest.mark.parametrize("name", POSITIVE)
def test_sequential_corpus_matches_original_hdl_after_proven_initialization(name, tmp_path, conversion_adapter):
    result = convert(project(name), tmp_path / name, adapter=conversion_adapter, random_count=24)
    report = document(result["validation"])
    assert report["valid"]
    assert all(report[key]["valid"] for key in ("bits", "topology", "components", "geometry", "model", "behavior"))
    behavior = report["behavior"]
    assert behavior["coverage"] == "sampled_cycles" and not behavior["exhaustive"]
    proof = behavior["initialization"]
    assert proof["valid"] and proof["internal_state_forcing"] is False
    assert proof["initial_state"] == "all_retained_register_bits_unknown"
    assert proof["known_register_bits"] == proof["register_bits"]
    assert proof["known_register_count"] == proof["register_count"]
    work = Path(result["directory"]) / "work"
    trace = document(work / "trace.json")
    assert proof["prefix"] == trace["initialization"]["cycles"]
    assert len(document(work / "digital-samples.json")["samples"]) == 3 * len(trace["cycles"])
    mapped = document(work / "mapped.json")
    assert mapped["profile"] == "sequential-v1"
    assert any(item["type"] == "D_FF" for item in mapped["instances"])
    assert document(result["manifest"])["profile"] == "sequential-v1"
    reopened = conversion_adapter.call("circuit.inspect", {"path": result["circuit"], "sidecar_path": result["sidecar"]})
    assert reopened["model"]["valid"]


@pytest.mark.parametrize("name", ["bare_counter", "hidden_uninitialized"])
def test_uninitialized_state_cannot_be_hidden_by_native_default_zero(name, tmp_path, conversion_adapter):
    destination = tmp_path / name
    with pytest.raises(FrontendError) as error:
        convert(project(name), destination, adapter=conversion_adapter)
    assert error.value.code == "UNINITIALIZED_STATE"
    failure = assert_no_published_result(destination)
    assert failure["stage"] == "validating_initialization"
    assert not (destination / "work/candidate.dig").exists()


def test_explicit_trace_preserves_enable_before_reset_priority(tmp_path, conversion_adapter):
    trace = {"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
             "initialization": {"kind": "input_sequence", "cycles": [
                 {"values": {"rst": 0, "en": 1, "d": 3}}]},
             "cycles": [{"values": values} for values in (
                 {"rst": 1, "en": 0, "d": 255}, {"rst": 1, "en": 1, "d": 255},
                 {"rst": 0, "en": 0, "d": 0}, {"rst": 0, "en": 1, "d": 128})]}
    result = convert(project("enable_before_reset"), tmp_path / "priority", adapter=conversion_adapter,
                     sequential_trace=trace)
    samples = document(Path(result["directory"]) / "work/reference-samples.json")["samples"]
    assert [row["values"]["q"] for row in samples if row["phase"] == "after_rise"] == [3, 85, 85, 128]
    assert document(result["validation"])["behavior"]["valid"]


def test_repeated_sequential_conversion_has_identical_native_artifacts(tmp_path, conversion_adapter):
    first = convert(project("counter"), tmp_path / "first", adapter=conversion_adapter, random_count=0)
    second = convert(project("counter"), tmp_path / "second", adapter=conversion_adapter, random_count=0)
    for artifact in ("circuit", "sidecar", "preview", "svg"):
        assert Path(first[artifact]).read_bytes() == Path(second[artifact]).read_bytes(), artifact


@pytest.mark.parametrize("phase", ["before_rise", "after_rise", "after_fall"])
def test_wrong_native_state_at_any_phase_prevents_publication(phase, tmp_path, conversion_adapter):
    class CorruptState(ForwardingAdapter):
        def call(self, method, params, **options):
            result = super().call(method, params, **options)
            if method == "circuit.simulate_cycles":
                result = deepcopy(result)
                sample = next(row for row in result["samples"] if row["phase"] == phase)
                identifier = next(iter(sample["values"]))
                sample["values"][identifier] = hex(int(sample["values"][identifier], 16) ^ 1)
            return result

    destination = tmp_path / phase
    with pytest.raises(FrontendError) as error:
        convert(project("d_register"), destination, adapter=CorruptState(conversion_adapter), random_count=0)
    assert error.value.code == "BEHAVIOR_VALIDATION"
    assert assert_no_published_result(destination)["stage"] == "validating_behavior"


def test_clock_tunnels_preserve_the_native_cycle_contract(tmp_path, conversion_adapter):
    result = convert(project("simultaneous"), tmp_path / "tunnels", adapter=conversion_adapter,
                     layout_strategy="reserved", tunnels=True, random_count=0)
    manifest = document(result["manifest"])
    assert manifest["behavior"]["valid"]
    assert manifest["layout"]["tunnels"]["nets"]
    mapped = document(Path(result["directory"]) / "work/mapped.json")
    clock_net = next(net["id"] for net in mapped["nets"] if net["bits"] == [mapped["clock"]["bit"]])
    assert clock_net in {net["net_id"] for net in manifest["layout"]["tunnels"]["nets"]}


def test_a_sequential_trace_is_not_silently_ignored_for_combinational_hdl(tmp_path, conversion_adapter):
    source = tmp_path / "buffer.v"
    source.write_text("module buffer(input a,output y); assign y=a; endmodule\n", encoding="utf-8")
    with pytest.raises(FrontendError) as error:
        convert(Project([source]), tmp_path / "unexpected", adapter=conversion_adapter, sequential_trace={})
    assert error.value.code == "UNEXPECTED_TRACE"
