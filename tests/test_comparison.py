"""Offline comparison regression with an isolated synthetic Trends CSV fixture."""
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from market_research.comparison import (ComparisonConfig, compare_keyword_signals, load_comparison_config,
                                       main, parse_comparison, run_comparison)
from market_research.multi_niche import load_batch_config, run_batch
from market_research.openlibrary_collector import OpenLibraryCollector


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Comparison must not invoke network or Ollama")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.__init__", forbidden)


@pytest.fixture
def context(tmp_path, synthetic_google_trends_csv):
    batch_config = load_batch_config(ROOT / "niches.json")
    for niche in batch_config.niches:
        niche.trends = None
    def handler(request):
        return httpx.Response(200, json={"numFound": 20, "docs": [
            {"key": "/works/OL1W", "title": request.url.params["q"]},
            {"key": "/works/OL2W", "title": "History of Ships"}]})
    collector = OpenLibraryCollector(client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval_seconds=0)
    batch = run_batch(batch_config, output_dir=tmp_path / "fixture-batches", collect_live=True, collector=collector)
    raw = json.loads((ROOT / "comparison.example.json").read_text(encoding="utf-8"))
    raw.update(csv_path=str(synthetic_google_trends_csv), batch_directory=batch["output_directory"],
               joint_export_confirmed=True)
    return ComparisonConfig.model_validate(raw)


def test_portable_example_reports_missing_csv_and_batch_with_actionable_errors(tmp_path):
    example = json.loads((ROOT / "comparison.example.json").read_text(encoding="utf-8"))
    # The portable template intentionally requires explicit confirmation after CSV inspection.
    assert "joint_export_confirmed" not in example
    with pytest.raises(ValidationError, match="joint_export_confirmed"):
        ComparisonConfig.model_validate(example)
    example["joint_export_confirmed"] = True
    local_config = tmp_path / "comparison.local.json"
    local_config.write_text(json.dumps(example), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="Joint Google Trends CSV not found"):
        load_comparison_config(local_config)

    csv_path = tmp_path / "SYNTHETIC-test-only.csv"
    csv_path.write_text("SYNTHETIC TEST FIXTURE — NOT MARKET DATA\n", encoding="utf-8")
    example["csv_path"] = csv_path.name
    example["batch_directory"] = "outputs/batches/REPLACE_WITH_YOUR_BATCH_ID"
    local_config.write_text(json.dumps(example), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="Compatible batch directory not found"):
        load_comparison_config(local_config)


def test_joint_import_rejects_demo_marker_instead_of_labeling_it_real(context):
    from market_research.trends_csv import SYNTHETIC_CSV_MARKER
    raw = (SYNTHETIC_CSV_MARKER + "\n" + context.csv_path.read_text(encoding="utf-8-sig")).encode("utf-8-sig")
    with pytest.raises(ValueError, match="SYNTHETIC"):
        parse_comparison(context, csv_bytes=raw)


def synthetic_csv(config, values):
    config.period_start = date(2024, 1, 7)
    config.period_end = config.period_start + timedelta(weeks=len(values) - 1)
    config.as_of = config.period_end + timedelta(days=8)
    keywords = list(config.keyword_by_niche.values())
    header = "Week," + ",".join(keyword + ": (United States)" for keyword in keywords)
    rows = [header] + [(config.period_start + timedelta(weeks=i)).isoformat() + "," + ",".join(map(str, row))
                       for i, row in enumerate(values)]
    config.csv_path.write_text("\n".join(rows), encoding="utf-8-sig")


def test_synthetic_csv_complete_week_metrics_and_zero_semantics(context):
    dataset = parse_comparison(context)
    metrics = {m.niche_id: m for m in compare_keyword_signals(dataset)}
    assert len(dataset.series) == 4 and len({s.niche_id for s in dataset.series}) == 4
    assert [m.positive_observed_weeks_including_partial for m in metrics.values()] == [53, 8, 4, 4]
    assert all(m.observed_weeks == 53 and m.completed_weeks == 52 for m in metrics.values())
    assert all(m.excluded_incomplete_weeks == [date(2026, 10, 18)] for m in metrics.values())
    assert all(m.completed_period_end == date(2026, 10, 11) for m in metrics.values())
    assert metrics["cozy-adults"].recent_four_week_average == 78.5
    assert metrics["cozy-adults"].previous_four_week_average == 57.5
    assert metrics["cozy-adults"].change_in_index_points == 21
    assert metrics["bold-easy"].mean_relative_index == pytest.approx(87 / 52)
    assert metrics["christmas-adults"].mean_relative_index == pytest.approx(40 / 52)
    assert metrics["kids-christmas"].mean_relative_index == pytest.approx(36 / 52)
    assert metrics["cozy-adults"].sparse_signal is False
    for niche_id in ["bold-easy", "christmas-adults", "kids-christmas"]:
        assert metrics[niche_id].sparse_signal and metrics[niche_id].direction == "insufficient evidence"
        assert metrics[niche_id].window_interpretation == "NOT_INTERPRETABLE_SPARSE_SIGNAL"
        assert any("SPARSE_SIGNAL" in warning for warning in metrics[niche_id].warnings)
    zeros = [p for s in dataset.series for p in s.observations if p.relative_index == 0]
    assert zeros and all(p.raw_value == "0" for p in zeros)
    assert dataset.declared_export_settings["settings_origin"] == "user_declared" and dataset.source_url is None


@pytest.mark.parametrize("replacement,reason", [
    ("2025-10-19,54,,0,0", "Missing"),
    ("2025-10-19,54,N/A,0,0", "Missing"),
    ("2025-10-19,54,<1,0,0", "Censored"),
    ("2025-10-19,54,NaN,0,0", "finite"),
    ("2025-10-19,54,101,0,0", "0-100"),
    ("2025-10-19,54,-1,0,0", "0-100"),
    ("2025-10-19,54,broken,0,0", "Non-numeric"),
    ("2025-02-30,54,0,0,0", "Malformed"),
    ("20251019,54,0,0,0", "YYYY-MM-DD"),
    ("2025-10-19,54,0,0", "Incomplete"),
])
def test_missing_or_corrupted_values_are_rejected_without_imputation(context, replacement, reason):
    text = context.csv_path.read_text(encoding="utf-8").replace("2025-10-19,54,0,0,0", replacement)
    with pytest.raises(ValueError, match=reason):
        parse_comparison(context, csv_bytes=text.encode("utf-8"))


@pytest.mark.parametrize("change", ["duplicate_date", "gap", "truncated", "wrong_region", "wrong_keyword", "duplicate_keyword", "category"])
def test_ambiguous_headers_and_periods_are_rejected(context, change):
    text = context.csv_path.read_text(encoding="utf-8")
    if change == "duplicate_date":
        text = text.replace("2025-10-26", "2025-10-19")
    elif change == "gap":
        text = text.replace("2025-10-26,39,11,10,11\n", "")
    elif change == "truncated":
        text = text.replace("2026-10-18,91,0,0,0", "")
    elif change == "wrong_region":
        text = text.replace("(Hoa Kỳ)", "(United Kingdom)", 1)
    elif change == "wrong_keyword":
        text = text.replace("bold and easy coloring book:", "unmeasured phrase:")
    elif change == "duplicate_keyword":
        text = text.replace("bold and easy coloring book:", "cozy coloring book:")
    else:
        text = text.replace("Tất cả danh mục", "Sách")
    with pytest.raises(ValueError):
        parse_comparison(context, csv_bytes=text.encode("utf-8"))


def test_keyword_mapping_is_explicit_not_positional_and_handles_bom_nfd(context):
    dataset = parse_comparison(context)
    keywords = list(context.keyword_by_niche)
    context.keyword_by_niche = dict(reversed(list(context.keyword_by_niche.items())))
    reordered = parse_comparison(context, csv_bytes=b"\xef\xbb\xbf" + context.csv_path.read_bytes())
    original_values = {s.niche_id: [p.relative_index for p in s.observations] for s in dataset.series}
    assert keywords != [s.niche_id for s in reordered.series]
    assert all([p.relative_index for p in s.observations] == original_values[s.niche_id] for s in reordered.series)
    assert all(s.geographic_metadata == "Hoa Kỳ" for s in reordered.series)


def test_independent_exports_cannot_be_mixed(context):
    dataset = parse_comparison(context)
    dataset.series[1].dataset_id = "different-standalone-export"
    with pytest.raises(ValueError, match="Independent export"):
        compare_keyword_signals(dataset)
    standalone = b"Week,cozy coloring book: (US)\n2025-10-19,100\n"
    with pytest.raises(ValueError, match="joint export"):
        parse_comparison(context, csv_bytes=standalone)
    raw = context.model_dump()
    raw["joint_export_confirmed"] = False
    with pytest.raises(ValidationError):
        ComparisonConfig.model_validate(raw)


def test_wrong_niche_mapping_rejected_and_dataset_context_identity_preserved(context):
    raw = context.model_dump()
    raw["keyword_by_niche"] = {**raw["keyword_by_niche"], "bold-easy": "cozy coloring book"}
    with pytest.raises(ValidationError):
        ComparisonConfig.model_validate(raw)
    first = parse_comparison(context)
    second = parse_comparison(context)
    assert first.dataset_id == second.dataset_id
    changed = context.csv_path.read_bytes().replace(b"2025-10-19,54", b"2025-10-19,55")
    assert parse_comparison(context, csv_bytes=changed).dataset_id != first.dataset_id


def test_known_source_url_must_match_joint_terms_and_region(context):
    raw = context.model_dump()
    raw["source_url"] = "https://trends.google.com/trends/explore?geo=US&q=cozy+coloring+book"
    with pytest.raises(ValidationError):
        ComparisonConfig.model_validate(raw)


def test_reports_abstain_and_preserve_inputs_no_standalone_double_count(context, tmp_path):
    historical = {p: p.read_bytes() for p in context.batch_directory.rglob("*") if p.is_file()}
    source_bytes = context.csv_path.read_bytes()
    first = run_comparison(context, output_dir=tmp_path / "reports")
    directory = Path(first["output_directory"])
    report = (directory / "comparison.md").read_text(encoding="utf-8")
    assert "INSUFFICIENT_EVIDENCE" in report and "SPARSE_SIGNAL" in report
    assert "observed_keyword_signal_order" in report and "chưa phải business opportunities" in report
    assert first["commercial_assessment"]["market_opportunity_rank"] is None
    assert len(first["commercial_assessment"]["research_priorities"]) <= 3
    assert first["llm_execution_status"] == "NOT_REQUESTED" and first["claim_validation_status"] == "NOT_RUN"
    assert first["observed_keyword_signal_order"] == ["cozy-adults", "bold-easy", "christmas-adults", "kids-christmas"]
    all_ids = set()
    for row in first["niches"]:
        ids = set(row["keyword_metrics"]["supporting_evidence_ids"])
        assert ids.isdisjoint(all_ids)
        all_ids |= ids
        assert all(":" + row["niche_id"] + ":" in evidence_id for evidence_id in ids)
        assert row["standalone_trends_excluded_from_comparison"] is True
        assert all(h["status"] == "UNTESTED_HYPOTHESIS" for h in row["product_hypotheses"])
        assert row["missing_commercial_data"]
        assert not any(field in row for field in ["sales_estimate", "price_estimate", "competition_score", "demand_score"])
    assert (directory / "source.csv").read_bytes() == source_bytes
    dataset = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    assert dataset["csv_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert all(series["observations"][-1]["complete_week"] is False for series in dataset["series"])
    outputs = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    second = run_comparison(context, output_dir=tmp_path / "reports")
    assert second["output_directory"] != first["output_directory"]
    assert all(p.read_bytes() == raw for p, raw in historical.items())
    assert all(p.read_bytes() == raw for p, raw in outputs.items())


def test_dense_search_and_huge_catalog_counts_still_do_not_justify_commercial_ranking(context, tmp_path):
    synthetic_csv(context, [[10, 20, 30, 40]] * 8)
    for niche_id in context.keyword_by_niche:
        path = context.batch_directory / niche_id / "result.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        result["catalog"]["source_total_matches"] = 10**9
        result["catalog"]["raw_items_returned"] = 10000
        result["catalog"]["relevance_candidates"] = 2
        # A legacy standalone metric is intentionally incompatible; the comparison must not consume it.
        result["trend_metrics"] = [{"metric_id": "legacy-standalone", "keyword": "cozy coloring book", "status": "insufficient_data"}]
        path.write_text(json.dumps(result), encoding="utf-8")
    report = run_comparison(context, output_dir=tmp_path / "reports")
    assert report["observed_keyword_signal_order"] == ["kids-christmas", "christmas-adults", "bold-easy", "cozy-adults"]
    assert report["commercial_assessment"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["commercial_assessment"]["market_opportunity_rank"] is None
    assert all(not row["keyword_metrics"]["sparse_signal"] for row in report["niches"])


def test_short_complete_dataset_abstains_from_window_interpretation(context):
    synthetic_csv(context, [[1, 2, 3, 4]] * 3)
    metrics = compare_keyword_signals(parse_comparison(context))
    assert all(m.mean_relative_index == i + 1 for i, m in enumerate(metrics))
    assert all(m.window_interpretation == "INSUFFICIENT_PERIODS" and m.direction == "insufficient evidence" for m in metrics)


def test_cli_offline_with_accurate_json_status(context, tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "input.json"
    config_path.write_text(context.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["comparison", "--config", str(config_path), "--output-dir", str(tmp_path / "cli")])
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["commercial_assessment"] == "INSUFFICIENT_EVIDENCE"
    assert Path(output["output_directory"], "comparison.md").is_file()


def test_batch_join_uses_ids_even_when_summary_entries_are_reordered(context, tmp_path):
    path = context.batch_directory / "summary.json"
    batch = json.loads(path.read_text(encoding="utf-8"))
    batch["niches"].reverse()
    path.write_text(json.dumps(batch), encoding="utf-8")
    report = run_comparison(context, output_dir=tmp_path / "reports")
    expected = {row["niche_id"]: row["name"] for row in batch["niches"]}
    assert all(row["name"] == expected[row["niche_id"]] for row in report["niches"])


@pytest.mark.parametrize("corruption", ["catalog_id", "niche_identity", "path_escape"])
def test_invalid_batch_mapping_is_rejected_before_creating_outputs(context, tmp_path, corruption):
    path = context.batch_directory / "cozy-adults/result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "catalog_id":
        result["catalog"]["record_ids"]["OL1W"] = "another-niche-catalog-OL1W"
        path.write_text(json.dumps(result), encoding="utf-8")
    elif corruption == "niche_identity":
        result["niche_id"] = "bold-easy"
        path.write_text(json.dumps(result), encoding="utf-8")
    else:
        path = context.batch_directory / "summary.json"
        batch = json.loads(path.read_text(encoding="utf-8"))
        batch["niches"][0]["result_path"] = "../outside.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
    destination = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        run_comparison(context, output_dir=destination)
    assert not destination.exists()
