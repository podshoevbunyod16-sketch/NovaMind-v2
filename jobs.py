"""jobs.py — background work abstraction with explicit job states.

Long operations (web search, document parsing, media generation, agent runs,
complex MCP calls) should be enqueued instead of blocking a request.  The
in-process backend is for single-node development; a Redis/Celery backend can
implement the same ``JobStore``/``Executor`` interface in production.

States: queued → running → waiting_confirmation → succeeded | failed | cancelled | expired.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED,
                             JobStatus.CANCELLED, JobStatus.EXPIRED})


@dataclass
class Job:
    id: str
    kind: str
    owner: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Any = None
    error: str = ""
    progress: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status.value,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "progress": self.progress,
            "result": self.result if self.status in TERMINAL_STATES else None,
            "error": self.error if self.status == JobStatus.FAILED else "",
        }


class JobStore:
    """Thread-safe in-memory store. Redis backend must keep the same API."""

    def __init__(self, max_jobs: int = 2000) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, kind: str, owner: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, owner=str(owner or "")[:200])
        with self._lock:
            if len(self._jobs) >= self._max_jobs:
                oldest = min(self._jobs.values(), key=lambda item: item.created_at)
                self._jobs.pop(oldest.id, None)
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str, owner: str = "") -> Job | None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job or (owner and job.owner != owner):
                return None
            return job

    def set_status(self, job_id: str, status: JobStatus,
                   *, result: Any = None, error: str = "", progress: float = 0.0) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in TERMINAL_STATES:
                return
            job.status = status
            job.updated_at = time.time()
            job.result = result
            job.error = error[:500]
            job.progress = progress

    def expire(self, older_than_seconds: float) -> int:
        edge = time.time() - older_than_seconds
        count = 0
        with self._lock:
            for job in self._jobs.values():
                if job.status not in TERMINAL_STATES and job.created_at < edge:
                    job.status = JobStatus.EXPIRED
                    job.updated_at = time.time()
                    count += 1
        return count


class InProcessExecutor:
    """Development executor: bounded threads, no cross-node guarantees."""

    def __init__(self, store: JobStore | None = None, workers: int = 4) -> None:
        self.store = store or JobStore()
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                        thread_name_prefix="novamind-job")

    def submit(self, kind: str, fn: Callable[..., Any], *args,
               owner: str = "", **kwargs) -> Job:
        job = self.store.create(kind, owner)

        def wrapped():
            self.store.set_status(job.id, JobStatus.RUNNING)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - store a safe message
                self.store.set_status(job.id, JobStatus.FAILED, error=type(exc).__name__)
                return None
            self.store.set_status(job.id, JobStatus.SUCCEEDED, result=result, progress=1.0)
            return result

        self._pool.submit(wrapped)
        return job

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


store = JobStore()
executor: InProcessExecutor | None = None


def get_executor() -> InProcessExecutor:
    global executor
    if executor is None:
        executor = InProcessExecutor(store)
    return executor
