"""Fixed layout corpus checked after Digital serialization and normalization."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import time

import pytest

from verilog2digital.adapter import Adapter
from verilog2digital.layout.general import layout_general


ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("layout_benchmark", ROOT / "scripts/benchmark_layout.py")
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


@pytest.fixture(scope="module")
def layout_adapter():
    with Adapter() as adapter:
        yield adapter


@pytest.fixture(scope="module")
def corpus(layout_adapter, tmp_path_factory):
    directory = tmp_path_factory.mktemp("general-layout-corpus")
    cases = {}

    def get(name):
        if name not in cases:
            case_dir = directory / name
            mapped = benchmark.load_case(name, layout_adapter, case_dir / "input")
            report = benchmark.benchmark_case(
                name, mapped, layout_adapter, case_dir,
                # Allow 10 seconds for the 248-component stress case on a cold or busy JVM.
                timeout=10 if name == "near_limit_248" else 20,
                render=name == "compact_alu",
            )
            cases[name] = mapped, report, case_dir
        return cases[name]

    return get


@pytest.mark.parametrize("name", benchmark.CASES)
def test_mandatory_corpus_preserves_native_components_geometry_and_bits(name, corpus):
    mapped, report, _ = corpus(name)
    assert report["valid"], json.dumps(report, indent=2)
    assert 1 <= report["components"] <= 250
    assert mapped["nets"]
    for result in report["results"].values():
        assert set(result["checks"]) == {
            "components", "topology", "bits", "model", "geometry", "proposed_geometry"
        }
        assert all(check["valid"] for check in result["checks"].values())
        assert all(result["metrics"][metric] >= 0 for metric in
                   ("bounding_area", "wire_length", "bends", "crossings", "segments"))
    general = report["results"]["general"]
    assert report["fallback_count"] == int(general["fallback_used"])
    if general["fallback_used"]:
        assert general["fallback_reason"]
        assert general["strategy"] == report["results"]["reserved"]["strategy"]
        assert any(item["code"] == "reserved_layout_fallback" for item in general["diagnostics"])
    else:
        assert general["fallback_reason"] is None
        assert general["strategy"] == "elk-orthogonal-v1"
    # Allow fallback time beyond the compact-routing budget.
    bound = report["timeout_seconds"] + 3 * report["results"]["reserved"]["runtime_seconds"] + 10
    assert general["runtime_seconds"] <= bound
    if name == "near_limit_248":
        assert len(mapped["instances"]) == 248
        assert mapped["profile"] == "datapath-v1"


def test_compact_alu_has_measured_visual_benefit(corpus):
    _, report, _ = corpus("compact_alu")
    assert report["valid"]
    assert not report["results"]["general"]["fallback_used"]
    ratios = report["general_to_reserved_ratios"]
    assert min(ratios["bounding_area"], ratios["wire_length"]) <= 0.9, ratios
    for result in report["results"].values():
        assert Path(result["preview"]["png"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert "<svg" in Path(result["preview"]["svg"]).read_text(encoding="utf-8")


def test_general_alu_serialized_artifacts_are_deterministic(corpus, layout_adapter, tmp_path):
    mapped, report, directory = corpus("compact_alu")
    before = deepcopy(mapped)
    layout = layout_general(mapped, layout_adapter, seed=1)
    assert not layout["fallback_used"], layout["diagnostics"]
    verified = benchmark.verify_layout(mapped, layout, layout_adapter, tmp_path)
    assert verified["valid"], verified["checks"]
    assert mapped == before
    original = json.loads((directory / "general/candidate.json").read_text(encoding="utf-8"))
    assert layout["circuit"] == original
    for key in ("circuit", "sidecar"):
        assert Path(verified[key]).read_bytes() == Path(report["results"]["general"][key]).read_bytes()


def test_accidental_wire_join_is_rejected_by_reloaded_native_topology(corpus, layout_adapter, tmp_path):
    mapped, report, directory = corpus("chain_small")
    assert report["valid"]
    circuit = json.loads((directory / "general/candidate.json").read_text(encoding="utf-8"))
    inspection = json.loads((directory / "general/inspection.json").read_text(encoding="utf-8"))
    descriptors = inspection["instances"]
    input_ids = [port["instance_id"] for port in mapped["inputs"]]
    assert len(input_ids) == 2
    pins = [next(item for item in descriptors if item["id"] == identifier)["pins"][0]["position"]
            for identifier in input_ids]
    owner = next(net["id"] for net in mapped["nets"]
                 if any(endpoint["instance_id"] == input_ids[0] for endpoint in net["endpoints"]))
    points = [pins[0], {"x": pins[0]["x"], "y": pins[1]["y"]}, pins[1]]
    for start, end in zip(points, points[1:]):
        if start != end:
            circuit["wires"].append({"from": start, "to": end, "net_id": owner})
    corrupted = benchmark.verify_layout(mapped, {"circuit": circuit, "descriptors": descriptors}, layout_adapter, tmp_path)
    assert not corrupted["valid"]
    assert not corrupted["checks"]["topology"]["valid"], corrupted["checks"]


def test_zero_budget_stress_fallback_is_bounded_and_preserves_intent(corpus, layout_adapter, tmp_path):
    mapped, report, _ = corpus("near_limit_248")

    class NoPlanner:
        def call(self, *_args, **_kwargs):
            pytest.fail("An exhausted compact budget must not start native planning")

    started = time.perf_counter()
    fallback = layout_general(mapped, NoPlanner(), timeout=0)
    elapsed = time.perf_counter() - started
    assert fallback["fallback_used"] and fallback["fallback_reason"]
    assert elapsed <= 3 * report["results"]["reserved"]["runtime_seconds"] + 10
    verified = benchmark.verify_layout(mapped, fallback, layout_adapter, tmp_path)
    assert verified["valid"], verified["checks"]
