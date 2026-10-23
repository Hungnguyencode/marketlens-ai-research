"""Transactional local execution state; research evidence remains in existing artifacts."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from time import sleep
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .research_contract import (RESEARCH_PHASES, RESEARCH_STEPS, WORKSPACE, ResearchOutcome,
                                ResearchRequest, prepare_request, verify_bundle)


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "outputs/jobs/jobs.sqlite3"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class SmokeRequest(BaseModel):
    """Closed, immutable request: no credentials, commands, paths or arbitrary text."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    steps: int = Field(default=3, ge=1, le=5, strict=True)


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)
    job_id: str
    job_type: Literal["smoke", "research"]
    request: SmokeRequest | ResearchRequest
    state: JobState
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancellation_requested_at: datetime | None
    phase: str
    completed_steps: int
    total_steps: int
    error_code: str | None
    error_message: str | None
    result_path: str | None
    attempt_count: int
    execution_provenance: Literal["NOT_RUN", "TEST_SMOKE", "RESEARCH_PIPELINE"]
    worker_id: str | None
    research_outcome: ResearchOutcome | None = None


class JobStoreError(RuntimeError):
    pass


class StoreBusy(JobStoreError):
    pass


class InvalidTransition(JobStoreError):
    pass


class UnsafeArtifact(JobStoreError):
    pass


class PublicationError(JobStoreError):
    """Fixed operation and numeric OS diagnostics only; never persist exception text."""
    def __init__(self, operation: str, error: OSError, attempts: int = 1):
        if operation not in {"rename", "verify", "reference"} or type(attempts) is not int or not 1 <= attempts <= 4:
            raise ValueError("Invalid publication diagnostic.")
        numeric = lambda name: value if type(value := getattr(error, name, None)) is int else None
        self.diagnostic = (f"operation={operation}; errno={numeric('errno')}; "
                           f"winerror={numeric('winerror')}; attempts={attempts}")
        super().__init__(self.diagnostic)


class ResearchInputError(ValueError):
    """Only fixed schema/path diagnostics, safe to display through the CLI."""


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("ID must be 32 lowercase hexadecimal characters.")
    return value


ERRORS = {
    "HANDLER_FAILED": "Handler failed; exception contents are withheld to protect credentials.",
    "INVALID_RESULT": "Result artifact is missing, unsafe, incomplete or inconsistent with its job or checksums.",
    "PUBLICATION_IO_FAILED": "Filesystem publication failed; output remains unpublished. Review OS permissions or locks before a new explicit submission.",
    "WORKER_STOPPED": "Worker stopped before completion; review partial outputs before any new submission.",
    "LEASE_EXPIRED": "Worker lease expired; execution is interrupted and requires review. No automatic replay.",
    "RESEARCH_INPUT_FAILED": "Research input is missing, unsafe or changed since submission; no external work was authorized for changed data.",
    "RESEARCH_COLLECTION_FAILED": "Requested source operation failed. Unpublished diagnostic artifacts may be available.",
    "RESEARCH_INFERENCE_FAILED": "Authorized Ollama analysis failed or timed out. Diagnostics remain unpublished.",
    "RESEARCH_CONFIGURATION_FAILED": "Ollama must use a credential-free local loopback HTTP endpoint.",
}


class JobStore:
    def __init__(self, database: str | Path = DEFAULT_DATABASE, *, artifact_root: str | Path | None = None,
                 input_root: str | Path | None = None,
                 busy_timeout: float = 2.0, lease_seconds: float = 30.0, clock=None):
        if not 0 < busy_timeout <= 10 or not 3 <= lease_seconds <= 300:
            raise ValueError("busy_timeout must be (0,10] seconds; lease_seconds must be [3,300].")
        self.database = Path(database).resolve()
        self.artifact_root = Path(artifact_root or self.database.parent / "artifacts").resolve()
        self.busy_timeout, self.lease_seconds = busy_timeout, lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY, heartbeat_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL, stopped_at TEXT)""")
            settings = dict(db.execute("SELECT key,value FROM settings"))
            version = settings.get("schema_version")
            if version not in (None, "1", "2"):
                raise JobStoreError("Unsupported job database schema version.")
            if settings.get("artifact_root", str(self.artifact_root)) != str(self.artifact_root):
                raise JobStoreError("Database artifact directory differs from the persisted configuration.")
            configured_input = input_root or settings.get("input_root", WORKSPACE)
            if str(configured_input).startswith(("\\\\", "//")):
                raise JobStoreError("Input root must be a local directory, not a network share.")
            self.input_root = Path(configured_input).resolve(strict=True)
            if not self.input_root.is_dir() or settings.get("input_root", str(self.input_root)) != str(self.input_root):
                raise JobStoreError("Database input directory differs from the persisted configuration.")
            if version == "1":
                if db.execute("SELECT 1 FROM workers WHERE stopped_at IS NULL AND lease_expires_at>? LIMIT 1",
                              (self._stamp(self._now()),)).fetchone():
                    raise JobStoreError("Stop M3.1 workers and wait for their leases before upgrading this database.")
                for name in ("immutable_job_request", "allowed_job_transition"):
                    db.execute(f"DROP TRIGGER IF EXISTS {name}")
                for name in ("jobs_queue", "worker_running_job"):
                    db.execute(f"DROP INDEX IF EXISTS {name}")
                db.execute("ALTER TABLE jobs RENAME TO jobs_v1")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL CHECK(job_type IN ('smoke','research')),
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','INTERRUPTED')),
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    cancellation_requested_at TEXT, phase TEXT NOT NULL,
                    completed_steps INTEGER NOT NULL CHECK(completed_steps>=0),
                    total_steps INTEGER NOT NULL CHECK(total_steps>=completed_steps),
                    error_code TEXT, error_message TEXT, result_path TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    execution_provenance TEXT NOT NULL DEFAULT 'NOT_RUN',
                    worker_id TEXT REFERENCES workers(worker_id), outcome_json TEXT,
                    CHECK(state!='COMPLETED' OR result_path IS NOT NULL)
                )""")
            if version == "1":
                db.execute("INSERT INTO jobs SELECT *,NULL FROM jobs_v1")
                db.execute("DROP TABLE jobs_v1")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created_at, job_id)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS worker_running_job ON jobs(worker_id) WHERE state='RUNNING'")
            db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_job_request
                    BEFORE UPDATE OF job_id,job_type,request_json,created_at ON jobs
                    BEGIN SELECT RAISE(ABORT, 'immutable job request'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS allowed_job_transition BEFORE UPDATE OF state ON jobs
                    WHEN OLD.state != NEW.state AND NOT (
                        (OLD.state='QUEUED' AND NEW.state IN ('RUNNING','CANCELLED')) OR
                        (OLD.state='RUNNING' AND NEW.state IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED')))
                    BEGIN SELECT RAISE(ABORT, 'invalid job state transition'); END""")
            for key, value in (("schema_version", "2"), ("artifact_root", str(self.artifact_root)),
                               ("input_root", str(self.input_root))):
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Job clock must use timezone-aware UTC timestamps.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _stamp(value):
        return value.isoformat(timespec="microseconds")

    @contextmanager
    def _connection(self, *, write=False):
        db = None
        try:
            db = sqlite3.connect(self.database, timeout=self.busy_timeout)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            if db:
                db.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StoreBusy("Job database is busy; retry the command later. No job was replayed.") from None
            raise JobStoreError("Job database operation failed; database details are withheld.") from None
        except sqlite3.IntegrityError:
            if db:
                db.rollback()
            raise InvalidTransition("Duplicate ID, immutable request, or invalid job state transition.") from None
        except sqlite3.DatabaseError:
            if db:
                db.rollback()
            raise JobStoreError("Job database cannot be read safely; database details are withheld.") from None
        except BaseException:
            if db:
                db.rollback()
            raise
        finally:
            if db:
                db.close()

    @staticmethod
    def _job(row):
        fields = dict(row)
        request_type = ResearchRequest if fields["job_type"] == "research" else SmokeRequest
        fields["request"] = request_type.model_validate_json(fields.pop("request_json"))
        raw_outcome = fields.pop("outcome_json")
        fields["research_outcome"] = ResearchOutcome.model_validate_json(raw_outcome) if raw_outcome else None
        return Job.model_validate(fields)

    def submit(self, job_type: str, payload: dict | None = None, *, job_id: str | None = None,
               deduplicate: bool = False, expected_input_sha256: str | None = None) -> Job:
        if job_type not in {"smoke", "research"}:
            raise ValueError("Only explicitly registered smoke and research job types are available.")
        try:
            request = (prepare_request(payload or {}, self.input_root) if job_type == "research"
                       else SmokeRequest.model_validate({} if payload is None else payload))
        except (ValidationError, ValueError, OSError) as exc:
            if job_type == "research":
                if isinstance(exc, ValidationError):
                    message = "; ".join(error["msg"] for error in exc.errors(include_input=False))
                elif isinstance(exc, OSError):
                    message = "Input file missing or unreadable within the configured input root."
                else:
                    message = str(exc)
                raise ResearchInputError("Research request unavailable: " + message) from None
            raise ValueError("Invalid smoke request: only integer steps between 1 and 5 are accepted.") from None
        if expected_input_sha256 is not None and (
                job_type != "research" or request.input_sha256 != expected_input_sha256):
            raise ResearchInputError("Input changed after preview; validate again before submission.")
        job_id = validate_id(uuid4().hex if job_id is None else job_id)
        with self._connection(write=True) as db:
            if deduplicate and job_type == "research":
                # Atomic across browser sessions. UUID upload names do not change identity.
                signature = request.model_dump(exclude={"input_path"})
                for row in db.execute("SELECT * FROM jobs WHERE job_type='research' ORDER BY created_at DESC,job_id DESC"):
                    previous = self._job(row)
                    prior = previous.request.model_dump(exclude={"input_path"})
                    same_input = all(prior[key] == signature[key] for key in
                                     ("mode", "input_sha256", "source_url"))
                    same_topic = prior["topic"].casefold() == signature["topic"].casefold()
                    if prior == signature or (same_input and same_topic and previous.state in
                                               {JobState.QUEUED, JobState.RUNNING}):
                        return previous
            db.execute("""INSERT INTO jobs (job_id,job_type,request_json,state,created_at,phase,
                completed_steps,total_steps) VALUES (?,?,?,'QUEUED',?,'QUEUED',0,?)""",
                (job_id, job_type, request.model_dump_json(), self._stamp(self._now()),
                 RESEARCH_STEPS if job_type == "research" else request.steps))
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        validate_id(job_id)
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobStoreError("Job not found.")
        return self._job(row)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Job list limit must be between 1 and 1000.")
        with self._connection() as db:
            return [self._job(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC,job_id DESC LIMIT ?", (limit,))]

    def register_worker(self) -> str:
        worker_id, now = uuid4().hex, self._now()
        with self._connection(write=True) as db:
            db.execute("INSERT INTO workers VALUES (?,?,?,NULL)",
                       (worker_id, self._stamp(now), self._stamp(now + timedelta(seconds=self.lease_seconds))))
        return worker_id

    def heartbeat(self, worker_id: str) -> bool:
        validate_id(worker_id)
        now = self._now()
        with self._connection(write=True) as db:
            cursor = db.execute("""UPDATE workers SET heartbeat_at=?,lease_expires_at=?
                WHERE worker_id=? AND stopped_at IS NULL AND lease_expires_at>?""",
                (self._stamp(now), self._stamp(now + timedelta(seconds=self.lease_seconds)), worker_id, self._stamp(now)))
            return cursor.rowcount == 1

    def _owned(self, db, job_id, worker_id):
        validate_id(job_id)
        validate_id(worker_id)
        row = db.execute("""SELECT jobs.* FROM jobs JOIN workers USING(worker_id)
            WHERE job_id=? AND worker_id=? AND state='RUNNING'
                AND stopped_at IS NULL AND lease_expires_at>?""",
            (job_id, worker_id, self._stamp(self._now()))).fetchone()
        if row is None:
            raise InvalidTransition("Job is not RUNNING under this worker's live lease.")
        return row

    def claim_next(self, worker_id: str) -> Job | None:
        validate_id(worker_id)
        now = self._stamp(self._now())
        with self._connection(write=True) as db:
            if not db.execute("SELECT 1 FROM workers WHERE worker_id=? AND stopped_at IS NULL AND lease_expires_at>?",
                              (worker_id, now)).fetchone():
                raise InvalidTransition("Worker lease is missing, stopped or expired.")
            row = db.execute("SELECT job_id FROM jobs WHERE state='QUEUED' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if row is None:
                return None
            job_id = row[0]
            db.execute("""UPDATE jobs SET state='RUNNING',started_at=?,phase='STARTING',worker_id=?,
                attempt_count=attempt_count+1,execution_provenance=CASE WHEN job_type='research'
                    THEN 'RESEARCH_PIPELINE' ELSE 'TEST_SMOKE' END WHERE job_id=? AND state='QUEUED'""",
                (now, worker_id, job_id))
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> Job:
        validate_id(job_id)
        now = self._stamp(self._now())
        with self._connection(write=True) as db:
            row = db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStoreError("Job not found.")
            if row[0] not in (JobState.QUEUED, JobState.RUNNING):
                raise InvalidTransition("Only QUEUED or RUNNING jobs accept cancellation requests.")
            db.execute("UPDATE jobs SET cancellation_requested_at=COALESCE(cancellation_requested_at,?) WHERE job_id=?",
                       (now, job_id))
            if row[0] == JobState.QUEUED:
                db.execute("UPDATE jobs SET state='CANCELLED',finished_at=? WHERE job_id=?", (now, job_id))
        return self.get(job_id)

    def check_owned(self, job_id: str, worker_id: str) -> Job:
        with self._connection() as db:
            return self._job(self._owned(db, job_id, worker_id))

    def report_progress(self, job_id: str, worker_id: str, phase: str, completed_steps: int) -> None:
        if phase not in {"STARTING", "SMOKE", "FINALIZING"} | RESEARCH_PHASES or type(completed_steps) is not int:
            raise ValueError("Progress requires an allowed phase and an integer completed step count.")
        with self._connection(write=True) as db:
            row = self._owned(db, job_id, worker_id)
            if not row["completed_steps"] <= completed_steps <= row["total_steps"]:
                raise ValueError("Progress must be monotonic and cannot exceed total steps.")
            db.execute("UPDATE jobs SET phase=?,completed_steps=? WHERE job_id=?", (phase, completed_steps, job_id))

    def output_directory(self, job_id: str) -> Path:
        validate_id(job_id)
        directory = self.artifact_root / job_id
        if not directory.resolve().is_relative_to(self.artifact_root) or directory.is_symlink():
            raise UnsafeArtifact("Job output directory escapes the configured artifact root.")
        return directory

    def _result_reference(self, job_id: str, relative_path: str | Path) -> str:
        relative = Path(relative_path)
        if (relative.is_absolute() or relative.drive or ".." in relative.parts
                or ":" in str(relative_path) or "\\" in str(relative_path)):
            raise UnsafeArtifact(ERRORS["INVALID_RESULT"])
        directory = self.output_directory(job_id)
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size == 0:
            raise UnsafeArtifact(ERRORS["INVALID_RESULT"])
        return path.relative_to(self.artifact_root).as_posix()

    def _publish_research(self, db, job_id, worker_id, request, outcome, checkpoint):
        directory = self.output_directory(job_id)
        # Retry only the proven Windows access/sharing/lock errors at rename. Never
        # replay research, retry invalid checksums or replace an existing target.
        for attempt, delay in enumerate((0, 0.05, 0.15, 0.30), 1):
            if checkpoint is not None:
                checkpoint()
            if delay:
                sleep(delay)
            if checkpoint is not None:
                checkpoint()
            self._owned(db, job_id, worker_id)
            try:
                verify_bundle(directory, job_id, request, outcome)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise UnsafeArtifact(ERRORS["INVALID_RESULT"]) from None
                raise PublicationError("verify", exc, attempt) from None
            self._owned(db, job_id, worker_id)
            if (directory / "result").exists():
                raise UnsafeArtifact(ERRORS["INVALID_RESULT"])
            try:
                (directory / "_partial").rename(directory / "result")
            except OSError as exc:
                if getattr(exc, "winerror", None) in {5, 32, 33} and attempt < 4:
                    continue
                raise PublicationError("rename", exc, attempt) from None
            # The final location, not just staging, must pass before SQLite can
            # publish its reference. A failure leaves an unpublished orphan.
            try:
                verify_bundle(directory, job_id, request, outcome, directory_name="result")
                reference = self._result_reference(job_id, "result/manifest.json")
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise UnsafeArtifact(ERRORS["INVALID_RESULT"]) from None
                raise PublicationError("verify", exc, attempt) from None
            if checkpoint is not None:
                checkpoint()
            self._owned(db, job_id, worker_id)
            return reference

    def finish(self, job_id: str, worker_id: str, state: JobState, *, result_path=None, error_code=None,
               research_outcome: ResearchOutcome | None = None, publication_error: PublicationError | None = None,
               publication_checkpoint=None) -> Job:
        if state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}:
            raise InvalidTransition("Worker can only finish a RUNNING job in a terminal state.")
        if state == JobState.FAILED and error_code not in {"HANDLER_FAILED", "INVALID_RESULT", "PUBLICATION_IO_FAILED",
                "RESEARCH_INPUT_FAILED", "RESEARCH_COLLECTION_FAILED", "RESEARCH_INFERENCE_FAILED",
                "RESEARCH_CONFIGURATION_FAILED"}:
            raise ValueError("Failure requires a known safe error code.")
        if publication_error is not None and (state != JobState.FAILED or error_code != "PUBLICATION_IO_FAILED"
                                               or not isinstance(publication_error, PublicationError)):
            raise ValueError("Publication diagnostics require a filesystem publication failure.")
        if state == JobState.INTERRUPTED:
            error_code = "WORKER_STOPPED"
        elif state != JobState.FAILED:
            error_code = None
        with self._connection(write=True) as db:
            row = self._owned(db, job_id, worker_id)
            reference = None
            if state == JobState.COMPLETED:
                if row["cancellation_requested_at"]:
                    state = JobState.CANCELLED
                elif row["completed_steps"] != row["total_steps"] or result_path is None:
                    raise UnsafeArtifact(ERRORS["INVALID_RESULT"])
                else:
                    reference = self._result_reference(job_id, result_path)
                    if row["job_type"] == "research":
                        try:
                            if result_path != "_partial/manifest.json" or research_outcome is None:
                                raise ValueError("Missing prepared research outcome")
                            request = ResearchRequest.model_validate_json(row["request_json"])
                            reference = self._publish_research(db, job_id, worker_id, request, research_outcome,
                                                               publication_checkpoint)
                        except (OSError, ValueError, KeyError, TypeError):
                            raise UnsafeArtifact(ERRORS["INVALID_RESULT"]) from None
            if state == JobState.CANCELLED and not row["cancellation_requested_at"]:
                raise InvalidTransition("Cancellation requires a persisted cancellation request.")
            if state in {JobState.CANCELLED, JobState.INTERRUPTED}:
                research_outcome = None
            if row["job_type"] == "smoke" and research_outcome is not None:
                raise ValueError("Smoke jobs cannot declare research outcomes.")
            phase = "PUBLICATION" if state == JobState.COMPLETED and row["job_type"] == "research" else row["phase"]
            message = ERRORS.get(error_code)
            if publication_error is not None:
                message += " " + publication_error.diagnostic
            db.execute("""UPDATE jobs SET state=?,finished_at=?,result_path=?,error_code=?,error_message=?,outcome_json=?,phase=?
                WHERE job_id=?""", (state, self._stamp(self._now()), reference, error_code,
                                     message, research_outcome.model_dump_json() if research_outcome else None,
                                     phase, job_id))
        return self.get(job_id)

    def recover_stale(self) -> int:
        """Fence expired owners; NEVER put their jobs back on the queue."""
        now = self._stamp(self._now())
        with self._connection(write=True) as db:
            cursor = db.execute("""UPDATE jobs SET state='INTERRUPTED',finished_at=?,error_code='LEASE_EXPIRED',
                error_message=?,result_path=NULL WHERE state='RUNNING' AND worker_id IN
                (SELECT worker_id FROM workers WHERE lease_expires_at<=? OR stopped_at IS NOT NULL)""",
                (now, ERRORS["LEASE_EXPIRED"], now))
            return cursor.rowcount

    def stop_worker(self, worker_id: str) -> None:
        validate_id(worker_id)
        now = self._stamp(self._now())
        with self._connection(write=True) as db:
            db.execute("UPDATE workers SET stopped_at=? WHERE worker_id=? AND stopped_at IS NULL", (now, worker_id))
            db.execute("""UPDATE jobs SET state='INTERRUPTED',finished_at=?,error_code='WORKER_STOPPED',
                error_message=?,result_path=NULL WHERE worker_id=? AND state='RUNNING'""",
                (now, ERRORS["WORKER_STOPPED"], worker_id))
