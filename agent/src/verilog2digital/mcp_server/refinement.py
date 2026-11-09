"""Bring-your-own-model layout refinement through recorded replay jobs.

The refinement loop writes each attempt's request.json before it asks for a
response, and replay raises EOFError once the recorded responses end. A zero-
or k-response replay job therefore reproduces the conversion, applies every
kept response through the unchanged parse/apply/reroute/native/Pareto path and
leaves the exact next prompt in its private job storage.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
import time
import uuid

from ..contracts import ContractError, validate
from ..frontend.errors import FrontendError
from ..frontend.project import Project
from ..jobs.manager import JobLimits
from ..jobs.publication import (PublicationError, bundle_bindings, contained_file, current_revision, read_document,
                                verify_sources)
from ..jobs.store import directory_bytes, file_lease
from ..llm.config import DEFAULTS, configuration
from . import results
from .runner import capped


MAX_RESPONSE_BYTES = 16 * 1024  # llm.actions bound; importing it would load pyslang into the server.
MAX_SESSIONS = 4
IDLE_SECONDS = 1800
MAX_SESSION_SECONDS = 4 * 3600
MAX_INVALID_RESPONSES = 16
MAX_PROMPT_BYTES = 256 * 1024
SESSION = re.compile(r"rs-[0-9a-f]{32}\Z")
INSTANCE = re.compile(r"[0-9a-f]{32}\Z")
METRICS = ("bounding_area", "wire_length", "bends", "crossings")
# The session no longer matches its output or sources; it is closed.
STALE = frozenset({"REFINEMENT_OUTPUT_CHANGED", "REFINEMENT_SOURCES_CHANGED"})
BASELINE = ("The output's selected revision was already refined (selected_revision). This session starts from the "
            "deterministic layout of the recorded conversion, so prompt.current_metrics describe that layout. "
            "refinement_finish publishes only a layout that is no worse than the selected revision on every canvas and "
            "metric and better on at least one.")
RULE = ("A proposal is accepted only when the rerouted candidate passes every native check (components, bits, "
        "connections, geometry and model) and its bounding_area, wire_length, bends and crossings are each less than "
        "or equal to the current verified layout's value, with at least one strictly lower. Worse, equal, tradeoff, "
        "malformed, out-of-scope, colliding and timed-out proposals keep the current layout. Every response that "
        "reaches verification consumes one attempt, accepted or not. refinement_finish publishes only after a "
        "complete job reruns the conversion with all kept responses and passes the original-Verilog behavior comparison.")
RESPOND = ("Follow prompt.messages: return one JSON object that matches prompt.response_schema, copy "
           "prompt.candidate_id exactly and move only the listed visible components or reroute visible nets. "
           "Pass it as `response` to refinement_propose. An empty actions list means no change: it ends this canvas, "
           "and a hierarchy design then continues with its next canvas.")


@dataclass
class Session:
    id: str
    output: Path
    current_sha256: str
    project: Project
    options: dict
    settings: dict
    limits: JobLimits
    sources: dict
    directory: Path
    created: float = field(default_factory=time.monotonic)
    used: float = field(default_factory=time.monotonic)
    responses: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    prompt: dict | None = None
    reason: str | None = None
    report: dict = field(default_factory=dict)
    runs: int = 0
    run: Path | None = None
    invalid: int = 0
    busy: bool = False
    selected: dict = field(default_factory=dict)

    def expired(self, now):
        return now - self.used > IDLE_SECONDS or now - self.created > MAX_SESSION_SECONDS


def _unchanged(sources, message):
    try:
        verify_sources(sources)
    except PublicationError as error:
        raise FrontendError("REFINEMENT_SOURCES_CHANGED", message, path=error.path) from error


def _canvases(report):
    """Per-canvas baseline and selected metrics of a refinement report, keyed by canvas index and module."""
    canvases = report.get("canvases") if isinstance(report, dict) else None
    return {(item.get("canvas"), item.get("module_id")): {
        "top": results.text(item.get("top"), 256), "accepted": item["accepted"] if type(item.get("accepted")) is int else 0,
        "baseline": results.metrics(item.get("baseline_metrics")), "selected": results.metrics(item.get("selected_metrics"))}
        for item in (canvases if isinstance(canvases, list) else [])[:256] if isinstance(item, dict)}


def _comparison(report, selected):
    """Metrics where the session's layout is worse than the output's selected revision, and whether any is better.

    A canvas one side never refined keeps its deterministic baseline, which both sides share."""
    mine, worse, better = _canvases(report), [], False
    for key in sorted(set(mine) | set(selected), key=str):
        ours = mine[key]["selected"] if key in mine else selected[key]["baseline"]
        theirs = selected[key]["selected"] if key in selected else mine[key]["baseline"]
        for metric in METRICS:
            if metric in ours and metric in theirs:
                if ours[metric] > theirs[metric]:
                    worse.append(f"canvas {key[0]} {metric} {ours[metric]} > {theirs[metric]}")
                better = better or ours[metric] < theirs[metric]
    return worse, better


def _remove(path, root):
    """Remove server-owned scratch only when it is an unredirected subtree of root."""
    path, root = Path(path), Path(root)
    if not path.exists() and not path.is_symlink():
        return
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or resolved == root or not resolved.is_relative_to(root):
        raise FrontendError("PRIVATE_STORAGE", "Refusing to remove redirected or unowned refinement scratch", path=str(path))
    if resolved.is_dir():
        directory_bytes(resolved, max_entries=100000)
        shutil.rmtree(resolved)
    else:
        resolved.unlink()


class Scratch:
    """A per-process area under the job root, released when its owner exits."""

    def __init__(self, job_root):
        self.base = Path(job_root).resolve() / "_mcp"
        self.base.mkdir(parents=True, exist_ok=True)
        self.base = self.base.resolve(strict=True)
        self._clean_stale()
        self.instance = uuid.uuid4().hex
        self._lease = file_lease(self.base / f"{self.instance}.lock", code="MCP_SCRATCH_BUSY")
        self._lease.__enter__()
        self.root = self.base / self.instance
        self.root.mkdir()

    def _clean_stale(self):
        """Remove areas whose owner exited; a live owner holds its lease until its area is gone."""
        names = {path.stem if path.suffix == ".lock" else path.name for path in sorted(self.base.iterdir())[:512]}
        for name in sorted(name for name in names if INSTANCE.fullmatch(name)):
            lock = self.base / f"{name}.lock"
            try:
                with file_lease(lock, code="MCP_SCRATCH_BUSY"):
                    _remove(self.base / name, self.base)
                lock.unlink(missing_ok=True)
            except (OSError, FrontendError):
                continue  # A live server owns it, or Windows still holds a handle.

    def close(self):
        if self._lease is None:
            return
        try:
            _remove(self.root, self.base)
        except (OSError, FrontendError):
            pass  # The next server start removes it.
        finally:
            self._lease.__exit__(None, None, None)
            self._lease = None
            (self.base / f"{self.instance}.lock").unlink(missing_ok=True)


def _prompt(job, canvas, attempt, request):
    metadata = request.get("metadata") if isinstance(request, dict) else None
    if (not isinstance(metadata, dict) or not isinstance(request.get("messages"), list)
            or not isinstance(request.get("schema"), dict) or not isinstance(metadata.get("candidate_id"), str)):
        raise FrontendError("REFINEMENT_PROMPT", "The recorded refinement request is malformed", path=str(job))
    return {"attempt": attempt, "canvas": {"index": canvas.get("canvas"), "top": results.text(canvas.get("top"), 256),
                                           "module_id": canvas.get("module_id")},
            "candidate_id": metadata["candidate_id"],
            "messages": [{"role": results.text(item.get("role"), 16), "content": item.get("content")}
                         for item in request["messages"] if isinstance(item, dict) and isinstance(item.get("content"), str)],
            "response_schema": request["schema"],
            "visible_instance_ids": metadata.get("visible_instance_ids", [])[:64],
            "visible_net_ids": metadata.get("visible_net_ids", [])[:64],
            "truncation": results.scalars(metadata.get("truncation"), 8),
            "current_metrics": results.metrics(canvas.get("selected_metrics")),
            "baseline_metrics": results.metrics(canvas.get("baseline_metrics"))}


def evidence(result, count):
    """Read a finished replay job before anything else can prune its storage."""
    job = Path(result["job_manifest"]).parent
    manifest = read_document(Path(result["manifest"]))
    report = manifest.get("refinement") if isinstance(manifest.get("refinement"), dict) else {}
    attempts = {}
    for canvas in report.get("canvases", []):
        for attempt in canvas.get("attempts", []) if isinstance(canvas, dict) else []:
            if isinstance(attempt, dict) and type(attempt.get("index")) is int:
                attempts[attempt["index"]] = (canvas, attempt)
    prompt, pending = None, attempts.get(count + 1)
    if pending and (pending[1].get("diagnostic") or {}).get("code") == "EOFError":
        canvas = pending[0]
        folder = job / "candidate" / "work" / "refinement" / f"canvas-{canvas['canvas']:04d}" / f"attempt-{count + 1:02d}"
        prompt = _prompt(job, canvas, count + 1, read_document(folder / "request.json", limit=MAX_PROMPT_BYTES))
    return {"result": result, "manifest": manifest, "report": report, "attempts": attempts, "prompt": prompt,
            "pending": pending, "sources": manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}}


def _reason(found, count, maximum):
    if found["prompt"] is not None:
        return None
    if count >= maximum:
        return f"All {maximum} attempts are used."
    code = (found["pending"][1].get("diagnostic") or {}).get("code") if found["pending"] else None
    if code == "TimeoutError":
        return "The refinement time budget is exhausted."
    latest = found["attempts"].get(count)
    if latest and latest[1].get("outcome") == "no_change":
        return "The last response proposed no change and no further canvas remains."
    if count == 0:
        return "This circuit has no canvas that layout refinement can change."
    return "Refinement ended without requesting another proposal."


def outcome(found, count):
    canvas, attempt = found["attempts"].get(count, (None, None))
    if attempt is None:
        return {"attempt": count, "result": "not_run", "accepted": False}
    quality = attempt.get("quality") if isinstance(attempt.get("quality"), dict) else {}
    result = {"attempt": count, "result": "accepted" if attempt.get("accepted") else results.text(attempt.get("outcome"), 32),
              "accepted": attempt.get("accepted") is True, "canvas": canvas.get("canvas")}
    if quality:
        result.update(relation=results.text(quality.get("relation"), 32), deltas_candidate_minus_current=results.metrics(quality.get("deltas")))
    if attempt.get("diagnostic"):
        result["diagnostic"] = results.diagnostic(attempt["diagnostic"])
    return result


class Refinement:
    def __init__(self, runner, policy, scratch, explicit_limits, ceiling, preflight):
        self.runner, self.policy, self.scratch, self.explicit_limits = runner, policy, scratch, explicit_limits
        self.ceiling, self.preflight = ceiling, preflight
        self.sessions = OrderedDict()

    def sweep(self):
        now = time.monotonic()
        for key in [key for key, item in self.sessions.items() if not item.busy and item.expired(now)]:
            self._drop(key)

    def _drop(self, key):
        session = self.sessions.pop(key, None)
        if session is not None:
            try:
                _remove(session.directory, self.scratch.root)
            except (OSError, FrontendError):
                pass

    def close(self):
        for key in list(self.sessions):
            self._drop(key)

    def get(self, handle):
        self.sweep()
        if not isinstance(handle, str) or not SESSION.fullmatch(handle):
            raise FrontendError("REFINEMENT_SESSION_INVALID", "A refinement session handle is rs- followed by 32 lowercase hexadecimal characters")
        session = self.sessions.get(handle)
        if session is None:
            raise FrontendError("REFINEMENT_SESSION_UNKNOWN", "This refinement session is unknown or has ended. Sessions end "
                                f"after {IDLE_SECONDS // 60} idle minutes, {MAX_SESSION_SECONDS // 3600} hours after "
                                "refinement_start, after refinement_finish, when its output or sources change, when the "
                                f"server restarts, or when refinement_start needs room beyond {MAX_SESSIONS} open sessions "
                                "and this was the least recently used idle one. Start a new one with refinement_start")
        if session.busy:
            raise FrontendError("REFINEMENT_SESSION_BUSY", "This refinement session is already running a job; wait for that call to finish")
        session.used = time.monotonic()
        self.sessions.move_to_end(handle)
        return session

    def _limits(self, recorded):
        values = {}
        if isinstance(recorded, dict):
            values = {key: recorded[key] for key in ("deadline_seconds", "memory_bytes", "process_memory_bytes",
                                                     "java_heap_bytes", "active_process_limit") if key in recorded}
        values.update({key: value for key, value in self.explicit_limits.items() if value is not None})
        # Limits recorded by another client, such as a CLI project, never exceed this server's.
        return capped(JobLimits(**values), self.ceiling)

    def _recorded(self, output):
        current, current_sha = current_revision(output)
        if current is None:
            raise FrontendError("NO_READY_RESULT", "Refinement needs a managed output folder with a selected revision "
                                "(the folder that contains current.json); run convert_design first", path=str(output))
        revision = contained_file(output, current["revision"] + "/manifest.json").parent
        manifest = read_document(revision / "manifest.json")
        bundle_bindings(revision, manifest)
        request = manifest.get("job", {}).get("request") if isinstance(manifest.get("job"), dict) else None
        project = request.get("project") if isinstance(request, dict) else None
        if (not isinstance(project, dict) or request.get("kind") != "convert" or not isinstance(request.get("options"), dict)
                or not isinstance(project.get("sources"), list) or not isinstance(manifest.get("sources"), dict)):
            raise FrontendError("REFINEMENT_UNSUPPORTED", "This result has no recorded managed conversion request; "
                                "convert it again with convert_design and refine that output", path=str(output))
        return current_sha, manifest, request, project

    async def start(self, ctx, arguments):
        self.sweep()
        output = self.policy.resolve(arguments["output"], "refinement output", kind="directory", write=True)
        current_sha, manifest, request, recorded = self._recorded(output)
        sources = dict(manifest["sources"])
        self.policy.require(sources, "recorded source or include file")
        self.policy.require(recorded.get("include_dirs", []), "recorded include directory")
        changed = ("A source or include file changed since this output was converted; run convert_design with replace=true, "
                   "then start refinement again")
        _unchanged(sources, changed)
        project = Project([Path(item) for item in recorded["sources"]], recorded.get("top"),
                          [Path(item) for item in recorded.get("include_dirs", [])],
                          dict(recorded.get("defines", {})), dict(recorded.get("parameters", {})))
        settings = {"mode": "replay", "responses": [], "max_attempts": arguments.get("max_attempts", DEFAULTS["max_attempts"]),
                    "budget_seconds": arguments.get("budget_seconds", DEFAULTS["budget_seconds"]),
                    "request": arguments.get("request", DEFAULTS["request"])}
        if arguments.get("context_tokens") is not None:
            settings["context_tokens"] = arguments["context_tokens"]
        settings = configuration(settings)
        options = {key: deepcopy(value) for key, value in request["options"].items() if key != "refinement"}
        limits = self._limits(request.get("limits"))
        while len(self.sessions) >= MAX_SESSIONS:
            idle = next((key for key, item in self.sessions.items() if not item.busy), None)
            if idle is None:
                raise FrontendError("REFINEMENT_SESSION_LIMIT", f"{MAX_SESSIONS} refinement sessions are running jobs; retry after one finishes")
            self._drop(idle)
        handle = "rs-" + uuid.uuid4().hex
        session = Session(handle, output, current_sha, project, options, settings, limits, sources, self.scratch.root / handle)
        session.selected = _canvases(manifest.get("refinement"))
        session.directory.mkdir()
        self.sessions[handle] = session

        def check(progress, cancel):
            # Every replay compiles these sources and their includes; check both before the first replay.
            _unchanged(sources, changed)
            _, files = self.preflight(project, limits, progress, cancel)
            session.sources = {**files, **sources}
        try:
            found = await self._replay(ctx, session, [], "refinement_start", check=check)
        except BaseException:
            self._drop(handle)
            raise
        if found["prompt"] is None:
            self._drop(handle)
            return {"state": "complete", "session": None, "reason": session.reason, "job_id": found["result"]["job_id"],
                    "baseline": results.refinement(found["report"]), "notice": results.UNTRUSTED}
        document = {"state": "awaiting_response"}
        if any(item["accepted"] for item in session.selected.values()):
            document["selected_revision"] = {"refined": True, "canvases": [
                {"canvas": key[0], "module_id": key[1], "top": item["top"], "selected_metrics": item["selected"]}
                for key, item in list(session.selected.items())[:16]]}
            document["diagnostics"] = [{"code": "REFINEMENT_STARTS_FROM_BASELINE", "message": BASELINE}]
        return self._status(session, found, document)

    async def _replay(self, ctx, session, responses, tool, *, final=False, check=None):
        from ..jobs import manager
        session.busy = True
        try:
            session.runs += 1
            directory = session.output if final else session.directory / f"run-{session.runs:02d}"
            settings = {**session.settings, "responses": list(responses)}
            if final:
                settings["max_attempts"] = len(responses)
            options = {**session.options, "refinement": settings}

            def work(progress, cancel):
                # Checks run while this call holds the job lock, so no queued job can change what they verified.
                if check is not None:
                    check(progress, cancel)
                result = manager.run_job(session.project, directory, options=options, limits=session.limits,
                                         replace=final, cache=False, progress=progress, cancel_event=cancel)
                return evidence(result, len(responses))

            try:
                found = await self.runner.run(ctx, tool, work)
            except BaseException:
                if not final:
                    try:
                        _remove(directory, self.scratch.root)
                    except (OSError, FrontendError):
                        pass
                raise
            self.policy.require(found["sources"], "source or include file read by the refinement job")
            if not final:
                previous, session.run = session.run, directory
                if previous is not None:
                    try:
                        _remove(previous, self.scratch.root)
                    except (OSError, FrontendError):
                        pass
                session.prompt = found["prompt"]
                session.reason = _reason(found, len(responses), session.settings["max_attempts"])
                session.report = found["report"]
            return found
        finally:
            session.busy = False
            session.used = time.monotonic()

    def _status(self, session, found, document):
        maximum = session.settings["max_attempts"]
        accepted = found["report"].get("accepted", 0) if type(found["report"].get("accepted")) is int else 0
        closing = ("Call refinement_finish to publish the accepted layout, or with publish=false to discard it." if accepted
                   else "No proposal was accepted; refinement_finish closes the session without publishing.")
        document.update(session=session.id, expires_after_idle_seconds=IDLE_SECONDS,
                        attempts={"used": len(session.responses), "max": maximum, "remaining": maximum - len(session.responses)},
                        accepted=accepted, prompt=session.prompt, reason=session.reason, acceptance_rule=RULE,
                        how_to_respond=RESPOND if session.prompt else closing,
                        refinement=results.refinement(found["report"]), job_id=found["result"]["job_id"],
                        notice=results.UNTRUSTED)
        if session.prompt is None:
            document["state"] = "complete"
        return document

    def _response(self, session, value):
        """Reject unusable text before it can take an attempt; everything else uses the full replay path."""
        def invalid(code, message):
            session.invalid += 1
            return FrontendError(code, message + f" The response did not consume an attempt ({session.invalid} of "
                                 f"{MAX_INVALID_RESPONSES} invalid responses used for this session).")

        def unique(pairs):
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError("duplicate field")
            return dict(pairs)
        try:
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise invalid("REFINEMENT_RESPONSE_INVALID", f"The response exceeds the {MAX_RESPONSE_BYTES}-byte limit.")
            batch = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))
            validate("layout-actions", batch)
        except (ValueError, UnicodeError, RecursionError, ContractError) as error:
            if isinstance(error, FrontendError):
                raise
            detail = str(error) if isinstance(error, ContractError) else "it must be one JSON object with unique fields and finite numbers"
            raise invalid("REFINEMENT_RESPONSE_INVALID", f"The response does not match prompt.response_schema: {results.text(detail, 500)}.") from error
        if batch["candidate_id"] != session.prompt["candidate_id"]:
            raise invalid("REFINEMENT_STALE_CANDIDATE", "The response's candidate_id differs from the current prompt's "
                          f"candidate_id {session.prompt['candidate_id']}; build the proposal from the latest prompt.")
        return raw

    async def propose(self, ctx, arguments):
        session = self.get(arguments["session"])
        if session.prompt is None:
            raise FrontendError("REFINEMENT_COMPLETE", f"This session accepts no further proposals: {session.reason} "
                                "Call refinement_finish to publish or discard it.")
        if session.invalid >= MAX_INVALID_RESPONSES:
            raise FrontendError("REFINEMENT_INVALID_LIMIT", "This session reached its invalid-response limit; call refinement_finish")
        raw = self._response(session, arguments["response"])
        previous = [(item["result"], item["accepted"]) for item in session.outcomes]

        def check(progress, cancel):
            _unchanged(session.sources, "A source or include file changed during refinement; start a new session")
        try:
            found = await self._replay(ctx, session, [*session.responses, raw], "refinement_propose", check=check)
        except FrontendError as error:
            if error.code in STALE:
                self._drop(session.id)
            raise
        count = len(session.responses) + 1
        session.responses.append(raw)
        latest = outcome(found, count)
        session.outcomes = [outcome(found, index) for index in range(1, count + 1)]
        document = {"state": "awaiting_response", "outcome": latest}
        if [(item["result"], item["accepted"]) for item in session.outcomes[:-1]] != previous:
            document["diagnostics"] = [{"code": "REFINEMENT_HISTORY_CHANGED", "message": "Earlier attempts did not reproduce "
                                        "their recorded outcomes in this replay; the newest verified job is authoritative"}]
        return self._status(session, found, document)

    async def finish(self, ctx, arguments):
        session = self.get(arguments["session"])
        if not arguments.get("publish", True):
            self._drop(session.id)
            return {"state": "abandoned", "published": False, "output_root": str(session.output)}
        accepted = session.report.get("accepted", 0) if type(session.report.get("accepted")) is int else 0
        if not accepted:
            self._drop(session.id)
            return {"state": "unchanged", "published": False, "output_root": str(session.output),
                    "reason": "No proposal was accepted, so the output's selected revision is unchanged."}
        if session.selected:
            # The session started from the deterministic layout; never replace a better selected revision.
            worse, better = _comparison(session.report, session.selected)
            if worse or not better:
                follow = ("propose further changes, or call refinement_finish with publish=false" if session.prompt
                          else "call refinement_finish with publish=false to close it")
                raise FrontendError("REFINEMENT_NOT_BETTER", "The session's layout is not better than the output's selected "
                                    f"revision ({'; '.join(worse[:4]) or 'every metric is equal'}). Nothing was published "
                                    f"and the session stays open: {follow}.", path=str(session.output))
        expected = [(item.get("canvas"), item.get("accepted"), results.metrics(item.get("selected_metrics")))
                    for item in session.report.get("canvases", []) if isinstance(item, dict)]

        def check(progress, cancel):
            # Under the job lock: a conversion queued before this call has selected its revision by now.
            try:
                current_sha = current_revision(session.output)[1]
            except (OSError, FrontendError):
                current_sha = None
            if current_sha != session.current_sha256:
                raise FrontendError("REFINEMENT_OUTPUT_CHANGED", "The output selected another revision after refinement_start; "
                                    "nothing was published. Start a new refinement session", path=str(session.output))
            _unchanged(session.sources, "A source or include file changed during refinement; nothing was published")
        try:
            found = await self._replay(ctx, session, list(session.responses), "refinement_finish", final=True, check=check)
        except FrontendError as error:
            if error.code in STALE:
                self._drop(session.id)
            raise
        actual = [(item.get("canvas"), item.get("accepted"), results.metrics(item.get("selected_metrics")))
                  for item in found["report"].get("canvases", []) if isinstance(item, dict)]
        self._drop(session.id)
        return {"state": "ready", "published": True, "reproduced": actual == expected, "result": found["result"],
                "manifest": found["manifest"]}
