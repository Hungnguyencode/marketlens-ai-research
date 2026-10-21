"""Offline execution infrastructure only; all databases and artifacts live in tmp_path."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import pytest
from pydantic import ValidationError

from market_research.job_store import (InvalidTransition, JobState, JobStore, JobStoreError,
                                       StoreBusy, UnsafeArtifact)
from market_research.job_worker import Worker, smoke_handler
from market_research.jobs import main


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "runtime/jobs.sqlite3", artifact_root=tmp_path / "SMOKE-artifacts")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def prepare_result(store, job, worker_id):
    output = store.output_directory(job.job_id)
    output.mkdir()
    (output / "smoke.json").write_text('{"artifact_kind":"TEST_SMOKE"}', encoding="utf-8")
    store.report_progress(job.job_id, worker_id, "FINALIZING", job.total_steps)
    return "smoke.json"


def test_creation_immutable_request_utc_and_restart_persistence(store):
    job = store.submit("smoke", {"steps": 2})
    assert job.state == JobState.QUEUED and job.attempt_count == 0
    assert job.execution_provenance == "NOT_RUN" and job.started_at is None
    assert job.created_at.utcoffset() == timedelta(0) and len(job.job_id) == 32
    with pytest.raises(ValidationError):
        job.request.steps = 4
    reopened = JobStore(store.database, artifact_root=store.artifact_root)
    assert reopened.get(job.job_id) == job
    with sqlite3.connect(store.database) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE jobs SET request_json='{}' WHERE job_id=?", (job.job_id,))
    assert reopened.get(job.job_id).request.steps == 2


@pytest.mark.parametrize("payload", [{"steps": 0}, {"steps": 6}, {"steps": "3"}, {"steps": True},
                                     {"steps": 1.5}, {"api_key": "SECRET_TO_REJECT"},
                                     {"command": "anything"}, {"module": "anything"}])
def test_input_validation_does_not_store_or_echo_disallowed_payload(store, payload):
    with pytest.raises(ValueError) as exc:
        store.submit("smoke", payload)
    assert "SECRET_TO_REJECT" not in str(exc.value)
    assert store.list_jobs() == []


def test_incomplete_research_request_is_rejected_and_missing_smoke_handler_fails(store):
    with pytest.raises(ValueError, match="unavailable"):
        store.submit("research", {})
    job = store.submit("smoke")
    with Worker(store, handlers={}) as worker:
        result = worker.run_once()
    assert result.state == JobState.FAILED and result.error_code == "HANDLER_FAILED"
    assert store.get(job.job_id).result_path is None


@pytest.mark.parametrize("job_id", ["", "../outside", "x" * 32, "A" * 32, "abc", "a" * 33])
def test_invalid_job_ids(store, job_id):
    with pytest.raises(ValueError):
        store.submit("smoke", job_id=job_id)
    with pytest.raises(ValueError):
        store.get(job_id)


def test_duplicate_id_and_unknown_job(store):
    job = store.submit("smoke", job_id="a" * 32)
    with pytest.raises(InvalidTransition):
        store.submit("smoke", job_id=job.job_id)
    with pytest.raises(JobStoreError, match="not found"):
        store.get("b" * 32)
    assert len(store.list_jobs()) == 1


def test_states_ownership_and_progress_are_enforced(store):
    queued = store.submit("smoke")
    first, second = store.register_worker(), store.register_worker()
    with pytest.raises(InvalidTransition):
        store.finish(queued.job_id, first, JobState.COMPLETED, result_path="smoke.json")
    job = store.claim_next(first)
    assert job.attempt_count == 1 and job.started_at and job.execution_provenance == "TEST_SMOKE"
    for owner in [second, "0" * 32]:
        with pytest.raises(InvalidTransition):
            store.report_progress(job.job_id, owner, "SMOKE", 1)
    store.report_progress(job.job_id, first, "SMOKE", 1)
    for phase, count in [("SMOKE", 0), ("SMOKE", 10), ("SECRET_TEXT", 2), ("SMOKE", True)]:
        with pytest.raises(ValueError):
            store.report_progress(job.job_id, first, phase, count)
    with pytest.raises(InvalidTransition):
        store.finish(job.job_id, first, JobState.QUEUED)
    with pytest.raises(InvalidTransition):
        store.finish(job.job_id, first, JobState.CANCELLED)
    path = prepare_result(store, job, first)
    done = store.finish(job.job_id, first, JobState.COMPLETED, result_path=path)
    assert done.finished_at and done.result_path == f"{job.job_id}/smoke.json"
    with pytest.raises(InvalidTransition):
        store.finish(job.job_id, first, JobState.FAILED, error_code="HANDLER_FAILED")
    with pytest.raises(InvalidTransition):
        store.request_cancel(job.job_id)
    with sqlite3.connect(store.database) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET state='QUEUED' WHERE job_id=?", (job.job_id,))
    assert store.claim_next(second) is None


def test_atomic_claim_two_concurrent_workers(store):
    job = store.submit("smoke")
    owners = [store.register_worker(), store.register_worker()]
    barrier = threading.Barrier(2)

    def claim(owner):
        independent = JobStore(store.database, artifact_root=store.artifact_root)
        barrier.wait(timeout=5)
        return independent.claim_next(owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, owners))
    assert sum(result is not None for result in results) == 1
    assert store.get(job.job_id).attempt_count == 1


def test_second_worker_does_not_interrupt_live_owner_or_execute_it(store):
    job = store.submit("smoke")
    entered, release = threading.Event(), threading.Event()

    def handler(request, context):
        entered.set()
        assert release.wait(5)
        return smoke_handler(request, context)

    with Worker(store, handlers={"smoke": handler}) as first, Worker(store) as second:
        with ThreadPoolExecutor(max_workers=1) as pool:
            execution = pool.submit(first.run_once)
            try:
                assert entered.wait(5)
                assert second.run_once() is None
                assert store.get(job.job_id).state == JobState.RUNNING
                assert store.recover_stale() == 0
            finally:
                release.set()
            assert execution.result(timeout=5).state == JobState.COMPLETED
    assert store.get(job.job_id).attempt_count == 1


def test_successful_smoke_is_explicitly_not_research_and_no_overwrite(store):
    job = store.submit("smoke", {"steps": 2})
    with Worker(store) as worker:
        result = worker.run_once()
        assert worker.run_once() is None
    assert result.job_id == job.job_id and result.state == JobState.COMPLETED
    assert result.phase == "FINALIZING" and result.completed_steps == result.total_steps == 2
    path = store.artifact_root / result.result_path
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["artifact_kind"] == "TEST_SMOKE"
    assert artifact["market_validation"] == "NOT_APPLICABLE"
    assert artifact["research_performed"] is artifact["ollama_inference_performed"] is False
    before = path.read_bytes()
    with Worker(store) as next_worker:
        assert next_worker.run_once() is None
    assert before == path.read_bytes()
    assert JobStore(store.database, artifact_root=store.artifact_root).get(job.job_id) == result


def test_handler_failure_is_sanitized_and_keeps_partial_output_unpublished(store):
    secret = "api_key=DO_NOT_PERSIST_THIS"

    def broken(request, context):
        (context.output_dir / "partial.txt").write_text("TEST partial", encoding="utf-8")
        raise RuntimeError(secret)

    store.submit("smoke")
    with Worker(store, handlers={"smoke": broken}) as worker:
        result = worker.run_once()
    assert result.state == JobState.FAILED and result.error_code == "HANDLER_FAILED"
    assert result.result_path is None and secret not in result.model_dump_json()
    with sqlite3.connect(store.database) as db:
        assert secret not in " ".join(str(tuple(row)) for row in db.execute("SELECT * FROM jobs"))


def test_queued_and_running_cancellation_semantics(store):
    queued = store.submit("smoke")
    cancelled = store.request_cancel(queued.job_id)
    assert cancelled.state == JobState.CANCELLED and cancelled.attempt_count == 0
    assert cancelled.started_at is None and cancelled.finished_at is not None

    def cancellation_without_handler_checkpoint(request, context):
        (context.output_dir / "smoke.json").write_text('{"artifact_kind":"TEST_SMOKE"}', encoding="utf-8")
        store.report_progress(context.job_id, context.worker_id, "FINALIZING", request.steps)
        requested = store.request_cancel(context.job_id)
        assert requested.state == JobState.RUNNING and requested.finished_at is None
        return "smoke.json"

    running = store.submit("smoke")
    with Worker(store, handlers={"smoke": cancellation_without_handler_checkpoint}) as worker:
        result = worker.run_once()
    assert result.job_id == running.job_id and result.state == JobState.CANCELLED
    assert result.result_path is None and result.cancellation_requested_at is not None


def test_cancellation_racing_success_wins_before_terminal_commit(store):
    job = store.submit("smoke")
    owner = store.register_worker()
    store.claim_next(owner)
    path = prepare_result(store, job, owner)
    store.request_cancel(job.job_id)
    result = store.finish(job.job_id, owner, JobState.COMPLETED, result_path=path)
    assert result.state == JobState.CANCELLED and result.result_path is None


@pytest.mark.parametrize("reference", ["../outside.json", "/outside.json", "C:\\outside.json", "x.json:secret",
                                       "missing.json", "."])
def test_result_path_safety_and_missing_artifact(store, reference):
    job = store.submit("smoke")
    owner = store.register_worker()
    store.claim_next(owner)
    prepare_result(store, job, owner)
    with pytest.raises(UnsafeArtifact):
        store.finish(job.job_id, owner, JobState.COMPLETED, result_path=reference)
    assert store.get(job.job_id).state == JobState.RUNNING


def test_success_requires_completed_steps_and_nonempty_artifact(store):
    job = store.submit("smoke")
    owner = store.register_worker()
    store.claim_next(owner)
    output = store.output_directory(job.job_id)
    output.mkdir()
    (output / "empty.json").write_bytes(b"")
    with pytest.raises(UnsafeArtifact):
        store.finish(job.job_id, owner, JobState.COMPLETED, result_path="empty.json")
    store.report_progress(job.job_id, owner, "FINALIZING", job.total_steps)
    with pytest.raises(UnsafeArtifact):
        store.finish(job.job_id, owner, JobState.COMPLETED, result_path="empty.json")


def test_handler_missing_result_fails_and_existing_directory_is_not_reused(store):
    first = store.submit("smoke")
    with Worker(store, handlers={"smoke": lambda request, ctx: "missing.json"}) as worker:
        result = worker.run_once()
    assert result.state == JobState.FAILED and result.error_code == "INVALID_RESULT"
    second = store.submit("smoke")
    output = store.output_directory(second.job_id)
    output.mkdir()
    protected = output / "existing.txt"
    protected.write_bytes(b"TEST partial output must remain")
    with Worker(store) as worker:
        result = worker.run_once()
    assert result.state == JobState.FAILED and result.job_id == second.job_id
    assert protected.read_bytes() == b"TEST partial output must remain"
    assert first.job_id != second.job_id


def test_expired_lease_recovery_never_requeues_or_revives_owner(tmp_path):
    clock = Clock()
    store = JobStore(tmp_path / "jobs.db", clock=clock, lease_seconds=10)
    queued = store.submit("smoke")
    owner = store.register_worker()
    store.claim_next(owner)
    other = store.register_worker()
    assert store.recover_stale() == 0
    clock.advance(9)
    assert store.heartbeat(other)
    clock.advance(2)
    assert store.recover_stale() == 1
    interrupted = store.get(queued.job_id)
    assert interrupted.state == JobState.INTERRUPTED and interrupted.error_code == "LEASE_EXPIRED"
    assert interrupted.result_path is None and interrupted.attempt_count == 1
    assert not store.heartbeat(owner) and store.claim_next(other) is None
    with pytest.raises(InvalidTransition):
        store.finish(queued.job_id, owner, JobState.COMPLETED, result_path="smoke.json")
    assert JobStore(store.database).get(queued.job_id).state == JobState.INTERRUPTED


def test_heartbeat_is_independent_of_handler_progress(tmp_path, monkeypatch):
    clock = Clock()
    store = JobStore(tmp_path / "jobs.db", clock=clock, lease_seconds=3)
    job = store.submit("smoke")
    entered, renew, release = threading.Event(), threading.Event(), threading.Event()
    original = store.heartbeat

    def heartbeat(owner):
        result = original(owner)
        renew.set()
        return result

    monkeypatch.setattr(store, "heartbeat", heartbeat)

    def long_phase(request, context):
        entered.set()
        assert release.wait(5)
        return smoke_handler(request, context)

    with Worker(store, handlers={"smoke": long_phase}) as worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            execution = pool.submit(worker.run_once)
            try:
                assert entered.wait(3)
                clock.advance(2)
                assert renew.wait(3)
                clock.advance(2)  # Past original lease; heartbeat renewal protects a live handler.
                assert store.recover_stale() == 0
            finally:
                release.set()
            assert execution.result(timeout=5).state == JobState.COMPLETED
    assert store.get(job.job_id).state == JobState.COMPLETED


def test_cooperative_shutdown_and_keyboard_interruption(store):
    stop = threading.Event()

    def stopped(request, context):
        stop.set()
        context.checkpoint()

    first = store.submit("smoke")
    with Worker(store, handlers={"smoke": stopped}, shutdown=stop) as worker:
        assert worker.run_once().state == JobState.INTERRUPTED
    assert store.get(first.job_id).error_code == "WORKER_STOPPED"

    def keyboard_interrupt(request, context):
        raise KeyboardInterrupt()

    second = store.submit("smoke")
    with Worker(store, handlers={"smoke": keyboard_interrupt}) as worker, pytest.raises(KeyboardInterrupt):
        worker.run_once()
    assert store.get(second.job_id).state == JobState.INTERRUPTED
    with Worker(store) as worker:
        assert worker.run_once() is None


def test_process_exit_leaves_running_then_explicit_recovery_interrupts(store):
    job = store.submit("smoke")
    code = ("from market_research.job_store import JobStore; import sys; "
            "s=JobStore(sys.argv[1],artifact_root=sys.argv[2]); "
            "w=s.register_worker(); s.claim_next(w)")
    subprocess.run([sys.executable, "-c", code, str(store.database), str(store.artifact_root)],
                   check=True, timeout=15, capture_output=True)
    assert JobStore(store.database, artifact_root=store.artifact_root).get(job.job_id).state == JobState.RUNNING
    after_expiry = datetime.now(timezone.utc) + timedelta(seconds=60)
    reopened = JobStore(store.database, artifact_root=store.artifact_root, clock=lambda: after_expiry)
    assert reopened.recover_stale() == 1 and reopened.get(job.job_id).state == JobState.INTERRUPTED
    with Worker(reopened) as worker:
        assert worker.run_once() is None


def test_database_lock_contention_is_bounded_and_transactional(store):
    bounded = JobStore(store.database, artifact_root=store.artifact_root, busy_timeout=0.05)
    owner = bounded.register_worker()
    job = bounded.submit("smoke")
    with sqlite3.connect(store.database) as lock:
        lock.execute("BEGIN IMMEDIATE")
        start = monotonic()
        with pytest.raises(StoreBusy):
            bounded.claim_next(owner)
        assert monotonic() - start < 2
        lock.rollback()
    assert bounded.get(job.job_id).state == JobState.QUEUED
    assert bounded.claim_next(owner).attempt_count == 1


def test_empty_queue_path_configuration_and_limits(store, tmp_path):
    with Worker(store) as worker:
        assert worker.run_once() is None
        for bad in (0, 61):
            with pytest.raises(ValueError):
                worker.run_forever(bad)
    assert store.artifact_root.is_relative_to(tmp_path) and store.database.is_relative_to(tmp_path)
    assert list(store.artifact_root.iterdir()) == []
    with pytest.raises(JobStoreError, match="configuration"):
        JobStore(store.database, artifact_root=tmp_path / "different")
    with pytest.raises(ValueError):
        store.list_jobs(0)


def test_cli_submit_worker_inspect_persist_across_independent_processes(tmp_path):
    db = tmp_path / "runtime/jobs.sqlite3"
    command = [sys.executable, "-m", "market_research.jobs", "--db", str(db)]

    def cli(*args):
        result = subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    queued = cli("submit-smoke", "--steps", "2")
    assert queued["state"] == "QUEUED"
    assert cli("list")[0]["job_id"] == queued["job_id"]
    assert cli("worker", "--once")["state"] == "COMPLETED"
    assert cli("inspect", queued["job_id"])["result_path"].endswith("/smoke.json")
    assert cli("worker", "--once") == {"queue": "EMPTY_OR_STOPPED"}
    cancelled = cli("submit-smoke")
    assert cli("cancel", cancelled["job_id"])["state"] == "CANCELLED"
    assert cli("recover") == {"interrupted_jobs": 0, "automatic_replays": 0}


def test_cli_safe_errors_no_tracebacks_or_input_echo(store, capsys):
    secret = "SECRET_INVALID_IDENTIFIER"
    assert main(["--db", str(store.database), "--artifact-dir", str(store.artifact_root), "inspect", secret]) == 2
    text = capsys.readouterr().out
    assert secret not in text and "Traceback" not in text
    with pytest.raises(SystemExit) as exc:
        main(["submit-smoke", "--steps", secret])
    text = capsys.readouterr().err
    assert exc.value.code == 2 and secret not in text


def test_continuous_worker_waits_for_bounded_poll_and_stops_cleanly(store):
    class StopOnIdle(threading.Event):
        def __init__(self):
            super().__init__()
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            self.set()
            return True

    shutdown = StopOnIdle()
    with Worker(store, shutdown=shutdown) as worker:
        worker.run_forever(poll_seconds=0.5)
    assert shutdown.waits == [0.5] and store.list_jobs() == []


def test_worker_once_cli_does_not_report_failed_job_as_success(store, capsys):
    job = store.submit("smoke")
    store.output_directory(job.job_id).mkdir()  # Existing partial directory is never overwritten.
    code = main(["--db", str(store.database), "--artifact-dir", str(store.artifact_root), "worker", "--once"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["state"] == "FAILED" and result["result_path"] is None


def test_import_has_no_database_thread_or_output_side_effects(tmp_path):
    code = ("import threading; before=len(threading.enumerate()); "
            "import market_research.jobs,market_research.job_worker; "
            "assert len(threading.enumerate())==before")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment,
                   timeout=15, check=True, capture_output=True)
    assert list(tmp_path.iterdir()) == []
