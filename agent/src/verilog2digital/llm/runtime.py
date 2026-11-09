"""Manage a local llama-server with bounded JSON requests and cleanup.

Its Windows Job stays inside the conversion worker's Job. The server binds
locally, uses installed models and exits when closed.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, ExitStack
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import threading
import time
import uuid

from jsonschema import Draft202012Validator

from ..runtime import repository_root
from ..jobs.processes import ManagedWorker, ProcessLimits
from ..jobs.store import file_lease
from .artifacts import ModelError, verify_installation, load_lock
from .resources import probe_resources, select_profile, owned_process_memory, memory_requirements

MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_LOG_BYTES = 8 * 1024**2
MAX_REJECTED_TEXT_BYTES = 16 * 1024
MAX_ALLOCATION_TAIL_BYTES = 16 * 1024
MAX_ALLOCATION_EXCERPT_BYTES = 4 * 1024


def _allocation_evidence(log):
    """Recognize allocator diagnostics from the pinned ggml implementation.

    Generic model-load failures do not establish an allocation failure.
    """
    if log is None:
        return None
    try:
        with log.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            offset = max(0, size - MAX_ALLOCATION_TAIL_BYTES)
            stream.seek(offset)
            tail = stream.read(MAX_ALLOCATION_TAIL_BYTES)
    except (OSError, ValueError):
        return None
    # Discard a partial first line before matching allocator prefixes.
    lines = tail.splitlines()[1:] if offset else tail.splitlines()
    signatures = (
        ("host", "ggml_cpu_buffer_allocation", rb"ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size [0-9]+"),
        ("host", "ggml_cpu_repack_allocation", rb"alloc_tensor_range: failed to allocate CPU_REPACK buffer of size [0-9]+"),
        ("host", "llama_cpu_repack_allocation", rb"llama_model_load: error loading model: unable to allocate CPU_REPACK buffer"),
        ("gpu", "ggml_cuda_malloc_oom", rb"ggml_backend_cuda_buffer_type_alloc_buffer: allocating [0-9.]+ MiB on device [0-9]+: cudaMalloc failed: out of memory"),
        ("gpu", "ggml_cuda_oom", rb"CUDA error: out of memory"),
    )
    for line in lines:
        # Native --verbosity 3 prefixes errors with a numeric timestamp and E.
        native = re.sub(rb"^(?:[0-9]+\.)+[0-9]+ E +", b"", line)
        for kind, signature, pattern in signatures:
            if re.fullmatch(pattern, native):
                excerpt = line.decode("utf-8", errors="replace").encode("utf-8")
                return {"source": "owned_server_stderr", "kind": kind, "signature": signature,
                        "excerpt": excerpt[:MAX_ALLOCATION_EXCERPT_BYTES].decode("utf-8", errors="ignore"),
                        "stderr_tail_sha256": hashlib.sha256(tail).hexdigest(), "stderr_bytes": size}
    return None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key[:80]!r}")
        result[key] = value
    return result


def strict_json(text):
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds its size bound")
        result = json.loads(text, object_pairs_hook=_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Nonfinite JSON: {value}")))
        pending = [(result, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if depth > 64 or count > 65536:
                raise ValueError("JSON response exceeds structural limits")
            if isinstance(value, str):
                value.encode("utf-8", errors="strict")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("JSON number overflowed to a nonfinite value")
            elif isinstance(value, dict):
                pending.extend((key, depth + 1) for key in value)
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ModelError("MODEL_JSON", f"Model response is not strict bounded JSON: {error}") from error


def _rejected_text(raw):
    if not isinstance(raw, str):
        return None
    sanitized = False
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeError:
        # Escape invalid Unicode scalars and record the transformation.
        encoded = raw.encode("utf-8", errors="backslashreplace")
        sanitized = True
    captured = encoded[:MAX_REJECTED_TEXT_BYTES].decode("utf-8", errors="ignore")
    captured_bytes = len(captured.encode("utf-8"))
    return {"raw_text": captured, "truncated": captured_bytes < len(encoded),
            "original_utf8_bytes": len(encoded), "captured_utf8_bytes": captured_bytes,
            "sha256": hashlib.sha256(encoded).hexdigest(), "sanitized": sanitized}


def _seconds(value, maximum=120):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ModelError("MODEL_LIMIT", f"Timeout must be finite and greater than zero through {maximum} seconds")
    return float(value)


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ModelError("MODEL_PROFILE", "Profile must be an object")
    profile = deepcopy(profile)
    models = load_lock()["models"]
    if profile.get("model_id") not in models or profile.get("backend") not in {"cuda", "cpu"}:
        raise ModelError("MODEL_PROFILE", "Profile must identify a pinned model and local backend")
    limits = {"context_tokens": (1024, 8192), "gpu_layers": (0, models[profile["model_id"]]["layers"] + 1),
              "batch_tokens": (32, 512), "micro_batch_tokens": (32, 256),
              "host_memory_bytes": (64 * 1024**2, 12 * 1024**3), "vram_margin_bytes": (256 * 1024**2, 4 * 1024**3),
              "idle_seconds": (1, 300), "threads": (1, 16)}
    for name, (low, high) in limits.items():
        if type(profile.get(name)) is not int or not low <= profile[name] <= high:
            raise ModelError("MODEL_PROFILE", f"{name} must be an integer from {low} through {high}")
    if (profile.get("kv_type") not in {"q8_0", "f16"} or profile["micro_batch_tokens"] > profile["batch_tokens"]
            or profile["backend"] == "cpu" and profile["gpu_layers"] != 0
            or profile["backend"] == "cuda" and (type(profile.get("gpu_index")) is not int or not 0 <= profile["gpu_index"] <= 15)):
        raise ModelError("MODEL_PROFILE", "Unsupported KV, batch or GPU configuration")
    if profile["backend"] == "cpu":
        profile["gpu_index"] = None
    profile["memory_requirements"] = memory_requirements(profile)
    profile["estimated_gpu_bytes"] = profile["memory_requirements"]["estimated_gpu_bytes"]
    return profile


class LocalModelRuntime:
    def __init__(self, profile=None, root=None):
        self.root = Path(root or repository_root()).resolve()
        self.profile = validate_profile(profile or select_profile())
        self._worker = None
        self._threads = []
        self._stop = threading.Event()
        self._guard = threading.RLock()
        self._memory_guard = threading.Lock()
        self._operation = threading.Lock()
        self._lease = None
        self._token = secrets.token_urlsafe(32)
        self._port = None
        self._log = None
        self._failure = None
        self._artifact = None
        self._artifact_guards = ExitStack()
        self._stdout_bytes = 0
        self._metrics = {"state": "unloaded", "starts": 0, "requests": 0,
                         "server_memory": {},
                         "peak_gpu_device_used_bytes": None, "gpu_samples": 0,
                         "gpu_metric": "device-wide sampled usage; includes unrelated applications; null means unmeasured"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _arguments(self):
        p = self.profile
        return [self._artifact["executable"], "--model", self._artifact["model_path"],
                "--host", "127.0.0.1", "--port", str(self._port), "--alias", p["model_id"],
                "--ctx-size", str(p["context_tokens"]), "--parallel", "1", "--batch-size", str(p["batch_tokens"]),
                "--ubatch-size", str(p["micro_batch_tokens"]), "--threads", str(p["threads"]),
                "--gpu-layers", str(p["gpu_layers"]), "--fit", "off", "--load-mode", "mmap",
                "--cache-type-k", p["kv_type"], "--cache-type-v", p["kv_type"], "--flash-attn", "on",
                "--cache-ram", "0", "--predict", "512", "--reasoning", "off", "--reasoning-budget", "0",
                "--chat-template-kwargs", '{"enable_thinking":false}', "--no-context-shift",
                "--sleep-idle-seconds", str(p["idle_seconds"]), "--verbosity", "3", "--no-webui", "--no-agent"]

    def _environment(self, directory):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("LLAMA_", "GGML_", "CUDA_", "HF_", "HUGGING_FACE_"))}
        windows = env.get("SystemRoot", r"C:\Windows")
        env["PATH"] = os.pathsep.join((str(Path(self._artifact["executable"]).parent), str(Path(windows) / "System32"), windows))
        env["TEMP"] = env["TMP"] = str(directory)
        env["LLAMA_API_KEY"] = self._token
        env["CUDA_VISIBLE_DEVICES"] = str(self.profile["gpu_index"]) if self.profile["backend"] == "cuda" else "-1"
        # Admission and sampling use nvidia-smi indices, which follow PCI bus order;
        # CUDA would otherwise number devices fastest first.
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        return env

    def _admit(self, resources):
        host = resources["host"]
        needs = memory_requirements(self.profile)
        if (host["available_commit_bytes"] < needs["required_available_host_commit_bytes"]
                or host["available_physical_bytes"] < needs["required_available_physical_bytes"]
                or self.profile["host_memory_bytes"] < needs["estimated_host_commit_bytes"]):
            error = ModelError("MODEL_MEMORY", "Available host memory changed before model startup")
            error.requested_profile = deepcopy(self.profile)
            raise error
        if self.profile["backend"] == "cuda":
            gpu = next((item for item in resources["gpus"] if item["index"] == self.profile["gpu_index"]), None)
            if gpu is None or gpu["free_bytes"] < needs["required_available_gpu_bytes"]:
                error = ModelError("MODEL_MEMORY", "Available GPU headroom changed before model startup")
                error.requested_profile = deepcopy(self.profile)
                raise error

    def _check(self, deadline, cancel_event=None, *, check_process=True):
        if cancel_event is not None and cancel_event.is_set():
            raise ModelError("MODEL_CANCELLED", "Local inference was cancelled")
        if self._failure:
            raise ModelError(*self._failure)
        if check_process and self._worker is not None:
            exit_code = self._worker.poll()
            if exit_code is not None:
                evidence = _allocation_evidence(self._log)
                error = ModelError("MODEL_ALLOCATION" if evidence else "MODEL_EXIT",
                                   f"Owned local model process exited with code {exit_code}"
                                   + (" after a native allocation failure" if evidence else ""))
                error.allocation_evidence = evidence
                raise error
        if time.monotonic() >= deadline:
            raise ModelError("MODEL_TIMEOUT", "Local inference exceeded its bounded deadline")

    @contextmanager
    def _operation_lock(self, deadline, cancel_event):
        while not self._operation.acquire(timeout=.05):
            self._check(deadline, cancel_event, check_process=False)
        try:
            self._check(deadline, cancel_event, check_process=False)
            yield
        finally:
            self._operation.release()

    def _request(self, method, path, body=None, *, timeout=1):
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if encoded is not None and len(encoded) > MAX_REQUEST_BYTES:
            raise ModelError("MODEL_CONTEXT", "Model request exceeds its byte limit")
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
        try:
            connection.request(method, path, body=encoded, headers={"Authorization": "Bearer " + self._token,
                "Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise ModelError("MODEL_RESPONSE_LIMIT", "Local server response exceeds its byte limit")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ModelError("MODEL_RESPONSE_LIMIT", "Local server response exceeds its byte limit")
            if response.status != 200:
                raise ModelError("MODEL_HTTP", f"Local server returned HTTP {response.status}: {data[:512].decode('utf-8', errors='replace')}")
            result = strict_json(data.decode("utf-8", errors="strict"))
            if not isinstance(result, dict):
                raise ModelError("MODEL_PROTOCOL", "Local server response must be a JSON object")
            return result
        finally:
            connection.close()

    def _bounded_request(self, method, path, body, deadline, cancel_event):
        output = queue.Queue(maxsize=1)
        def run():
            try:
                output.put((True, self._request(method, path, body, timeout=max(.05, deadline - time.monotonic()))))
            except BaseException as error:
                output.put((False, error))
        thread = threading.Thread(target=run, name="v2d-model-http", daemon=True)
        thread.start()
        self._threads.append(thread)
        while thread.is_alive():
            self._check(deadline, cancel_event)
            thread.join(.05)
        self._check(deadline, cancel_event)
        ok, result = output.get_nowait()
        if not ok:
            if isinstance(result, ModelError):
                raise result
            raise ModelError("MODEL_HTTP", f"Local inference connection failed: {result}") from result
        return result

    def _read_stdout(self, worker):
        try:
            while chunk := worker.stdout.read1(4096):
                self._stdout_bytes += len(chunk)
                if self._stdout_bytes > MAX_LOG_BYTES:
                    self._failure = ("MODEL_LOG_LIMIT", "Local server stdout exceeded its byte limit")
                    worker.terminate_tree()
                    return
        except (OSError, ValueError):
            return

    def _watch(self, worker):
        next_gpu = 0
        while not self._stop.wait(.1):
            try:
                if self._log.stat().st_size > MAX_LOG_BYTES:
                    self._failure = ("MODEL_LOG_LIMIT", "Local server stderr exceeded its byte limit")
                    worker.terminate_tree()
                    return
            except OSError:
                self._failure = ("MODEL_LOG_IO", "Cannot monitor the owned server stderr log")
                worker.terminate_tree()
                return
            try:
                self._record_memory(owned_process_memory(worker))
            except Exception:
                pass
            if self.profile["gpu_index"] is not None and time.monotonic() >= next_gpu:
                try:
                    resources = probe_resources()
                    gpu = next((item for item in resources["gpus"] if item["index"] == self.profile["gpu_index"]), None)
                    if gpu:
                        self._record_gpu(gpu["used_bytes"])
                except Exception:
                    pass
                next_gpu = time.monotonic() + 1

    def _record_gpu(self, used_bytes):
        if type(used_bytes) is not int or used_bytes < 0:
            return
        with self._memory_guard:
            previous = self._metrics["peak_gpu_device_used_bytes"]
            self._metrics.update(peak_gpu_device_used_bytes=max(previous or 0, used_bytes),
                                 gpu_samples=self._metrics["gpu_samples"] + 1)

    def _record_memory(self, sample):
        # Keep sampling separate from the unload lock to avoid deadlock on join.
        # Late samples may only raise recorded peaks.
        with self._memory_guard:
            old = self._metrics["server_memory"]
            merged = {**old, **sample}
            for name in ("peak_working_set_bytes", "peak_private_committed_bytes"):
                if name in old or name in sample:
                    merged[name] = max(old.get(name, 0), sample.get(name, 0))
            if old.get("process_exited") or sample.get("process_exited"):
                merged.update(process_exited=True, working_set_bytes=0, private_committed_bytes=0)
            self._metrics["server_memory"] = merged
            return dict(merged)

    def start(self, *, timeout=60, cancel_event=None):
        deadline = time.monotonic() + _seconds(timeout)
        with self._operation_lock(deadline, cancel_event):
            if self._worker is not None and self._worker.poll() is None:
                return self.status()
            if self._worker is not None:
                self.unload()
            begun = time.monotonic()
            self._failure = None
            self._stop.clear()
            self._stdout_bytes = 0
            self._metrics["server_memory"] = {}
            try:
                self._check(deadline, cancel_event)
                self._artifact = verify_installation(self.profile["model_id"], self.profile["backend"], self.root,
                                                    check=lambda: self._check(deadline, cancel_event), guards=self._artifact_guards)
                self._check(deadline, cancel_event)
                # Managed logs share the job quota; standalone logs use .runs.
                temporary = os.environ.get("V2D_JOB_TMP")
                log_root = Path(temporary).resolve() / "llm" if temporary else self.root / ".runs/llm"
                directory = log_root / uuid.uuid4().hex
                directory.mkdir(parents=True)
                self._log = directory / "server.stderr.log"
                self._lease = file_lease(self.root / ".tools/llama.cpp/runtime.lock", code="MODEL_BUSY")
                self._lease.__enter__()
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    self._port = listener.getsockname()[1]
                resources = probe_resources()
                self._admit(resources)
                self._metrics.update({"state": "loading", "resources_before": resources})
                self._worker = ManagedWorker.start(self._arguments(), cwd=directory, env=self._environment(directory),
                    stderr_path=self._log, limits=ProcessLimits(job_memory_bytes=self.profile["host_memory_bytes"], active_process_limit=4))
                worker = self._worker
                for target, name in ((self._read_stdout, "stdout"), (self._watch, "watch")):
                    thread = threading.Thread(target=target, args=(worker,), name="v2d-model-" + name, daemon=True)
                    self._threads.append(thread)
                    thread.start()
                while True:
                    self._check(deadline, cancel_event)
                    try:
                        health = self._request("GET", "/health", timeout=min(.25, max(.01, deadline - time.monotonic())))
                        if health.get("status") == "ok":
                            break
                    except (OSError, http.client.HTTPException, ModelError) as error:
                        if isinstance(error, ModelError) and error.code not in {"MODEL_HTTP"}:
                            raise
                    time.sleep(.05)
                self._metrics.update({"state": "ready", "startup_seconds": time.monotonic() - begun,
                                      "starts": self._metrics["starts"] + 1})
                return self.status()
            except BaseException:
                self.unload()
                raise

    def generate_json(self, messages, schema, *, max_tokens=512, temperature=0, seed=1, timeout=30, cancel_event=None):
        deadline = time.monotonic() + _seconds(timeout, 120)
        if (type(max_tokens) is not int or not 1 <= max_tokens <= 512 or type(seed) is not int or not 0 <= seed <= 2**31 - 1
                or type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 1
                or not isinstance(messages, list) or not 1 <= len(messages) <= 16
                or any(not isinstance(row, dict) or set(row) != {"role", "content"} or row["role"] not in {"system", "user", "assistant"}
                       or not isinstance(row["content"], str) for row in messages) or not isinstance(schema, dict)):
            raise ModelError("MODEL_REQUEST", "Invalid bounded messages, schema or generation settings")
        try:
            if len(json.dumps({"messages": messages, "schema": schema}, allow_nan=False).encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ModelError("MODEL_CONTEXT", "Model request exceeds its byte limit")
            Draft202012Validator.check_schema(schema)
        except ModelError:
            raise
        except Exception as error:
            raise ModelError("MODEL_SCHEMA", f"Invalid output schema: {error}") from error
        # Never resolve remote schema references supplied through an inference request.
        count = 0
        def refs(value, depth=0):
            nonlocal count
            count += 1
            if count > 4096 or depth > 32:
                raise ModelError("MODEL_SCHEMA", "Output schema exceeds structural bounds")
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in value and (key != "$ref" or not str(value[key]).startswith("#/$defs/")):
                        raise ModelError("MODEL_SCHEMA", "Output schema permits only local definitions references")
                for nested in value.values():
                    refs(nested, depth + 1)
            elif isinstance(value, list):
                for nested in value:
                    refs(nested, depth + 1)
        refs(schema)
        if self._worker is None:
            self.start(timeout=max(.001, deadline - time.monotonic()), cancel_event=cancel_event)
        with self._operation_lock(deadline, cancel_event):
            started = time.monotonic()
            raw = None
            try:
                envelope = {"type": "json_schema", "json_schema": {"name": "layout_action", "strict": True, "schema": schema}}
                request = {"model": self.profile["model_id"], "messages": messages, "max_tokens": max_tokens,
                           "temperature": temperature, "seed": seed, "stream": False, "response_format": envelope,
                           "chat_template_kwargs": {"enable_thinking": False}}
                # Count the model's actual formatted prompt, not a character heuristic.
                template = self._bounded_request("POST", "/apply-template", request, deadline, cancel_event)
                if not isinstance(template, dict) or not isinstance(template.get("prompt"), str):
                    raise ModelError("MODEL_PROTOCOL", "Local server omitted the formatted prompt")
                tokens = self._bounded_request("POST", "/tokenize", {"content": template["prompt"], "add_special": True,
                    "parse_special": True, "with_pieces": False}, deadline, cancel_event)
                if not isinstance(tokens, dict) or not isinstance(tokens.get("tokens"), list):
                    raise ModelError("MODEL_PROTOCOL", "Local server omitted prompt tokens")
                prompt_tokens = len(tokens["tokens"])
                if prompt_tokens + max_tokens + 16 > self.profile["context_tokens"]:
                    raise ModelError("MODEL_CONTEXT", "Formatted prompt plus output exceeds the fixed context budget")
                result = self._bounded_request("POST", "/v1/chat/completions", request, deadline, cancel_event)
                choices = result.get("choices") if isinstance(result, dict) else None
                choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else None
                message = choice.get("message") if choice else None
                raw = message.get("content") if isinstance(message, dict) else None
                if choice is None or choice.get("finish_reason") != "stop":
                    raise ModelError("MODEL_TRUNCATED", "Model response did not finish with a complete stop")
                value = strict_json(raw)
                error = next(Draft202012Validator(schema).iter_errors(value), None)
                if error is not None:
                    raise ModelError("MODEL_SCHEMA", f"Model response violates the output schema: {error.message}")
                self._metrics["requests"] += 1
                self._metrics["last_request_seconds"] = time.monotonic() - started
                return {"raw_text": raw, "value": value, "usage": result.get("usage", {}),
                        "timings": {"wall_seconds": time.monotonic() - started, "prompt_tokens": prompt_tokens,
                                    "server": result.get("timings", {})}, "model": deepcopy(self._artifact["model"]),
                        "runtime": deepcopy(self._artifact["runtime"]), "profile": deepcopy(self.profile)}
            except ModelError as error:
                if error.code in {"MODEL_JSON", "MODEL_SCHEMA", "MODEL_TRUNCATED"}:
                    error.rejected_response = _rejected_text(raw)
                if error.code not in {"MODEL_CONTEXT", "MODEL_SCHEMA", "MODEL_JSON", "MODEL_TRUNCATED", "MODEL_REQUEST"}:
                    self.unload()
                raise
            except BaseException:
                self.unload()
                raise

    def status(self):
        result = deepcopy(self._metrics)
        result["profile"] = deepcopy(self.profile)
        result["process"] = self._worker.metrics() if self._worker is not None else result.get("process", {})
        if self._worker is not None:
            try:
                result["server_memory"] = self._record_memory(owned_process_memory(self._worker))
            except OSError:
                pass
        if self._artifact:
            result["model"] = deepcopy(self._artifact["model"])
            result["runtime"] = deepcopy(self._artifact["runtime"])
        if self._worker is not None and self._worker.poll() is None:
            try:
                props = self._request("GET", "/props", timeout=.3)
                result["server"] = {name: props[name] for name in ("is_sleeping", "total_slots") if name in props}
            except (OSError, http.client.HTTPException, ModelError):
                pass
        return result

    def unload(self):
        with self._guard:
            self._stop.set()
            worker, self._worker = self._worker, None
            try:
                if worker is not None:
                    try:
                        self._record_memory(owned_process_memory(worker))
                    except Exception:
                        # Telemetry failures must not prevent server termination or lease release.
                        pass
                    worker.close()
                    self._metrics["process"] = worker.metrics()
                for thread in self._threads:
                    if thread is not threading.current_thread():
                        thread.join(4)
                self._threads.clear()
                if worker is not None:
                    self._record_memory({"process_exited": True, "working_set_bytes": 0, "private_committed_bytes": 0})
            finally:
                self._artifact_guards.close()
                if self._lease is not None:
                    lease, self._lease = self._lease, None
                    lease.__exit__(None, None, None)
                self._metrics["state"] = "unloaded"
                self._metrics["resources_after_unload"] = probe_resources()
                if self._log and self._log.exists():
                    self._metrics["stderr_bytes"] = self._log.stat().st_size
                    self._metrics["log_path"] = str(self._log)

    close = unload
