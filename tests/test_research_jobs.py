"""SYNTHETIC inputs and fake services only; exercise the production pipeline offline."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from market_research.analysis import build_verified_facts, fallback_analysis
from market_research.collectors import GoogleBooksCollectionError, GoogleBooksCollector
from market_research.job_store import JobState, JobStore, JobStoreError, ResearchInputError
from market_research.job_worker import Worker
from market_research.jobs import main
from market_research.models import Evidence, GroundedClaim, ResearchConfig, ResearchResult
from market_research.orchestrator import ResearchExecutionContext, run_research
from market_research.ollama import OllamaClient
from market_research.research_contract import ResearchRequest, verify_bundle
from market_research.research_job import research_handler


TOPIC = "cozy coloring books for adults"
SOURCE = "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book"
PRODUCTION_COLLECT = GoogleBooksCollector.collect
PRODUCTION_ANALYZE = OllamaClient.analyze


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live collection/inference forbidden in offline tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector.collect", forbidden)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", forbidden)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.plan", forbidden)


@pytest.fixture
def research(tmp_path):
    source = tmp_path / "SYNTHETIC-input.csv"
    source.write_text("SYNTHETIC TEST FIXTURE — NOT MARKET EVIDENCE\n\nTuần,cozy coloring book: (Hoa Kỳ)\n"
                      + "\n".join(f"2025-01-{5 + index * 7:02d},{value}" for index, value in enumerate([10, 20, 30, 40]))
                      + "\n2025-02-02,50\n2025-02-09,60\n2025-02-16,70\n2025-02-23,80\n", encoding="utf-8-sig")
    store = JobStore(tmp_path / "runtime/jobs.sqlite3", input_root=tmp_path)
    payload = {"topic": TOPIC, "mode": "csv_import", "input_path": source.name, "source_url": SOURCE}
    return store, source, payload


def execute(store, **options):
    with Worker(store, **options) as worker:
        return worker.run_once()


def load_result(store, job):
    verify_bundle(store.output_directory(job.job_id), job.job_id, job.request, job.research_outcome,
                  directory_name="result")
    manifest_path = store.artifact_root / job.result_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle = manifest_path.parent
    result = ResearchResult.model_validate_json((bundle / manifest["artifacts"]["json"]["path"]).read_text(encoding="utf-8"))
    return bundle, manifest, result


def fake_analysis(self, niche, evidence, report_language, computed_metrics=None, verified_facts=None,
                  correction_diagnostics=None):
    analysis = fallback_analysis(niche, evidence, verified_facts)
    analysis.hypotheses = []
    return analysis


def test_offline_pipeline_preserves_facts_sources_and_publishes_isolated_bundle(research, tmp_path):
    store, source, payload = research
    before = source.read_bytes()
    job = store.submit("research", payload)
    result = execute(store)
    assert result.job_id == job.job_id and result.state == JobState.COMPLETED
    assert result.completed_steps == result.total_steps == 8 and result.phase == "PUBLICATION"
    assert result.execution_provenance == "RESEARCH_PIPELINE"
    assert result.research_outcome.validation_status == "DEGRADED" and not result.research_outcome.valid
    bundle, manifest, report = load_result(store, result)
    assert bundle.name == "result" and not (bundle.parent / "_partial").exists()
    assert report.metadata["analysis_execution_status"] == "not_requested"
    assert report.metadata["evidence_records_received_by_ollama"] == 0
    assert [item.metric_value for item in report.evidence] == [10, 20, 30, 40, 50, 60, 70, 80]
    assert all(item.source_url == SOURCE and item.keyword == "cozy coloring book" for item in report.evidence)
    rebuilt = build_verified_facts([item.model_dump(mode="json") for item in report.evidence], report.metadata["computed_metrics"])
    assert rebuilt == report.verified_facts and report.metadata["job_id"] == job.job_id
    assert manifest["request"]["input_sha256"] == hashlib.sha256(before).hexdigest()
    for entry in manifest["artifacts"].values():
        path = bundle / entry["path"]
        assert path.is_file() and path.is_relative_to(bundle)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    assert not (tmp_path / "logs").exists() and not (tmp_path / "snapshots").exists()
    assert before == source.read_bytes()


def test_request_serialization_and_database_restart_are_immutable(research):
    store, _, payload = research
    job = store.submit("research", payload)
    with pytest.raises(ValidationError):
        job.request.allow_inference = True
    restored = JobStore(store.database)
    assert restored.input_root == store.input_root
    assert restored.get(job.job_id) == job
    assert ResearchRequest.model_validate_json(job.request.model_dump_json()) == job.request
    assert not job.request.allow_network and not job.request.allow_inference
    assert job.request.model == "qwen3:4b-instruct"


def test_execution_context_owns_reports_even_when_legacy_output_dir_is_omitted(research, tmp_path, monkeypatch):
    _, source, _ = research
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "SYNTHETIC-owned-bundle"
    root.mkdir()
    result = run_research(ResearchConfig(), TOPIC, mode="csv_import", trends_csv_path=str(source), source_url=SOURCE,
                          execution=ResearchExecutionContext(root, False, False, lambda: None, lambda *args: None))
    assert all((root / name).is_file() for name in result.metadata["report_paths"].values())
    assert not (tmp_path / "outputs").exists() and not (tmp_path / "snapshots").exists()


@pytest.mark.parametrize("changes", [
    {"input_path": "missing.csv"}, {"input_path": "../outside.csv"}, {"input_path": "C:ambiguous.csv"},
    {"input_path": "\\\\server\\share\\data.csv"}, {"input_path": "SYNTHETIC-input.csv:stream"},
    {"source_url": None}, {"source_url": "https://trends.google.com/trends/explore?key=DO_NOT_STORE"},
    {"source_url": "https://name:password@trends.google.com/trends/explore"},
    {"source_url": "https://trends.google.com:DO_NOT_STORE/trends/explore?geo=US"},
    {"source_url": "https://trends.google.com/trends/explore?geo=US&q=one,two"},
    {"allow_network": True}, {"allow_inference": "yes"}, {"model": "--arbitrary-command"},
    {"topic": "../unsafe-topic"}, {"topic": "api_key=DO_NOT_STORE"}, {"api_key": "DO_NOT_STORE"},
    {"mode": "live", "input_path": None, "source_url": None},
])
def test_research_request_rejects_unsafe_ambiguous_or_unauthorized_inputs(research, changes):
    store, _, payload = research
    with pytest.raises(ResearchInputError) as exc:
        store.submit("research", {**payload, **changes})
    assert "DO_NOT_STORE" not in str(exc.value) and store.list_jobs() == []


def test_external_file_requires_explicit_operator_input_root(research, tmp_path):
    store, source, payload = research
    restricted = JobStore(tmp_path / "restricted/jobs.sqlite3", input_root=tmp_path / "runtime")
    with pytest.raises(ResearchInputError, match="input root"):
        restricted.submit("research", {**payload, "input_path": str(source)})
    assert store.submit("research", {**payload, "input_path": str(source)}).request.input_path == source.name


@pytest.mark.parametrize("remove", [False, True])
def test_changed_or_missing_queued_input_fails_before_external_work(research, remove):
    store, source, payload = research
    queued = store.submit("research", {**payload, "allow_inference": True})
    if remove:
        source.unlink()
    else:
        source.write_text("SYNTHETIC changed input", encoding="utf-8")
    failed = execute(store)
    assert failed.job_id == queued.job_id and failed.state == JobState.FAILED
    assert failed.error_code == "RESEARCH_INPUT_FAILED" and failed.result_path is None
    assert not (store.output_directory(queued.job_id) / "result").exists()


def test_two_research_jobs_do_not_overwrite_each_other(research):
    store, _, payload = research
    first, second = store.submit("research", payload), store.submit("research", payload)
    one = execute(store)
    before = {path: path.read_bytes() for path in store.output_directory(first.job_id).rglob("*") if path.is_file()}
    two = execute(store)
    assert {one.job_id, two.job_id} == {first.job_id, second.job_id}
    assert one.research_outcome.run_id != two.research_outcome.run_id
    assert all(path.read_bytes() == contents for path, contents in before.items())


@pytest.mark.parametrize("invalid", [False, True])
def test_real_validator_decides_valid_or_invalid_independently_of_completed_job(research, monkeypatch, invalid):
    store, _, payload = research

    def analyze(*args, **kwargs):
        analysis = fake_analysis(*args, **kwargs)
        if invalid:
            analysis.evidence_based_inferences = [GroundedClaim(text="SYNTHETIC unsupported sales claim 9999.", fact_ids=["missing-fact"])]
        return analysis

    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", analyze)
    store.submit("research", {**payload, "allow_inference": True})
    job = execute(store)
    assert job.state == JobState.COMPLETED
    expected = "INVALID" if invalid else "VALID"
    assert job.research_outcome.validation_status == expected
    assert job.research_outcome.valid is (not invalid)
    bundle, manifest, report = load_result(store, job)
    assert report.validation.status == expected and report.metadata["analysis_provenance"] == "ollama"
    assert manifest["outcome"]["validation_status"] == expected
    if invalid:
        markdown = (bundle / manifest["artifacts"]["markdown"]["path"]).read_text(encoding="utf-8")
        assert "Không có phát biểu phân tích nào của Ollama được chấp nhận" in markdown


def test_ollama_timeout_retains_unpublished_diagnostics_and_safe_error(research, monkeypatch):
    store, _, payload = research
    secret = "DO_NOT_PERSIST_AUTHORIZED_KEY"

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout(f"Bearer {secret}")

    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", timeout)
    queued = store.submit("research", {**payload, "allow_inference": True})
    job = execute(store)
    assert job.state == JobState.FAILED and job.error_code == "RESEARCH_INFERENCE_FAILED"
    assert job.research_outcome.validation_status == "DEGRADED" and job.result_path is None
    output = store.output_directory(queued.job_id)
    assert not (output / "result").exists() and list((output / "_partial/reports").glob("*.json"))
    assert all(secret.encode() not in path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert secret not in job.model_dump_json()


def test_source_429_is_failed_execution_with_blocked_validation_and_sanitized_artifacts(research, monkeypatch):
    store, _, _ = research

    def blocked(*args, **kwargs):
        raise GoogleBooksCollectionError("api_key=DO_NOT_PERSIST", status_code=429, reason="quota")

    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector.collect", blocked)
    store.submit("research", {"topic": TOPIC, "mode": "live", "allow_network": True})
    job = execute(store)
    assert job.state == JobState.FAILED and job.error_code == "RESEARCH_COLLECTION_FAILED"
    assert job.research_outcome.validation_status == "BLOCKED" and job.research_outcome.evidence_count == 0
    assert job.result_path is None
    assert all(b"DO_NOT_PERSIST" not in path.read_bytes()
               for path in store.output_directory(job.job_id).rglob("*") if path.is_file())


def test_authorized_catalog_uses_existing_normalizer_without_inference(research, monkeypatch):
    store, _, _ = research
    # Restore the production collect method, stub only its HTTP response seam.
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector.collect", PRODUCTION_COLLECT)
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector._request",
                        lambda *a, **k: {"totalItems": 1, "items": [{"id": "SYNTHETIC-catalog", "volumeInfo": {"title": "SYNTHETIC catalog fixture"}}]})
    store.submit("research", {"topic": TOPIC, "mode": "live", "allow_network": True})
    job = execute(store)
    _, _, result = load_result(store, job)
    assert job.state == JobState.COMPLETED and result.evidence[0].metric_name == "catalog_results_reported"
    assert not result.validation.valid and not result.validation.evidence_sufficient


def test_replay_empty_input_finishes_honestly_without_fake_evidence(research, tmp_path):
    store, _, _ = research
    replay = tmp_path / "SYNTHETIC-empty.json"
    replay.write_text("[]", encoding="utf-8")
    store.submit("research", {"topic": TOPIC, "mode": "replay", "input_path": replay.name})
    job = execute(store)
    assert job.state == JobState.COMPLETED and job.research_outcome.validation_status == "INSUFFICIENT_EVIDENCE"
    _, _, result = load_result(store, job)
    assert result.evidence == [] and result.verified_facts == [] and not result.validation.valid


def test_replay_preserves_original_evidence_ids_and_retrieval_time(research, tmp_path, sample_evidence):
    store, _, _ = research
    replay = tmp_path / "SYNTHETIC-snapshot.json"
    replay.write_text(json.dumps(sample_evidence), encoding="utf-8")
    store.submit("research", {"topic": TOPIC, "mode": "replay", "input_path": replay.name})
    job = execute(store)
    _, _, result = load_result(store, job)
    original = Evidence.model_validate(sample_evidence[0])
    assert result.evidence == [original] and result.run_id != original.run_id
    assert result.verified_facts[0].evidence_ids == [original.evidence_id]


def test_input_credentials_are_rejected_without_copying_them_into_artifacts(research, tmp_path, sample_evidence):
    store, _, _ = research
    sample_evidence[0]["source_url"] = "https://example.invalid/data?key=DO_NOT_COPY"
    replay = tmp_path / "SYNTHETIC-unsafe.json"
    replay.write_text(json.dumps(sample_evidence), encoding="utf-8")
    store.submit("research", {"topic": TOPIC, "mode": "replay", "input_path": replay.name})
    job = execute(store)
    assert job.state == JobState.FAILED and job.error_code == "RESEARCH_INPUT_FAILED"
    assert all(b"DO_NOT_COPY" not in path.read_bytes()
               for path in store.output_directory(job.job_id).rglob("*") if path.is_file())


def test_local_csv_geography_mismatch_cannot_be_published_as_us_evidence(research):
    store, source, payload = research
    source.write_text(source.read_text(encoding="utf-8-sig").replace("Hoa Kỳ", "Vietnam"), encoding="utf-8")
    store.submit("research", payload)
    job = execute(store)
    assert job.state == JobState.FAILED and job.result_path is None


def test_source_url_keyword_must_match_actual_csv_series(research):
    store, _, payload = research
    store.submit("research", {**payload, "source_url": "https://trends.google.com/trends/explore?geo=US&q=different"})
    job = execute(store)
    assert job.state == JobState.FAILED and job.result_path is None


def test_remote_or_credentialled_ollama_endpoint_is_not_authorized_by_local_inference_flag(research, monkeypatch):
    store, _, payload = research
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://user:DO_NOT_SEND@example.invalid:11434")
    store.submit("research", {**payload, "allow_inference": True})
    job = execute(store)
    assert job.state == JobState.FAILED and job.error_code == "RESEARCH_CONFIGURATION_FAILED"
    assert "DO_NOT_SEND" not in job.model_dump_json() and job.result_path is None


def test_multi_keyword_csv_is_rejected_before_analysis(research):
    store, source, payload = research
    source.write_text("Week,cozy: (US),other niche: (US)\n2025-01-05,20,40\n2025-01-12,30,50\n", encoding="utf-8")
    store.submit("research", {**payload, "allow_inference": True})
    job = execute(store)
    assert job.state == JobState.FAILED and job.result_path is None


def test_cancellation_after_blocking_model_call_never_publishes(research, monkeypatch):
    store, _, payload = research
    queued = store.submit("research", {**payload, "allow_inference": True})

    def cancelled(*args, **kwargs):
        assert store.request_cancel(queued.job_id).state == JobState.RUNNING
        return fake_analysis(*args, **kwargs)

    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", cancelled)
    job = execute(store)
    assert job.state == JobState.CANCELLED and job.result_path is None and job.research_outcome is None
    assert not (store.output_directory(job.job_id) / "result").exists()


def test_http_checkpoint_cancels_before_ollama_json_repair(research, monkeypatch):
    store, _, payload = research
    queued = store.submit("research", {**payload, "allow_inference": True})
    calls = []

    def response(request):
        calls.append(request)
        store.request_cancel(queued.job_id)
        return httpx.Response(200, json={"response": "invalid JSON would normally trigger repair"})

    client = httpx.Client(transport=httpx.MockTransport(response))
    # analyze must be the real method for its bounded repair loop to be exercised.
    monkeypatch.setattr(OllamaClient, "analyze", PRODUCTION_ANALYZE)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient",
                        lambda base, model, timeout, checkpoint: OllamaClient(base, model, timeout, client=client, checkpoint=checkpoint))
    job = execute(store)
    client.close()
    assert job.state == JobState.CANCELLED and len(calls) == 1 and job.result_path is None


def test_http_checkpoint_cancels_before_google_books_retry(research, monkeypatch):
    store, _, _ = research
    queued = store.submit("research", {"topic": TOPIC, "mode": "live", "allow_network": True})
    calls = []

    def response(request):
        calls.append(request)
        store.request_cancel(queued.job_id)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "SYNTHETIC rate limit"}})

    client = httpx.Client(transport=httpx.MockTransport(response))
    monkeypatch.setattr(GoogleBooksCollector, "collect", PRODUCTION_COLLECT)
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector",
                        lambda timeout, key, checkpoint: GoogleBooksCollector(timeout, key, client=client, checkpoint=checkpoint))
    job = execute(store)
    client.close()
    assert job.state == JobState.CANCELLED and len(calls) == 1


@pytest.mark.parametrize("corruption", ["checksum", "traversal", "missing"])
def test_publication_rechecks_complete_bundle_and_safe_references(research, corruption):
    store, _, payload = research
    store.submit("research", payload)

    def corrupt(request, context):
        prepared = research_handler(request, context)
        manifest_path = context.output_dir / "_partial/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if corruption == "checksum":
            path = context.output_dir / "_partial" / manifest["artifacts"]["json"]["path"]
            path.write_bytes(path.read_bytes() + b" ")
        elif corruption == "missing":
            (context.output_dir / "_partial" / manifest["artifacts"]["html"]["path"]).unlink()
        else:
            manifest["artifacts"]["json"]["path"] = "../outside.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return prepared

    job = execute(store, handlers={"research": corrupt})
    assert job.state == JobState.FAILED and job.error_code == "INVALID_RESULT" and job.result_path is None
    assert not (store.output_directory(job.job_id) / "result").exists()


def test_cancel_racing_terminal_transaction_prevents_directory_publication(research, monkeypatch):
    store, _, payload = research
    queued = store.submit("research", payload)
    original = store.finish

    def finish(job_id, owner, state, **kwargs):
        if state == JobState.COMPLETED:
            store.request_cancel(job_id)
        return original(job_id, owner, state, **kwargs)

    monkeypatch.setattr(store, "finish", finish)
    job = execute(store)
    assert job.state == JobState.CANCELLED and job.result_path is None
    assert (store.output_directory(queued.job_id) / "_partial/manifest.json").is_file()
    assert not (store.output_directory(queued.job_id) / "result").exists()


def test_report_write_failure_cannot_publish_partial_report(research, monkeypatch):
    store, _, payload = research
    store.submit("research", payload)

    def failed(*args, **kwargs):
        raise OSError("DO_NOT_LOG_ERROR_CONTENT")

    monkeypatch.setattr("market_research.orchestrator.write_reports", failed)
    job = execute(store)
    assert job.state == JobState.FAILED and job.error_code == "HANDLER_FAILED" and job.result_path is None
    assert not (store.output_directory(job.job_id) / "result").exists()


def test_heartbeat_protects_slow_authorized_analysis_from_second_worker(research, monkeypatch):
    store, _, payload = research
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store.clock = lambda: clock[0]
    store.lease_seconds = 3
    queued = store.submit("research", {**payload, "allow_inference": True})
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    heartbeat = store.heartbeat

    def renew(owner):
        result = heartbeat(owner)
        renewed.set()
        return result

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(6)
        return fake_analysis(*args, **kwargs)

    monkeypatch.setattr(store, "heartbeat", renew)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", blocking)
    with Worker(store) as first:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(first.run_once)
            try:
                assert entered.wait(3)
                clock[0] += timedelta(seconds=2)
                assert renewed.wait(3)
                clock[0] += timedelta(seconds=2)
                with Worker(store) as second:
                    assert second.run_once() is None
                    assert store.get(queued.job_id).state == JobState.RUNNING
            finally:
                release.set()
            assert running.result(timeout=6).state == JobState.COMPLETED


def test_empty_worker_start_and_smoke_never_construct_service_clients(research, monkeypatch):
    store, _, _ = research

    def forbidden(*args, **kwargs):
        raise AssertionError("Constructing a service client without an authorized job")

    monkeypatch.setattr("market_research.orchestrator.OllamaClient.__init__", forbidden)
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector.__init__", forbidden)
    assert execute(store) is None
    store.submit("smoke")
    assert execute(store).state == JobState.COMPLETED


def test_cli_research_runs_in_fresh_guarded_process_without_network(research, tmp_path):
    store, source, _ = research
    renamed = source.with_name("SYNTHETIC-nguồn-tuần.csv")
    source.rename(renamed)
    source = renamed
    guard = tmp_path / "network-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\ndef deny(*a,**k): raise RuntimeError('OFFLINE_TEST_NETWORK_FORBIDDEN')\n"
        "socket.socket.connect=deny\nsocket.getaddrinfo=deny\n", encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(guard), "PYTHONIOENCODING": "ascii"}
    prefix = [sys.executable, "-m", "market_research.jobs", "--db", str(store.database)]

    def cli(*args):
        process = subprocess.run([*prefix, *args], capture_output=True, text=True, encoding="utf-8", timeout=20, env=environment)
        assert process.returncode == 0, process.stdout + process.stderr
        return json.loads(process.stdout)

    queued = cli("submit-research", "--topic", TOPIC, "--mode", "csv_import", "--input", source.name, "--source-url", SOURCE)
    assert queued["state"] == "QUEUED" and not queued["request"]["allow_inference"]
    assert cli("worker", "--once")["state"] == "COMPLETED"
    inspected = cli("inspect", queued["job_id"])
    assert inspected["research_outcome"]["model_execution_status"] == "not_requested"
    assert inspected["result_path"].endswith("/result/manifest.json")


def test_cli_missing_input_has_helpful_safe_error(research, capsys):
    store, _, _ = research
    code = main(["--db", str(store.database), "submit-research", "--topic", TOPIC, "--mode", "csv_import",
                 "--input", "missing.csv", "--source-url", SOURCE])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["error"] == "INVALID_RESEARCH_REQUEST"
    assert "missing or unreadable" in output["message"] and "Traceback" not in output["message"]


def create_v1_database(path, *, active=False):
    """Minimal exact M3.1 columns; represents persisted local infrastructure, not research data."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE workers(worker_id TEXT PRIMARY KEY,heartbeat_at TEXT NOT NULL,lease_expires_at TEXT NOT NULL,stopped_at TEXT);
            CREATE TABLE jobs(job_id TEXT PRIMARY KEY,job_type TEXT NOT NULL CHECK(job_type='smoke'),request_json TEXT NOT NULL,
                state TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,cancellation_requested_at TEXT,
                phase TEXT NOT NULL,completed_steps INTEGER NOT NULL,total_steps INTEGER NOT NULL,error_code TEXT,error_message TEXT,
                result_path TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,execution_provenance TEXT NOT NULL DEFAULT 'NOT_RUN',
                worker_id TEXT REFERENCES workers(worker_id));
        """)
        db.execute("INSERT INTO settings VALUES ('schema_version','1')")
        db.execute("INSERT INTO settings VALUES ('artifact_root',?)", (str(path.parent / "artifacts"),))
        if active:
            db.execute("INSERT INTO workers VALUES (?,?,?,NULL)", ("b" * 32, "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"))
        db.execute("""INSERT INTO jobs(job_id,job_type,request_json,state,created_at,phase,completed_steps,total_steps)
            VALUES (?,'smoke','{"steps":2}','QUEUED','2026-01-01T00:00:00+00:00','QUEUED',0,2)""", ("a" * 32,))


def test_schema_v1_migrates_transactionally_without_losing_smoke_jobs(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    create_v1_database(database)
    store = JobStore(database, input_root=tmp_path)
    assert store.get("a" * 32).request.steps == 2 and store.get("a" * 32).research_outcome is None
    assert execute(store).state == JobState.COMPLETED
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "2"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE jobs SET request_json='{}' WHERE job_id=?", ("a" * 32,))


def test_schema_v1_upgrade_refuses_live_legacy_worker(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    create_v1_database(database, active=True)
    with pytest.raises(JobStoreError, match="Stop M3.1 workers"):
        JobStore(database)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "1"
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"


def test_schema_v1_completed_smoke_stays_terminal_with_original_bytes(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    create_v1_database(database)
    original = tmp_path / "artifacts" / ("a" * 32) / "smoke.json"
    original.parent.mkdir(parents=True)
    original.write_bytes(b'{"artifact_kind":"TEST_SMOKE","legacy":true}')
    with sqlite3.connect(database) as db:
        db.execute("""UPDATE jobs SET state='COMPLETED',completed_steps=2,phase='FINALIZING',attempt_count=1,
            execution_provenance='TEST_SMOKE',finished_at='2026-01-01T00:00:01+00:00',result_path=?""",
                   (f"{'a' * 32}/smoke.json",))
    before = original.read_bytes()
    store = JobStore(database)
    assert store.get("a" * 32).state == JobState.COMPLETED and execute(store) is None
    assert original.read_bytes() == before
