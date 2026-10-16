"""One COM apartment, one queue. No COM reference ever leaves its worker."""
import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from queue import Queue, Empty, Full
from threading import Lock, Thread
from uuid import uuid4

from .errors import EngineeringError
from .lock import FileLock, WorkspaceLock

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    operation: str
    arguments: dict
    id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "queued"
    submitted: str = field(default_factory=now)
    result: dict | None = None
    error: dict | None = None
    future: Future = field(default_factory=Future)

    def snapshot(self):
        return {"job_id": self.id, "operation": self.operation, "state": self.state,
                "submitted": self.submitted, "result": self.result, "error": self.error}


class StaActor:
    def __init__(self, factory, audit_path, use_com=True, operation_root=None):
        self.factory, self.audit_path, self.use_com = factory, audit_path, use_com
        self.operation_root = operation_root
        self.queue = Queue(maxsize=128)
        self.jobs = OrderedDict()
        self.lock = Lock()
        self.closed = False
        self.thread = Thread(target=self._run, name="solidworks-sta", daemon=True)
        self.thread.start()

    def _audit(self, event, job):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct MCP processes share the log; writes must not interleave.
        with FileLock(self.audit_path.with_suffix(".lock"), timeout=10):
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": now(), "pid": os.getpid(), "event": event,
                                        "arguments": job.arguments, **job.snapshot()},
                                       ensure_ascii=False, allow_nan=False) + "\n")

    def submit(self, operation, arguments):
        with self.lock:
            if self.closed:
                raise EngineeringError("SERVER_STOPPING", "Worker is stopping")
            if self.queue.full():
                raise EngineeringError("QUEUE_FULL", "SOLIDWORKS queue is full")
            job = Job(operation, arguments)
            self._audit("submitted", job)
            self.jobs[job.id] = job
            while len(self.jobs) > 512:
                first = next(iter(self.jobs))
                if not self.jobs[first].future.done():
                    break
                self.jobs.pop(first)
            self.queue.put_nowait(job)
            return job

    async def invoke(self, operation, arguments, timeout):
        job = self.submit(operation, arguments)
        try:
            # Cancellation/timeout must never cancel a running COM mutation.
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job.future)), timeout)
        except TimeoutError:
            return {"ok": False, "job_id": job.id, "state": "pending",
                    "error": {"code": "TIMEOUT_PENDING", "message":
                              "Operation may still be running. Poll sw_job_status; do not repeat the mutation."}}

    def status(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise EngineeringError("JOB_NOT_FOUND", "Job expired or belongs to another server session")
            return job.snapshot()

    def _run(self):
        backend = None
        init_error = None
        initialized = False
        try:
            if self.use_com:
                import pythoncom
                pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
                initialized = True
            backend = self.factory()
        except Exception as exc:
            init_error = str(exc)
        try:
            while True:
                try:
                    job = self.queue.get(timeout=0.05)
                except Empty:
                    if self.closed:
                        break
                    if initialized:
                        pythoncom.PumpWaitingMessages()
                    continue
                if job is None:
                    break
                try:
                    with self.lock:
                        job.state = "waiting_for_lock" if self.operation_root else "running"
                    guard = (WorkspaceLock(self.operation_root, timeout=None,
                                           on_wait=pythoncom.PumpWaitingMessages if initialized else None)
                             if self.operation_root else nullcontext())
                    with guard:
                        with self.lock:
                            job.state = "running"
                        self._audit("started", job)
                        if init_error:
                            raise EngineeringError("WORKER_UNAVAILABLE", init_error)
                        result = backend.execute(job.operation, job.arguments)
                        # Reject COM proxies, nonfinite values and unserializable results here.
                        json.dumps(result, allow_nan=False)
                        with self.lock:
                            job.result, job.state = result, "succeeded"
                except Exception as exc:
                    if isinstance(exc, EngineeringError):
                        error = exc.as_dict()
                    else:
                        log.exception("SOLIDWORKS operation failed: %s", job.operation)
                        error = {"code": "API_ERROR", "message": str(exc), "details": {
                            "partial_changes_possible": True, "advice": "Inspect document before retrying."}}
                    with self.lock:
                        job.error, job.state = error, "failed"
                try:
                    self._audit("finished", job)
                except (OSError, EngineeringError):
                    log.exception("Cannot append completion to audit log")
                job.future.set_result({"ok": job.state == "succeeded", **job.snapshot()})
                self.queue.task_done()
        finally:
            # Release all proxies in their own apartment; never quit user's application.
            backend = None
            if initialized:
                pythoncom.CoUninitialize()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.queue.put_nowait(None)
        except Full:
            pass
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            log.warning("Worker has unfinished jobs at shutdown; reconcile the audit log before retrying")
