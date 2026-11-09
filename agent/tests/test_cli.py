"""Exercise the public command with real files and machine-readable outcomes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_cli(*arguments):
    return subprocess.run([sys.executable, "-X", "faulthandler", str(ROOT / "scripts/v2d.py"), *map(str, arguments)],
                          cwd=ROOT, env={**os.environ, "PYTHONNOUSERSITE": "1"}, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


def test_cli_converts_from_paths_with_spaces_and_emits_one_json_result(tmp_path):
    source = tmp_path / "source circuit.v"
    source.write_text("module circuit(input a,b,output y); assign y=a^b; endmodule\n", encoding="utf-8")
    output = tmp_path / "output circuit"
    result = run_cli("convert", source, "-o", output, "--json", "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["state"] == "ready"
    report = json.loads(Path(document["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] and report["behavior"]["input_vectors"] == 4
    assert Path(document["circuit"]).is_file() and Path(document["preview"]).is_file()


def test_cli_reports_ambiguous_top_with_nonzero_exit_and_no_published_circuit(tmp_path):
    output = tmp_path / "ambiguous"
    result = run_cli("convert", ROOT / "tests/fixtures/verilog/frontend/two_tops.v",
                     "-o", output, "--json", "--quiet")
    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostic"]["code"] == "AMBIGUOUS_TOP"
    assert (output / "failure.json").is_file()
    assert not (output / "manifest.json").exists()
    assert not (output / "circuit.dig").exists()


def test_cli_rejects_duplicate_defines_before_creating_output(tmp_path):
    output = tmp_path / "duplicate"
    result = run_cli("convert", ROOT / "tests/fixtures/verilog/scalar/buffer.v", "-o", output,
                     "-D", "MODE=1", "-D", "MODE=0", "--json", "--quiet")
    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostic"]["code"] == "DUPLICATE_OPTION"
    assert not output.exists()


@pytest.mark.parametrize("contents", ["null", "[]", "{broken", '{"values":{"d":0,"d":255}}',
                                      '{"initialization":{},"initialization":{}}'])
def test_cli_rejects_invalid_trace_before_starting_conversion(tmp_path, contents):
    trace = tmp_path / "trace.json"
    trace.write_text(contents, encoding="utf-8")
    output = tmp_path / "invalid_trace"
    result = run_cli("convert", ROOT / "tests/fixtures/verilog/sequential/d_register.v", "-o", output,
                     "--trace", trace, "--json", "--quiet")
    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostic"]["code"] == "INVALID_TRACE"
    assert not output.exists()


def test_cli_accepts_an_explicit_synchronous_trace(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"schema_version": 1, "clock": {"port": "clk", "edge": "rising"},
                                "initialization": {"kind": "input_sequence", "cycles": [{"values": {"d": 17}}]},
                                "cycles": [{"values": {"d": 128}}, {"values": {"d": 255}}]}), encoding="utf-8")
    result = run_cli("convert", ROOT / "tests/fixtures/verilog/sequential/d_register.v", "-o", tmp_path / "result",
                     "--trace", trace, "--json", "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    report = json.loads(Path(document["validation"]).read_text(encoding="utf-8"))
    assert report["behavior"]["checked_cycles"] == 2
    assert report["behavior"]["initialization"]["prefix_cycles"] == 1


@pytest.mark.parametrize("arguments,message", [
    (["--spacing", "41"], "--spacing must be a multiple of 20"),
    (["--spacing", "420"], "--spacing must be a multiple of 20"),
    (["--layout-timeout", "0.5"], "--layout-timeout must be from 1 through 120 seconds"),
    (["--layout-timeout", "nan"], "--layout-timeout must be from 1 through 120 seconds"),
    (["--random-vectors", "2049"], "--random-vectors must be from 0 through 2048")])
def test_out_of_range_conversion_flags_fail_before_a_job_or_output(tmp_path, monkeypatch, capsys, arguments, message):
    from verilog2digital import cli
    from verilog2digital.jobs import manager
    calls = []
    monkeypatch.setattr(manager, "run_job", lambda *args, **kwargs: calls.append(kwargs))
    output = tmp_path / "unused"
    assert cli.main(["convert", str(ROOT / "tests/fixtures/verilog/scalar/buffer.v"), "-o", str(output),
                     *arguments, "--json", "--quiet"]) == 1
    diagnostic = json.loads(capsys.readouterr().out)["diagnostic"]
    assert diagnostic["code"] == "INVALID_OPTION" and diagnostic["message"].startswith(message)
    assert not calls and not output.exists()


def test_unexpected_command_failure_still_ends_event_stream_with_error_record(monkeypatch, capsys):
    from verilog2digital import cli
    from verilog2digital.jobs import manager
    def broken(*args, **kwargs):
        raise KeyError("unexpected worker field")
    monkeypatch.setattr(manager, "run_inspection", broken)
    assert cli.main(["inspect", str(ROOT / "tests/fixtures/verilog/scalar/buffer.v"), "--events"]) == 1
    captured = capsys.readouterr()
    record = json.loads(captured.out.splitlines()[-1])
    assert record["type"] == "error" and record["result"]["state"] == "failed"
    assert record["result"]["diagnostic"] == {"code": "COMMAND_FAILED", "message": "'unexpected worker field'"}
    assert "KeyError" in captured.err


def test_existing_benchmark_directory_is_reported_and_preserved(tmp_path, monkeypatch, capsys):
    from verilog2digital import cli
    from verilog2digital.jobs import manager
    monkeypatch.setattr(manager, "run_job", lambda *args, **kwargs: pytest.fail("benchmark started a job"))
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    assert cli.main(["benchmark", str(ROOT / "tests/fixtures/verilog/scalar/buffer.v"), "-o", str(output),
                     "--json", "--quiet"]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "OUTPUT_EXISTS"
    assert [path.name for path in output.iterdir()] == ["keep.txt"]


def test_replay_envelope_message_names_the_required_schema_version(tmp_path, monkeypatch, capsys):
    from verilog2digital import cli
    from verilog2digital.jobs import manager
    monkeypatch.setattr(manager, "run_job", lambda *args, **kwargs: pytest.fail("invalid replay started a job"))
    recorded = tmp_path / "replay.json"
    recorded.write_text(json.dumps({"schema_version": 2, "responses": []}), encoding="utf-8")
    assert cli.main(["convert", str(ROOT / "tests/fixtures/verilog/scalar/buffer.v"), "-o", str(tmp_path / "unused"),
                     "--replay-actions", str(recorded), "--json", "--quiet"]) == 1
    diagnostic = json.loads(capsys.readouterr().out)["diagnostic"]
    assert diagnostic == {"code": "INVALID_REPLAY",
                          "message": "Replay files require schema_version 1 and a responses list of raw JSON strings"}
