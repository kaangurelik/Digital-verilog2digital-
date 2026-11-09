"""Supervise isolated jobs independently of native-library and routing progress."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from ..contracts import ContractError, validate
from ..frontend.errors import FrontendError
from ..runtime import development_toolchain, repository_root, require_venv
from .store import (JobStore, TERMINAL, MAX_JOB_BYTES, atomic_json, decode_json, directory_bytes, file_lease,
                    prune_jobs, read_json, recover_unlocked, utc_now)


MAX_MESSAGE_BYTES = 65536
MAX_STDERR_BYTES = 16 * 1024 * 1024


def _fingerprint():
    from ..environment import runtime_fingerprint
    return runtime_fingerprint()


class JobError(FrontendError):
    def __init__(self, code, message, *, job_id=None, job_manifest=None, **location):
        super().__init__(code, message, **location)
        self.job_id, self.job_manifest = job_id, str(job_manifest) if job_manifest else None

    def as_dict(self):
        return {**super().as_dict(), **({"job_id": self.job_id, "job_manifest": self.job_manifest} if self.job_id else {})}


@dataclass(frozen=True)
class JobLimits:
    deadline_seconds: float = 300
    memory_bytes: int = 6 * 1024**3
    process_memory_bytes: int | None = None
    java_heap_bytes: int = 1024**3
    active_process_limit: int = 32

    def validate(self):
        if (isinstance(self.deadline_seconds, bool) or not isinstance(self.deadline_seconds, (int, float))
                or not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 86400):
            raise JobError("INVALID_LIMITS", "Job deadline must be finite and greater than zero, up to 24 hours")
        if type(self.memory_bytes) is not int or not 64 * 1024**2 <= self.memory_bytes <= 1024**4:
            raise JobError("INVALID_LIMITS", "Job memory limit must be an integer from 64 MiB through 1 TiB")
        if (self.process_memory_bytes is not None and (type(self.process_memory_bytes) is not int
                or not 64 * 1024**2 <= self.process_memory_bytes <= self.memory_bytes)):
            raise JobError("INVALID_LIMITS", "Per-process memory must fit inside the total job memory limit")
        if (type(self.java_heap_bytes) is not int or not 64 * 1024**2 <= self.java_heap_bytes <= 64 * 1024**3
                or self.java_heap_bytes >= self.memory_bytes
                or self.process_memory_bytes is not None and self.java_heap_bytes >= self.process_memory_bytes):
            raise JobError("INVALID_LIMITS", "Java heap must be 64 MiB through 64 GiB and leave room within job/process memory caps")
        if type(self.active_process_limit) is not int or not 1 <= self.active_process_limit <= 128:
            raise JobError("INVALID_LIMITS", "Active process limit must be from 1 through 128")


def _store_root():
    root = Path(os.environ.get("V2D_JOB_ROOT", repository_root() / ".runs/jobs")).resolve()
    if not str(root).isascii():
        raise JobError("UNSUPPORTED_JOB_ROOT", "Set V2D_JOB_ROOT to an ASCII path for native worker storage")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_json(project):
    if project is None:
        return None
    return {"sources": [str(Path(path).resolve()) for path in project.sources], "top": project.top,
            "include_dirs": [str(Path(path).resolve()) for path in project.include_dirs],
            "defines": dict(project.defines), "parameters": dict(project.parameters)}


class _ProgressDispatcher:
    """A blocked UI/pipe observer cannot block job cancellation or deadlines."""
    def __init__(self, callback, warnings):
        self.callback, self.warnings = callback, warnings
        self.events = queue.Queue(maxsize=32)
        self.thread = None
        if callback is not None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="v2d-job-observer")
            self.thread.start()

    def _run(self):
        while True:
            event = self.events.get()
            if event is None:
                return
            try:
                self.callback(event)
            except Exception as error:
                if len(self.warnings) < 16:
                    self.warnings.append(str(error)[:1000])

    def submit(self, event):
        if self.thread is None:
            return
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # Coalesce stale progress; terminal event remains the newest one.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def close(self):
        if self.thread is not None:
            self.submit(None)
            self.thread.join(timeout=.05)
            if self.thread.is_alive() and len(self.warnings) < 16:
                self.warnings.append("Progress observer did not finish within its bounded shutdown interval")


def job_status(job_dir):
    return JobStore(Path(job_dir)).read()


def _selected_by_output(status):
    output = status["request"].get("output_root")
    if status["kind"] != "convert" or not output:
        return False
    try:
        from .publication import current_revision
        current, _ = current_revision(Path(output))
    except (OSError, ValueError):
        return False
    return current is not None and current["job_id"] == status["job_id"]


def cancel_job(job_dir):
    store = JobStore(Path(job_dir))
    status = store.read()
    if status["state"] in TERMINAL:
        return {"job_id": status["job_id"], "accepted": False, "reason": "already_terminal", "state": status["state"]}
    atomic_json(store.directory / "cancel.json", {"job_id": status["job_id"], "requested_utc": utc_now()})
    # The job may finish, or commit its selection, while the request is written.
    status = store.read()
    if status["state"] in TERMINAL:
        return {"job_id": status["job_id"], "accepted": False, "reason": "already_terminal", "state": status["state"]}
    if _selected_by_output(status):
        return {"job_id": status["job_id"], "accepted": False, "reason": "already_committed", "state": "ready"}
    return {"job_id": status["job_id"], "accepted": True, "state": "cancellation_requested"}


def recover_jobs(store_root=None):
    root = Path(store_root).resolve() if store_root else _store_root()
    root.mkdir(parents=True, exist_ok=True)
    with file_lease(root / ".manager.lock"):
        recovered = recover_unlocked(root)
        prune_jobs(root)
        return recovered


def _launch_worker(store, limits):
    from .processes import ManagedWorker, ProcessLimits
    root = repository_root()
    toolchain = development_toolchain(root)
    executable = toolchain.executable("python")
    environment = toolchain.environment(executable)
    environment["V2D_JAVA_HEAP_BYTES"] = str(limits.java_heap_bytes)
    temporary = store.directory / "tmp"
    temporary.mkdir()
    # Python's tempfile reads TMPDIR first; every temporary location belongs to the job.
    environment.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary),
                       V2D_JOB_TMP=str(temporary), V2D_JOB_ID=store.directory.name)
    return ManagedWorker.start([executable, "-X", "faulthandler", "-m", "verilog2digital.jobs.worker",
                                "--request", str(store.directory / "request.json")],
        cwd=store.directory, env=environment, stderr_path=store.directory / "worker.stderr.log",
        limits=ProcessLimits(job_memory_bytes=limits.memory_bytes,
                             process_memory_bytes=limits.process_memory_bytes,
                             active_process_limit=limits.active_process_limit))


def _read_events(stream, events, stopped):
    try:
        while not stopped.is_set():
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise JobError("WORKER_PROTOCOL", "Worker notification exceeds its bound or is not a complete line")
            event = decode_json(line)
            validate("job-event-v1", event)
            while not stopped.is_set():
                try:
                    events.put(event, timeout=.1)
                    break
                except queue.Full:
                    pass
    except Exception as error:
        while not stopped.is_set():
            try:
                events.put(error, timeout=.1)
                break
            except queue.Full:
                pass


def _checkpoint(store, deadline, cancel_event):
    if (cancel_event is not None and cancel_event.is_set()) or (store.directory / "cancel.json").exists():
        raise JobError("CANCELLED", "The conversion job was cancelled")
    if time.monotonic() >= deadline:
        raise JobError("DEADLINE_EXCEEDED", "The conversion job exceeded its total deadline")
    log = store.directory / "worker.stderr.log"
    if log.exists() and log.stat().st_size > MAX_STDERR_BYTES:
        raise JobError("JOB_LOG_LIMIT", "Worker error output exceeded its 16 MiB bound")


def _supervise(store, limits, checkpoint, progress):
    worker = None
    stopped = threading.Event()
    reader = None
    metrics = {}
    try:
        worker = _launch_worker(store, limits)
        store.update(state="running", stage="starting_worker", worker_pid=worker.pid)
        events = queue.Queue(maxsize=32)
        reader = threading.Thread(target=_read_events, args=(worker.stdout, events, stopped), daemon=True,
                                  name=f"v2d-job-{store.directory.name}")
        reader.start()
        outcome = None
        root_exited_at = None
        while True:
            checkpoint()
            for _ in range(64):
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if isinstance(event, Exception):
                    raise JobError("WORKER_PROTOCOL", f"Invalid worker notification: {event}") from event
                params = event["params"]
                if params["job_id"] != store.directory.name or outcome is not None:
                    raise JobError("WORKER_PROTOCOL", "Worker identity differs or notifications follow its terminal result")
                if event["method"] == "job.progress":
                    status = store.update(stage=params["stage"])
                    progress(status)
                else:
                    outcome = event
            exit_code = worker.poll()
            if exit_code is not None:
                if root_exited_at is None:
                    root_exited_at = time.monotonic()
                metrics = worker.metrics()
                if metrics.get("active_processes", 0):
                    # Process signaling and Job Object counters may settle at different times.
                    if time.monotonic() - root_exited_at < 1:
                        time.sleep(.02)
                        continue
                    raise JobError("WORKER_PROCESS_LEAK", "Worker exited while owned native helper processes remained alive")
                if reader.is_alive() or not events.empty():
                    reader.join(timeout=.05)
                    continue
                if outcome is None:
                    raise JobError("WORKER_CRASH", f"Worker exited without a result (exit code {exit_code})")
                if outcome["method"] == "job.error":
                    diagnostic = outcome["params"]["diagnostic"]
                    raise JobError(diagnostic["code"], diagnostic["message"],
                                   **{key: diagnostic[key] for key in ("path", "line", "column") if key in diagnostic})
                if exit_code != 0:
                    raise JobError("WORKER_CRASH", f"Worker returned a result but exited with code {exit_code}")
                path = Path(outcome["params"]["result_path"]).resolve()
                if path != store.directory / "worker-result.json":
                    raise JobError("WORKER_PROTOCOL", "Worker result must be its own fixed result artifact")
                result = read_json(path)
                if result.get("schema_version") != 2 or result.get("job_id") != store.directory.name or result.get("kind") != store.read()["kind"]:
                    raise JobError("WORKER_PROTOCOL", "Worker result identity or schema does not match its request")
                return result, metrics
            time.sleep(.05)
    finally:
        stopped.set()
        if worker is not None:
            try:
                if worker.metrics().get("active_processes", 0):
                    worker.terminate_tree()
                end = time.monotonic() + 5
                while worker.metrics().get("active_processes", 0) and time.monotonic() < end:
                    time.sleep(.02)
                metrics = worker.metrics()
                # Preserve failure metrics even when no result reaches the caller.
                atomic_json(store.directory / "process-metrics.json", metrics)
                if metrics.get("active_processes", 0):
                    raise JobError("PROCESS_CLEANUP", "Owned native processes did not exit within the cleanup bound")
            finally:
                worker.close()
        if reader is not None:
            reader.join(timeout=1)


def _run(project, output_root, *, kind, options, limits, replace, progress, cancel_event, cache):
    require_venv(repository_root())
    limits = limits or JobLimits()
    if not isinstance(limits, JobLimits):
        raise JobError("INVALID_LIMITS", "limits must be a JobLimits value")
    limits.validate()
    root = _store_root()
    with file_lease(root / ".manager.lock"):
        recover_unlocked(root)
        prune_jobs(root)
        request = {"schema_version": 2, "job_id": uuid.uuid4().hex, "kind": kind,
                   "cache": cache,
                   "project": _project_json(project), "options": dict(options or {}), "limits": asdict(limits),
                   "output_root": str(Path(output_root).resolve()) if output_root is not None else None}
        try:
            store = JobStore.create(root, request)
        except ContractError as error:
            raise JobError("INVALID_JOB_REQUEST", str(error)) from error
        started = time.monotonic()
        deadline = started + limits.deadline_seconds
        warnings = []
        dispatcher = _ProgressDispatcher(progress, warnings)
        next_disk_check = 0
        def notify(status):
            dispatcher.submit({"job_id": status["job_id"], "state": status["state"], "stage": status["stage"],
                               "sequence": status["event_seq"], "job_manifest": str(store.path)})
        def checkpoint():
            nonlocal next_disk_check
            _checkpoint(store, deadline, cancel_event)
            if time.monotonic() >= next_disk_check:
                directory_bytes(store.directory, limit=MAX_JOB_BYTES)
                next_disk_check = time.monotonic() + .5
        output_owned = False
        committed = False
        try:
            from .publication import output_lease, publish_revision
            with (output_lease(Path(output_root), replace=replace) if output_root is not None else nullcontext(None)) as lease:
                output_owned = lease is not None
                notify(store.read())
                checkpoint()
                fingerprint = _fingerprint()
                store.update(runtime_fingerprint=fingerprint)
                atomic_json(store.directory / "worker-context.json", {"schema_version": 1, "runtime_fingerprint": fingerprint,
                    "cache": {"enabled": cache, "root": str(root.parent / "cache")}})
                checkpoint()
                response, metrics = _supervise(store, limits, checkpoint, notify)
                if _fingerprint()["sha256"] != fingerprint["sha256"]:
                    raise JobError("TOOL_CHANGED", "A runtime executable, dependency or agent implementation changed during this job")
                snapshot = response.get("source_snapshot", {"sources": {}, "binding": {}, "source_map": {}})
                metadata = {"job_id": request["job_id"], "request": request, "source_snapshot": snapshot,
                            "runtime_fingerprint": fingerprint,
                            "cache": response.get("cache", {"enabled": False, "hit": False}),
                            "resources": {"limits": asdict(limits), "metrics": metrics},
                            "timings": {"worker_seconds": time.monotonic() - started}, "progress_warnings": warnings}
                store.update(source_snapshot=snapshot, resources=metadata["resources"],
                             artifacts={"worker_result": str(store.directory / "worker-result.json"),
                                        "candidate": str(store.directory / "candidate") if kind == "convert" else None})
                checkpoint()
                if kind == "convert":
                    candidate = Path(response["candidate"]["directory"]).resolve()
                    if candidate != store.directory / "candidate":
                        raise JobError("WORKER_PROTOCOL", "Worker candidate is outside its owned directory")
                    notify(store.update(stage="publishing"))
                    result = publish_revision(candidate, Path(lease["root"]), request["job_id"],
                        expected_current_sha256=lease["current_sha256"], replace=replace,
                        job_metadata=metadata, original_hashes=snapshot["sources"], checkpoint=checkpoint)
                    committed = True
                elif kind == "inspect":
                    result = {"schema_version": 2, "state": "ready", "job_id": request["job_id"],
                              "inspection": response["inspection"], "inspection_path": response["inspection_path"]}
                else:
                    result = {"schema_version": 2, "state": "ready" if response["validation"]["valid"] else "failed",
                              "job_id": request["job_id"], "validation": response["validation"],
                              "validation_path": response["validation_path"]}
                result.update(job_manifest=str(store.path), progress_warnings=warnings)
                if kind == "convert":
                    result["cache"] = metadata["cache"]
                status = store.update(state=result["state"], stage=result["state"], result=result,
                                      duration_seconds=time.monotonic() - started, progress_warnings=warnings)
                notify(status)
                return result
        except (Exception, KeyboardInterrupt) as error:
            # After current.json commits, metadata or observer errors cannot undo success.
            if committed:
                result.setdefault("progress_warnings", []).append(f"Job record update after publication: {error}")
                return result
            diagnostic = error.as_dict() if isinstance(error, FrontendError) else {
                "code": "CANCELLED" if isinstance(error, KeyboardInterrupt) else getattr(error, "code", "JOB_FAILED"),
                "message": "The conversion job was cancelled" if isinstance(error, KeyboardInterrupt) else str(error) or type(error).__name__}
            state = "cancelled" if diagnostic["code"] == "CANCELLED" else "failed"
            metrics_path = store.directory / "process-metrics.json"
            resources = {"limits": asdict(limits), "metrics": read_json(metrics_path) if metrics_path.exists() else {}}
            status = store.update(state=state, diagnostics=[diagnostic], resources=resources,
                                  duration_seconds=time.monotonic() - started, progress_warnings=warnings)
            notify(status)
            if output_owned:
                atomic_json(Path(output_root) / "failure.json", {"schema_version": 2, "state": state,
                    "job_id": request["job_id"], "job_manifest": str(store.path), "stage": status["stage"], "diagnostic": diagnostic})
            raise JobError(diagnostic["code"], diagnostic["message"], job_id=request["job_id"], job_manifest=store.path,
                           **{key: diagnostic[key] for key in ("path", "line", "column") if key in diagnostic}) from error
        finally:
            dispatcher.close()
            try:
                retention = prune_jobs(root, keep_ids={store.directory.name})
                atomic_json(root / "retention.json", retention)
            except (OSError, ValueError, FrontendError) as error:
                if len(warnings) < 16:
                    warnings.append(f"Job retention check: {error}")


def run_job(project, output_root, *, options=None, limits=None, replace=False, progress=None, cancel_event=None, cache=True):
    options = dict(options or {})
    if "refinement" in options:
        from ..llm.config import configuration
        options["refinement"] = configuration(options["refinement"])
        if options["refinement"]["mode"] == "local":
            limits = limits or JobLimits()
            if not isinstance(limits, JobLimits):
                raise JobError("INVALID_LIMITS", "limits must be a JobLimits value")
            limits.validate()
            model_bytes = options["refinement"].get("model_memory_mib", 6144) * 1024**2
            if model_bytes > limits.memory_bytes or (limits.process_memory_bytes is not None
                                                     and model_bytes > limits.process_memory_bytes):
                raise JobError("INVALID_LIMITS", "Model memory ceiling must fit within the job and per-process ceilings; "
                               "reduce --model-memory-mib or raise the corresponding job limit")
        if options["refinement"]["mode"] != "off":
            # Replay requires unchanged model and runtime files.
            cache = False
    return _run(project, output_root, kind="convert", options=options, limits=limits, replace=replace,
                progress=progress, cancel_event=cancel_event, cache=cache)


def run_inspection(project, *, limits=None, progress=None, cancel_event=None):
    return _run(project, None, kind="inspect", options={}, limits=limits, replace=False,
                progress=progress, cancel_event=cancel_event, cache=False)


def run_validation(path, *, artifacts_only=False, limits=None, progress=None, cancel_event=None):
    return _run(None, None, kind="validate", options={"target_path": str(Path(path).resolve()), "artifacts_only": artifacts_only},
                limits=limits, replace=False, progress=progress, cancel_event=cancel_event, cache=False)
