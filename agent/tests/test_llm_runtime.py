from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import time

import pytest

from verilog2digital.llm import runtime
from verilog2digital.llm.artifacts import ModelError
from verilog2digital.llm.resources import select_profile, recovery_profile


def resources(*, gpu=True, commit=10 * 1024**3, free=11 * 1024**3):
    return {"host": {"available_commit_bytes": commit, "available_physical_bytes": 14 * 1024**3},
            "gpus": [{"index": 0, "driver": "592.82", "free_bytes": free}] if gpu else []}


def profile():
    return select_profile(resources=resources())


class Worker:
    def poll(self):
        return None
    def metrics(self):
        return {"active_processes": 1}


def fake_runtime(monkeypatch, *, raw='{"decision":"keep"}', finish="stop", tokens=30):
    client = runtime.LocalModelRuntime(profile())
    client._worker = Worker()
    client._artifact = {"model": {"id": "test"}, "runtime": {"commit": "test"}}
    requests = []
    def request(method, path, body, deadline, cancel):
        requests.append((path, deepcopy(body)))
        if path == "/apply-template":
            return {"prompt": "formatted"}
        if path == "/tokenize":
            return {"tokens": [1] * tokens}
        return {"choices": [{"finish_reason": finish, "message": {"content": raw}}], "usage": {"completion_tokens": 5}}
    monkeypatch.setattr(client, "_bounded_request", request)
    monkeypatch.setattr(client, "unload", lambda: setattr(client, "_worker", None))
    return client, requests


SCHEMA = {"type": "object", "properties": {"decision": {"const": "keep"}}, "required": ["decision"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": "Choose a decision."}]


def test_constrained_envelope_has_required_schema_wrapper_and_raw_response(monkeypatch):
    client, requests = fake_runtime(monkeypatch)
    result = client.generate_json(MESSAGES, SCHEMA)
    sent = requests[-1][1]
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["stream"] is False
    assert result["raw_text"] == '{"decision":"keep"}'
    assert result["value"] == {"decision": "keep"}
    assert result["timings"]["prompt_tokens"] == 30


@pytest.mark.parametrize("raw", ['{"decision":"keep","decision":"keep"}', '{"x":NaN}', '[', '"\\ud800"'])
def test_non_json_or_ambiguous_output_rejected(monkeypatch, raw):
    client, _ = fake_runtime(monkeypatch, raw=raw)
    with pytest.raises(ModelError):
        client.generate_json(MESSAGES, SCHEMA)
    assert client._worker is not None  # Completed response rejection needs no reload.


def test_local_schema_still_rejects_server_violation(monkeypatch):
    client, _ = fake_runtime(monkeypatch, raw='{"decision":"delete"}')
    with pytest.raises(ModelError, match="violates"):
        client.generate_json(MESSAGES, SCHEMA)


def test_truncated_output_never_becomes_action(monkeypatch):
    client, _ = fake_runtime(monkeypatch, finish="length")
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    assert caught.value.code == "MODEL_TRUNCATED"


def test_prompt_budget_uses_formatted_token_count_before_generation(monkeypatch):
    client, requests = fake_runtime(monkeypatch, tokens=4000)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    assert caught.value.code == "MODEL_CONTEXT"
    assert [path for path, _ in requests] == ["/apply-template", "/tokenize"]


@pytest.mark.parametrize("field,value", [("context_tokens", True), ("context_tokens", 1000000), ("gpu_layers", 999),
                                       ("host_memory_bytes", 1), ("kv_type", "q4_0"), ("idle_seconds", 0), ("threads", 0)])
def test_invalid_profile_rejected(field, value):
    item = profile()
    item[field] = value
    with pytest.raises(ModelError):
        runtime.LocalModelRuntime(item)


def test_profiles_fit_small_gpu_and_decline_large_when_headroom_missing():
    assert select_profile(resources=resources())["gpu_layers"] == 37
    assert select_profile(resources=resources(gpu=False))["backend"] == "cpu"
    assert select_profile(resources=resources(free=1024**3))["gpu_layers"] == 0
    with pytest.raises(ModelError):
        select_profile("qwen3-14b-q4_k_m", resources=resources(gpu=False))
    with pytest.raises(ModelError):
        select_profile(resources=resources(commit=1024**3))
    with pytest.raises(ModelError):
        select_profile(resources=resources(commit=3 * 1024**3))
    with pytest.raises(ModelError):
        select_profile(resources=resources(), host_memory_bytes=2 * 1024**3)


@pytest.mark.parametrize("idle", [1, 30, 300])
def test_selector_preserves_explicit_idle_timeout(idle):
    assert select_profile(resources=resources(), idle_seconds=idle)["idle_seconds"] == idle


@pytest.mark.parametrize("idle", [True, 0, 301, 1.5, "30"])
def test_selector_rejects_invalid_idle_timeout(idle):
    with pytest.raises(ModelError) as caught:
        select_profile(resources=resources(), idle_seconds=idle)
    assert caught.value.code == "MODEL_PROFILE"
    assert caught.value.requested_profile is None


def test_constructed_admission_failure_retains_requested_profile_for_smaller_retry():
    smaller = recovery_profile(profile(), resources())
    cap = smaller["memory_requirements"]["estimated_host_commit_bytes"]
    with pytest.raises(ModelError) as caught:
        select_profile(resources=resources(), host_memory_bytes=cap, idle_seconds=17)
    error = caught.value
    assert error.code == "MODEL_MEMORY"
    assert error.requested_profile["context_tokens"] == 4096
    result = recovery_profile(error.requested_profile, resources())
    assert result["context_tokens"] == 2048
    assert result["host_memory_bytes"] == cap
    assert result["idle_seconds"] == 17
    assert "requested_profile" not in error.as_dict()
    result["memory_requirements"]["estimated_host_commit_bytes"] = 0
    assert error.requested_profile["memory_requirements"]["estimated_host_commit_bytes"] > 0
    with pytest.raises(ModelError) as early:
        select_profile("qwen3-14b-q4_k_m", resources=resources(gpu=False))
    assert early.value.requested_profile is None


def test_recovery_preserves_caps_device_and_unrelated_settings_without_mutating_input():
    original = profile()
    original.update(gpu_index=3, threads=4, idle_seconds=11, host_memory_bytes=5 * 1024**3,
                    vram_margin_bytes=2 * 1024**3)
    before = deepcopy(original)
    current = resources()
    current["gpus"][0]["index"] = 3
    result = recovery_profile(original, current)
    assert original == before
    for key in ("host_memory_bytes", "gpu_index", "threads", "idle_seconds", "vram_margin_bytes",
                "model_id", "backend", "gpu_layers", "batch_tokens", "micro_batch_tokens", "kv_type"):
        assert result[key] == original[key]
    assert result["context_tokens"] == 2048


@pytest.mark.parametrize("change", [
    {"context_tokens": 2048}, {"backend": "cpu", "gpu_layers": 0}, {"gpu_layers": 18},
    {"kv_type": "f16"}, {"model_id": "qwen3-4b-q5_k_m"},
    {"model_id": "qwen3-14b-q4_k_m", "gpu_layers": 41},
])
def test_recovery_does_not_invent_an_unmeasured_configuration(change):
    candidate = profile()
    candidate.update(change)
    assert recovery_profile(candidate, resources()) is None


@pytest.mark.parametrize("failure", ["commit", "physical", "cap", "gpu", "device", "driver"])
def test_recovery_requires_fresh_host_gpu_and_explicit_cap_admission(failure):
    candidate = profile()
    current = resources()
    if failure == "commit":
        current["host"]["available_commit_bytes"] = 1024**3
    elif failure == "physical":
        current["host"]["available_physical_bytes"] = 1024**3
    elif failure == "cap":
        candidate["host_memory_bytes"] = 1024**3
    elif failure == "gpu":
        current["gpus"][0]["free_bytes"] = 1024**3
    elif failure == "device":
        current["gpus"][0]["index"] = 1
    else:
        current["gpus"][0]["driver"] = "551.00"
    assert recovery_profile(candidate, current) is None


def test_startup_admission_requested_profile_is_an_independent_snapshot():
    client = runtime.LocalModelRuntime(profile())
    with pytest.raises(ModelError) as caught:
        client._admit(resources(commit=1024**3))
    client.profile["memory_requirements"]["estimated_host_commit_bytes"] = 0
    assert caught.value.requested_profile["memory_requirements"]["estimated_host_commit_bytes"] > 0


@pytest.mark.parametrize("message,kind,signature", [
    ("ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 1765048320", "host", "ggml_cpu_buffer_allocation"),
    ("alloc_tensor_range: failed to allocate CPU_REPACK buffer of size 1765048320", "host", "ggml_cpu_repack_allocation"),
    ("llama_model_load: error loading model: unable to allocate CPU_REPACK buffer", "host", "llama_cpu_repack_allocation"),
    ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1600.50 MiB on device 0: cudaMalloc failed: out of memory", "gpu", "ggml_cuda_malloc_oom"),
    ("CUDA error: out of memory", "gpu", "ggml_cuda_oom"),
])
def test_exited_owned_allocator_failure_has_bounded_typed_evidence(tmp_path, message, kind, signature):
    client = runtime.LocalModelRuntime(profile())
    client._worker = type("Exited", (), {"poll": lambda self: 1})()
    client._log = tmp_path / "owned.stderr.log"
    content = ("0.00.396.914 E " + message + "\n").encode()
    client._log.write_bytes(content)
    with pytest.raises(ModelError) as caught:
        client._check(time.monotonic() + 1)
    error = caught.value
    assert error.code == "MODEL_ALLOCATION"
    assert error.allocation_evidence == {
        "source": "owned_server_stderr", "kind": kind, "signature": signature,
        "excerpt": content.decode().rstrip(), "stderr_tail_sha256": hashlib.sha256(content).hexdigest(),
        "stderr_bytes": len(content)}
    assert "allocation_evidence" not in error.as_dict()


@pytest.mark.parametrize("message", [
    "out of memory", "srv prompt: CUDA error: out of memory", "failed to load model",
    "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1600.50 MiB on device 0: cudaMalloc failed: invalid argument",
    "model response: ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 50",
])
def test_arbitrary_exit_text_is_not_allocation_evidence(tmp_path, message):
    client = runtime.LocalModelRuntime(profile())
    client._worker = type("Exited", (), {"poll": lambda self: 1})()
    client._log = tmp_path / "owned.stderr.log"
    client._log.write_text(message, encoding="utf-8")
    with pytest.raises(ModelError) as caught:
        client._check(time.monotonic() + 1)
    assert caught.value.code == "MODEL_EXIT"
    assert caught.value.allocation_evidence is None


def test_allocation_scan_is_tail_bounded_and_does_not_run_for_live_or_cancelled_server(tmp_path, monkeypatch):
    client = runtime.LocalModelRuntime(profile())
    client._log = tmp_path / "owned.stderr.log"
    signature = b"CUDA error: out of memory\n"
    data = signature + b"x" * (runtime.MAX_ALLOCATION_TAIL_BYTES + 1)
    client._log.write_bytes(data)
    assert runtime._allocation_evidence(client._log) is None
    data += b"\n" + signature
    client._log.write_bytes(data)
    evidence = runtime._allocation_evidence(client._log)
    assert evidence["stderr_tail_sha256"] == hashlib.sha256(data[-runtime.MAX_ALLOCATION_TAIL_BYTES:]).hexdigest()
    assert evidence["stderr_bytes"] == len(data)
    monkeypatch.setattr(runtime, "_allocation_evidence", lambda *_: pytest.fail("must not read allocation log"))
    client._worker = Worker()
    client._check(time.monotonic() + 1)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ModelError) as caught:
        client._check(time.monotonic() + 1, cancel)
    assert caught.value.code == "MODEL_CANCELLED"


def test_missing_allocation_log_keeps_ordinary_exit_and_excerpt_is_utf8_bounded(tmp_path):
    client = runtime.LocalModelRuntime(profile())
    client._worker = type("Exited", (), {"poll": lambda self: 1})()
    client._log = tmp_path / "missing.log"
    with pytest.raises(ModelError) as caught:
        client._check(time.monotonic() + 1)
    assert caught.value.code == "MODEL_EXIT"
    client._log.write_bytes(b"ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size " + b"1" * 6000)
    evidence = runtime._allocation_evidence(client._log)
    assert len(evidence["excerpt"].encode("utf-8")) == runtime.MAX_ALLOCATION_EXCERPT_BYTES


def test_optional_memory_sampling_failure_cannot_skip_owned_server_close(monkeypatch):
    client = runtime.LocalModelRuntime(profile())
    class Owned:
        closed = False
        def close(self):
            self.closed = True
        def metrics(self):
            return {"active_processes": 0 if self.closed else 1, "closed": self.closed}
    worker = Owned()
    client._worker = worker
    monkeypatch.setattr(runtime, "owned_process_memory", lambda *_: (_ for _ in ()).throw(RuntimeError("optional counter failure")))
    monkeypatch.setattr(runtime, "probe_resources", resources)
    client.close()
    assert worker.closed
    assert client._metrics["process"] == {"active_processes": 0, "closed": True}
    assert client._metrics["server_memory"]["working_set_bytes"] == 0


def test_launch_disables_ram_prompt_cache_tools_network_and_thinking():
    client = runtime.LocalModelRuntime(profile())
    client._port = 1234
    client._artifact = {"executable": "C:/tools/llama-server.exe", "model_path": "C:/tools/model.gguf"}
    args = client._arguments()
    for key, value in {"--host": "127.0.0.1", "--parallel": "1", "--cache-ram": "0", "--ctx-size": "4096",
                       "--load-mode": "mmap", "--reasoning": "off", "--reasoning-budget": "0"}.items():
        assert args[args.index(key) + 1] == value
    assert "--no-agent" in args and "--no-webui" in args and "--no-context-shift" in args
    assert "--api-key" not in args


def test_environment_drops_inherited_llama_and_foreign_dll_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LLAMA_ARG_HF_REPO", "remote/model")
    monkeypatch.setenv("GGML_RPC_SERVERS", "untrusted")
    monkeypatch.setenv("PATH", "C:/foreign/dlls")
    client = runtime.LocalModelRuntime(profile())
    client._artifact = {"executable": str(tmp_path / "llama-server.exe")}
    env = client._environment(tmp_path)
    assert "LLAMA_ARG_HF_REPO" not in env and "GGML_RPC_SERVERS" not in env
    assert "foreign" not in env["PATH"]
    assert env["LLAMA_API_KEY"] and env["CUDA_VISIBLE_DEVICES"] == "0"


def test_remote_schema_does_not_get_resolved(monkeypatch):
    client, requests = fake_runtime(monkeypatch)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, {"$ref": "https://invalid.example/schema"})
    assert caught.value.code == "MODEL_SCHEMA"
    assert requests == []


@pytest.mark.parametrize("kwargs", [{"timeout": float("inf")}, {"timeout": 0}, {"max_tokens": 513},
                                    {"temperature": float("nan")}, {"seed": True}])
def test_invalid_generation_limits(monkeypatch, kwargs):
    client, requests = fake_runtime(monkeypatch)
    with pytest.raises(ModelError):
        client.generate_json(MESSAGES, SCHEMA, **kwargs)
    assert not requests


def test_cancel_check_is_independent_of_model_http():
    client = runtime.LocalModelRuntime(profile())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ModelError) as caught:
        client._check(time.monotonic() + 1, cancel)
    assert caught.value.code == "MODEL_CANCELLED"


def test_start_does_not_ignore_cancel_for_running_worker():
    client = runtime.LocalModelRuntime(profile())
    client._worker = Worker()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ModelError) as caught:
        client.start(cancel_event=cancel)
    assert caught.value.code == "MODEL_CANCELLED"


def test_cpu_profile_normalizes_missing_gpu_index():
    item = select_profile(resources=resources(gpu=False))
    item.pop("gpu_index")
    assert runtime.validate_profile(item)["gpu_index"] is None


def test_contended_runtime_lock_observes_deadline():
    client = runtime.LocalModelRuntime(profile())
    client._operation.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(ModelError) as caught:
            client.start(timeout=.1)
        assert caught.value.code == "MODEL_TIMEOUT"
        assert time.monotonic() - started < .5
    finally:
        client._operation.release()


def test_gpu_admission_rechecks_current_headroom_after_hash_verification():
    client = runtime.LocalModelRuntime(profile())
    client._artifact = {"model": {"layers": 36, "size": 2497280256}}
    client._admit(resources())
    with pytest.raises(ModelError) as caught:
        client._admit(resources(free=1024**3))
    assert caught.value.code == "MODEL_MEMORY"


@pytest.mark.parametrize("raw", ['{"reason":"\\ud800"}', '{"\\udfff":1}', '{"usage":1e999}', '[' * 65 + '0' + ']' * 65])
def test_decoded_unicode_nonfinite_numbers_and_depth_rejected(raw):
    with pytest.raises(ModelError) as caught:
        runtime.strict_json(raw)
    assert caught.value.code == "MODEL_JSON"


def test_complete_unicode_surrogate_pair_remains_valid():
    assert runtime.strict_json('"\\ud83d\\ude80"') == "\U0001f680"


def test_explicit_f16_cache_profile_keeps_default_q8_unchanged():
    item = profile()
    assert item["kv_type"] == "q8_0"
    item["kv_type"] = "f16"
    assert runtime.validate_profile(item)["kv_type"] == "f16"


def test_telemetry_failure_cannot_disable_mandatory_stderr_bound(monkeypatch, tmp_path):
    client = runtime.LocalModelRuntime(profile())
    client._log = tmp_path / "stderr.log"
    client._log.write_bytes(b"")
    monkeypatch.setattr(runtime, "MAX_LOG_BYTES", 32)
    monkeypatch.setattr(runtime, "probe_resources", lambda: {"gpus": []})
    calls = []
    def broken_memory(worker):
        calls.append("memory")
        client._log.write_bytes(b"x" * 33)
        raise OSError("telemetry unavailable")
    monkeypatch.setattr(runtime, "owned_process_memory", broken_memory)
    worker = type("Worker", (), {"terminate_tree": lambda self: calls.append("terminated")})()
    thread = threading.Thread(target=client._watch, args=(worker,), daemon=True)
    thread.start()
    thread.join(1)
    try:
        assert not thread.is_alive()
        assert calls == ["memory", "terminated"]
        assert client._failure[0] == "MODEL_LOG_LIMIT"
    finally:
        client._stop.set()
        thread.join(1)


def test_delayed_closed_and_running_samples_preserve_peaks_and_closed_current_usage():
    client = runtime.LocalModelRuntime(profile())
    client._record_memory({"peak_working_set_bytes": 100, "peak_private_committed_bytes": 200,
                           "working_set_bytes": 80, "private_committed_bytes": 120, "process_exited": False})
    client._record_memory({"process_exited": True, "working_set_bytes": 0, "private_committed_bytes": 0})
    result = client._record_memory({"peak_working_set_bytes": 150, "peak_private_committed_bytes": 180,
                                   "working_set_bytes": 140, "private_committed_bytes": 120, "process_exited": False})
    assert result["peak_working_set_bytes"] == 150 and result["peak_private_committed_bytes"] == 200
    assert result["working_set_bytes"] == result["private_committed_bytes"] == 0
    assert result["process_exited"] is True


@pytest.mark.parametrize("raw,finish,code", [
    ('{"decision":', "length", "MODEL_TRUNCATED"),
    ('{"decision":"keep","decision":"delete"}', "stop", "MODEL_JSON"),
    ('{"decision":"delete"}', "stop", "MODEL_SCHEMA"),
])
def test_rejected_generated_text_is_retained_separately_from_diagnostic(monkeypatch, raw, finish, code):
    client, _ = fake_runtime(monkeypatch, raw=raw, finish=finish)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    assert caught.value.code == code
    evidence = caught.value.rejected_response
    assert evidence["raw_text"] == raw
    assert evidence["sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert evidence["original_utf8_bytes"] == evidence["captured_utf8_bytes"] == len(raw.encode("utf-8"))
    assert evidence["truncated"] is evidence["sanitized"] is False
    assert "rejected_response" not in caught.value.as_dict()
    assert client._worker is not None


def test_rejected_evidence_is_utf8_bounded_without_splitting_a_character(monkeypatch):
    raw = "x" + "\U0001f680" * 6000
    client, _ = fake_runtime(monkeypatch, raw=raw)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    evidence = caught.value.rejected_response
    assert evidence["truncated"] is True and evidence["sanitized"] is False
    assert evidence["captured_utf8_bytes"] == 16381
    assert raw.startswith(evidence["raw_text"])
    assert evidence["original_utf8_bytes"] == len(raw.encode("utf-8"))
    json.dumps(evidence, ensure_ascii=False).encode("utf-8", errors="strict")


def test_invalid_unicode_evidence_is_explicitly_escaped_for_safe_reports(monkeypatch):
    client, _ = fake_runtime(monkeypatch, raw="bad\ud800value")
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    evidence = caught.value.rejected_response
    assert evidence["raw_text"] == r"bad\ud800value"
    assert evidence["sanitized"] is True and evidence["truncated"] is False
    json.dumps(evidence, ensure_ascii=False).encode("utf-8", errors="strict")


def test_pre_generation_failure_has_no_rejected_generated_text(monkeypatch):
    client, _ = fake_runtime(monkeypatch, tokens=4000)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    assert caught.value.rejected_response is None


def test_malformed_choice_does_not_crash_or_invent_generated_text(monkeypatch):
    client, _ = fake_runtime(monkeypatch)
    previous = client._bounded_request
    def request(method, path, body, deadline, cancel):
        if path == "/v1/chat/completions":
            return {"choices": [None]}
        return previous(method, path, body, deadline, cancel)
    monkeypatch.setattr(client, "_bounded_request", request)
    with pytest.raises(ModelError) as caught:
        client.generate_json(MESSAGES, SCHEMA)
    assert caught.value.code == "MODEL_TRUNCATED"
    assert caught.value.rejected_response is None
    assert client._worker is not None


def test_unmeasured_gpu_is_distinct_from_a_successfully_measured_zero():
    client = runtime.LocalModelRuntime(profile())
    assert client.status()["peak_gpu_device_used_bytes"] is None
    assert client.status()["gpu_samples"] == 0
    client._record_gpu(0)
    assert client.status()["peak_gpu_device_used_bytes"] == 0
    assert client.status()["gpu_samples"] == 1
    client._record_gpu(1024)
    client._record_gpu(128)
    client._record_gpu(None)
    assert client.status()["peak_gpu_device_used_bytes"] == 1024
    assert client.status()["gpu_samples"] == 3


def test_cpu_watcher_never_claims_or_probes_selected_gpu_usage(monkeypatch, tmp_path):
    client = runtime.LocalModelRuntime(select_profile(resources=resources(gpu=False)))
    client._log = tmp_path / "server.log"
    client._log.write_bytes(b"")
    waits = iter([False, True])
    client._stop = type("Stop", (), {"wait": lambda self, timeout: next(waits)})()
    monkeypatch.setattr(runtime, "owned_process_memory", lambda worker: {})
    def forbidden():
        pytest.fail("CPU profile must not probe an unselected GPU")
    monkeypatch.setattr(runtime, "probe_resources", forbidden)
    client._watch(Worker())
    assert client.status()["peak_gpu_device_used_bytes"] is None
    assert client.status()["gpu_samples"] == 0
