"""SYNTHETIC unit fixtures emulate genuine-import contracts; never market evidence."""
import json
import math
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from pathlib import Path

import pytest

from market_research.models import Evidence, GroundedClaim, ResearchResult
from market_research.report_intelligence import build_report_intelligence
from market_research.reports import render_html, render_markdown, write_reports
from test_html_reports import Document

SOURCE = "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book"


@pytest.fixture
def make_result(synthetic_historical_result_json):
    def make(values=None, *, start=date(2025, 10, 19), as_of=datetime(2026, 10, 23, tzinfo=timezone.utc)):
        values = values if values is not None else [54] * 53
        result = ResearchResult.model_validate(synthetic_historical_result_json)
        result.mode = "csv_import"
        result.finished_at = as_of
        result.evidence = [Evidence(
            evidence_id=f"fixture-e-{i}", run_id=result.run_id, niche=result.niche, keyword="cozy coloring book",
            source_name="Google Trends (manual CSV export)", source_url=SOURCE, collected_at=as_of,
            target_region="United States", observation_period=(start + timedelta(weeks=i)).isoformat(),
            metric_name="search_interest_index", metric_value=None if value in (None, "<1") else value,
            metric_unit="relative index", source_excerpt="SYNTHETIC unit fixture emulating real format",
            raw_observation={"period_column": "Tuần", "raw_value": "n/a" if value is None else str(value),
                             "geographic_metadata": "Hoa Kỳ", "csv_filename": "SYNTHETIC-test-only.csv"},
            extraction_method="User-provided Google Trends chart CSV import; manual export, not autonomous collection",
            collection_status="partial" if value in (None, "<1") else "success") for i, value in enumerate(values)]
        result.metadata = {"analysis_execution_status": "not_requested", "fixture_label": "SYNTHETIC_UNIT_TEST_ONLY"}
        result.validation.status = "DEGRADED"
        result.validation.valid = False
        return result
    return make


def series(result):
    return build_report_intelligence(result)["series"][0]


def metrics(result):
    return {item["key"]: item for item in series(result)["measurements"]}


def test_53_week_statistics_latest_windows_and_peak_are_reproducible(make_result):
    values = [54] * 53
    values[1], values[10], values[52] = 39, 100, 91
    values[44:48], values[48:52] = [55, 58, 57, 60], [70, 80, 82, 82]
    result = make_result(values)
    original = result.model_dump_json()
    measures = metrics(result)
    assert measures["count"]["value"] == 53 and measures["usable"]["value"] == 52
    assert measures["latest"]["value"] == "91" and "chưa hoàn tất" in measures["latest"]["unit"]
    assert measures["latest_usable"]["value"] == 82
    assert measures["recent4"]["value"] == 78.5 and measures["previous4"]["value"] == 57.5
    assert measures["delta4"]["value"] == 21
    assert measures["peak"]["value"] == 100 and measures["minimum"]["value"] == 39
    assert measures["peak"]["evidence_ids"] == ["fixture-e-10"]
    assert measures["range"]["value"] == 61
    average = sum(values[:52]) / 52
    deviation = math.sqrt(sum((value - average) ** 2 for value in values[:52]) / 52)
    assert measures["mean"]["value"] == round(average, 2)
    assert measures["std"]["value"] == round(deviation, 2)
    assert measures["peak_neighbors"]["value"] == "54 → 100 → 54"
    assert measures["largest_step"]["value"] == 46
    assert [measures[key]["value"] for key in ("up", "down", "flat")] == [5, 1, 1]
    assert len(series(result)["findings"]) == 4
    ids = {item.evidence_id for item in result.evidence}
    for measure in measures.values():
        assert measure["evidence_ids"] and set(measure["evidence_ids"]) <= ids
        assert measure["formula"] and measure["metric_id"].startswith("report_")
    assert build_report_intelligence(result) == build_report_intelligence(result)
    assert result.model_dump_json() == original


@pytest.mark.parametrize("value", [None, "<1", -1, 101, float("nan"), float("inf")])
def test_bad_or_censored_last_week_does_not_pull_older_windows_forward(make_result, value):
    values = [54] * 53
    values[51] = value
    result = make_result(values)
    m = metrics(result)
    assert m["usable"]["value"] == 51
    assert not {"recent4", "previous4", "delta4", "up"} & m.keys()
    assert m["censored"]["value"] == (1 if value == "<1" else 0)
    assert "chưa đủ" in " ".join(series(result)["caveats"]).lower()


def test_raw_value_conflict_is_excluded_without_rewriting_it(make_result):
    result = make_result()
    result.evidence[51].raw_observation["raw_value"] = "99"
    m = metrics(result)
    assert m["usable"]["value"] == 51 and "delta4" not in m
    assert result.evidence[51].metric_value == 54
    assert result.evidence[51].raw_observation["raw_value"] == "99"


def test_all_missing_values_produce_no_fabricated_numeric_signal(make_result):
    result = make_result([None] * 8)
    m = metrics(result)
    assert m["usable"]["value"] == 0
    assert not {"peak", "std", "mean", "recent4", "delta4"} & m.keys()
    assert "Thấp" in series(result)["confidence"]


@pytest.mark.parametrize("mutation", ["duplicate", "duplicate-id", "missing-week", "unsorted", "bad-date"])
def test_dates_and_cadence_fail_closed_for_directional_windows(make_result, mutation):
    result = make_result()
    if mutation == "duplicate": result.evidence[51].observation_period = result.evidence[50].observation_period
    if mutation == "duplicate-id": result.evidence[51].evidence_id = result.evidence[50].evidence_id
    if mutation == "missing-week": del result.evidence[49]
    if mutation == "unsorted": result.evidence[0], result.evidence[1] = result.evidence[1], result.evidence[0]
    if mutation == "bad-date": result.evidence[2].observation_period = "not-a-date"
    m = metrics(result)
    assert "delta4" not in m and "up" not in m
    if mutation in {"duplicate", "duplicate-id"}: assert m["usable"]["value"] == 0 and "std" not in m


@pytest.mark.parametrize("values", [[10], [10, 20, 30, 40], [0] * 8, [80, 70, 60, 50, 40, 30, 20, 10], [10, 20, 30, 40, 50, 60, 70, 80]])
def test_short_flat_rising_and_falling_series(make_result, values):
    result = make_result(values, start=date(2025, 1, 5))
    m = metrics(result)
    assert m["usable"]["value"] == len(values)
    if len(values) < 8:
        assert "delta4" not in m and "Thấp" in series(result)["confidence"]
        assert ("recent4" in m) == (len(values) >= 4)
    else:
        assert m["delta4"]["value"] == ((sum(values[4:]) - sum(values[:4])) / 4)
        assert m["flat"]["value"] == (7 if values == [0] * 8 else 0)
        if values == [0] * 8: assert m["std"]["value"] == 0 and m["censored"]["value"] == 0


def test_tied_peaks_keep_every_source_id_and_date(make_result):
    result = make_result([10, 80, 30, 80])
    m = metrics(result)
    assert m["peak"]["evidence_ids"] == ["fixture-e-1", "fixture-e-3"]
    assert all(result.evidence[index].observation_period in m["peak"]["unit"] for index in (1, 3))


@pytest.mark.parametrize("header", ["Day", "Ngày", "Month", "Tháng", "Date", ""])
def test_non_weekly_imports_are_not_called_four_week_trends(make_result, header):
    result = make_result()
    for item in result.evidence: item.raw_observation["period_column"] = header
    assert build_report_intelligence(result)["series"] == []


@pytest.mark.parametrize("mutation", ["synthetic", "mixed", "legacy-synthetic", "catalog", "unsafe-url", "wrong-query", "wrong-region"])
def test_only_eligible_non_synthetic_trends_records_enter_calculations(make_result, mutation):
    result = make_result()
    for item in result.evidence:
        if mutation == "synthetic": item.raw_observation["data_provenance"] = "SYNTHETIC"
        if mutation == "catalog": item.source_name = "Google Books API"
        if mutation == "unsafe-url": item.source_url = "javascript:alert(1)"
        if mutation == "wrong-query": item.source_url = SOURCE.replace("cozy%20coloring%20book", "other")
        if mutation == "wrong-region": item.target_region = "Canada"
    if mutation == "mixed": result.evidence[0].raw_observation["data_provenance"] = "SYNTHETIC"
    if mutation == "legacy-synthetic": result.evidence[0].raw_observation["SYNTHETIC"] = True
    assert build_report_intelligence(result)["series"] == []


def test_independently_normalized_exports_are_never_combined(make_result):
    result = make_result([10, 20, 30, 40])
    other = make_result([60, 70, 80, 90])
    for item in other.evidence:
        item.evidence_id += "-other"
        item.raw_observation["csv_filename"] = "other-export.csv"
    result.evidence += other.evidence
    view = build_report_intelligence(result)
    assert len(view["series"]) == 2
    assert all("delta4" not in {m["key"] for m in s["measurements"]} for s in view["series"])


def test_report_renderers_link_every_findings_metric_and_preserve_original_records(make_result, tmp_path):
    result = make_result()
    result.metadata["computed_metrics"] = [{"change_in_index_points": 987654321, "keyword": "fabricated"}]
    before = result.model_dump(mode="json")
    paths = write_reports(result, tmp_path)
    html, md = render_html(result), render_markdown(result)
    doc = Document(html)
    ids = {attrs.get("id") for _, attrs in doc.nodes}
    hrefs = {attrs.get("href") for tag, attrs in doc.nodes if tag == "a"}
    view = build_report_intelligence(result)
    for s in view["series"]:
        for finding in s["findings"]:
            for metric_id in finding["metric_ids"]:
                assert metric_id in ids and "#" + metric_id in hrefs and metric_id in md
        for m in s["measurements"]:
            for eid in m["evidence_ids"]:
                assert eid in md and "#evidence-" + str(next(i for i,e in enumerate(result.evidence) if e.evidence_id == eid)) in hrefs
    for text in (md, html):
        assert all(label in text for label in ("OBSERVED FACT", "INTERPRETATION", "HYPOTHESIS", "Competitive Landscape — Not Yet Verified", "Evidence cần"))
    assert "987654321" not in html.split('<section id="audit"', 1)[0] and "987654321" not in md.split("## Phụ lục kiểm toán", 1)[0]
    assert json.loads("".join(doc.raw)) == before
    assert result.model_dump(mode="json") == before
    assert json.loads(Path(paths["json"]).read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("status", ["VALID", "DEGRADED", "INVALID", "INSUFFICIENT_EVIDENCE", "BLOCKED"])
def test_measurements_do_not_change_validation_or_promote_rejected_claims(make_result, status):
    result = make_result()
    result.validation.status = status
    result.validation.valid = result.validation.claims_valid = status == "VALID"
    result.analysis.evidence_based_inferences = [GroundedClaim(text="UNSUPPORTED_MARKER", fact_ids=[])]
    before = result.validation.model_dump()
    html, md = render_html(result), render_markdown(result)
    assert ("UNSUPPORTED_MARKER" in html.split('<section id="audit"', 1)[0]) == (status == "VALID")
    assert ("UNSUPPORTED_MARKER" in md.split("## Phụ lục kiểm toán", 1)[0]) == (status == "VALID")
    assert result.validation.model_dump() == before


@pytest.mark.parametrize("flag", ["schema_valid", "valid", "claims_valid", "evidence_sufficient"])
def test_inconsistent_valid_flags_keep_llm_claims_in_audit_in_both_formats(make_result, flag):
    result = make_result()
    result.validation.status = "VALID"
    result.validation.valid = True
    setattr(result.validation, flag, False)
    result.analysis.evidence_based_inferences = [GroundedClaim(text="REJECTED_LLM_MARKER", fact_ids=[])]
    assert "REJECTED_LLM_MARKER" not in render_html(result).split('<section id="audit"', 1)[0]
    assert "REJECTED_LLM_MARKER" not in render_markdown(result).split("## Phụ lục kiểm toán", 1)[0]


def test_new_quantitative_cells_escape_untrusted_text(make_result):
    result = make_result()
    payload = '<script>alert(1)</script>'
    for item in result.evidence:
        item.keyword = payload
        item.source_url = "https://trends.google.com/trends/explore?geo=US&q=" + quote(payload)
    result.evidence[-1].raw_observation["raw_value"] = '<img src=x onerror=alert(1)>'
    doc = Document(render_html(result))
    assert not {"script", "img", "iframe"} & {tag for tag, _ in doc.nodes}
    assert payload in " ".join(doc.text)
