"""Transactional local execution state; research evidence remains in existing artifacts."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    job_type: Literal["smoke"]
    request: SmokeRequest
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
    execution_provenance: Literal["NOT_RUN", "TEST_SMOKE"]
    worker_id: str | None


class JobStoreError(RuntimeError):
    pass


class StoreBusy(JobStoreError):
    pass


class InvalidTransition(JobStoreError):
    pass


class UnsafeArtifact(JobStoreError):
    pass


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("ID must be 32 lowercase hexadecimal characters.")
    return value


ERRORS = {
    "HANDLER_FAILED": "Handler failed; exception contents are withheld to protect credentials.",
    "INVALID_RESULT": "Handler did not return an existing, nonempty artifact inside its job directory.",
    "WORKER_STOPPED": "Worker stopped before completion; review partial outputs before any new submission.",
    "LEASE_EXPIRED": "Worker lease expired; execution is interrupted and requires review. No automatic replay.",
}


class JobStore:
    def __init__(self, database: str | Path = DEFAULT_DATABASE, *, artifact_root: str | Path | None = None,
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY, heartbeat_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL, stopped_at TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL CHECK(job_type='smoke'),
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','INTERRUPTED')),
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    cancellation_requested_at TEXT, phase TEXT NOT NULL,
                    completed_steps INTEGER NOT NULL CHECK(completed_steps>=0),
                    total_steps INTEGER NOT NULL CHECK(total_steps>=completed_steps),
                    error_code TEXT, error_message TEXT, result_path TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    execution_provenance TEXT NOT NULL DEFAULT 'NOT_RUN',
                    worker_id TEXT REFERENCES workers(worker_id),
                    CHECK(state!='COMPLETED' OR result_path IS NOT NULL)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created_at, job_id);
                CREATE UNIQUE INDEX IF NOT EXISTS worker_running_job ON jobs(worker_id) WHERE state='RUNNING';
                CREATE TRIGGER IF NOT EXISTS immutable_job_request
                    BEFORE UPDATE OF job_id,job_type,request_json,created_at ON jobs
                    BEGIN SELECT RAISE(ABORT, 'immutable job request'); END;
                CREATE TRIGGER IF NOT EXISTS allowed_job_transition BEFORE UPDATE OF state ON jobs
                    WHEN OLD.state != NEW.state AND NOT (
                        (OLD.state='QUEUED' AND NEW.state IN ('RUNNING','CANCELLED')) OR
                        (OLD.state='RUNNING' AND NEW.state IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED')))
                    BEGIN SELECT RAISE(ABORT, 'invalid job state transition'); END;
            """)
            db.execute("BEGIN IMMEDIATE")
            for key, value in (("schema_version", "1"), ("artifact_root", str(self.artifact_root))):
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))
                if db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0] != value:
                    raise JobStoreError("Database schema or artifact directory differs from the persisted configuration.")

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
        fields["request"] = SmokeRequest.model_validate_json(fields.pop("request_json"))
        return Job.model_validate(fields)

    def submit(self, job_type: str, payload: dict | None = None, *, job_id: str | None = None) -> Job:
        if job_type != "smoke":
            raise ValueError("Only the offline smoke job is implemented; research jobs are unavailable in M3.1.")
        try:
            request = SmokeRequest.model_validate({} if payload is None else payload)
        except ValidationError:
            raise ValueError("Invalid smoke request: only integer steps between 1 and 5 are accepted.") from None
        job_id = validate_id(uuid4().hex if job_id is None else job_id)
        with self._connection(write=True) as db:
            db.execute("""INSERT INTO jobs (job_id,job_type,request_json,state,created_at,phase,
                completed_steps,total_steps) VALUES (?,?,?,'QUEUED',?,'QUEUED',0,?)""",
                (job_id, job_type, request.model_dump_json(), self._stamp(self._now()), request.steps))
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
                attempt_count=attempt_count+1,execution_provenance='TEST_SMOKE' WHERE job_id=? AND state='QUEUED'""",
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
        if phase not in {"STARTING", "SMOKE", "FINALIZING"} or type(completed_steps) is not int:
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

    def finish(self, job_id: str, worker_id: str, state: JobState, *, result_path=None, error_code=None) -> Job:
        if state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}:
            raise InvalidTransition("Worker can only finish a RUNNING job in a terminal state.")
        if state == JobState.FAILED and error_code not in {"HANDLER_FAILED", "INVALID_RESULT"}:
            raise ValueError("Failure requires a known safe error code.")
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
            if state == JobState.CANCELLED and not row["cancellation_requested_at"]:
                raise InvalidTransition("Cancellation requires a persisted cancellation request.")
            db.execute("""UPDATE jobs SET state=?,finished_at=?,result_path=?,error_code=?,error_message=?
                WHERE job_id=?""", (state, self._stamp(self._now()), reference, error_code,
                                     ERRORS.get(error_code), job_id))
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
