"""Offline publication boundaries; SYNTHETIC inputs and temporary resources only."""
import errno
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from market_research.job_store import InvalidTransition, JobState, JobStore, PublicationError
from market_research.job_worker import Worker
from market_research.ollama import OllamaClient
from market_research.research_contract import ResearchOutcome, verify_bundle
from market_research.research_job import research_handler
from market_research.research_workspace import WorkspaceError, verified_reports


@pytest.fixture
def publication(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live services forbidden in SYNTHETIC publication tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(OllamaClient, "plan", forbidden)
    monkeypatch.setattr(OllamaClient, "analyze", forbidden)
    source = tmp_path / "SYNTHETIC-empty-replay.json"
    source.write_text("[]", encoding="utf-8")
    store = JobStore(tmp_path / "runtime/jobs.sqlite3", input_root=tmp_path,
                     artifact_root=tmp_path / "artifacts")
    job = store.submit("research", {"topic": "SYNTHETIC publication gate", "mode": "replay",
                                     "input_path": source.name})
    return store, job


def assert_bundle_valid(store, job, directory_name="_partial"):
    output = store.output_directory(job.job_id)
    manifest = json.loads((output / directory_name / "manifest.json").read_text(encoding="utf-8"))
    outcome = ResearchOutcome.model_validate(manifest["outcome"])
    verify_bundle(output, job.job_id, job.request, outcome, directory_name=directory_name)


def assert_no_replay(store, job):
    assert store.get(job.job_id).attempt_count == 1
    with Worker(store) as next_worker:
        assert next_worker.run_once() is None
    assert len(store.list_jobs()) == 1


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_rename_denial_publishes_once_without_research_replay(publication, monkeypatch, winerror):
    store, queued = publication
    rename = Path.rename
    attempts, handlers = [], []

    def temporarily_denied(path, target):
        if path.name == "_partial" and Path(target).name == "result":
            attempts.append(path)
            if len(attempts) < 3:
                error = PermissionError(errno.EACCES, "SYNTHETIC private OS text")
                error.winerror = winerror
                raise error
        return rename(path, target)

    def once(request, context):
        handlers.append(context.job_id)
        return research_handler(request, context)

    monkeypatch.setattr(Path, "rename", temporarily_denied)
    with Worker(store, handlers={"research": once}) as worker:
        done = worker.run_once()
        assert worker.run_once() is None
    assert done.state == JobState.COMPLETED
    assert done.job_id == queued.job_id and done.attempt_count == 1
    assert len(attempts) == 3 and handlers == [queued.job_id]
    assert done.research_outcome.validation_status == "INSUFFICIENT_EVIDENCE"
    assert_bundle_valid(store, done, "result")
    assert verified_reports(store, done.job_id).result.evidence == []


def test_after_rename_corruption_cannot_be_committed_as_completed(publication, monkeypatch):
    store, queued = publication
    rename = Path.rename

    def corrupt_after_move(path, target):
        result = rename(path, target)
        if path.name == "_partial" and Path(target).name == "result":
            manifest = json.loads((Path(target) / "manifest.json").read_text(encoding="utf-8"))
            report = Path(target) / manifest["artifacts"]["json"]["path"]
            report.write_bytes(report.read_bytes() + b" ")
        return result

    monkeypatch.setattr(Path, "rename", corrupt_after_move)
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == JobState.FAILED and done.error_code == "INVALID_RESULT"
    assert done.result_path is None
    with pytest.raises(WorkspaceError):
        verified_reports(store, queued.job_id)
    assert_no_replay(store, done)


def test_lease_expiring_during_publication_cannot_commit_completion(publication, monkeypatch):
    store, queued = publication
    now = [datetime.now(timezone.utc)]
    store.clock = lambda: now[0]
    rename = Path.rename

    def expire_after_move(path, target):
        result = rename(path, target)
        if path.name == "_partial" and Path(target).name == "result":
            now[0] += timedelta(seconds=store.lease_seconds + 1)
        return result

    monkeypatch.setattr(Path, "rename", expire_after_move)
    with Worker(store) as worker:
        worker.run_once()
    done = store.get(queued.job_id)
    assert done.state == JobState.INTERRUPTED and done.result_path is None
    with pytest.raises(WorkspaceError):
        verified_reports(store, done.job_id)
    assert_no_replay(store, done)


@pytest.mark.skipif(os.name != "nt", reason="Exercises native Windows file-handle rename semantics")
def test_windows_open_report_handle_blocks_publication_without_replay(publication, monkeypatch):
    """A persistent native Windows denial must remain failed after bounded rename attempts."""
    store, queued = publication
    held = []
    errors = []
    calls = []
    finish = store.finish

    def observe_finish(*args, **kwargs):
        try:
            return finish(*args, **kwargs)
        except PublicationError as exc:
            errors.append(exc.__context__)
            raise

    monkeypatch.setattr(store, "finish", observe_finish)

    def hold_report(request, context):
        calls.append(context.job_id)
        prepared = research_handler(request, context)
        stage = context.output_dir / "_partial"
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        held.append((stage / manifest["artifacts"]["json"]["path"]).open("rb"))
        return prepared

    try:
        with Worker(store, handlers={"research": hold_report}) as worker:
            done = worker.run_once()
    finally:
        for handle in held:
            handle.close()
    assert calls == [queued.job_id]
    assert len(errors) == 1 and isinstance(errors[0], PermissionError)
    assert errors[0].winerror in {5, 32, 33}
    assert done.state == JobState.FAILED and done.error_code == "PUBLICATION_IO_FAILED"
    assert "operation=rename; errno=13; winerror=5; attempts=4" in done.error_message
    assert done.phase == "READY_TO_PUBLISH" and done.completed_steps == done.total_steps == 8
    assert done.result_path is None and not (store.output_directory(done.job_id) / "result").exists()
    # Closing the handle makes verification possible, but must never revive/replay the job.
    assert_bundle_valid(store, done)
    with pytest.raises(WorkspaceError):
        verified_reports(store, done.job_id)
    assert_no_replay(store, done)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_directory_rename_failure_is_not_retried_or_published(publication, monkeypatch, error_number):
    store, queued = publication
    rename = Path.rename
    attempts = []

    def fail_publication(path, target):
        if path.name == "_partial" and Path(target).name == "result":
            attempts.append(path)
            raise OSError(error_number, "SYNTHETIC failure content must stay private")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publication)
    with Worker(store) as worker:
        done = worker.run_once()
    assert len(attempts) == 1
    assert done.state == JobState.FAILED and done.error_code == "PUBLICATION_IO_FAILED"
    assert done.completed_steps == 8 and done.result_path is None
    assert "SYNTHETIC" not in done.error_message
    assert not (store.output_directory(queued.job_id) / "result").exists()
    assert_bundle_valid(store, done)
    assert_no_replay(store, done)


def test_sqlite_terminal_update_failure_does_not_expose_renamed_bundle(publication):
    store, queued = publication
    # A real SQLite transaction error after rename, not a mocked successful commit.
    with sqlite3.connect(store.database) as db:
        db.execute("""CREATE TRIGGER synthetic_abort_completion BEFORE UPDATE OF state ON jobs
                   WHEN NEW.state='COMPLETED'
                   BEGIN SELECT RAISE(ABORT, 'SYNTHETIC transaction failure'); END""")
    with Worker(store) as worker, pytest.raises(InvalidTransition):
        worker.run_once()
    done = store.get(queued.job_id)
    assert done.state == JobState.INTERRUPTED and done.result_path is None
    assert done.completed_steps == 8
    assert_bundle_valid(store, done, "result")
    with pytest.raises(WorkspaceError):
        verified_reports(store, done.job_id)
    assert_no_replay(store, done)


@pytest.mark.parametrize("action", ["complete", "cancel", "interrupt"])
def test_publication_interleaving_preserves_owner_and_reader_boundary(publication, action):
    store, queued = publication
    ready, release = threading.Event(), threading.Event()
    calls = []

    def pause_prepared(request, context):
        calls.append(context.job_id)
        prepared = research_handler(request, context)
        ready.set()
        assert release.wait(10), "SYNTHETIC publication interleaving timed out"
        return prepared

    with Worker(store, handlers={"research": pause_prepared}) as first:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.run_once)
            try:
                assert ready.wait(10)
                persisted = store.get(queued.job_id)
                assert persisted.state == JobState.RUNNING and persisted.result_path is None
                assert persisted.completed_steps == 8
                assert_bundle_valid(store, persisted)
                with Worker(store) as second:
                    assert second.run_once() is None
                with pytest.raises(WorkspaceError):
                    verified_reports(store, queued.job_id)
                if action == "cancel":
                    store.request_cancel(queued.job_id)
                elif action == "interrupt":
                    first.shutdown.set()
            finally:
                release.set()
            done = future.result(timeout=10)
    assert calls == [queued.job_id]
    expected = {"complete": JobState.COMPLETED, "cancel": JobState.CANCELLED,
                "interrupt": JobState.INTERRUPTED}[action]
    assert done.state == expected
    if action == "complete":
        assert done.result_path == f"{done.job_id}/result/manifest.json"
        assert verified_reports(store, done.job_id).result.evidence == []
        assert not (store.output_directory(done.job_id) / "_partial").exists()
    else:
        assert done.result_path is None
        assert not (store.output_directory(done.job_id) / "result").exists()
        assert_bundle_valid(store, done)
    assert_no_replay(store, done)


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_persistent_windows_denial_has_bounded_safe_diagnostics(publication, monkeypatch, winerror):
    store, queued = publication
    rename = Path.rename
    attempts, waits = [], []
    monkeypatch.setattr("market_research.job_store.sleep", waits.append)

    def denied(path, target):
        if path.name == "_partial" and Path(target).name == "result":
            attempts.append(path)
            error = PermissionError(errno.EACCES, "SYNTHETIC credential must never escape")
            error.winerror = winerror
            raise error
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", denied)
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == JobState.FAILED and done.error_code == "PUBLICATION_IO_FAILED"
    assert len(attempts) == 4 and waits == [0.05, 0.15, 0.30]
    assert f"operation=rename; errno=13; winerror={winerror}; attempts=4" in done.error_message
    assert "SYNTHETIC" not in done.error_message and done.result_path is None
    assert done.research_outcome is None
    assert_bundle_valid(store, done)
    with pytest.raises(WorkspaceError):
        verified_reports(store, queued.job_id)
    assert_no_replay(store, done)


@pytest.mark.skipif(os.name != "nt", reason="Exercises native Windows file-handle rename semantics")
def test_native_windows_handle_released_after_denial_publishes_same_bundle(publication, monkeypatch):
    store, queued = publication
    rename = Path.rename
    held, denials, handlers = [], [], []

    def native_then_release(path, target):
        try:
            return rename(path, target)
        except PermissionError as exc:
            if path.name == "_partial" and Path(target).name == "result":
                denials.append(exc.winerror)
                held.pop().close()
            raise

    def prepare_with_handle(request, context):
        handlers.append(context.job_id)
        prepared = research_handler(request, context)
        manifest = json.loads((context.output_dir / "_partial/manifest.json").read_text(encoding="utf-8"))
        held.append((context.output_dir / "_partial" / manifest["artifacts"]["json"]["path"]).open("rb"))
        return prepared

    monkeypatch.setattr(Path, "rename", native_then_release)
    try:
        with Worker(store, handlers={"research": prepare_with_handle}) as worker:
            done = worker.run_once()
    finally:
        for handle in held:
            handle.close()
    assert denials == [5] and handlers == [queued.job_id]
    assert done.state == JobState.COMPLETED and done.attempt_count == 1
    assert_bundle_valid(store, done, "result")
    assert verified_reports(store, done.job_id).result.evidence == []
    assert_no_replay(store, done)


@pytest.mark.parametrize("mutation", ["checksum", "target", "shutdown", "lease"])
def test_every_rename_retry_rechecks_bundle_target_and_ownership(publication, monkeypatch, mutation):
    store, queued = publication
    now = [datetime.now(timezone.utc)]
    store.clock = lambda: now[0]
    rename = Path.rename
    calls = []
    shutdown = threading.Event()

    def first_denial(path, target):
        if path.name == "_partial" and Path(target).name == "result":
            calls.append(path)
            if len(calls) == 1:
                if mutation == "checksum":
                    (path / "manifest.json").write_bytes((path / "manifest.json").read_bytes() + b"CORRUPT")
                elif mutation == "target":
                    Path(target).mkdir()
                    (Path(target) / "stale.txt").write_text("SYNTHETIC stale artifact", encoding="utf-8")
                elif mutation == "shutdown":
                    shutdown.set()
                else:
                    now[0] += timedelta(seconds=store.lease_seconds + 1)
                error = PermissionError(errno.EACCES, "SYNTHETIC transient denial")
                error.winerror = 5
                raise error
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", first_denial)
    with Worker(store, shutdown=shutdown) as worker:
        worker.run_once()
    done = store.get(queued.job_id)
    expected = JobState.INTERRUPTED if mutation in {"lease", "shutdown"} else JobState.FAILED
    assert done.state == expected and done.result_path is None and len(calls) == 1
    if expected == JobState.FAILED:
        assert done.error_code == "INVALID_RESULT"
    if mutation == "target":
        assert (store.output_directory(done.job_id) / "result/stale.txt").read_text(encoding="utf-8") == "SYNTHETIC stale artifact"
    with pytest.raises(WorkspaceError):
        verified_reports(store, done.job_id)
    assert_no_replay(store, done)


def test_new_explicit_submission_after_failure_uses_new_job_and_new_artifacts(publication):
    store, queued = publication
    prepared_ids = []

    def fail_after_preparation(request, context):
        prepared = research_handler(request, context)
        prepared_ids.append(prepared.outcome.run_id)
        raise RuntimeError("SYNTHETIC private failure")

    with Worker(store, handlers={"research": fail_after_preparation}) as worker:
        failed = worker.run_once()
    old_files = {p: p.read_bytes() for p in store.output_directory(failed.job_id).rglob("*") if p.is_file()}
    retry = store.submit("research", queued.request.model_dump(exclude={"input_sha256"}))
    with Worker(store) as worker:
        done = worker.run_once()
        assert worker.run_once() is None
    assert retry.job_id != failed.job_id and done.job_id == retry.job_id
    assert store.get(failed.job_id).state == JobState.FAILED
    assert done.state == JobState.COMPLETED and done.research_outcome.run_id not in prepared_ids
    assert all(p.read_bytes() == raw for p, raw in old_files.items())
    result = verified_reports(store, done.job_id).result
    assert result.metadata["job_id"] == retry.job_id and len(store.list_jobs()) == 2
    with pytest.raises(WorkspaceError):
        verified_reports(store, failed.job_id)


def test_bundle_from_another_job_cannot_be_published(publication):
    import shutil
    from market_research.research_contract import PreparedResearchResult

    store, queued = publication
    with Worker(store) as worker:
        first = worker.run_once()
    original = verified_reports(store, first.job_id).downloads
    second = store.submit("research", queued.request.model_dump(exclude={"input_sha256"}))

    def stale(request, context):
        shutil.copytree(store.output_directory(first.job_id) / "result", context.output_dir / "_partial")
        context.progress("READY_TO_PUBLISH", 8)
        return PreparedResearchResult(outcome=first.research_outcome)

    with Worker(store, handlers={"research": stale}) as worker:
        done = worker.run_once()
    assert done.job_id == second.job_id and done.state == JobState.FAILED
    assert done.error_code == "INVALID_RESULT" and done.result_path is None
    assert verified_reports(store, first.job_id).downloads == original
    with pytest.raises(WorkspaceError):
        verified_reports(store, second.job_id)


def test_process_crash_after_rename_is_fenced_on_worker_restart(publication):
    store, queued = publication
    child_code = """
import os, sys
from pathlib import Path
import httpx
from market_research.job_store import JobStore
from market_research.job_worker import Worker
from market_research.ollama import OllamaClient
def forbidden(*args, **kwargs):
    raise AssertionError('Live services forbidden in crash regression')
httpx.HTTPTransport.handle_request = forbidden
OllamaClient.plan = forbidden
OllamaClient.analyze = forbidden
rename = Path.rename
def crash_after_rename(path, target):
    result = rename(path, target)
    if path.name == '_partial' and Path(target).name == 'result':
        os._exit(73)
    return result
Path.rename = crash_after_rename
store = JobStore(sys.argv[1], input_root=sys.argv[2], artifact_root=sys.argv[3])
with Worker(store) as worker:
    worker.run_once()
"""
    child = subprocess.run([sys.executable, "-c", child_code, str(store.database),
                            str(store.input_root), str(store.artifact_root)], timeout=20,
                           capture_output=True)
    assert child.returncode == 73
    unfinished = store.get(queued.job_id)
    assert unfinished.state == JobState.RUNNING and unfinished.result_path is None
    assert_bundle_valid(store, unfinished, "result")
    with pytest.raises(WorkspaceError):
        verified_reports(store, unfinished.job_id)
    store.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=store.lease_seconds + 1)
    with Worker(store) as restarted:
        assert restarted.run_once() is None
    interrupted = store.get(queued.job_id)
    assert interrupted.state == JobState.INTERRUPTED and interrupted.error_code == "LEASE_EXPIRED"
    assert interrupted.result_path is None and interrupted.attempt_count == 1
    with pytest.raises(WorkspaceError):
        verified_reports(store, interrupted.job_id)
