"""Run blocking managed jobs one at a time, off the event loop, with MCP progress and cancellation."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import logging
import queue
import threading
import time

import anyio
import anyio.to_thread

from ..frontend.errors import FrontendError
from ..jobs.store import JOB_ID, TERMINAL


MAX_WAITING = 8
MAX_KNOWN_JOBS = 64
BUSY_WAIT_SECONDS = 30
CANCEL_GRACE_SECONDS = 30
DRAIN_SECONDS = 2
LATE_EVENT_SECONDS = .5
logger = logging.getLogger("verilog2digital.mcp")


def capped(limits, ceiling):
    """A project's or a recorded job's resources may lower the server's job limits but never raise them."""
    limits.validate()
    memory = min(limits.memory_bytes, ceiling.memory_bytes)
    result = replace(limits, deadline_seconds=min(limits.deadline_seconds, ceiling.deadline_seconds), memory_bytes=memory,
                     process_memory_bytes=None if limits.process_memory_bytes is None else min(limits.process_memory_bytes, memory),
                     java_heap_bytes=min(limits.java_heap_bytes, ceiling.java_heap_bytes),
                     active_process_limit=min(limits.active_process_limit, ceiling.active_process_limit))
    result.validate()
    return result


class JobRunner:
    """One job at a time per job root; queued calls wait instead of failing with JOB_BUSY."""

    def __init__(self):
        self._job = threading.Lock()
        self._state = threading.Lock()
        self._waiting = 0
        self._known = OrderedDict()

    def _remember(self, job_id, tool, cancel, active):
        with self._state:
            record = self._known.pop(job_id, None) or {"job_id": job_id, "tool": tool, "cancel": cancel}
            record["active"] = active
            self._known[job_id] = record
            for key in [key for key, item in self._known.items() if not item["active"]][:max(0, len(self._known) - MAX_KNOWN_JOBS)]:
                self._known.pop(key)

    def known(self, job_id):
        with self._state:
            record = self._known.get(job_id)
            return dict(record) if record else None

    def recent(self, limit=16):
        with self._state:
            return [{key: item[key] for key in ("job_id", "tool", "active")} for item in reversed(self._known.values())][:limit]

    @property
    def waiting(self):
        with self._state:
            return self._waiting

    def cancel(self, job_id):
        with self._state:
            record = self._known.get(job_id)
        if record and record["active"]:
            record["cancel"].set()
            return True
        return False

    async def run(self, ctx, tool, function):
        """Call function(progress, cancel_event) in a worker thread that holds the job lock."""
        with self._state:
            if self._waiting >= MAX_WAITING:
                raise FrontendError("JOB_QUEUE_FULL", f"{MAX_WAITING} tool calls already wait for the job runner; retry after they finish")
            self._waiting += 1
        cancel, done = threading.Event(), threading.Event()
        events = queue.Queue(maxsize=256)
        waiting, phase, gate, running = [True], ["queued"], threading.Lock(), set()

        def stop_waiting():
            with self._state:
                if waiting[0]:
                    waiting[0] = False
                    self._waiting -= 1

        def progress(event):
            if isinstance(event, dict) and isinstance(event.get("job_id"), str) and JOB_ID.fullmatch(event["job_id"]):
                active = event.get("state") not in TERMINAL
                self._remember(event["job_id"], tool, cancel, active)
                (running.add if active else running.discard)(event["job_id"])
            try:
                events.put_nowait(event)
            except queue.Full:
                pass

        def work():
            # A call cancelled before its thread starts never runs; abandon() released its place.
            with gate:
                if phase[0] != "queued":
                    return None
                phase[0] = "started"
            try:
                announced = False
                while not self._job.acquire(timeout=.1):
                    if cancel.is_set():
                        raise FrontendError("CANCELLED", "The request was cancelled while waiting for another job")
                    if not announced:
                        announced = True
                        progress({"stage": "waiting_for_previous_job"})
                stop_waiting()
                try:
                    deadline = time.monotonic() + BUSY_WAIT_SECONDS
                    while True:
                        if cancel.is_set():
                            raise FrontendError("CANCELLED", "The request was cancelled before its job started")
                        try:
                            return function(progress, cancel)
                        except FrontendError as error:
                            # Another process (desktop app or CLI) may hold the same job root.
                            if (error.code != "JOB_BUSY" or getattr(error, "job_id", None) or cancel.is_set()
                                    or time.monotonic() >= deadline):
                                raise
                        progress({"stage": "waiting_for_job_storage"})
                        time.sleep(.25)
                finally:
                    self._job.release()
            finally:
                stop_waiting()
                with self._state:
                    for record in self._known.values():
                        if record["cancel"] is cancel:
                            record["active"] = False
                done.set()

        def abandon():
            """Release the place of a call whose work never started; True when it never will."""
            with gate:
                if phase[0] == "queued":
                    phase[0] = "abandoned"
            if phase[0] != "abandoned":
                return False
            stop_waiting()
            done.set()
            return True

        outcome, drained = {}, anyio.Event()
        async with anyio.create_task_group() as group:
            group.start_soon(self._pump, ctx, events, done, running, drained)
            try:
                outcome["value"] = await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
            except anyio.get_cancelled_exc_class():
                # MCP cancellation stops the supervised job; wait for its process-tree cleanup. A cancelled call no
                # longer waits for the runner, so it gives up its place at once.
                cancel.set()
                stop_waiting()
                if not abandon():
                    with anyio.CancelScope(shield=True):
                        with anyio.move_on_after(CANCEL_GRACE_SECONDS):
                            while not done.is_set():
                                await anyio.sleep(.05)
                raise
            except Exception as error:
                abandon()
                outcome["error"] = error
            # The last stages (publishing, ready) still reach the host; a host that stops reading cannot hold the result.
            with anyio.move_on_after(DRAIN_SECONDS):
                await drained.wait()
            group.cancel_scope.cancel()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    @staticmethod
    async def _pump(ctx, events, done, running, drained):
        """Forward job events as progress until the call is done, its queue is empty and its jobs reported an end."""
        count, finished = 0, None
        try:
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    if done.is_set():
                        finished = finished or time.monotonic()
                        # The job manager's observer thread can deliver a terminal event just after the job returns.
                        if not running or time.monotonic() - finished >= LATE_EVENT_SECONDS:
                            return
                    await anyio.sleep(.02 if done.is_set() else .05)
                    continue
                count += 1
                stage = str(event.get("stage") or event.get("state") or "running")[:80]
                job = event.get("job_id")
                message = f"{stage.replace('_', ' ')} (job {job})" if isinstance(job, str) else stage.replace("_", " ")
                try:
                    await ctx.report_progress(count, None, message)
                except Exception as error:  # A closed request stream must not affect the job.
                    logger.debug("progress notification dropped: %s", error)
        finally:
            drained.set()
