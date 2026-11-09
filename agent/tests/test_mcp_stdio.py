"""Launch `scripts/v2d.py mcp` and talk to it over stdio, as an MCP host does."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest


ROOT = Path(__file__).resolve().parents[2]
COMMAND = [sys.executable, "-X", "faulthandler", str(ROOT / "scripts/v2d.py"), "mcp"]


@pytest.fixture
def allowed(tmp_path):
    folder = tmp_path / "allowed"
    folder.mkdir()
    shutil.copy(ROOT / "tests/fixtures/verilog/scalar/full_adder.v", folder)
    return folder


def environment(tmp_path):
    return {"V2D_JOB_ROOT": str(tmp_path / "jobs"), "PYTHONNOUSERSITE": "1"}


@pytest.mark.parametrize("mode,version", [("2026-07-28", "2026-07-28"), ("legacy", "2025-11-25")])
def test_stdio_server_serves_stateless_and_handshake_clients(tmp_path, allowed, mode, version):
    parameters = StdioServerParameters(command=COMMAND[0], args=[*COMMAND[1:], "--allow-dir", str(allowed)],
                                       env=environment(tmp_path), cwd=str(ROOT))
    progress = []

    async def record(value, total, message):
        progress.append(message)

    async def main():
        with (tmp_path / "server.log").open("w", encoding="utf-8") as errors:
            async with Client(stdio_client(parameters, errlog=errors), mode=mode) as client:
                assert client.protocol_version == version
                tools = [tool.name for tool in (await client.list_tools()).tools]
                doctor = await client.call_tool("doctor", {})
                inspected = await client.call_tool("inspect_design", {"sources": ["full_adder.v"]}, progress_callback=record)
                converted = await client.call_tool("convert_design", {"sources": [str(allowed / "full_adder.v")],
                                                                      "output": "out"}, progress_callback=record)
                return tools, doctor, inspected, converted
    tools, doctor, inspected, converted = anyio.run(main)
    assert tools[:3] == ["doctor", "inspect_design", "convert_design"] and tools[-1] == "refinement_finish"
    assert doctor.structured_content["paths"]["allowed_directories"] == [str(allowed.resolve())]
    assert inspected.structured_content["top_candidates"] == ["full_adder"]
    assert not converted.is_error and converted.structured_content["validation"]["valid"], converted.structured_content
    assert Path(converted.structured_content["circuit"]).is_file() and progress
    assert "v2d mcp: serving over stdio" in (tmp_path / "server.log").read_text(encoding="utf-8")


def test_stdout_carries_only_protocol_messages_and_closing_stdin_cancels_and_exits_cleanly(tmp_path, allowed):
    with (tmp_path / "stderr.log").open("wb") as errors:
        process = subprocess.Popen([*COMMAND, "--allow-dir", str(allowed)], cwd=ROOT, env={**os.environ, **environment(tmp_path)},
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in iter(process.stdout.readline, b"")] + [lines.put(None)],
                              daemon=True)
    reader.start()
    received = []

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def until(found):
        while not any(found(item) for item in received):
            line = lines.get(timeout=240)
            assert line is not None, "The server closed stdout early"
            received.append(json.loads(line.decode("utf-8")))
    convert = {"sources": ["full_adder.v"], "output": "raw-out"}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw-test", "version": "1"}}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "convert_design", "arguments": convert, "_meta": {"progressToken": "first"}}})
        until(lambda item: item.get("id") == 2)
        selected = (allowed / "raw-out/current.json").read_bytes()
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "convert_design", "arguments": {**convert, "replace": True, "no_cache": True},
            "_meta": {"progressToken": "second"}}})
        until(lambda item: item.get("params", {}).get("progressToken") == "second" and "lowering" in item["params"].get("message", ""))
        process.stdin.close()  # The host is gone: the running job is cancelled before the process exits.
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            process.kill()
    while (line := lines.get(timeout=10)) is not None:
        received.append(json.loads(line.decode("utf-8")))
    assert all(item.get("jsonrpc") == "2.0" and ("id" in item or "method" in item) for item in received)
    assert received[0]["result"]["protocolVersion"] == "2025-11-25"
    answer = next(item for item in received if item.get("id") == 2)["result"]
    assert answer["isError"] is False and answer["structuredContent"]["state"] == "ready"
    assert (allowed / "raw-out/current.json").read_bytes() == selected
    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "jobs").glob("*/job.json")]
    assert sorted(job["state"] for job in jobs if job["kind"] == "convert") == ["cancelled", "ready"]
    assert not any((tmp_path / "jobs/_mcp").iterdir())
    assert "v2d mcp: serving over stdio" in (tmp_path / "stderr.log").read_text(encoding="utf-8")


def test_calls_cancelled_on_arrival_release_their_place_and_closing_stays_prompt(tmp_path, allowed):
    """A host that cancels in the same write as its call (a Stop button) must not use up the runner."""
    with (tmp_path / "stderr.log").open("wb") as errors:
        process = subprocess.Popen([*COMMAND, "--allow-dir", str(allowed)], cwd=ROOT, env={**os.environ, **environment(tmp_path)},
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in iter(process.stdout.readline, b"")] + [lines.put(None)],
                     daemon=True).start()
    received = []

    def answer(request_id):
        while not any(item.get("id") == request_id for item in received):
            line = lines.get(timeout=240)
            assert line is not None, "The server closed stdout early"
            received.append(json.loads(line.decode("utf-8")))
        return next(item for item in received if item.get("id") == request_id)

    def message(item):
        return json.dumps(item) + "\n"
    inspect = {"name": "inspect_design", "arguments": {"sources": ["full_adder.v"]}}
    try:
        process.stdin.write((message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw-test", "version": "1"}}})
            + message({"jsonrpc": "2.0", "method": "notifications/initialized"})).encode("utf-8"))
        process.stdin.flush()
        answer(1)
        for request in range(10, 22):
            process.stdin.write((message({"jsonrpc": "2.0", "id": request, "method": "tools/call", "params": inspect})
                                 + message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                            "params": {"requestId": request, "reason": "stopped"}})).encode("utf-8"))
            process.stdin.flush()
        # Once the server answers this, it has dispatched every cancellation above.
        process.stdin.write(message({"jsonrpc": "2.0", "id": 98, "method": "ping"}).encode("utf-8"))
        process.stdin.flush()
        answer(98)
        process.stdin.write(message({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": inspect}).encode("utf-8"))
        process.stdin.flush()
        result = answer(99)["result"]
        assert result["isError"] is False and result["structuredContent"]["state"] == "ready", result
        started = time.monotonic()
        process.stdin.close()
        assert process.wait(timeout=60) == 0 and time.monotonic() - started < 5
    finally:
        if process.poll() is None:
            process.kill()


def test_mcp_command_reports_configuration_errors_on_stderr(tmp_path):
    def run(*arguments):
        return subprocess.run([*COMMAND, *arguments], cwd=ROOT, env={**os.environ, **environment(tmp_path)},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=120)
    missing = run("--allow-dir", str(tmp_path / "missing"))
    assert missing.returncode == 1 and missing.stdout == "" and "ALLOW_DIR_INVALID" in missing.stderr
    # An empty value, such as an unset host variable, must not allow the working directory.
    for value in ("", " "):
        blank = run("--allow-dir", value)
        assert blank.returncode == 1 and blank.stdout == "" and "ALLOW_DIR_INVALID" in blank.stderr, blank.stderr
    limits = run("--allow-dir", str(tmp_path), "--deadline", "0")
    assert limits.returncode == 1 and limits.stdout == "" and "INVALID_LIMITS" in limits.stderr
    idle = run()
    assert idle.returncode == 0 and idle.stdout == "", idle.stderr
    assert "allowed directories: none" in idle.stderr
