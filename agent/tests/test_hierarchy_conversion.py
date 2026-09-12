"""Native hierarchy must agree with original HDL and retain portable identities."""
import hashlib
import json
from pathlib import Path

import pytest

from verilog2digital.conversion import convert
from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.project import Project
from test_conversion import conversion_adapter, ForwardingAdapter, assert_no_published_result


FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/verilog/hierarchy"
POSITIVE = ("repeated_logic", "parameter_specializations", "nested_arithmetic", "stateful_children",
            "controller_datapath", "port_aliases", "generated_children", "unused_state_output", "sized_parameters")


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def project(name):
    return Project([FIXTURES / f"{name}.v"], top=name)


@pytest.mark.parametrize("name", POSITIVE)
def test_hierarchy_corpus_validates_all_local_canvases_and_expanded_behavior(name, tmp_path, conversion_adapter):
    result = convert(project(name), tmp_path / name, adapter=conversion_adapter, random_count=24)
    report, manifest, package = (document(result[key]) for key in ("validation", "manifest", "package"))
    assert result["state"] == "ready" and manifest["profile"] == "hierarchy-v1"
    for check in ("components", "topology", "bits", "model", "geometry", "behavior"):
        assert report[check]["valid"], report[check]
    assert all(row["valid"] for row in report["modules"].values())
    assert report["hierarchy"]["module_count"] == len(package["specializations"]) >= 2
    assert len(package["modules"]) == len(package["specializations"]) - 1
    assert all("/" not in item["type"] and "\\" not in item["type"] for item in package["modules"])
    assert all("root" not in item for item in package["modules"])
    assert all(isinstance(item["hdl_path"], list) for item in package["occurrences"])
    for artifact in manifest["artifacts"].values():
        path = Path(result["directory"]) / artifact["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    if name == "stateful_children":
        proof = report["behavior"]["initialization"]
        assert proof["register_count"] == 2 and proof["known_register_bits"] == 16
        defaults = report["behavior"]["digital_assumptions"]["initialization"]["native_defaults"]
        assert len(defaults) == 2 and all("." in identifier for identifier in defaults)
    if name == "parameter_specializations":
        children = [item for item in package["specializations"] if item["name"] == "hier_add"]
        assert len(children) == 2 and children[0]["file"] != children[1]["file"]
        assert children[0]["display_name"] != children[1]["display_name"]


@pytest.mark.parametrize("name", ("repeated_logic", "port_aliases", "stateful_children"))
def test_flatten_option_preserves_original_behavior(name, tmp_path, conversion_adapter):
    hierarchical = convert(project(name), tmp_path / "hierarchy", adapter=conversion_adapter, random_count=8)
    trace_path = Path(hierarchical["directory"]) / "work/trace.json"
    trace = document(trace_path) if trace_path.exists() else None
    flat = convert(project(name), tmp_path / "flat", adapter=conversion_adapter, random_count=8,
                   flatten=True, sequential_trace=trace)
    assert "package" in hierarchical and "package" not in flat
    assert document(hierarchical["validation"])["behavior"]["valid"]
    assert document(flat["validation"])["behavior"]["valid"]
    first = document(Path(hierarchical["directory"]) / "work/reference-samples.json")["samples"]
    second = document(Path(flat["directory"]) / "work/reference-samples.json")["samples"]
    assert first == second


def test_hierarchy_native_artifacts_are_byte_deterministic(tmp_path, conversion_adapter):
    results = [convert(project("parameter_specializations"), tmp_path / name,
                       adapter=conversion_adapter, random_count=0) for name in ("first", "second")]
    packages = [document(result["package"]) for result in results]
    assert packages[0] == packages[1]
    for item in packages[0]["snapshot"]["files"]:
        assert (Path(results[0]["directory"]) / item["path"]).read_bytes() == (Path(results[1]["directory"]) / item["path"]).read_bytes()
    for key in ("preview", "svg"):
        assert Path(results[0][key]).read_bytes() == Path(results[1][key]).read_bytes()


def test_unknown_state_behind_unused_child_output_is_rejected(tmp_path, conversion_adapter):
    destination = tmp_path / "unknown"
    with pytest.raises(FrontendError) as caught:
        convert(project("hidden_child_state"), destination, adapter=conversion_adapter)
    assert caught.value.code == "UNINITIALIZED_STATE"
    assert_no_published_result(destination)


@pytest.mark.parametrize("artifact", ("child", "sidecar", "top"))
def test_package_change_during_render_never_publishes_ready(artifact, tmp_path, conversion_adapter):
    class MutatingAdapter(ForwardingAdapter):
        def call(self, method, params, **options):
            result = self.native.call(method, params, **options)
            if method == "circuit.render" and params["format"] == "png":
                root = Path(params["package"]["root"])
                name = "circuit.dig" if artifact == "top" else params["package"]["modules"][0]["type"]
                target = root / (name + ".ids.json" if artifact == "sidecar" else name)
                target.write_bytes(target.read_bytes() + b" ")
            return result
    destination = tmp_path / artifact
    with pytest.raises(FrontendError) as caught:
        convert(project("repeated_logic"), destination, adapter=MutatingAdapter(conversion_adapter))
    assert caught.value.code == "ARTIFACT_CHANGED"
    assert_no_published_result(destination)


def test_top_change_after_final_inspection_cannot_become_snapshot_baseline(tmp_path, conversion_adapter):
    class MutatingAdapter(ForwardingAdapter):
        def call(self, method, params, **options):
            result = self.native.call(method, params, **options)
            if method == "circuit.inspect" and Path(params["path"]).name == "circuit.dig":
                self.top_inspections = getattr(self, "top_inspections", 0) + 1
                if self.top_inspections == 2:
                    target = Path(params["path"])
                    target.write_bytes(target.read_bytes() + b" ")
            return result
    destination = tmp_path / "changed-top"
    with pytest.raises(FrontendError) as caught:
        convert(project("repeated_logic"), destination, adapter=MutatingAdapter(conversion_adapter))
    assert caught.value.code == "ARTIFACT_CHANGED"
    assert_no_published_result(destination)
