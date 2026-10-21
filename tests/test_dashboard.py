"""Offline dashboard adapters using synthetic catalog and Trends fixtures."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from market_research.dashboard_data import (ArtifactError, ArtifactStore, catalog_rows, chart_rows,
                                          default_selection, keyword_rows, overview_rows)
from market_research.multi_niche import NICHE_KEYWORDS


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dashboard must not call network services or Ollama")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.__init__", forbidden)


@pytest.fixture
def artifacts(synthetic_dashboard_artifacts):
    return (synthetic_dashboard_artifacts["store"],
            synthetic_dashboard_artifacts["batch_result"],
            synthetic_dashboard_artifacts["comparison_result"])


def test_normal_views_four_independent_niches_and_source_links(artifacts):
    store, batch_result, comparison_result = artifacts
    index = store.discover()
    assert not index.issues and len(index.batches) == len(index.comparisons) == 1
    batch = index.batches[batch_result["batch_id"]]
    comparison = index.comparisons[comparison_result["run_id"]]
    assert default_selection(index) == (batch.run_id, comparison.run_id)
    assert len(overview_rows(batch, comparison)) == len(keyword_rows(comparison)) == 4
    assert len(chart_rows(comparison, list(NICHE_KEYWORDS))) == 52 * 4
    points = chart_rows(comparison, list(NICHE_KEYWORDS), include_incomplete=True)
    assert len(points) == 53 * 4 and sum(not p["complete_week"] for p in points) == 4
    for niche_id in NICHE_KEYWORDS:
        rows = catalog_rows(batch, niche_id)
        assert len(rows) == 1 and rows[0]["Tên sách"] == batch.niches[niche_id].catalog.query
        assert rows[0]["Tác giả"] == "SYNTHETIC Test Author"
        assert rows[0]["Nguồn"] == "https://openlibrary.org/works/OL1W"
        assert len(catalog_rows(batch, niche_id, candidates_only=False)) == 2
        assert all(p["Keyword"] == comparison.metrics[niche_id].keyword for p in chart_rows(comparison, [niche_id]))
    assert all(row["LLM"] == "NOT_REQUESTED" and row["Claim validation"] == "NOT_RUN"
               for row in overview_rows(batch, comparison))
    # Standalone batch data is missing; the independently selected shared comparison is available for all four.
    assert all(row["CSV standalone trong batch"] == "missing" and row["Comparison chung"] == "Có"
               for row in overview_rows(batch, comparison))


def test_ranking_abstention_sparse_and_no_commercial_estimates(artifacts):
    store, _, report = artifacts
    comparison = store.discover().comparisons[report["run_id"]]
    decision = comparison.report["commercial_assessment"]
    assert decision["status"] == "INSUFFICIENT_EVIDENCE" and decision["market_opportunity_rank"] is None
    assert all(action["label"] == "RESEARCH_PRIORITY" for action in decision["research_priorities"])
    assert sum(m.sparse_signal for m in comparison.metrics.values()) == 3
    assert all(m.direction == "insufficient evidence" for m in comparison.metrics.values() if m.sparse_signal)
    assert not any("sales_estimate" in row or "price_estimate" in row or "competition_score" in row
                   for row in comparison.report["niches"])


@pytest.mark.parametrize("mutation", ["wrong_batch", "stale_checksum", "fake_top3", "dataset_values", "order", "missing_dataset"])
def test_mismatched_or_stale_comparison_is_excluded_without_losing_batch(artifacts, mutation):
    store, batch, comparison = artifacts
    path = Path(comparison["output_directory"]) / "comparison.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "wrong_batch":
        report["batch_provenance"]["batch_id"] = "unrelated-batch"
    elif mutation == "stale_checksum":
        original = Path(batch["output_directory"]) / "cozy-adults/result.json"
        original.write_bytes(original.read_bytes() + b"\n")
    elif mutation == "fake_top3":
        report["commercial_assessment"].update(status="RANKABLE", market_opportunity_rank=list(NICHE_KEYWORDS)[:3])
    elif mutation == "order":
        report["observed_keyword_signal_order"].reverse()
    elif mutation == "missing_dataset":
        (Path(comparison["output_directory"]) / "dataset.json").unlink()
    else:
        dataset_path = Path(comparison["output_directory"]) / "dataset.json"
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        dataset["series"][0]["observations"][0]["relative_index"] = 1
        dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    path.write_text(json.dumps(report), encoding="utf-8")
    index = store.discover()
    assert batch["batch_id"] in index.batches and not index.comparisons
    assert index.issues and default_selection(index) == (batch["batch_id"], None)


def test_newest_corrupt_run_falls_back_to_compatible_selection(artifacts):
    store, batch, comparison = artifacts
    bad = store.outputs / "comparisons/comparison-newer-corrupt"
    bad.mkdir()
    (bad / "comparison.json").write_text("{", encoding="utf-8")
    index = store.discover()
    assert default_selection(index) == (batch["batch_id"], comparison["run_id"])
    assert len(index.issues) == 1


def test_missing_and_corrupt_snapshots_do_not_invent_book_metadata(artifacts):
    store, batch, comparison = artifacts
    root = Path(batch["output_directory"])
    (root / "bold-easy/catalog-snapshot.json").unlink()
    (root / "kids-christmas/catalog-snapshot.json").write_text("[]", encoding="utf-8")
    index = store.discover()
    view = index.batches[batch["batch_id"]]
    assert set(view.catalog_issues) == {"bold-easy", "kids-christmas"}
    assert catalog_rows(view, "bold-easy") == [] and catalog_rows(view, "kids-christmas") == []
    assert len(view.catalogs) == 2 and comparison["run_id"] in index.comparisons


def test_unsafe_catalog_links_are_not_displayed(artifacts):
    store, batch, _ = artifacts
    path = Path(batch["output_directory"]) / "cozy-adults/catalog-snapshot.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["work_url"] = "javascript:alert(1)"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = store.discover().batches[batch["batch_id"]]
    assert "cozy-adults" in view.catalog_issues and not catalog_rows(view, "cozy-adults")


@pytest.mark.parametrize("path", ["../README.md", "batches/../../outside.json", "C:/Windows/win.ini", "/etc/passwd"])
def test_paths_are_confined_to_repository_outputs(artifacts, path):
    store, _, _ = artifacts
    with pytest.raises(ArtifactError):
        store.read(path)
    with pytest.raises(ArtifactError):
        store.run_id("../outside")


def test_nested_artifact_cannot_escape_selected_run(artifacts):
    store, batch, _ = artifacts
    path = Path(batch["output_directory"]) / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["niches"][0]["result_path"] = "../unrelated/result.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    index = store.discover()
    assert not index.batches and not index.comparisons and index.issues


def test_cache_fingerprint_changes_and_cross_batch_presenter_rejects_pairing(artifacts):
    store, batch, comparison = artifacts
    before = store.fingerprint()
    index = store.discover()
    view = copy.deepcopy(index.comparisons[comparison["run_id"]])
    view.batch_id = "unrelated-batch"
    with pytest.raises(ArtifactError):
        overview_rows(index.batches[batch["batch_id"]], view)
    path = Path(comparison["output_directory"]) / "dataset.json"
    path.write_bytes(path.read_bytes() + b"\n")
    assert store.fingerprint() != before


def test_empty_artifacts_and_read_only_loading(tmp_path, artifacts):
    assert default_selection(ArtifactStore(tmp_path / "empty").discover()) == (None, None)
    store, _, _ = artifacts
    contents = {p: p.read_bytes() for p in store.outputs.rglob("*") if p.is_file()}
    store.discover()
    assert all(p.read_bytes() == value for p, value in contents.items())


def test_missing_local_golden_is_not_promoted_to_ai_insights(tmp_path):
    store = ArtifactStore(tmp_path)
    assert "a7c48c0031e4" not in store.historical_paths()
    with pytest.raises(ArtifactError, match="Không tìm thấy run lịch sử"):
        store.load_historical("a7c48c0031e4")
    assert not store.discover().batches


def test_streamlit_pages_with_apptest_when_dependency_is_available(artifacts, app_artifact_root,
                                                                   tmp_path, monkeypatch):
    pytest.importorskip("streamlit", reason="Streamlit is not installed; offline loader/presentation tests run independently")
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "dashboard.py")).run(timeout=30)
    assert not app.exception
    for page in ["Phân tích từ khóa", "Khám phá niche", "Đánh giá cơ hội", "Bằng chứng & truy vết"]:
        next(button for button in app.sidebar.button if button.label == page).click().run(timeout=30)
        assert not app.exception
        if page == "Khám phá niche":
            for niche_id in NICHE_KEYWORDS:
                next(widget for widget in app.selectbox if widget.label == "Chọn niche").set_value(niche_id).run(timeout=30)
                assert not app.exception
    assert app_artifact_root == artifacts[0].repository
    empty_root = tmp_path / "SYNTHETIC-empty-dashboard"
    empty_root.mkdir()
    monkeypatch.setenv("MARKETLENS_ARTIFACTS_DIR", str(empty_root))
    empty = AppTest.from_file(str(ROOT / "dashboard.py")).run(timeout=30)
    assert not empty.exception
    assert "Chưa có batch nghiên cứu hợp lệ" in [title.value for title in empty.title]
