"""Independent cooperative worker. Importing this module starts no threads or jobs."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .job_store import (InvalidTransition, Job, JobState, JobStore, JobStoreError,
                        PublicationError, SmokeRequest, StoreBusy, UnsafeArtifact)
from .research_contract import PreparedResearchResult, ResearchFailed, ResearchRequest


class JobCancelled(Exception):
    pass


class WorkerInterrupted(Exception):
    pass


@dataclass(frozen=True)
class JobContext:
    store: JobStore
    job_id: str
    worker_id: str
    output_dir: Path
    shutdown: threading.Event
    lease_lost: threading.Event

    def checkpoint(self) -> None:
        if self.lease_lost.is_set():
            raise WorkerInterrupted()
        try:
            job = self.store.check_owned(self.job_id, self.worker_id)
        except InvalidTransition:
            raise WorkerInterrupted() from None
        if job.cancellation_requested_at is not None:
            raise JobCancelled()
        if self.shutdown.is_set():
            raise WorkerInterrupted()

    def progress(self, phase: str, completed_steps: int) -> None:
        self.checkpoint()
        self.store.report_progress(self.job_id, self.worker_id, phase, completed_steps)


class JobHandler(Protocol):
    def __call__(self, request: SmokeRequest | ResearchRequest, context: JobContext) -> str | PreparedResearchResult:
        """Return an existing artifact path relative to context.output_dir, or raise."""
        ...


def smoke_handler(request: SmokeRequest, context: JobContext) -> str:
    """Offline infrastructure exercise, deliberately NOT a ResearchResult."""
    context.progress("STARTING", 0)
    completed = []
    for index in range(request.steps):
        context.checkpoint()
        completed.append(f"offline_smoke_step_{index + 1}")
        context.progress("SMOKE", len(completed))
    context.progress("FINALIZING", len(completed))
    with (context.output_dir / "smoke.json").open("x", encoding="utf-8") as handle:
        json.dump({"artifact_kind": "TEST_SMOKE", "job_id": context.job_id,
                   "completed_steps": completed, "research_performed": False,
                   "network_collection_performed": False, "ollama_inference_performed": False,
                   "market_validation": "NOT_APPLICABLE"}, handle, ensure_ascii=False, indent=2)
    return "smoke.json"


def _research_handler(request: ResearchRequest, context: JobContext) -> PreparedResearchResult:
    from .research_job import research_handler
    return research_handler(request, context)


class Worker:
    """One sequential executor; a dedicated heartbeat protects long handler phases."""
    def __init__(self, store: JobStore, *, handlers: dict[str, JobHandler] | None = None,
                 shutdown: threading.Event | None = None):
        self.store = store
        self.handlers = {"smoke": smoke_handler, "research": _research_handler} if handlers is None else dict(handlers)
        self.shutdown = shutdown if shutdown is not None else threading.Event()
        self.worker_id = None
        self._lease_lost = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._thread = None
        self._execution = threading.Lock()

    def __enter__(self):
        if self.worker_id is not None:
            raise RuntimeError("Worker instances cannot be started twice.")
        self.worker_id = self.store.register_worker()
        self._thread = threading.Thread(target=self._heartbeat, name="marketlens-job-heartbeat", daemon=True)
        self._thread.start()
        return self

    def _heartbeat(self):
        while not self._heartbeat_stop.wait(self.store.lease_seconds / 3):
            try:
                if not self.store.heartbeat(self.worker_id):
                    self._lease_lost.set()
                    return
            except StoreBusy:
                # SQLite waits at most busy_timeout; a later heartbeat may still renew a live lease.
                continue
            except JobStoreError:
                self._lease_lost.set()
                return

    def __exit__(self, exc_type, exc_value, traceback):
        self._heartbeat_stop.set()
        if self._thread:
            self._thread.join(timeout=self.store.busy_timeout + 1)
        if self.worker_id:
            self.store.stop_worker(self.worker_id)

    def _interrupted(self, job_id):
        self.store.recover_stale()
        job = self.store.get(job_id)
        if job.state == JobState.RUNNING:
            return self.store.finish(job_id, self.worker_id, JobState.INTERRUPTED)
        return job

    def run_once(self) -> Job | None:
        if self.worker_id is None:
            raise RuntimeError("Use Worker as a context manager before executing jobs.")
        with self._execution:
            if self.shutdown.is_set() or self._lease_lost.is_set():
                return None
            self.store.recover_stale()
            job = self.store.claim_next(self.worker_id)
            if job is None:
                return None
            try:
                context = JobContext(self.store, job.job_id, self.worker_id,
                                     self.store.output_directory(job.job_id), self.shutdown, self._lease_lost)
                context.checkpoint()
                # Never reuse a partial output directory, even if an operator edited the database.
                context.output_dir.mkdir(exist_ok=False)
                handler = self.handlers.get(job.job_type)
                if handler is None:
                    raise NotImplementedError("No registered handler")
                result_path = handler(job.request, context)
                context.checkpoint()
                if isinstance(result_path, PreparedResearchResult):
                    return self.store.finish(job.job_id, self.worker_id, JobState.COMPLETED,
                                             result_path=result_path.result_path, research_outcome=result_path.outcome,
                                             publication_checkpoint=context.checkpoint)
                return self.store.finish(job.job_id, self.worker_id, JobState.COMPLETED, result_path=result_path)
            except ResearchFailed as exc:
                return self.store.finish(job.job_id, self.worker_id, JobState.FAILED,
                                         error_code=exc.code, research_outcome=exc.outcome)
            except JobCancelled:
                return self.store.finish(job.job_id, self.worker_id, JobState.CANCELLED)
            except WorkerInterrupted:
                return self._interrupted(job.job_id)
            except PublicationError as exc:
                return self.store.finish(job.job_id, self.worker_id, JobState.FAILED,
                                         error_code="PUBLICATION_IO_FAILED", publication_error=exc)
            except UnsafeArtifact:
                return self.store.finish(job.job_id, self.worker_id, JobState.FAILED, error_code="INVALID_RESULT")
            except (KeyboardInterrupt, SystemExit):
                self._interrupted(job.job_id)
                raise
            except JobStoreError:
                self.shutdown.set()
                self._interrupted(job.job_id)
                raise
            except Exception:
                # Never persist exception text, repr, traceback, payload values or credentials.
                return self.store.finish(job.job_id, self.worker_id, JobState.FAILED, error_code="HANDLER_FAILED")

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("Polling interval must be between 0.1 and 60 seconds.")
        while not self.shutdown.is_set() and not self._lease_lost.is_set():
            try:
                job = self.run_once()
            except StoreBusy:
                job = None
            if job is None:
                self.shutdown.wait(poll_seconds)
