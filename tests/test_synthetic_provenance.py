"""Generated demo data only; real-import cases emulate formats, never collect evidence."""
import hashlib
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from market_research.job_store import JobState, JobStore, ResearchInputError
from market_research.job_worker import Worker
from market_research.research_workspace import WorkspaceError, preview_upload, verified_reports
from market_research.trends_csv import (GoogleTrendsCsvCollector, SYNTHETIC_CSV_MARKER,
                                       SYNTHETIC_NOTICE, SYNTHETIC_SOURCE)
from scripts.create_synthetic_demo import create_demo

REAL_URL = "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book"


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network or inference in synthetic provenance tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.plan", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.analyze", forbidden)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    path = create_demo(tmp_path / "SYNTHETIC-demo")
    store = JobStore(tmp_path / "runtime/jobs.sqlite3", input_root=tmp_path)
    monkeypatch.setenv("MARKETLENS_JOBS_DB", str(store.database))
    monkeypatch.setenv("MARKETLENS_INPUT_ROOT", str(store.input_root))
    monkeypatch.setenv("MARKETLENS_JOB_ARTIFACT_ROOT", str(store.artifact_root))
    return path, store


def collect(path, source=SYNTHETIC_SOURCE):
    return GoogleTrendsCsvCollector(path, source).collect(
        run_id="synthetic-test", niche="Test only", keyword="cozy coloring book", target_region="United States")


def test_demo_records_have_distinct_provenance_and_unchanged_values_and_id_algorithm(demo):
    path, _ = demo
    before = path.read_bytes()
    rows = collect(path)
    assert [item.metric_value for item in rows] == [54, None, 80, 64]
    assert [item.raw_observation["raw_value"] for item in rows] == ["54", "<1", "80", "64"]
    for item in rows:
        digest = hashlib.sha256(f"synthetic-test|{GoogleTrendsCsvCollector.name}|cozy coloring book|{item.observation_period}".encode()).hexdigest()[:16]
        assert item.evidence_id == f"synthetic-test-{digest}"
        assert item.source_url == SYNTHETIC_SOURCE and "SYNTHETIC" in item.source_name
        assert item.metric_name == "synthetic_demo_index" and "SYNTHETIC" in item.metric_unit
        assert item.raw_observation["data_provenance"] == "SYNTHETIC"
        assert item.raw_observation["synthetic_marker"] == SYNTHETIC_CSV_MARKER
        assert item.raw_observation["geographic_metadata"] == "Hoa Kỳ"
        assert SYNTHETIC_NOTICE == item.extraction_method
    assert path.read_bytes() == before


def test_demo_marker_survives_bom_blank_lines_and_metadata_before_marker(demo):
    path, _ = demo
    path.write_text("\nDanh mục: Tất cả danh mục\n\n" + path.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
    assert all(item.source_url == SYNTHETIC_SOURCE for item in collect(path))
    with pytest.raises(ValueError, match="SYNTHETIC"):
        collect(path, REAL_URL)


@pytest.mark.parametrize("mismatch", ["region", "multiple-keywords"])
def test_demo_still_requires_us_single_keyword_series(demo, mismatch):
    path, store = demo
    text = path.read_text(encoding="utf-8-sig")
    if mismatch == "region":
        text = text.replace("Hoa Kỳ", "Canada")
    else:
        text = text.replace("cozy coloring book: (Hoa Kỳ)", "cozy coloring book: (Hoa Kỳ),other: (Hoa Kỳ)")
        text = text.replace("2025-10-19,54", "2025-10-19,54,12")
    with pytest.raises(WorkspaceError):
        preview_upload(store, path.name, text.encode("utf-8-sig"), topic="Demo", source_url="")
    assert not store.list_jobs()


@pytest.mark.parametrize("source", [REAL_URL, "https://example.invalid/", SYNTHETIC_SOURCE + "-other"])
def test_demo_rejects_reconstructed_or_other_source_identifiers(demo, source):
    path, store = demo
    with pytest.raises(ValueError, match="SYNTHETIC"):
        collect(path, source)
    with pytest.raises((WorkspaceError, ResearchInputError)):
        preview_upload(store, path.name, path.read_bytes(), topic="Demo", source_url=source)
    assert not store.list_jobs()


def test_reserved_identifier_requires_content_marker_not_filename_or_topic(demo):
    path, store = demo
    raw = path.read_text(encoding="utf-8-sig").replace(SYNTHETIC_CSV_MARKER + "\n", "").encode("utf-8-sig")
    with pytest.raises(WorkspaceError):
        preview_upload(store, "SYNTHETIC.csv", raw, topic="SYNTHETIC demo", source_url=SYNTHETIC_SOURCE)
    payload = {"topic": "Demo", "mode": "csv_import", "input_path": path.relative_to(store.input_root).as_posix(),
               "source_url": REAL_URL}
    with pytest.raises(ResearchInputError, match="SYNTHETIC"):
        store.submit("research", payload)
    path.write_bytes(raw)
    payload["source_url"] = SYNTHETIC_SOURCE
    with pytest.raises(ResearchInputError, match="marker"):
        store.submit("research", payload)


@pytest.mark.parametrize("change", ["missing-url", "keyword", "region", "url-region", "unsafe-url"])
def test_real_import_validation_stays_strict(demo, change):
    path, store = demo
    raw = path.read_text(encoding="utf-8-sig").replace(SYNTHETIC_CSV_MARKER + "\n", "")
    url = REAL_URL
    if change == "missing-url": url = ""
    if change == "keyword": raw = raw.replace("cozy coloring book:", "different keyword:")
    if change == "region": raw = raw.replace("Hoa Kỳ", "Canada")
    if change == "url-region": url = url.replace("geo=US", "geo=CA")
    if change == "unsafe-url": url += "&token=private"
    with pytest.raises(WorkspaceError):
        preview_upload(store, "input.csv", raw.encode("utf-8-sig"), topic="Real format test", source_url=url)
    assert not store.list_jobs()


def test_real_format_still_uses_original_source_and_metric(demo):
    path, store = demo
    raw = path.read_text(encoding="utf-8-sig").replace(SYNTHETIC_CSV_MARKER + "\n", "").encode("utf-8-sig")
    view = preview_upload(store, "input.csv", raw, topic="Real format test", source_url=REAL_URL)
    rows = collect(store.input_root / view.request.input_path, REAL_URL)
    assert view.request.source_url == REAL_URL
    assert all(item.source_name == "Google Trends (manual CSV export)" and item.metric_name == "search_interest_index"
               and "data_provenance" not in item.raw_observation for item in rows)


def test_demo_preview_rejects_inference_even_when_confirmed(demo):
    path, store = demo
    with pytest.raises(WorkspaceError, match="SYNTHETIC"):
        preview_upload(store, path.name, path.read_bytes(), topic="Demo", source_url="",
                       allow_inference=True, inference_confirmed=True)
    with pytest.raises(ResearchInputError, match="SYNTHETIC"):
        store.submit("research", {"topic": "Demo", "mode": "csv_import", "input_path": path.relative_to(store.input_root).as_posix(),
                                  "source_url": SYNTHETIC_SOURCE, "allow_inference": True})


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.hrefs = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "a": self.hrefs.append(dict(attrs).get("href", ""))


def test_demo_worker_reports_audit_and_download_integrity(demo):
    path, store = demo
    view = preview_upload(store, path.name, path.read_bytes(), topic="Demo", source_url="")
    assert view.request.source_url == SYNTHETIC_SOURCE
    assert view.observations[1]["Index gốc"] == "<1"
    job = store.submit("research", view.payload(), expected_input_sha256=view.request.input_sha256)
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == JobState.COMPLETED and done.completed_steps == 8
    verified = verified_reports(store, job.job_id)
    result = verified.result
    assert result.validation.status == "DEGRADED" and not result.validation.evidence_sufficient
    assert result.metadata["data_provenance"] == "SYNTHETIC"
    assert result.metadata["collection_method"] == "synthetic_demo_csv"
    assert result.metadata["analysis_execution_status"] == "not_requested"
    assert result.metadata["evidence_records_received_by_ollama"] == 0
    assert result.metadata["computed_metrics"] == []
    assert SYNTHETIC_NOTICE in result.validation.warnings
    assert all(item.metric_name == "synthetic_demo_index" for item in result.evidence)
    for role in ("markdown", "html", "json"):
        text = verified.downloads[role][1].decode("utf-8")
        assert "SYNTHETIC" in text and REAL_URL not in text
        assert "Google Trends (manual CSV export)" not in text
    html = verified.downloads["html"][1].decode("utf-8")
    assert 'aria-label="SYNTHETIC provenance"' in html and "&lt;1" in html
    assert not any(href.startswith(("http", "urn:")) for href in Links(html).hrefs)
    assert "](urn:" not in verified.downloads["markdown"][1].decode("utf-8")
    manifest_path = store.artifact_root / done.result_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["input"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    html_path = manifest_path.parent / manifest["artifacts"]["html"]["path"]
    original = html_path.read_bytes()
    html_path.write_bytes(original + b"tampered")
    with pytest.raises(WorkspaceError): verified_reports(store, job.job_id)


@pytest.mark.parametrize("mixed", [False, True])
def test_replay_does_not_promote_synthetic_records_or_mix_with_real(demo, mixed):
    path, store = demo
    rows = [item.model_dump(mode="json") for item in collect(path)]
    if mixed:
        real = dict(rows[0], evidence_id="real-format-test", source_url=REAL_URL,
                    source_name="Google Trends (manual CSV export)", metric_name="search_interest_index", raw_observation={})
        rows.append(real)
    replay = store.input_root / "demo-replay.json"
    replay.write_text(json.dumps(rows), encoding="utf-8")
    job = store.submit("research", {"topic": "Demo replay", "mode": "replay", "input_path": replay.name})
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == (JobState.FAILED if mixed else JobState.COMPLETED)
    if mixed:
        assert done.result_path is None
    else:
        result = verified_reports(store, job.job_id).result
        assert result.metadata["data_provenance"] == "SYNTHETIC" and not result.validation.valid


@pytest.mark.parametrize("field,value", [("source_url", REAL_URL), ("metric_name", "search_interest_index"),
                                         ("source_name", "Google Trends (manual CSV export)")])
def test_replay_rejects_relabeling_demo_as_real_source_or_signal(demo, field, value):
    path, store = demo
    rows = [item.model_dump(mode="json") for item in collect(path)]
    rows[0][field] = value
    replay = store.input_root / "inconsistent-demo.json"
    replay.write_text(json.dumps(rows), encoding="utf-8")
    store.submit("research", {"topic": "Demo replay", "mode": "replay", "input_path": replay.name})
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == JobState.FAILED and done.result_path is None


def test_ui_labels_demo_preview_result_evidence_and_audit(demo, monkeypatch):
    path, store = demo
    raw = path.read_bytes()
    monkeypatch.setattr("streamlit.file_uploader", lambda *args, **kwargs:
                        SimpleNamespace(size=len(raw), name=path.name, getvalue=lambda: raw))
    app = AppTest.from_string("from market_research.dashboard_workspace import show_workspace\nshow_workspace()").run(timeout=20)
    app.text_input[0].set_value("Demo")
    def click(label):
        next(item for item in app.button if item.label == label).click().run(timeout=20)
        assert not app.exception
    click("Kiểm tra CSV & xem trước")
    assert any(SYNTHETIC_NOTICE in item.value for item in app.warning)
    assert "SYNTHETIC demo CSV" in "\n".join(item.value for item in app.get("html"))
    click("Gửi yêu cầu đã xem trước")
    with Worker(store) as worker:
        assert worker.run_once().state == JobState.COMPLETED
    click("Mở Theo dõi job")
    assert len(app.get("download_button")) == 3
    assert sum(SYNTHETIC_NOTICE in item.value for item in app.warning) >= 2
    assert SYNTHETIC_NOTICE in "\n".join(item.value for item in app.text)
