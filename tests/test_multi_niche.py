"""Synthetic offline fixtures; MockTransport never contacts a live provider."""
import copy
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from market_research.multi_niche import DatasetMappingError, load_batch_config, main, run_batch
from market_research.openlibrary_collector import OpenLibraryCollector


@pytest.fixture(autouse=True)
def forbid_external_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This integration suite must not call network services or Ollama")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.__init__", forbidden)


def write_csv(path, keyword="cozy coloring book", region="Hoa Kỳ", values=None):
    values = values if values is not None else range(10, 90, 10)
    dates = [(date(2024, 1, 7) + timedelta(weeks=i)).isoformat() for i in range(len(values))]
    path.write_text("Danh mục: Tất cả danh mục\n\nTuần," + keyword + ": (" + region + ")\n" +
                    "\n".join(f"{day},{value}" for day, value in zip(dates, values)) + "\n", encoding="utf-8-sig")


@pytest.fixture
def registry_file(tmp_path):
    raw = json.loads((Path(__file__).resolve().parents[1] / "niches.json").read_text(encoding="utf-8"))
    cozy = raw["niches"][0]["trends"]
    cozy.update(csv_path="cozy.csv", period_start="2024-01-07", period_end="2024-02-25")
    write_csv(tmp_path / "cozy.csv")
    config = tmp_path / "niches.json"
    config.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config


def mock_collector(fail_query=None, duplicates=False):
    requests = []
    def handler(request):
        query = request.url.params["q"]
        requests.append(request)
        if query == fail_query:
            return httpx.Response(503)
        # IDs deliberately overlap across niches: each batch evidence ID must still be independent.
        docs = [{"key": "/works/OL100W", "title": query, "author_name": ["Synthetic Test Author"], "edition_count": 3},
                {"key": "/works/OL101W", "title": "History of Ships"}]
        if duplicates:
            docs.append(copy.deepcopy(docs[0]))
        return httpx.Response(200, json={"docs": docs, "numFound": 999})
    return OpenLibraryCollector(client=httpx.Client(transport=httpx.MockTransport(handler)),
                                min_interval_seconds=0, max_retries=0), requests


def read_result(batch, niche_id):
    root = Path(batch["output_directory"]) / niche_id
    return json.loads((root / "result.json").read_text(encoding="utf-8")), root


def test_four_independent_niches_and_truthful_artifacts(registry_file, tmp_path):
    collector, requests = mock_collector()
    batch = run_batch(registry_file, output_dir=tmp_path / "batches", collect_live=True, collector=collector)
    assert len(requests) == len(batch["niches"]) == 4
    assert len({str(request.url) for request in requests}) == 4
    assert batch["execution_status"] == "COMPLETED"
    assert batch["ranking_status"] == "NOT_YET_RANKABLE" and batch["cross_niche_trends_comparable"] is False
    all_catalog_ids = set()
    for summary in batch["niches"]:
        result, root = read_result(batch, summary["niche_id"])
        evidence = json.loads((root / "trends-evidence.json").read_text(encoding="utf-8"))
        facts = json.loads((root / "verified-facts.json").read_text(encoding="utf-8"))
        snapshot = json.loads((root / "catalog-snapshot.json").read_text(encoding="utf-8"))
        ids = set(result["catalog"]["record_ids"].values())
        assert ids.isdisjoint(all_catalog_ids)
        all_catalog_ids.update(ids)
        assert result["catalog"]["parsed_items"] == 2 and result["catalog"]["relevance_candidates"] == 1
        assert result["catalog"]["source_total_matches"] == 999
        assert snapshot["query"] == result["catalog"]["query"]
        assert all(item["original_query"] == snapshot["query"] for item in snapshot["items"])
        assert result["catalog"]["provenance"] == "live"  # mocked live-mode transport, not proof of network access
        assert result["llm_execution_status"] == "NOT_REQUESTED"
        assert result["claim_validation_status"] == "NOT_RUN" and result["completeness"] == "PARTIAL_EVIDENCE"
        assert result["analysis_status"] == "DETERMINISTIC_SUMMARY"
        assert "Sales" in result["missing_data"] and "US retail availability" in result["missing_data"]
        assert result["candidate_product_hypotheses"][0]["status"] == "UNTESTED_HYPOTHESIS"
        if result["niche_id"] == "cozy-adults":
            assert len(evidence) == 8 and result["trends_status"] == "available"
            assert result["trend_interpretation"]["direction"] == "increasing"
            assert result["evidence_coverage"] == "CATALOG_AND_TRENDS"
            assert result["trends_provenance"]["collection_method"] == "manual_csv_import"
            assert len(result["trends_provenance"]["csv_sha256"]) == 64
            assert {row["keyword"] for row in evidence} == {"cozy coloring book"}
            assert all(row["run_id"] == result["run_id"] and row["niche"] == result["name"] for row in evidence)
            evidence_ids = {row["evidence_id"] for row in evidence}
            assert all(set(fact["evidence_ids"]) <= evidence_ids for fact in facts)
            assert set(result["trend_interpretation"]["supporting_fact_ids"]) <= {f["fact_id"] for f in facts}
            assert all(row["metric_name"] == "search_interest_index" for row in evidence)
        else:
            assert not evidence and not facts and not result["trend_metrics"]
            assert result["evidence_coverage"] == "CATALOG_ONLY" and result["trends_status"] == "missing"
            assert result["trend_interpretation"]["direction"] == "insufficient evidence"
            assert not result["trend_interpretation"]["supporting_fact_ids"]
        assert "claim validation: NOT_RUN" in (root / "summary.md").read_text(encoding="utf-8")
    root = Path(batch["output_directory"])
    report = (root / "summary.md").read_text(encoding="utf-8")
    assert "chưa xếp hạng" in report and "CATALOG_ONLY" in report and "CATALOG_AND_TRENDS" in report
    assert "market_size_score" not in (root / "summary.json").read_text(encoding="utf-8")
    assert len((root / "execution.jsonl").read_text(encoding="utf-8").splitlines()) == 4


@pytest.mark.parametrize("change", ["identity", "keyword", "url_region", "duplicate_identity", "duplicate_dataset"])
def test_ambiguous_config_mappings_are_rejected(registry_file, change):
    raw = json.loads(registry_file.read_text(encoding="utf-8"))
    if change == "identity":
        raw["niches"][0]["trends"]["niche_id"] = "bold-easy"
    elif change == "keyword":
        raw["niches"][1]["trends"] = copy.deepcopy(raw["niches"][0]["trends"])
        raw["niches"][1]["trends"]["niche_id"] = "bold-easy"
    elif change == "url_region":
        raw["niches"][0]["trends"]["source_url"] = raw["niches"][0]["trends"]["source_url"].replace("geo=US", "geo=GB")
    elif change == "duplicate_identity":
        raw["niches"][1] = copy.deepcopy(raw["niches"][0])
    else:
        trend = copy.deepcopy(raw["niches"][0]["trends"])
        trend.update(niche_id="bold-easy", keyword="bold and easy coloring books",
                     source_url="https://trends.google.com/trends/explore?geo=US&q=bold+and+easy+coloring+books")
        raw["niches"][1]["trends"] = trend
    registry_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_batch_config(registry_file)


@pytest.mark.parametrize("change", ["keyword", "region", "period", "multiple_series", "out_of_range"])
def test_csv_mismatches_are_rejected_before_requests_or_artifacts(registry_file, tmp_path, change):
    csv = registry_file.parent / "cozy.csv"
    if change == "keyword":
        write_csv(csv, keyword="bold and easy coloring books")
    elif change == "region":
        write_csv(csv, region="United Kingdom")
    elif change == "period":
        write_csv(csv, values=[10, 20])
    elif change == "out_of_range":
        write_csv(csv, values=[120] * 8)
    else:
        csv.write_text("Week,cozy coloring book: (US),bold and easy coloring books: (US)\n2024-01-07,1,2\n", encoding="utf-8")
    collector, requests = mock_collector()
    destination = tmp_path / "batch-must-not-exist"
    with pytest.raises(DatasetMappingError):
        run_batch(registry_file, output_dir=destination, collect_live=True, collector=collector)
    assert not requests and not destination.exists()


def test_one_provider_failure_does_not_stop_other_niches(registry_file, tmp_path):
    collector, requests = mock_collector(fail_query="bold and easy coloring books")
    batch = run_batch(registry_file, output_dir=tmp_path / "batches", collect_live=True, collector=collector)
    assert len(requests) == 4 and len(batch["niches"]) == 4
    results = {row["niche_id"]: row for row in batch["niches"]}
    failed = results.pop("bold-easy")
    assert failed["catalog"]["status"] == "failed" and failed["completeness"] == "BLOCKED"
    assert failed["catalog"]["parsed_items"] == 0 and failed["errors"][0]["status_code"] == 503
    assert all(row["catalog"]["status"] == "success" for row in results.values())


def test_duplicate_records_do_not_inflate_batch_candidates(registry_file, tmp_path):
    collector, _ = mock_collector(duplicates=True)
    batch = run_batch(registry_file, output_dir=tmp_path / "batches", collect_live=True, collector=collector)
    assert all(row["catalog"]["raw_items_returned"] == 3 and row["catalog"]["parsed_items"] == 2
               and row["catalog"]["relevance_candidates"] == 1 for row in batch["niches"])


def test_cache_replay_preserves_timestamps_and_rejects_wrong_query(registry_file, tmp_path):
    raw = json.loads(registry_file.read_text(encoding="utf-8"))
    collector, requests = mock_collector()
    timestamps = {}
    for niche in raw["niches"]:
        result = collector.collect(niche["catalog_query"])
        snapshot = tmp_path / f"{niche['niche_id']}.json"
        snapshot.write_text(result.model_dump_json(), encoding="utf-8")
        niche["catalog_snapshot"] = snapshot.name
        timestamps[niche["niche_id"]] = result.retrieved_at.isoformat()
    registry_file.write_text(json.dumps(raw), encoding="utf-8")
    requests.clear()
    batch = run_batch(registry_file, output_dir=tmp_path / "batches", collector=collector)
    assert not requests and batch["collection_mode"] == "offline"
    for row in batch["niches"]:
        assert row["catalog"]["provenance"] == "cache_replay"
        assert datetime.fromisoformat(row["catalog"]["retrieved_at"]) == datetime.fromisoformat(timestamps[row["niche_id"]])
        snapshot = json.loads((Path(batch["output_directory"]) / row["niche_id"] / "catalog-snapshot.json").read_text(encoding="utf-8"))
        assert all(item["provenance"] == "cache_replay" for item in snapshot["items"] + snapshot["relevant_items"])
    raw["niches"][1]["catalog_snapshot"] = raw["niches"][0]["catalog_snapshot"]
    registry_file.write_text(json.dumps(raw), encoding="utf-8")
    other = run_batch(registry_file, output_dir=tmp_path / "batches", collector=collector)
    wrong = next(row for row in other["niches"] if row["niche_id"] == "bold-easy")
    assert wrong["catalog"]["status"] == "failed" and wrong["catalog"]["parsed_items"] == 0


def test_two_trends_exports_remain_independent_not_cross_normalized(registry_file, tmp_path):
    raw = json.loads(registry_file.read_text(encoding="utf-8"))
    bold = copy.deepcopy(raw["niches"][0]["trends"])
    bold.update(niche_id="bold-easy", csv_path="bold.csv", keyword="bold and easy coloring books",
                source_url="https://trends.google.com/trends/explore?geo=US&q=bold+and+easy+coloring+books")
    raw["niches"][1]["trends"] = bold
    write_csv(tmp_path / "bold.csv", keyword=bold["keyword"], values=list(range(90, 10, -10)))
    registry_file.write_text(json.dumps(raw), encoding="utf-8")
    batch = run_batch(registry_file, output_dir=tmp_path / "batches")
    evidence_ids, fact_ids = set(), set()
    for niche_id in ["cozy-adults", "bold-easy"]:
        result, root = read_result(batch, niche_id)
        evidence = json.loads((root / "trends-evidence.json").read_text(encoding="utf-8"))
        facts = json.loads((root / "verified-facts.json").read_text(encoding="utf-8"))
        own_evidence_ids = {e["evidence_id"] for e in evidence}
        own_fact_ids = {f["fact_id"] for f in facts}
        assert own_evidence_ids.isdisjoint(evidence_ids) and own_fact_ids.isdisjoint(fact_ids)
        evidence_ids |= own_evidence_ids
        fact_ids |= own_fact_ids
        assert all(set(f["evidence_ids"]) <= own_evidence_ids for f in facts)
        assert result["ranking_status"] == "NOT_YET_RANKABLE" and result["cross_niche_trends_comparable"] is False
    assert batch["niches"][0]["trend_interpretation"]["direction"] == "increasing"
    assert batch["niches"][1]["trend_interpretation"]["direction"] == "decreasing"


def test_missing_optional_sources_and_unique_outputs(registry_file, tmp_path):
    first = run_batch(registry_file, output_dir=tmp_path / "batches")
    root = Path(first["output_directory"])
    original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    second = run_batch(registry_file, output_dir=tmp_path / "batches")
    assert first["output_directory"] != second["output_directory"]
    assert all((root / path).read_bytes() == content for path, content in original.items())
    assert all(row["catalog"]["status"] == "not_configured" for row in first["niches"])
    assert Path(first["output_directory"]).is_relative_to(tmp_path)
    (registry_file.parent / "cozy.csv").unlink()
    missing = run_batch(registry_file, output_dir=tmp_path / "batches")
    assert missing["niches"][0]["trends_status"] == "failed"
    assert len(missing["niches"]) == 4


def test_offline_cli_never_calls_provider_or_ollama(registry_file, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline CLI must not collect live data")
    monkeypatch.setattr(OpenLibraryCollector, "collect", forbidden)
    monkeypatch.setattr(sys, "argv", ["multi_niche", "--config", str(registry_file), "--no-ollama",
                                      "--output-dir", str(tmp_path / "cli-batches")])
    main()
    output = json.loads(capsys.readouterr().out)
    assert len(output["niches"]) == 4 and output["execution_status"] == "COMPLETED"
    assert output["niches"][0]["trends"] == "available"
    assert Path(output["output_directory"], "summary.md").is_file()


def test_disabled_sources_and_present_but_insufficient_trends_are_distinct(registry_file, tmp_path):
    raw = json.loads(registry_file.read_text(encoding="utf-8"))
    raw["niches"][0]["catalog_enabled"] = False
    raw["niches"][0]["trends"]["period_end"] = "2024-01-14"
    raw["niches"][1]["trends_enabled"] = False
    write_csv(tmp_path / "cozy.csv", values=[10, 20])
    registry_file.write_text(json.dumps(raw), encoding="utf-8")
    collector, requests = mock_collector()
    batch = run_batch(registry_file, output_dir=tmp_path / "batches", collect_live=True, collector=collector)
    assert len(requests) == 3
    cozy = batch["niches"][0]
    assert cozy["catalog"]["status"] == "disabled" and cozy["trends_status"] == "available"
    assert cozy["trend_observations"] == 2 and cozy["trend_sufficient"] is False
    assert cozy["trend_interpretation"]["direction"] == "insufficient evidence"
    assert batch["niches"][1]["trends_status"] == "disabled"
