"""Bounded proposals, native verification and retention of the best valid canvas."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time

from ..frontend.errors import FrontendError
from ..jobs.publication import atomic_document, digest
from .config import configuration


def context_byte_budget(context_tokens):
    """Budget geometry bytes while reserving room for completion.

    The 4224-byte starting budget fits typical 2048-token requests; exact
    formatted-token counts enforce the limit.
    """
    return min(6900, max(2048, (context_tokens - 640) * 3))


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _diagnostic(error, *, code=None):
    """Keep rejected external text printable without losing a valid canvas."""
    result = error.as_dict() if isinstance(error, FrontendError) else {
        "code": type(error).__name__, "message": str(error)}
    if code is not None:
        result["code"] = code
    return {key: (value[:2000].encode("utf-8", errors="backslashreplace").decode("utf-8")
                  if isinstance(value, str) else value) for key, value in result.items()}


def check_record(record):
    """Do not turn post-inspection file edits into a new trusted baseline."""
    if record["validation"].get("valid") is not True:
        raise FrontendError("REFINEMENT_BASELINE", "Refinement requires a natively validated baseline")
    for key, hash_key in (("path", "sha256"), ("sidecar_path", "sidecar_sha256")):
        if digest(Path(record["paths"][key])) != record["emitted"][hash_key]:
            raise FrontendError("ARTIFACT_CHANGED", "A verified layout artifact changed before selection")


@contextmanager
def candidate_adapter(directory, package, deadline):
    """Each attempt owns its adapter and a copy of any bound child circuits."""
    from ..adapter import Adapter
    directory.mkdir(parents=True, exist_ok=True)
    if package is not None:
        original = Path(package["root"]).resolve(strict=True)
        for item in package["modules"]:
            for name, wanted in ((item["type"], item["sha256"]),
                                 (item["type"] + ".ids.json", item["sidecar_sha256"])):
                from ..jobs.publication import contained_file
                source = contained_file(original, name)
                target = contained_file(directory, name, must_exist=False)
                if digest(source) != wanted:
                    raise FrontendError("PACKAGE_CHANGED", "A child changed before candidate isolation")
                shutil.copyfile(source, target)
                if digest(target) != wanted or digest(source) != wanted:
                    raise FrontendError("PACKAGE_CHANGED", "A child changed during candidate isolation")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("The refinement budget expired before native validation")
    with Adapter(timeout=remaining) as native:
        if package is None:
            yield native
        else:
            from ..hierarchical_conversion import PackageAdapter
            catalog = {item["type"]: deepcopy(item) for item in package["modules"]}
            yield PackageAdapter(native, directory, catalog)


class RefinementSession:
    """Share model, time and attempt limits across module canvases.

    Providers receive JSON messages only. Each attempt owns its adapter, leaving
    the conversion adapter usable after refinement fails.
    """
    def __init__(self, options, directory, *, progress=None, provider=None,
                 adapter_factory=candidate_adapter):
        self.options = configuration(options)
        self.directory = Path(directory)
        self.progress = progress
        self.provider = provider
        self.adapter_factory = adapter_factory
        self.runtime = None
        self._pending_profile = None
        self._memory_retry_used = False
        self.started = None
        self.deadline = None
        self.attempt_count = 0
        self.disabled = False
        self.closed = False
        context_tokens = self.options.get("context_tokens")
        self.context_bytes = context_byte_budget(context_tokens) if context_tokens is not None else 6900
        self.report = {"schema_version": 1, "mode": self.options["mode"],
                       "policy": "strict_native_pareto", "limits": {
                           "attempts": self.options["max_attempts"],
                           "seconds": self.options["budget_seconds"]},
                       "canvases": [], "accepted": 0, "diagnostics": [],
                       "electrical_edits_permitted": False}

    @property
    def enabled(self):
        return self.options["mode"] != "off"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _status(self):
        if self.runtime is not None:
            try:
                status = self.runtime.status()
                # Keep metrics and bindings after temporary job logs are pruned.
                status.pop("log_path", None)
                json.dumps(status, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.report["runtime"] = status
                return status
            except Exception as error:
                self.report["diagnostics"].append(_diagnostic(error, code="MODEL_STATUS"))

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception as error:
                self.report["diagnostics"].append(_diagnostic(error, code="MODEL_CLOSE"))
            finally:
                self._status()
        if self.started is not None:
            self.report["session_seconds"] = round(time.monotonic() - self.started, 6)

    def _response(self, prompt):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The refinement time budget has expired")
        if self.provider is not None:
            response = self.provider(deepcopy(prompt), timeout=remaining)
            return response if isinstance(response, dict) and "raw_text" in response else {"raw_text": response}
        if self.options["mode"] == "replay":
            index = self.attempt_count - 1
            if index >= len(self.options["responses"]):
                raise EOFError("The recorded response sequence has ended")
            return {"raw_text": self.options["responses"][index], "source": "recorded_response"}
        if self.runtime is None:
            self._prepare_runtime()
        if self.progress:
            self.progress("loading_model")
        self.runtime.start(timeout=min(90, remaining))
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Model startup exhausted the refinement time budget")
        return self.runtime.generate_json(prompt["messages"], prompt["schema"],
                    max_tokens=512, temperature=0, seed=1, timeout=min(30, remaining))

    def _prepare_runtime(self):
        if self.runtime is None and self.provider is None and self.options["mode"] == "local":
            from .runtime import LocalModelRuntime, select_profile
            profile = self._pending_profile or select_profile(self.options["model_id"],
                context_tokens=self.options.get("context_tokens"),
                host_memory_bytes=self.options.get("model_memory_mib", 6144) * 1024**2,
                idle_seconds=self.options.get("idle_seconds", 30))
            self.runtime = LocalModelRuntime(profile)
            self._pending_profile = None
            self.context_bytes = min(self.context_bytes, context_byte_budget(profile["context_tokens"]))

    def _recover_memory(self, error, attempt):
        """Schedule at most one measured smaller profile inside existing budgets."""
        from .artifacts import ModelError
        if (self.provider is not None or self.options["mode"] != "local"
                or not isinstance(error, ModelError)
                or getattr(error, "code", None) not in {"MODEL_MEMORY", "MODEL_ALLOCATION"}):
            return
        evidence = getattr(error, "allocation_evidence", None)
        evidence_valid = False
        if isinstance(evidence, dict):
            try:
                if len(json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 32768:
                    attempt["allocation_evidence"] = deepcopy(evidence)
                    evidence_valid = (evidence.get("source") == "owned_server_stderr"
                        and evidence.get("kind") in {"host", "gpu", "native"}
                        and isinstance(evidence.get("signature"), str) and 0 < len(evidence["signature"]) <= 256
                        and isinstance(evidence.get("excerpt"), str) and len(evidence["excerpt"].encode("utf-8")) <= 4096
                        and isinstance(evidence.get("stderr_tail_sha256"), str) and len(evidence["stderr_tail_sha256"]) == 64
                        and all(c in "0123456789abcdef" for c in evidence["stderr_tail_sha256"])
                        and type(evidence.get("stderr_bytes")) is int and evidence["stderr_bytes"] > 0)
            except (TypeError, ValueError, RecursionError):
                pass
        if error.code == "MODEL_ALLOCATION" and not evidence_valid:
            return
        if (self._memory_retry_used or self.attempt_count >= self.options["max_attempts"]
                or time.monotonic() >= self.deadline):
            return
        previous = self.runtime.profile if self.runtime is not None else getattr(error, "requested_profile", None)
        if not isinstance(previous, dict):
            return
        self._memory_retry_used = True
        try:
            # Release the old server and model lease before checking retry headroom.
            if self.runtime is not None:
                self.runtime.close()
                status = self._status()
                if status is not None:
                    if status.get("process") and (status["process"].get("active_processes") != 0
                                                   or status["process"].get("closed") is not True):
                        raise RuntimeError("The previous model did not finish closing its owned processes")
                    self.report.setdefault("retired_runtimes", []).append(deepcopy(status))
                self.runtime = None
            from .resources import recovery_profile
            replacement = recovery_profile(previous)
            if replacement is None or time.monotonic() >= self.deadline:
                return
            decision = {"after_attempt": self.attempt_count, "reason": error.code,
                        "action": "retry_smaller_profile", "from": deepcopy(previous),
                        "to": deepcopy(replacement)}
            self.report["memory_recovery"] = decision
            attempt["memory_recovery"] = deepcopy(decision)
            self._pending_profile = replacement
            self.disabled = False
        except Exception as recovery_error:
            self.report["diagnostics"].append(_diagnostic(recovery_error, code="MODEL_RECOVERY"))

    def refine(self, mapped, record, *, package=None):
        if not self.enabled:
            return record
        from .actions import candidate_id, parse_actions, apply_actions
        from .context import build_context, explain_verified
        from ..layout.candidates import route_placed, verify_candidate, compare_quality
        check_record(record)
        original = deepcopy(record)
        mapped = deepcopy(mapped)
        immutable_hash = _json_hash(mapped)
        best = deepcopy(record)
        identity = {"canvas": len(self.report["canvases"]), "top": mapped["top"],
                    "module_id": mapped.get("module_id")}
        if best["layout"].get("tunnels", {}).get("nets"):
            canvas = {**identity, "accepted": 0, "attempts": [],
                      "skipped": "Existing named-tunnel branches are not yet supported by fixed-placement rerouting"}
            self.report["canvases"].append(canvas)
            return {**record, "refinement": canvas}
        canvas = {**identity, "baseline_id": candidate_id(mapped, best["layout"]),
                  "baseline_metrics": deepcopy(best["validation"]["geometry"]["metrics"]),
                  "attempts": [], "accepted": 0,
                  "verified_summary": explain_verified(mapped, best["layout"], best["validation"])}
        self.report["canvases"].append(canvas)
        if self.started is None:
            self.started = time.monotonic()
            self.deadline = self.started + self.options["budget_seconds"]
        folder = self.directory / f"canvas-{canvas['canvas']:04d}"
        folder.mkdir(parents=True, exist_ok=False)
        atomic_document(folder / "baseline.json", {"candidate_id": canvas["baseline_id"],
                        "mapped": mapped, "layout": best["layout"], "validation": best["validation"]})
        while (not self.disabled and self.attempt_count < self.options["max_attempts"]
               and time.monotonic() < self.deadline):
            self.attempt_count += 1
            attempt = {"index": self.attempt_count, "accepted": False}
            canvas["attempts"].append(attempt)
            attempt_dir = folder / f"attempt-{self.attempt_count:02d}"
            attempt_dir.mkdir()
            began = time.monotonic()
            stage = "preparing_model"
            try:
                if self.progress:
                    self.progress("refining_layout")
                try:
                    self._prepare_runtime()
                except Exception:
                    self.disabled = True
                    raise
                prompt = build_context(mapped, best["layout"], validation=best["validation"],
                    request=self.options["request"],
                    max_bytes=self.context_bytes,
                    feedback=[{key: row[key] for key in ("diagnostic", "quality") if key in row}
                              for row in canvas["attempts"][-3:-1]])
                atomic_document(attempt_dir / "request.json", prompt)
                attempt["candidate_id"] = candidate_id(mapped, best["layout"])
                stage = "requesting_model"
                try:
                    response = self._response(prompt)
                except Exception as error:
                    error_code = getattr(error, "code", None)
                    rejected = getattr(error, "rejected_response", None)
                    if isinstance(rejected, dict):
                        try:
                            captured_bytes = len(json.dumps(rejected, ensure_ascii=False,
                                                           allow_nan=False).encode("utf-8"))
                        except (TypeError, ValueError, RecursionError):
                            captured_bytes = 65537
                        if captured_bytes <= 65536:
                            # Retain failed output for diagnostics, excluding it from replay.
                            atomic_document(attempt_dir / "rejected-response.json", rejected)
                            attempt["rejected_response"] = deepcopy(rejected)
                    if error_code == "MODEL_CONTEXT" and self.context_bytes > 2048:
                        self.context_bytes = max(2048, int(self.context_bytes * .7))
                    elif error_code in {"MODEL_SCHEMA", "MODEL_JSON", "MODEL_TRUNCATED"}:
                        pass  # A completed bad response can use the next bounded attempt.
                    else:
                        self.disabled = True
                    raise
                stage = "validating_response"
                raw = response["raw_text"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
                    raise ValueError("Model response must be UTF-8 JSON of at most64 KiB")
                # Preserve exactly what the model said, including invalid JSON.
                atomic_document(attempt_dir / "response.json", {"raw_text": raw})
                attempt["response"] = raw
                metadata = {key: value for key, value in response.items() if key not in {"value", "raw_text"}}
                if len(json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 16384:
                    attempt["model"] = metadata
                batch = parse_actions(raw, mapped=mapped, layout=best["layout"],
                                      expected_candidate_id=attempt["candidate_id"],
                                      visible_instance_ids=prompt["metadata"]["visible_instance_ids"],
                                      visible_net_ids=prompt["metadata"]["visible_net_ids"])
                attempt["actions"] = batch
                if not batch["actions"]:
                    attempt["outcome"] = "no_change"
                    break
                applied = apply_actions(batch, mapped, best["layout"])
                if self.progress:
                    self.progress("routing_proposal")
                candidate = route_placed(deepcopy(mapped), deepcopy(best["layout"]), applied["positions"],
                    spacing=best["layout"].get("spacing", 40), deadline=self.deadline,
                    priority_nets=applied["priority_nets"], reverse=applied["reverse"])
                with self.adapter_factory(attempt_dir, package, self.deadline) as adapter:
                    if self.progress:
                        self.progress("validating_proposal")
                    verified = verify_candidate(deepcopy(mapped), candidate, adapter, attempt_dir, deadline=self.deadline)
                quality = compare_quality(best["validation"], verified["validation"])
                attempt["quality"] = quality
                # Copy attempt evidence before attaching the refinement report
                # to avoid cycles and later mutations.
                attempt["native_validation"] = deepcopy(verified["validation"])
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("Candidate verification exceeded the refinement budget")
                if quality["improved"]:
                    check_record(verified)
                    best = {**verified, "mapped": deepcopy(mapped)}
                    attempt["accepted"] = True
                    attempt["outcome"] = "improved"
                    canvas["accepted"] += 1
                    self.report["accepted"] += 1
                else:
                    attempt["outcome"] = "retained_incumbent"
            except Exception as error:
                attempt["outcome"] = "rejected"
                attempt["diagnostic"] = _diagnostic(error)
                if stage in {"preparing_model", "requesting_model"}:
                    self._recover_memory(error, attempt)
            finally:
                attempt["seconds"] = round(time.monotonic() - began, 6)
                atomic_document(attempt_dir / "outcome.json", attempt)
            if _json_hash(mapped) != immutable_hash:
                raise FrontendError("ELECTRICAL_INTENT_CHANGED", "Candidate processing changed immutable mapped intent")
        check_record(original)
        check_record(best)
        if _json_hash(mapped) != immutable_hash:
            raise FrontendError("ELECTRICAL_INTENT_CHANGED", "Candidate processing changed immutable mapped intent")
        canvas["selected_id"] = candidate_id(mapped, best["layout"])
        canvas["selected_metrics"] = deepcopy(best["validation"]["geometry"]["metrics"])
        self._status()
        atomic_document(folder / "selection.json", canvas)
        return {**best, "refinement": canvas}


def install_candidate(record, path):
    """Install a verified pair under a not-yet-registered hierarchy basename."""
    check_record(record)
    path = Path(path)
    targets = {"path": path, "sidecar_path": path.with_name(path.name + ".ids.json")}
    for key, target in targets.items():
        source = Path(record["paths"][key])
        if source.resolve() != target.resolve():
            temporary = target.with_name(target.name + ".refinement-tmp")
            shutil.copyfile(source, temporary)
            if digest(temporary) != digest(source):
                raise FrontendError("ARTIFACT_CHANGED", "A selected candidate changed while being installed")
            temporary.replace(target)
    result = {**record, "paths": {key: str(path) for key, path in targets.items()},
              "emitted": {**record["emitted"], **{key: str(path) for key, path in targets.items()}}}
    check_record(result)
    return result
