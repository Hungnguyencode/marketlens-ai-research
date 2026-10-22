import csv
import json
import os
from datetime import date, timedelta
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from market_research.comparison import ComparisonConfig, run_comparison
from market_research.dashboard_data import ArtifactStore
from market_research.models import (Evidence, GroundedClaim, MarketAnalysis, ProductIdea,
                                    ResearchResult, ValidationResult, VerifiedFact)
from market_research.multi_niche import load_batch_config, run_batch
from market_research.openlibrary_collector import OpenLibraryCollector


ROOT = Path(__file__).resolve().parents[1]


def clear_marketlens_environment(monkeypatch):
    """Application settings must never leak into offline tests or child processes."""
    for name in list(os.environ):
        if name.startswith("MARKETLENS_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def isolated_marketlens_environment(monkeypatch):
    clear_marketlens_environment(monkeypatch)


def write_synthetic_google_trends_csv(path):
    """Write test-only SYNTHETIC observations; never use this as market evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    start = date(2025, 10, 19)
    keywords = [
        "cozy coloring book",
        "bold and easy coloring book",
        "christmas coloring book for adults",
        "kids christmas activity book",
    ]
    values = [[54, 0, 0, 0] for _ in range(53)]
    values[1][0] = 39
    for week, value in zip(range(44, 48), [55, 58, 57, 60]):
        values[week][0] = value
    for week, value in zip(range(48, 52), [70, 80, 82, 82]):
        values[week][0] = value
    values[-1][0] = 91
    for week, value in zip(range(1, 9), [11, 11, 11, 11, 11, 11, 11, 10]):
        values[week][1] = value
    for week in range(1, 5):
        values[week][2] = 10
    for week, value in [(1, 11), (4, 9), (5, 8), (6, 8)]:
        values[week][3] = value

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["SYNTHETIC TEST FIXTURE — NOT MARKET DATA"])
        writer.writerow(["Danh mục: Tất cả danh mục"])
        writer.writerow([])
        writer.writerow(["Tuần", *[f"{keyword}: (Hoa Kỳ)" for keyword in keywords]])
        for index, row in enumerate(values):
            writer.writerow([(start + timedelta(weeks=index)).isoformat(), *row])
    return path


@pytest.fixture
def synthetic_google_trends_csv(tmp_path):
    return write_synthetic_google_trends_csv(tmp_path / "SYNTHETIC-google-trends.csv")


@pytest.fixture
def synthetic_dashboard_artifacts(tmp_path_factory):
    """Build isolated SYNTHETIC test artifacts; no files under repository outputs are read or written."""
    root = tmp_path_factory.mktemp("SYNTHETIC-marketlens")
    repository = root / "workspace"
    repository.mkdir()
    (repository / "SYNTHETIC_TEST_FIXTURE.txt").write_text(
        "SYNTHETIC TEST FIXTURES ONLY — NOT RESEARCH EVIDENCE.\n", encoding="utf-8"
    )
    csv_path = write_synthetic_google_trends_csv(root / "SYNTHETIC-data" / "joint-export.csv")
    batch_config = load_batch_config(ROOT / "niches.json")
    for niche in batch_config.niches:
        niche.trends = None

    def synthetic_catalog(request):
        query = request.url.params["q"]
        return httpx.Response(200, json={"numFound": 2, "docs": [
            {"key": "/works/OL1W", "title": query, "author_name": ["SYNTHETIC Test Author"],
             "first_publish_year": 2020, "edition_count": 3},
            {"key": "/works/OL2W", "title": "SYNTHETIC Unrelated Fixture"},
        ]})

    collector = OpenLibraryCollector(
        client=httpx.Client(transport=httpx.MockTransport(synthetic_catalog)), min_interval_seconds=0
    )
    batch = run_batch(batch_config, output_dir=repository / "outputs/batches",
                      collect_live=True, collector=collector)
    raw = json.loads((ROOT / "comparison.example.json").read_text(encoding="utf-8"))
    raw.update(csv_path=str(csv_path), batch_directory=batch["output_directory"], joint_export_confirmed=True)
    comparison = run_comparison(ComparisonConfig.model_validate(raw),
                                output_dir=repository / "outputs/comparisons")
    store = ArtifactStore(repository)
    index = store.discover()
    return {
        "repository": repository,
        "store": store,
        "batch_result": batch,
        "comparison_result": comparison,
        "batch": index.batches[batch["batch_id"]],
        "comparison": index.comparisons[comparison["run_id"]],
        "csv_path": csv_path,
    }


@pytest.fixture
def synthetic_historical_result_json():
    """An explicitly synthetic schema fixture used only to test historical-artifact rejection paths."""
    run_id = "a7c48c0031e4"
    evidence_id = "synthetic-evidence-1"
    fact_id = "synthetic-fact-1"
    now = datetime.now(timezone.utc)
    evidence = Evidence(
        evidence_id=evidence_id, run_id=run_id, niche="cozy coloring books for adults",
        keyword="cozy coloring book", source_name="SYNTHETIC TEST SOURCE",
        source_url="https://example.invalid/synthetic-test-only", collected_at=now,
        target_region="United States", observation_period="SYNTHETIC TEST FIXTURE",
        metric_name="synthetic_fixture_metric", metric_value=1, metric_unit="test-only",
        source_excerpt="SYNTHETIC TEST FIXTURE — NOT MARKET EVIDENCE",
        extraction_method="Synthetic test fixture; no collection or inference occurred.",
        collection_status="success",
    )
    fact = VerifiedFact(
        fact_id=fact_id, keyword="cozy coloring book", metric_name="synthetic_fixture_metric",
        value=1, unit="test-only", observation_period="SYNTHETIC TEST FIXTURE",
        evidence_ids=[evidence_id], description="SYNTHETIC TEST FIXTURE — NOT A MARKET FACT",
    )
    ideas = [ProductIdea(
        what_to_sell=f"SYNTHETIC test-only concept {index}", target_customer="SYNTHETIC test persona",
        why_now=GroundedClaim(text="SYNTHETIC test-only claim", fact_ids=[fact_id]),
        supporting_fact_ids=[fact_id], supporting_evidence_ids=[evidence_id],
        uncertainties=["SYNTHETIC fixture only"], evidence_status="hypothesis",
        differentiation="SYNTHETIC test-only text",
    ) for index in (1, 2)]
    analysis = MarketAnalysis(
        main_keywords=["cozy coloring book"], related_keywords=[], keyword_research_candidates=[],
        evidence_based_inferences=[], hypotheses=[], trend_direction="stable",
        trend_direction_fact_ids=[fact_id], trend_reasons=[], target_customers=[],
        customer_needs_or_complaints=[], competition_assessment="SYNTHETIC test fixture only",
        indicative_price_range=None, product_ideas=ideas, confidence="low",
        missing_or_unavailable_data=["SYNTHETIC TEST FIXTURE — NOT MARKET EVIDENCE"],
        factual_observations=["SYNTHETIC TEST FIXTURE — NOT A MARKET FACT"],
    )
    result = ResearchResult(
        run_id=run_id, niche="cozy coloring books for adults", category="SYNTHETIC TEST FIXTURE",
        target_region="United States", report_language="Vietnamese", mode="replay",
        started_at=now, finished_at=now, evidence=[evidence], analysis=analysis,
        validation=ValidationResult(valid=True, status="VALID", schema_valid=True,
            claims_valid=True, evidence_sufficient=True), verified_facts=[fact],
        metadata={"analysis_provenance": "SYNTHETIC_TEST_FIXTURE",
                  "analysis_execution_status": "SYNTHETIC_TEST_FIXTURE"},
    )
    return result.model_dump(mode="json")


@pytest.fixture
def app_artifact_root(monkeypatch, synthetic_dashboard_artifacts):
    """Point Streamlit AppTest at isolated test artifacts, never the user's ignored outputs."""
    root = synthetic_dashboard_artifacts["repository"]
    monkeypatch.setenv("MARKETLENS_ARTIFACTS_DIR", str(root))
    return root


@pytest.fixture
def sample_evidence():
    return [Evidence(evidence_id="r1-e1", run_id="r1", niche="cozy coloring books for adults",
      keyword="cozy coloring books for adults", source_name="Google Books API",
      source_url="https://www.googleapis.com/books/v1/volumes?q=cozy", collected_at=Evidence.now(),
      target_region="United States", observation_period="snapshot", metric_name="catalog_results_reported",
      metric_value=12, metric_unit="bibliographic records", source_excerpt="API reported 12 records",
      raw_observation={"totalItems": 12}, extraction_method="fixture explicitly identified as synthetic test fixture",
      collection_status="success").model_dump(mode="json")]
