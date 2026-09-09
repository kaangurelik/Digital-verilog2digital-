"""A persistent, bounded JSON-RPC connection to the native Digital adapter."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import uuid

from .contracts import ContractError, validate, validate_circuit
from .runtime import development_toolchain, repository_root


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def _java_heap_options() -> list[str]:
    """Jobs explicitly set a heap ceiling; standalone launches retain defaults."""
    raw = os.environ.get("V2D_JAVA_HEAP_BYTES")
    if raw is None:
        return []
    if not raw or len(raw) > 12 or not raw.isascii() or not raw.isdecimal():
        raise AdapterError("V2D_JAVA_HEAP_BYTES must contain a decimal byte count")
    size = int(raw)
    if not 64 * 1024**2 <= size <= 64 * 1024**3:
        raise AdapterError("V2D_JAVA_HEAP_BYTES must be from 64 MiB through 64 GiB")
    return ["-Xms32m", f"-Xmx{size}"]


class Adapter:
    """One outstanding request per process; a failed transport is never reused."""
    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, root: Path | str | None = None, *, timeout: float = 60):
        heap_options = _java_heap_options()
        self.root = Path(root).resolve() if root else repository_root()
        self.timeout = timeout
        self._lock = threading.RLock()
        self._next_id = 1
        self._closed = False
        self._responses: queue.Queue = queue.Queue(maxsize=16)
        toolchain = development_toolchain(self.root)
        from .release import native_asset
        jar = native_asset(self.root, "digital-adapter/target/digital-adapter.jar")
        if not jar.is_file():
            raise AdapterError("Build the adapter first: .venv/Scripts/python.exe scripts/build.py adapter")
        directory = Path(os.environ["V2D_JOB_TMP"]) / "adapter" if os.environ.get("V2D_JOB_TMP") else self.root / ".runs/adapter"
        directory.mkdir(parents=True, exist_ok=True)
        self.log_path = directory / f"{uuid.uuid4().hex}.stderr.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        java = toolchain.executable("java")
        try:
            self._process = subprocess.Popen(
                [java, *heap_options, "-Djava.awt.headless=true", "-jar", str(jar)],
                cwd=self.root, env=toolchain.environment(java), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self._log, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self._log.close()
            raise
        self._reader = threading.Thread(target=self._read_responses, daemon=True, name="digital-adapter-reader")
        self._reader.start()
        try:
            self.capabilities = self.call("capabilities", {})
            if self.capabilities.get("schema_version") != 1:
                raise AdapterError("Adapter does not support schema version 1")
        except Exception:
            self.close()
            raise

    def _read_responses(self) -> None:
        try:
            while not self._closed:
                line = self._process.stdout.readline(self.MAX_MESSAGE + 1)
                if not line:
                    self._responses.put(AdapterError("Adapter closed its output"))
                    return
                if len(line) > self.MAX_MESSAGE:
                    self._responses.put(AdapterError("Adapter response exceeds the message limit"))
                    return
                self._responses.put(line)
        except (OSError, UnicodeError, ValueError) as error:
            if not self._closed:
                self._responses.put(AdapterError(f"Cannot read adapter response: {error}"))

    def call(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        deadline = self.timeout if timeout is None else timeout
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("Adapter request timeout must be a finite positive number")
        params = {} if params is None else params
        if method == "circuit.emit":
            validate_circuit(params.get("circuit"), self.root)
        elif method == "layout.plan":
            validate("layout-request", params, self.root)
        elif method == "circuit.simulate_cycles":
            validate("simulate-cycles-request", params, self.root)
        with self._lock:
            if self._closed:
                raise AdapterError("Adapter is closed")
            request_id = self._next_id
            self._next_id += 1
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            validate("rpc-request", request, self.root)
            message = json.dumps(request, ensure_ascii=False, allow_nan=False)
            if len(message.encode("utf-8")) > self.MAX_MESSAGE:
                raise ContractError("Adapter request exceeds the message limit")
            try:
                self._process.stdin.write(message + "\n")
                self._process.stdin.flush()
                response = self._responses.get(timeout=deadline)
                if isinstance(response, Exception):
                    raise response
                response = json.loads(response)
                validate("rpc-response", response, self.root)
                if response["id"] != request_id:
                    raise AdapterError("Adapter response ID does not match its request")
            except (OSError, ValueError, queue.Empty, AdapterError) as error:
                self.close()
                reason = "Adapter request timed out" if isinstance(error, queue.Empty) else str(error)
                raise AdapterError(f"{reason}; details: {self.log_path}") from error
            if "error" in response:
                error = response["error"]
                raise AdapterError(error["message"], code=error["code"], data=error.get("data"))
            result = response["result"]
            if not isinstance(result, dict):
                raise AdapterError("Adapter result must be an object")
            if method == "circuit.inspect":
                validate("inspection", result, self.root)
            elif method == "layout.plan":
                validate("layout-response", result, self.root)
            elif method == "circuit.simulate_cycles":
                validate("simulate-cycles-response", result, self.root)
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._process.stdin.close()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=3)
            finally:
                self._process.stdout.close()
                self._log.close()
                self._reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
