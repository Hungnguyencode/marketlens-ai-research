"""Offline publication boundaries; SYNTHETIC inputs and temporary resources only."""
import errno
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from market_research.job_store import InvalidTransition, JobState, JobStore, UnsafeArtifact
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


@pytest.mark.skipif(os.name != "nt", reason="Exercises native Windows file-handle rename semantics")
def test_windows_open_report_handle_blocks_publication_without_replay(publication, monkeypatch):
    """A controlled mechanism, not proof of the cause of historical intermittent failures."""
    store, queued = publication
    held = []
    errors = []
    calls = []
    finish = store.finish

    def observe_finish(*args, **kwargs):
        try:
            return finish(*args, **kwargs)
        except UnsafeArtifact as exc:
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
    assert done.state == JobState.FAILED and done.error_code == "INVALID_RESULT"
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
    assert done.state == JobState.FAILED and done.error_code == "INVALID_RESULT"
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
