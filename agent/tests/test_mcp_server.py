"""Drive the MCP server in process through the SDK client, over both protocol eras."""
import base64
import json
from pathlib import Path
import shutil
import threading
import time

import _winapi
import anyio
from mcp import Client
import pytest

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.jobs import manager
from verilog2digital.mcp_server import ServerState, build_server, refinement, results, runner, server as mcp_server
from verilog2digital.mcp_server.policy import PathPolicy


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures"
TOOLS = ["doctor", "inspect_design", "convert_design", "validate_result", "get_result", "get_preview", "job_status",
         "cancel_job", "refinement_start", "refinement_propose", "refinement_finish"]
READ_ONLY = {"doctor", "inspect_design", "validate_result", "get_result", "get_preview", "job_status"}
COMPACT = {"in0000": (0, 0), "in0001": (0, 80), "in0002": (0, 160), "g0000": (180, 40), "out0000": (360, 40)}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("V2D_JOB_ROOT", str(tmp_path / "jobs"))
    # Concurrent edits elsewhere in the tree must not turn these runs into TOOL_CHANGED.
    fingerprint = manager._fingerprint()
    monkeypatch.setattr(manager, "_fingerprint", lambda: fingerprint)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    for name in ("scalar/full_adder.v", "frontend/two_tops.v", "frontend/malformed.v", "hierarchy/repeated_logic.v"):
        shutil.copy(FIXTURES / "verilog" / name, allowed)
    shutil.copy(FIXTURES / "mcp/refine_and.v", allowed)
    return allowed


@pytest.fixture
def state(workspace):
    value = ServerState([workspace])
    yield value
    value.close()


def run(state, steps, mode="auto"):
    async def main():
        async with Client(build_server(state), mode=mode) as client:
            return await steps(client)
    return anyio.run(main)


def call(state, name, arguments=None, mode="auto"):
    return run(state, lambda client: client.call_tool(name, arguments or {}), mode)


def failed(result, code):
    assert result.is_error, result.structured_content
    assert result.structured_content["diagnostic"]["code"] == code, result.structured_content
    assert json.loads(result.content[-1].text) == result.structured_content
    return result.structured_content["diagnostic"]


@pytest.mark.parametrize("mode,version", [("2026-07-28", "2026-07-28"), ("legacy", "2025-11-25")])
def test_tools_are_listed_in_a_fixed_order_with_bounded_schemas_for_both_eras(state, mode, version):
    async def steps(client):
        assert client.protocol_version == version
        return await client.list_tools(), await client.list_tools()
    first, second = run(state, steps, mode)
    assert [tool.name for tool in first.tools] == TOOLS == [tool.name for tool in second.tools]
    for tool in first.tools:
        assert tool.title and len(tool.description) > 80
        assert tool.input_schema["type"] == "object" and tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["required"] == ["state"]
        annotations = tool.annotations
        assert annotations.open_world_hint is False and annotations.destructive_hint is False
        assert annotations.read_only_hint is (tool.name in READ_ONLY)
        assert annotations.idempotent_hint is (tool.name in READ_ONLY | {"cancel_job"})
    convert = next(tool for tool in first.tools if tool.name == "convert_design").input_schema["properties"]
    assert convert["spacing"] == {**convert["spacing"], "minimum": 40, "maximum": 400, "multipleOf": 20}
    assert convert["random_vectors"]["maximum"] == 2048 and convert["layout_timeout"]["maximum"] == 120
    assert not {"llm", "refinement", "refinement_attempts", "replay_actions", "layout_request"} & set(convert)
    assert "untrusted data" in next(tool for tool in first.tools if tool.name == "inspect_design").description
    propose = next(tool for tool in first.tools if tool.name == "refinement_propose").input_schema
    action = propose["properties"]["response"]["anyOf"][0]
    assert action["properties"]["actions"]["maxItems"] == 8 and "layout_delta" in propose["$defs"]
    start = next(tool for tool in first.tools if tool.name == "refinement_start").description
    assert "4 hours" in start and "least recently used" in start and "already refined" in start


@pytest.mark.parametrize("mode,version", [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
def test_capabilities_advertise_no_change_notifications_or_subscriptions(state, mode, version):
    # The lists never change and no resource publishes updates, in either era.
    async def steps(client):
        return client.protocol_version, client.server_capabilities
    negotiated, capabilities = run(state, steps, mode)
    assert negotiated == version
    assert capabilities.tools.list_changed is not True and capabilities.resources.subscribe is not True
    assert capabilities.resources.list_changed is not True
    assert capabilities.prompts is None or capabilities.prompts.list_changed is not True


def test_resources_expose_the_supported_verilog_rules_and_action_schema(state):
    async def steps(client):
        listed = await client.list_resources()
        return listed, await client.read_resource("v2d://schemas/layout-actions")
    listed, schema = run(state, steps)
    uris = [str(item.uri) for item in listed.resources]
    assert {"v2d://docs/user-guide", "v2d://docs/local-model", "v2d://schemas/layout-actions"} <= set(uris)
    assert json.loads(schema.contents[0].text) == json.loads((ROOT / "schemas/layout-actions.schema.json").read_text(encoding="utf-8"))


def test_doctor_reports_tools_allowed_paths_and_default_limits(state, workspace):
    result = call(state, "doctor")
    document = result.structured_content
    assert not result.is_error and document["state"] == "ready", document
    assert document["paths"] == {"configured": True, "allowed_directories": [str(workspace.resolve())]}
    assert document["default_limits"] == {"deadline_seconds": 300, "memory_mib": 6144, "java_heap_mib": 1024}
    assert document["tools"]["java"]["available"] and document["server"]["transport"] == "stdio"


def test_blank_and_server_owned_allowed_directories_are_refused_at_startup(workspace, tmp_path):
    # Path("") is the working directory; an unset host variable must not allow it.
    for value in ("", " ", "\t"):
        with pytest.raises(FrontendError) as error:
            PathPolicy([value])
        assert error.value.code == "ALLOW_DIR_INVALID"
    for folder in (tmp_path / "jobs", tmp_path / "cache/entries", ROOT / "scripts", ROOT / "agent/src/verilog2digital"):
        folder.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FrontendError) as error:
            ServerState([folder])
        assert error.value.code == "ALLOW_DIR_INVALID" and "own job storage" in error.value.message


def test_cache_and_installation_folders_are_never_user_paths(workspace, tmp_path):
    entry = tmp_path / "cache/entries" / ("a" * 64)
    entry.mkdir(parents=True)
    (entry / "data.v").write_text("module m(input a, output y); assign y = a; endmodule\n", encoding="utf-8")
    state = ServerState([tmp_path, ROOT])
    try:
        for name, arguments in (("get_result", {"path": str(entry)}), ("inspect_design", {"sources": [str(entry / "data.v")]}),
                                ("inspect_design", {"sources": [str(ROOT / "scripts/v2d.py")]}),
                                ("convert_design", {"sources": [str(workspace / "full_adder.v")],
                                                    "output": str(tmp_path / "cache/entries" / ("f" * 64))}),
                                ("convert_design", {"sources": [str(workspace / "full_adder.v")],
                                                    "output": str(ROOT / "agent/src/verilog2digital/out")})):
            assert "own job storage, cache or installation" in failed(call(state, name, arguments), "PATH_NOT_ALLOWED")["message"]
    finally:
        state.close()
    assert not (tmp_path / "cache/entries" / ("f" * 64)).exists() and not (ROOT / "agent/src/verilog2digital/out").exists()


def test_without_allowed_directories_every_path_is_refused_with_setup_advice(workspace):
    state = ServerState([])
    try:
        diagnostic = failed(call(state, "convert_design", {"sources": [str(workspace / "full_adder.v")],
                                                          "output": str(workspace / "out")}), "NO_ALLOWED_DIRECTORIES")
        assert "--allow-dir" in diagnostic["message"] and "VerilogToDigital.exe --mcp" in diagnostic["message"]
        failed(call(state, "get_result", {"path": str(workspace)}), "NO_ALLOWED_DIRECTORIES")
        document = call(state, "doctor").structured_content
        assert document["paths"]["configured"] is False and "--allow-dir" in document["paths"]["configuration_hint"]
    finally:
        state.close()
    assert not (workspace / "out").exists()


def test_paths_must_resolve_inside_an_allowed_directory(state, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.v").write_text("module secret(input a, output y); assign y = a; endmodule\n", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(workspace / "link"))
    for source in (str(outside / "secret.v"), "../outside/secret.v", "link/secret.v"):
        diagnostic = failed(call(state, "inspect_design", {"sources": [source]}), "PATH_NOT_ALLOWED")
        assert str(workspace.resolve()) in diagnostic["message"]
    failed(call(state, "convert_design", {"sources": ["full_adder.v"], "output": "link/out"}), "PATH_NOT_ALLOWED")
    failed(call(state, "convert_design", {"sources": ["full_adder.v"], "output": str(workspace)}), "PATH_NOT_ALLOWED")
    for name in ("full_adder.v:stream", "NUL", "CON.v", "\\\\?\\C:\\design.v", "\\design.v"):
        failed(call(state, "inspect_design", {"sources": [name]}), "INVALID_PATH")
    failed(call(state, "inspect_design", {"sources": ["missing.v"]}), "PATH_NOT_FOUND")
    failed(call(state, "get_preview", {"path": str(outside)}), "PATH_NOT_ALLOWED")
    # Whether a file outside the allowed directories exists is not revealed.
    for source in (str(outside / "missing.v"), str(tmp_path / "no-such-folder/x.v"), "../outside/missing.v", "link/missing.v"):
        failed(call(state, "inspect_design", {"sources": [source]}), "PATH_NOT_ALLOWED")
    assert not (outside / "out").exists() and not (workspace / "out").exists()


def test_include_files_outside_allowed_directories_are_refused_before_publication(state, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.vh").write_text("`define MASK 1'b1\n", encoding="utf-8")
    (workspace / "direct.v").write_text('`include "../outside/secret.vh"\n'
                                        "module direct(input a, output y); assign y = a ^ `MASK; endmodule\n", encoding="utf-8")
    (workspace / "selected.v").write_text("`include `HEADER\n"
                                          "module selected(input a, output y); assign y = a ^ `MASK; endmodule\n", encoding="utf-8")
    for arguments in ({"sources": ["direct.v"]}, {"sources": ["selected.v"], "defines": {"HEADER": '"../outside/secret.vh"'}}):
        diagnostic = failed(call(state, "inspect_design", arguments), "PATH_NOT_ALLOWED")
        assert "secret.vh" in diagnostic["message"]
        failed(call(state, "convert_design", {**arguments, "output": "out"}), "PATH_NOT_ALLOWED")
        assert not (workspace / "out").exists()


def guarded(workspace, tmp_path, guard):
    marker = tmp_path / "elsewhere/marker.vcd"
    marker.parent.mkdir(exist_ok=True)
    (workspace / "guarded.v").write_text(f"module guarded(input [1:0] a, output [1:0] y);\n  assign y = a;\n{guard}\n"
                                         f"  initial begin $dumpfile(\"{marker.as_posix()}\"); $dumpvars(0, guarded); end\n"
                                         "`endif\nendmodule\n", encoding="utf-8")
    return marker


def test_code_that_a_tool_macro_hides_from_the_inspection_never_runs(state, workspace, tmp_path):
    # Every tool preprocesses one design: the reference undefines Icarus's own __ICARUS__, so it skips this block too.
    marker = guarded(workspace, tmp_path, "`ifdef __ICARUS__")
    assert call(state, "convert_design", {"sources": ["guarded.v"], "output": "out", "layout": "reserved"}
                ).structured_content["state"] == "ready"
    assert not marker.exists()
    # The inspection undefines slang's own __slang__, so it checks the block that Yosys and Icarus would compile.
    guarded(workspace, tmp_path, "`ifndef __slang__")
    diagnostic = failed(call(state, "convert_design", {"sources": ["guarded.v"], "output": "other", "layout": "reserved"}),
                        "UNSUPPORTED_INITIALIZATION")
    assert diagnostic["line"] == 4 and not marker.exists() and not (workspace / "other").exists()


def test_invalid_arguments_are_tool_errors_with_stable_codes(state, workspace):
    failed(call(state, "convert_design", {"sources": ["full_adder.v"], "output": "out", "llm": "4b"}), "INVALID_ARGUMENTS")
    assert "spacing" in failed(call(state, "convert_design", {"sources": ["full_adder.v"], "output": "out", "spacing": 30}),
                               "INVALID_ARGUMENTS")["message"]
    failed(call(state, "convert_design", {"output": "out"}), "SOURCE_OR_PROJECT")
    failed(call(state, "inspect_design", {"sources": ["full_adder.v"], "project": "project.json"}), "SOURCE_OR_PROJECT")
    failed(call(state, "convert_design", {"sources": ["full_adder.v"]}), "OUTPUT_REQUIRED")
    failed(call(state, "refinement_propose", {"session": "nope", "response": {}}), "INVALID_ARGUMENTS")
    failed(call(state, "refinement_propose", {"session": "rs-" + "0" * 32, "response": {}}), "REFINEMENT_SESSION_UNKNOWN")
    failed(call(state, "job_status", {"job_id": "0" * 32}), "JOB_UNKNOWN")
    failed(call(state, "cancel_job", {"job_id": "0" * 32}), "JOB_UNKNOWN")
    for name in ("get_result", "get_preview", "validate_result"):
        failed(call(state, name, {"path": "."}), "NO_READY_RESULT")
    (workspace / "empty").mkdir()
    failed(call(state, "refinement_start", {"output": "empty"}), "NO_READY_RESULT")
    failed(call(state, "refinement_start", {"output": "full_adder.v"}), "PATH_NOT_DIRECTORY")
    failed(call(state, "refinement_start", {"output": "out", "max_attempts": 9}), "INVALID_ARGUMENTS")


def test_project_files_use_command_line_merging_path_checks_and_server_limits(state, workspace, tmp_path):
    project = {"schema_version": 1, "sources": ["full_adder.v"], "output": "project-out",
               "conversion": {"layout_strategy": "reserved", "refinement": {"mode": "local"}},
               "resources": {"deadline_seconds": 3600, "memory_mib": 65536, "java_heap_mib": 16384}}
    (workspace / "project.json").write_text(json.dumps(project), encoding="utf-8")
    (workspace / "escape.json").write_text(json.dumps({**project, "output": "../escaped"}), encoding="utf-8")
    (workspace / "root.json").write_text(json.dumps({**project, "output": "."}), encoding="utf-8")
    (tmp_path / "trace.json").write_text("{}", encoding="utf-8")
    (workspace / "trace.json").write_text(json.dumps({**project, "trace": "../trace.json"}), encoding="utf-8")
    for name in ("escape.json", "trace.json", "root.json"):
        failed(call(state, "convert_design", {"project": name}), "PATH_NOT_ALLOWED")
    # Loading a project reads its trace, so even inspection refuses one outside the allowed directories.
    failed(call(state, "inspect_design", {"project": "trace.json"}), "PATH_NOT_ALLOWED")
    assert not (tmp_path / "escaped").exists()
    # A project's own output folder is checked only when the call uses it.
    assert call(state, "inspect_design", {"project": "escape.json"}).structured_content["state"] == "ready"
    for name in ("escape.json", "root.json"):
        overridden = call(state, "convert_design", {"project": name, "output": name[:-5] + "-out"}).structured_content
        assert overridden["state"] == "ready" and Path(overridden["output_root"]) == (workspace / (name[:-5] + "-out")).resolve()
    assert not (tmp_path / "escaped").exists()
    document = call(state, "convert_design", {"project": "project.json"}).structured_content
    assert document["state"] == "ready" and Path(document["output_root"]) == (workspace / "project-out").resolve()
    assert [note["code"] for note in document["notes"]] == ["PROJECT_RESOURCES_CAPPED", "PROJECT_REFINEMENT_IGNORED"]
    assert document["layout"]["strategy"] == "scalar-reserved-tracks-v1"
    # A project (or a recorded job) can lower the server's limits, the doctor default_limits, but never raise them.
    job = call(state, "job_status", {"job_id": document["job_id"]}).structured_content["job"]
    assert (job["limits"]["deadline_seconds"], job["limits"]["memory_bytes"], job["limits"]["java_heap_bytes"]) == (
        300, 6 * 1024**3, 1024**3)
    assert state._job_limits({"deadline_seconds": 200, "memory_mib": 4096}).deadline_seconds == 200
    recorded = state.refinement._limits({"deadline_seconds": 3600, "memory_bytes": 64 * 1024**3, "java_heap_bytes": 16 * 1024**3})
    assert (recorded.deadline_seconds, recorded.memory_bytes, recorded.java_heap_bytes) == (300, 6 * 1024**3, 1024**3)


def test_inspection_reports_top_candidates_and_located_source_errors(state, workspace):
    # Some hosts send arrays as JSON text; they are decoded like the SDK's typed tools do.
    document = call(state, "inspect_design", {"sources": '["two_tops.v"]'}).structured_content
    assert document["state"] == "ready" and document["top"] is None
    assert document["top_candidates"] == ["invert_top", "passthrough_top"] and "untrusted" in document["notice"]
    diagnostic = failed(call(state, "inspect_design", {"sources": ["malformed.v"]}), "SLANG_ERROR")
    assert Path(diagnostic["path"]) == (workspace / "malformed.v").resolve() and diagnostic["line"] == 2
    status = call(state, "job_status", {"job_id": diagnostic["job_id"]}).structured_content["job"]
    assert status["state"] == "failed" and status["tool"] == "inspect_design" and status["diagnostics"][0]["code"] == "SLANG_ERROR"


def test_conversion_result_preview_validation_and_job_records(state, workspace, monkeypatch):
    progress = []

    async def record(value, total, message):
        progress.append((value, message))

    async def convert(client):
        return await client.call_tool("convert_design", {"sources": ["full_adder.v"], "output": "out", "layout": "reserved"},
                                      progress_callback=record)
    result = run(state, convert)
    document = result.structured_content
    assert not result.is_error and document["state"] == "ready", document
    assert Path(document["circuit"]).is_file() and Path(document["circuit"]).is_relative_to(workspace / "out" / "revisions")
    assert document["validation"]["valid"] and document["validation"]["behavior"]["exhaustive"]
    assert document["preview"] == {"selected": True, "before_refinement": False, "path": document["preview"]["path"]}
    assert [value for value, _ in progress] == list(range(1, len(progress) + 1))
    assert any(document["job_id"] in message and "publishing" in message for _, message in progress)
    failed(call(state, "convert_design", {"sources": ["full_adder.v"], "output": "out"}), "OUTPUT_EXISTS")

    summary = call(state, "get_result", {"path": "out"}).structured_content
    assert summary["selected_revision"] is True and summary["top"] == "full_adder" and summary["job_id"] == document["job_id"]
    assert {item["artifact"] for item in summary["files"]} >= {"circuit", "sidecar", "preview", "validation"}
    assert summary["validation"]["valid"] and summary["sources"][0]["path"] == str((workspace / "full_adder.v").resolve())

    preview = call(state, "get_preview", {"path": "out"})
    image = preview.content[0]
    assert image.type == "image" and image.mime_type == "image/png"
    assert base64.b64decode(image.data)[:8] == b"\x89PNG\r\n\x1a\n"
    metadata = preview.structured_content["metadata"]
    assert preview.structured_content["image_included"] and len(metadata["instances"]) == 10
    assert all(len(item["pixel_bounds"]) == 4 for item in metadata["instances"])
    failed(call(state, "get_preview", {"path": "out", "which": "before_refinement"}), "PREVIEW_UNAVAILABLE")
    monkeypatch.setattr(mcp_server, "MAX_IMAGE_BYTES", 100)
    large = call(state, "get_preview", {"path": "out"})
    assert [item.type for item in large.content] == ["text"] and large.structured_content["image_included"] is False
    assert Path(large.structured_content["path"]).is_file()

    for artifacts_only in (True, False):
        checked = call(state, "validate_result", {"path": "out", "artifacts_only": artifacts_only}).structured_content
        assert checked["valid"] and checked["behavior_replayed"] is False, checked
        assert checked["scope"] == ("artifacts_and_native_model" if artifacts_only else "result_freshness_and_native_model")
    job = call(state, "job_status", {"job_id": document["job_id"]}).structured_content["job"]
    assert job["state"] == "ready" and job["kind"] == "convert" and job["result"]["circuit"] == document["circuit"]
    listed = call(state, "job_status").structured_content["jobs"]
    assert document["job_id"] in {item["job_id"] for item in listed} and not any(item["active"] for item in listed)
    assert call(state, "cancel_job", {"job_id": document["job_id"]}).structured_content["accepted"] is False


def test_full_validation_refuses_recorded_sources_outside_allowed_directories(state, workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(workspace / "full_adder.v", elsewhere)
    other = ServerState([elsewhere])
    try:
        assert not call(other, "convert_design", {"sources": ["full_adder.v"], "output": "out", "layout": "reserved"}).is_error
    finally:
        other.close()
    shutil.move(str(elsewhere / "out"), str(workspace / "moved"))
    assert "artifacts_only=true" in failed(call(state, "validate_result", {"path": "moved"}), "PATH_NOT_ALLOWED")["message"]
    assert call(state, "validate_result", {"path": "moved", "artifacts_only": True}).structured_content["valid"]


def test_parallel_tool_calls_queue_instead_of_failing_busy(state):
    async def steps(client):
        found = []
        async with anyio.create_task_group() as group:
            for name in ("full_adder.v", "two_tops.v", "refine_and.v"):
                async def one(name=name):
                    found.append(await client.call_tool("inspect_design", {"sources": [name]}))
                group.start_soon(one)
        return found
    found = run(state, steps)
    assert all(not item.is_error for item in found), [item.structured_content for item in found]
    assert len({item.structured_content["job_id"] for item in found}) == 3


class Progress:
    def __init__(self):
        self.messages = []

    async def report_progress(self, value, total, message):
        self.messages.append(message)


def test_calls_cancelled_before_their_thread_starts_release_their_place_at_once(monkeypatch):
    monkeypatch.setattr(runner, "CANCEL_GRACE_SECONDS", 20)
    jobs, ran = runner.JobRunner(), []

    async def main():
        started = time.monotonic()
        for _ in range(runner.MAX_WAITING + 4):
            with anyio.CancelScope() as scope:
                scope.cancel()  # The host cancelled in the same write as its call.
                await jobs.run(Progress(), "inspect_design", lambda progress, cancel: ran.append(1))
        elapsed = time.monotonic() - started
        return elapsed, await jobs.run(Progress(), "inspect_design", lambda progress, cancel: "done")
    elapsed, value = anyio.run(main)
    assert value == "done" and not ran and jobs.waiting == 0 and elapsed < 5, elapsed


def test_the_last_progress_events_reach_the_host_before_the_result():
    jobs, sink, late = runner.JobRunner(), Progress(), Progress()
    job = "a" * 32

    def finishing(progress, cancel):
        for stage, state in (("lowering", "running"), ("publishing", "running"), ("ready", "ready")):
            progress({"job_id": job, "state": state, "stage": stage})
        return "value"

    def observed_late(progress, cancel):
        # The job manager's observer thread may deliver the terminal event just after the job returns.
        progress({"job_id": job, "state": "running", "stage": "publishing"})
        threading.Timer(.2, progress, [{"job_id": job, "state": "ready", "stage": "ready"}]).start()
        return "value"

    async def main():
        return await jobs.run(sink, "convert_design", finishing), await jobs.run(late, "convert_design", observed_late)
    assert anyio.run(main) == ("value", "value")
    assert sink.messages == [f"{stage} (job {job})" for stage in ("lowering", "publishing", "ready")]
    assert late.messages == [f"{stage} (job {job})" for stage in ("publishing", "ready")]


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
def test_cancelling_a_tool_call_cancels_its_job(state, workspace, mode):
    async def steps(client):
        started, jobs = anyio.Event(), []

        async def watch(value, total, message):
            if "lowering" in message:
                jobs.append(message.rsplit("job ", 1)[1].rstrip(")"))
                started.set()
        async with anyio.create_task_group() as group:
            async def convert():
                await client.call_tool("convert_design", {"sources": ["full_adder.v"], "output": "out"}, progress_callback=watch)
            group.start_soon(convert)
            with anyio.fail_after(120):
                await started.wait()
            group.cancel_scope.cancel()
        return jobs[0]
    job_id = run(state, steps, mode)
    status = call(state, "job_status", {"job_id": job_id}).structured_content["job"]
    assert status["state"] == "cancelled" and status["measured"].get("active_processes", 0) == 0, status
    assert not (workspace / "out/current.json").exists()


def test_cancel_job_tool_stops_a_running_conversion(state, workspace):
    async def steps(client):
        started, jobs, found = anyio.Event(), [], {}

        async def watch(value, total, message):
            if "lowering" in message and not jobs:
                jobs.append(message.rsplit("job ", 1)[1].rstrip(")"))
                started.set()
        async with anyio.create_task_group() as group:
            async def convert():
                found["convert"] = await client.call_tool("convert_design", {"sources": ["full_adder.v"], "output": "out"},
                                                          progress_callback=watch)
            group.start_soon(convert)
            with anyio.fail_after(120):
                await started.wait()
            found["cancel"] = await client.call_tool("cancel_job", {"job_id": jobs[0]})
        return found
    found = run(state, steps)
    assert found["cancel"].structured_content["accepted"] is True, found["cancel"].structured_content
    assert failed(found["convert"], "CANCELLED") and found["convert"].structured_content["state"] == "cancelled"
    assert not (workspace / "out/current.json").exists()


def test_scratch_of_exited_servers_is_removed_but_live_scratch_is_kept(state, workspace, tmp_path):
    stale = state.scratch.base / ("a" * 32)
    (stale / "rs-run").mkdir(parents=True)
    (stale / "rs-run/file.txt").write_text("x", encoding="utf-8")
    (state.scratch.base / ("b" * 32 + ".lock")).write_bytes(b"0")
    other = ServerState([workspace])
    try:
        assert not stale.exists() and not (state.scratch.base / ("b" * 32 + ".lock")).exists()
        assert state.scratch.root.is_dir() and other.scratch.root.is_dir()
    finally:
        other.close()
    assert not other.scratch.root.exists() and state.scratch.root.is_dir()


def test_results_are_bounded_and_outside_locations_are_withheld(state, workspace, tmp_path):
    document = results.success({"state": "ready", "items": ["x" * 1000] * 200, "path": "kept"}).structured_content
    assert len(document["items"]) > 60 and document["items_omitted"] == 200 - len(document["items"]) and document["path"] == "kept"
    assert "omitted_for_size" not in document and len(json.dumps(document).encode("utf-8")) <= results.MAX_RESULT_BYTES
    # An oversized inspection keeps a share of every list instead of losing all top candidates and modules.
    names = [f"m{index:03d}_" + "x" * 240 for index in range(300)]
    inspection = results.success({"state": "ready", "top": None, "top_candidates": names, "modules": names, "diagnostics": [],
                                  "notice": results.UNTRUSTED}).structured_content
    kept = len(inspection["top_candidates"])
    assert kept == len(inspection["modules"]) > 64 and inspection["top_candidates"] == names[:kept]
    assert inspection["top_candidates_omitted"] == inspection["modules_omitted"] == 300 - kept
    assert "omitted_for_size" not in inspection and len(json.dumps(inspection).encode("utf-8")) <= results.MAX_RESULT_BYTES
    large = results.success({"state": "ready", "blob": {"text": "x" * 70000}, "items": ["a"] * 10}).structured_content
    assert large["omitted_for_size"] == ["blob"] and large["items"] == ["a"] * 10
    item = results.diagnostic({"code": "SLANG_ERROR", "message": "quoted secret", "path": str(tmp_path / "x.v"), "line": 1},
                              state.policy)
    assert item["message"] == results.WITHHELD and item["line"] == 1
    inside = results.diagnostic({"code": "SLANG_ERROR", "message": "m" * 5000, "path": str(workspace / "x.v")}, state.policy)
    assert inside["message"].endswith("[truncated]") and len(inside["message"]) < 2100


def compact(prompt):
    context = json.loads(prompt["messages"][1]["content"])
    actions = []
    for item in context["components"]:
        target = COMPACT[item["id"]]
        delta = {"x": target[0] - item["position"][0], "y": target[1] - item["position"][1]}
        if any(delta.values()):
            actions.append({"action": "move_component", "instance_id": item["id"], "delta": delta})
    return {"schema_version": 1, "candidate_id": prompt["candidate_id"], "actions": actions,
            "explanation": "Arrange the gate and its ports in a short row."}


def test_refinement_session_publishes_a_verified_improvement(state, workspace):
    assert not call(state, "convert_design", {"sources": ["refine_and.v"], "output": "out", "layout": "reserved"}).is_error
    before = (workspace / "out/current.json").read_bytes()

    async def steps(client):
        start = (await client.call_tool("refinement_start", {"output": "out", "max_attempts": 2, "include_preview": True}))
        session, prompt = start.structured_content["session"], start.structured_content["prompt"]
        stale = await client.call_tool("refinement_propose", {"session": session, "response": {
            "schema_version": 1, "candidate_id": "f" * 64, "actions": [], "explanation": "stale"}})
        broken = await client.call_tool("refinement_propose", {"session": session, "response": '{"schema_version": 1,'})
        accepted = await client.call_tool("refinement_propose", {"session": session, "response": json.dumps(compact(prompt))})
        finish = await client.call_tool("refinement_finish", {"session": session})
        after = await client.call_tool("refinement_propose", {"session": session, "response": compact(prompt)})
        return start, stale, broken, accepted, finish, after
    start, stale, broken, accepted, finish, after = run(state, steps)
    document = start.structured_content
    assert document["state"] == "awaiting_response" and document["attempts"] == {"used": 0, "max": 2, "remaining": 2}
    assert [item.type for item in start.content] == ["image", "text"]
    prompt = document["prompt"]
    assert prompt["attempt"] == 1 and len(prompt["candidate_id"]) == 64 and prompt["messages"][0]["role"] == "system"
    assert prompt["response_schema"]["required"] == ["schema_version", "candidate_id", "actions", "explanation"]
    assert set(COMPACT) == set(prompt["visible_instance_ids"]) and "strictly lower" in document["acceptance_rule"]
    assert "did not consume an attempt" in failed(stale, "REFINEMENT_STALE_CANDIDATE")["message"]
    failed(broken, "REFINEMENT_RESPONSE_INVALID")
    proposal = accepted.structured_content
    assert proposal["outcome"]["result"] == "accepted" and proposal["outcome"]["relation"] == "improved", proposal
    assert all(value <= 0 for value in proposal["outcome"]["deltas_candidate_minus_current"].values())
    assert proposal["attempts"]["used"] == 1 and proposal["accepted"] == 1 and proposal["prompt"]["attempt"] == 2
    assert proposal["prompt"]["candidate_id"] != prompt["candidate_id"]
    published = finish.structured_content
    assert not finish.is_error and published["published"] and published["reproduced"], published
    assert published["refinement"]["accepted"] == 1 and published["validation"]["valid"]
    assert published["preview"]["before_refinement"] and published["layout"]["strategy"] == "constrained-orthogonal-v1"
    assert (workspace / "out/current.json").read_bytes() != before
    failed(after, "REFINEMENT_SESSION_UNKNOWN")
    baseline = call(state, "get_preview", {"path": "out", "which": "before_refinement"}).structured_content
    assert baseline["metadata"]["comparison_scope"] == "before_refinement"
    assert not any(state.scratch.root.iterdir())

    # A second session starts from the deterministic layout and cannot replace the better refined revision.
    refined = (workspace / "out/current.json").read_bytes()

    async def again(client):
        start = (await client.call_tool("refinement_start", {"output": "out", "max_attempts": 2})).structured_content
        shorter = {"schema_version": 1, "candidate_id": start["prompt"]["candidate_id"], "explanation": "shorter output wire",
                   "actions": [{"action": "move_component", "instance_id": "out0000", "delta": {"x": -40, "y": 0}}]}
        moved = (await client.call_tool("refinement_propose", {"session": start["session"], "response": shorter})).structured_content
        finish = await client.call_tool("refinement_finish", {"session": start["session"]})
        closed = (await client.call_tool("refinement_finish", {"session": start["session"], "publish": False})).structured_content
        return start, moved, finish, closed
    start, moved, finish, closed = run(state, again)
    assert start["selected_revision"]["refined"] and [item["code"] for item in start["diagnostics"]] == ["REFINEMENT_STARTS_FROM_BASELINE"]
    assert start["prompt"]["current_metrics"] != start["selected_revision"]["canvases"][0]["selected_metrics"]
    assert moved["outcome"]["accepted"], moved
    assert "session stays open" in failed(finish, "REFINEMENT_NOT_BETTER")["message"]
    assert closed["state"] == "abandoned" and (workspace / "out/current.json").read_bytes() == refined


def test_refinement_finish_never_replaces_a_conversion_queued_before_it(state, workspace):
    convert = {"sources": ["refine_and.v"], "output": "out", "layout": "reserved"}
    assert not call(state, "convert_design", convert).is_error

    async def steps(client):
        start = (await client.call_tool("refinement_start", {"output": "out", "max_attempts": 2})).structured_content
        proposal = await client.call_tool("refinement_propose", {"session": start["session"], "response": compact(start["prompt"])})
        assert proposal.structured_content["accepted"] == 1, proposal.structured_content
        found, running = {}, anyio.Event()

        async def holding(value, total, message):
            if "preflight" in message:
                running.set()  # The conversion's own jobs run: it holds the job runner.
        async with anyio.create_task_group() as group:
            async def convert_again():
                found["convert"] = await client.call_tool("convert_design", {**convert, "replace": True, "spacing": 60},
                                                          progress_callback=holding)
            group.start_soon(convert_again)
            with anyio.fail_after(120):
                await running.wait()
            found["finish"] = await client.call_tool("refinement_finish", {"session": start["session"]})
        found["after"] = await client.call_tool("refinement_finish", {"session": start["session"]})
        return found
    found = run(state, steps)
    converted = found["convert"].structured_content
    assert not found["convert"].is_error and converted["state"] == "ready", converted
    assert "nothing was published" in failed(found["finish"], "REFINEMENT_OUTPUT_CHANGED")["message"]
    failed(found["after"], "REFINEMENT_SESSION_UNKNOWN")
    assert call(state, "get_result", {"path": "out"}).structured_content["job_id"] == converted["job_id"]


def test_finishing_compares_every_canvas_with_an_already_refined_revision():
    base = {"bounding_area": 100, "wire_length": 50, "bends": 6, "crossings": 0}
    selected = refinement._canvases({"canvases": [{"canvas": 0, "module_id": None, "accepted": 1, "top": "t", "baseline_metrics": base,
                                                   "selected_metrics": {**base, "bounding_area": 60, "bends": 2}}]})

    def session(**metrics):
        return {"canvases": [{"canvas": 0, "module_id": None, "baseline_metrics": base, "selected_metrics": {**base, **metrics}}]}
    assert refinement._comparison(session(bounding_area=80, bends=2), selected) == (["canvas 0 bounding_area 80 > 60"], False)
    assert refinement._comparison(session(bounding_area=60, bends=2), selected) == ([], False)
    assert refinement._comparison(session(bounding_area=60, bends=2, wire_length=40), selected) == ([], True)
    # A canvas the session never reached keeps the deterministic baseline, which the selected revision improved.
    worse, better = refinement._comparison({"canvases": []}, selected)
    assert worse == ["canvas 0 bounding_area 100 > 60", "canvas 0 bends 6 > 2"] and not better


def test_hierarchy_canvases_share_attempts_and_discarding_publishes_nothing(state, workspace):
    assert not call(state, "convert_design", {"sources": ["repeated_logic.v"], "output": "out", "layout": "reserved"}).is_error
    before = (workspace / "out/current.json").read_bytes()

    async def steps(client):
        start = (await client.call_tool("refinement_start", {"output": "out", "max_attempts": 3})).structured_content
        skip = {"schema_version": 1, "candidate_id": start["prompt"]["candidate_id"], "actions": [], "explanation": "next canvas"}
        moved = (await client.call_tool("refinement_propose", {"session": start["session"], "response": skip})).structured_content
        unchanged = (await client.call_tool("refinement_finish", {"session": start["session"], "publish": False})).structured_content
        return start, moved, unchanged
    start, moved, unchanged = run(state, steps)
    assert start["prompt"]["canvas"]["index"] == 0 and start["prompt"]["canvas"]["top"] == "hier_xor"
    assert moved["outcome"]["result"] == "no_change" and moved["prompt"]["attempt"] == 2
    assert moved["prompt"]["canvas"]["index"] == 1 and moved["attempts"]["remaining"] == 2
    assert unchanged == {"state": "abandoned", "published": False, "output_root": str((workspace / "out").resolve())}
    assert (workspace / "out/current.json").read_bytes() == before


def test_refinement_sessions_are_bounded_and_expire(state, workspace, monkeypatch):
    convert = {"sources": ["refine_and.v"], "output": "out", "layout": "reserved"}
    assert not call(state, "convert_design", convert).is_error
    start = call(state, "refinement_start", {"output": "out", "max_attempts": 1}).structured_content
    proposal = call(state, "refinement_propose", {"session": start["session"], "response": compact(start["prompt"])}).structured_content
    assert proposal["state"] == "complete" and proposal["outcome"]["accepted"] and proposal["reason"] == "All 1 attempts are used."
    failed(call(state, "refinement_propose", {"session": start["session"], "response": compact(start["prompt"])}), "REFINEMENT_COMPLETE")
    # Another revision selected meanwhile: the session's replay must not overwrite it.
    selected = call(state, "convert_design", {**convert, "replace": True, "spacing": 60}).structured_content["revision"]
    failed(call(state, "refinement_finish", {"session": start["session"]}), "REFINEMENT_OUTPUT_CHANGED")
    assert call(state, "get_result", {"path": "out"}).structured_content["directory"] == selected
    monkeypatch.setattr(refinement, "MAX_SESSIONS", 1)
    first = call(state, "refinement_start", {"output": "out"}).structured_content["session"]
    second = call(state, "refinement_start", {"output": "out"}).structured_content["session"]
    failed(call(state, "refinement_finish", {"session": first}), "REFINEMENT_SESSION_UNKNOWN")
    assert [path.name for path in state.scratch.root.iterdir()] == [second]
    monkeypatch.setattr(refinement, "IDLE_SECONDS", -1)
    failed(call(state, "refinement_finish", {"session": second}), "REFINEMENT_SESSION_UNKNOWN")
    assert not any(state.scratch.root.iterdir())
