import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from market_research.analysis import fallback_analysis
from market_research.collectors import GoogleBooksCollectionError, GoogleBooksCollector
from market_research.models import Evidence, GroundedClaim, MarketAnalysis
from market_research.ollama import OllamaClient
from market_research.orchestrator import run_research
from market_research.reports import write_reports
from market_research.tools import ToolRegistry
from market_research.validator import validate_analysis
from market_research.models import ResearchConfig
from market_research.planner import AgentPlanner
from market_research.trends_csv import GoogleTrendsCsvCollector


def test_evidence_schema_and_json_roundtrip(tmp_path, sample_evidence):
    item = Evidence.model_validate(sample_evidence[0])
    assert item.evidence_id == "r1-e1"
    assert item.metric_value == 12


def test_tool_allowlist_and_budget():
    registry = ToolRegistry({"allowed": lambda: "ok"})
    assert registry.call("allowed", budget=1) == "ok"
    with pytest.raises(ValueError): registry.call("shell", budget=1)
    with pytest.raises(RuntimeError): registry.call("allowed", budget=1)


def test_planner_validates_allowlist_and_labels_fallback():
    planner = AgentPlanner(("google_books_catalog",), llm=lambda **_: {"keyword_candidates": ["cozy"],
        "decisions": [{"tool_name": "shell", "keyword": "cozy", "rationale": "bad"}], "rationale": "bad"})
    plan, source = planner.plan("cozy", ["cozy"])
    assert source == "deterministic_fallback_invalid_tool"
    assert plan.decisions[0].tool_name == "google_books_catalog"


def test_collector_normalization_with_explicit_mock():
    def handler(request):
        return httpx.Response(200, json={"totalItems": 1, "items": [{"id": "b1", "volumeInfo": {"title": "Cozy Test", "authors": ["A"]}, "saleInfo": {"listPrice": {"amount": 9.99, "currencyCode": "USD"}}}]})
    collector = GoogleBooksCollector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = collector.collect(run_id="r1", niche="cozy", keyword="cozy", target_region="United States")
    assert rows[0].metric_value == 1
    assert rows[1].metric_value == 9.99 and rows[1].metric_unit == "USD"
    assert rows[1].source_excerpt.startswith("Cozy Test")


def test_unknown_evidence_id_rejected(sample_evidence):
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    analysis.product_ideas[0].supporting_evidence_ids = ["invented"]
    result = validate_analysis(analysis, sample_evidence)
    assert not result.valid
    assert "invented" in result.unsupported_assertions[0]


def test_numeric_claim_must_match_input_metric(sample_evidence):
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    from market_research.analysis import build_verified_facts
    fact = build_verified_facts(sample_evidence)[0]
    analysis.evidence_based_inferences = [GroundedClaim(text="The catalog reported 12 records.", fact_ids=[fact.fact_id])]
    assert validate_analysis(analysis, sample_evidence).claims_valid
    analysis.evidence_based_inferences = [GroundedClaim(text="The catalog reported 120 records.", fact_ids=[fact.fact_id])]
    assert not validate_analysis(analysis, sample_evidence).claims_valid


def test_ollama_response_parsing():
    payload = fallback_analysis("cozy coloring books for adults", []).model_dump(mode="json")
    payload.pop("factual_observations")
    expected = json.dumps(payload)
    assert OllamaClient.parse_analysis("```json\n" + expected + "\n```").trend_direction == "insufficient evidence"
    with pytest.raises(ValueError): OllamaClient.parse_analysis("not json")


def test_collector_failure_is_explicit():
    def fail(request): return httpx.Response(503)
    collector = GoogleBooksCollector(client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(GoogleBooksCollectionError):
        collector.collect(run_id="r1", niche="cozy", keyword="cozy", target_region="US")


def test_429_respects_retry_after_and_retries_only_within_bound():
    calls, waits = [], []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"totalItems": 0})
    collector = GoogleBooksCollector(client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep_fn=waits.append, max_backoff_seconds=2)
    rows = collector.collect(run_id="r1", niche="cozy", keyword="cozy", target_region="US")
    assert len(calls) == 2 and waits == [1.0]
    assert rows[0].metric_value == 0


def test_429_quota_failure_is_diagnostic_and_never_logs_api_key():
    def handler(request):
        assert request.url.params.get("key") == "test-secret-key"
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": {"message": request.url.params["key"]}})
    collector = GoogleBooksCollector(api_key="test-secret-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=1, max_backoff_seconds=2,
        sleep_fn=lambda _: None)
    with pytest.raises(GoogleBooksCollectionError) as caught:
        collector.collect(run_id="r1", niche="cozy", keyword="cozy", target_region="US")
    diagnostic = str(caught.value) + str(caught.value.diagnostic())
    assert caught.value.status_code == 429
    assert caught.value.attempts == 1
    assert "test-secret-key" not in diagnostic
    assert caught.value.retry_after_seconds == 30


def test_google_books_403_quota_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"message": "quota exceeded", "errors": [{"reason": "dailyLimitExceeded"}]}})
    collector = GoogleBooksCollector(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep_fn=lambda _: None)
    with pytest.raises(GoogleBooksCollectionError) as caught:
        collector.collect(run_id="r1", niche="cozy", keyword="cozy", target_region="US")
    assert len(calls) == 1
    assert caught.value.reason == "dailyLimitExceeded"


def test_ollama_diagnostic_uses_generate_endpoint():
    def handler(request):
        assert request.url.path == "/api/generate"
        body = request.read()
        assert body and b"qwen3:4b-instruct" in body
        return httpx.Response(200, json={"model": "qwen3:4b-instruct", "response": "OK", "done": True})
    result = OllamaClient(client=httpx.Client(transport=httpx.MockTransport(handler))).diagnose()
    assert result["response"] == "OK" and result["done"] is True


def test_ollama_analysis_uses_schema_and_accepts_full_response(sample_evidence):
    expected = {"main_keywords": [sample_evidence[0]["keyword"]], "trend_direction": "insufficient evidence",
        "product_ideas": [
            {"what_to_sell": "Illustrated indoor scenes", "intended_customer_hypothesis": "adult colorists",
             "differentiation_rationale": "calm room layouts", "uncertainties": []},
            {"what_to_sell": "Nature-themed pages", "intended_customer_hypothesis": "garden hobbyists",
             "differentiation_rationale": "seasonal plants", "uncertainties": []}]}
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert isinstance(payload["format"], dict)
        assert payload["format"]["title"] == "AnalysisGenerationDraft"
        assert payload["options"]["num_predict"] > 0
        assert "fact_selection_options" in payload["prompt"]
        assert "fact_e_" not in payload["prompt"] and "fact_trend_" not in payload["prompt"]
        return httpx.Response(200, json={"response": json.dumps(expected, ensure_ascii=False)})
    result = OllamaClient(client=httpx.Client(transport=httpx.MockTransport(handler))).analyze(
        "cozy coloring books for adults", sample_evidence)
    assert result.main_keywords == expected["main_keywords"]
    assert result.product_ideas[0].what_to_sell == "Illustrated indoor scenes"
    assert len(result.product_ideas) == 2 and result.indicative_price_range is None
    assert result.confidence == "low" and "chưa được xác minh" in result.competition_assessment
    assert all(item.supporting_fact_ids == [] for item in result.product_ideas)
    assert len(requests) == 1


def test_ollama_analysis_corrects_incomplete_json_once(sample_evidence):
    expected = {"main_keywords": [sample_evidence[0]["keyword"]], "trend_direction": "insufficient evidence",
        "product_ideas": [
            {"what_to_sell": "Indoor scenes", "intended_customer_hypothesis": "adult colorists",
             "differentiation_rationale": "cozy layouts", "uncertainties": []},
            {"what_to_sell": "Nature pages", "intended_customer_hypothesis": "garden hobbyists",
             "differentiation_rationale": "seasonal flora", "uncertainties": []}]}
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        body = {"response": "{\"niche\":\"cozy coloring books\",\"findings\":[]}"} if len(calls) == 1 else {
            "response": json.dumps(expected, ensure_ascii=False)}
        return httpx.Response(200, json=body)
    result = OllamaClient(client=httpx.Client(transport=httpx.MockTransport(handler))).analyze(
        "cozy coloring books for adults", sample_evidence)
    assert result.main_keywords == expected["main_keywords"]
    assert len(calls) == 2 and "CORRECTION" in calls[1]["prompt"]


def test_validator_guided_ollama_correction_is_single_bounded_request(sample_evidence):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "not json"})
    client = OllamaClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="failed MarketAnalysis validation"):
        client.analyze("cozy coloring books for adults", sample_evidence,
            correction_diagnostics=["price claim is unsupported"])
    assert len(calls) == 1
    assert "price claim is unsupported" in calls[0]["prompt"]


def _historical_d65_corrected_analysis():
    from market_research.analysis import (attach_python_direction_reference, build_verified_facts,
        compute_trend_metrics, materialize_analysis)
    from market_research.models import AnalysisGenerationDraft, ResearchResult, ValidationResult
    from market_research.reports import write_reports

    # This deterministic series is a SYNTHETIC test fixture, never a market dataset.
    start = date(2025, 10, 19)
    values = [54] * 53
    values[1] = 39
    values[10] = 100  # synthetic peak on 2025-12-28
    values[44:48] = [55, 58, 57, 60]
    values[48:52] = [70, 80, 82, 82]
    values[52] = 91  # synthetic incomplete-week fixture
    original_evidence = [Evidence(
        evidence_id=f"SYNTHETIC-evidence-{index:02d}", run_id="SYNTHETIC-test-run",
        niche="cozy coloring books for adults", keyword="cozy coloring book",
        source_name="SYNTHETIC TEST FIXTURE", source_url="https://example.invalid/SYNTHETIC-test-only",
        collected_at=datetime(2026, 10, 20, tzinfo=timezone.utc), target_region="United States",
        observation_period=(start + timedelta(weeks=index)).isoformat(), metric_name="search_interest_index",
        metric_value=value, metric_unit="SYNTHETIC test-only relative index",
        source_excerpt="SYNTHETIC TEST FIXTURE — NOT MARKET EVIDENCE",
        raw_observation={"period_column": "Week", "raw_value": str(value)},
        extraction_method="SYNTHETIC unit-test fixture; no source collection occurred.",
        collection_status="success") for index, value in enumerate(values)]
    rows = [item.model_dump(mode="json") for item in original_evidence]
    metrics = compute_trend_metrics(rows, as_of=date(2026, 10, 20))
    facts = build_verified_facts(rows, metrics)
    original = ResearchResult(
        run_id="SYNTHETIC-test-run", niche="cozy coloring books for adults",
        category="SYNTHETIC TEST FIXTURE", target_region="United States", report_language="Vietnamese",
        mode="csv_import", started_at=original_evidence[0].collected_at,
        finished_at=original_evidence[0].collected_at, evidence=original_evidence,
        analysis=fallback_analysis("cozy coloring books for adults", rows, facts),
        validation=ValidationResult(valid=False, status="INVALID", schema_valid=True,
            claims_valid=False, evidence_sufficient=True,
            unsupported_assertions=["SYNTHETIC test-only starting state"]),
        verified_facts=facts,
        metadata={"live_collection_status": "success", "fixture_label": "SYNTHETIC_TEST_ONLY"},
    )
    by_alias = {
        "previous_four_week_average": next(f for f in facts if f.metric_name == "previous_four_week_average_relative_index"),
        "recent_four_week_average": next(f for f in facts if f.metric_name == "recent_four_week_average_relative_index"),
        "four_week_change": next(f for f in facts if f.metric_name == "four_week_average_change"),
        "four_week_direction": next(f for f in facts if f.metric_name == "four_week_average_direction"),
        "observed_peak": next(f for f in facts if f.metric_name == "peak_search_interest_index"),
    }
    def selection(alias):
        fact = by_alias[alias]
        return {"alias": alias, "keyword": fact.keyword, "observation_period": fact.observation_period}
    peak = by_alias["observed_peak"]
    previous, recent, change, direction = (by_alias[key] for key in
        ("previous_four_week_average", "recent_four_week_average", "four_week_change", "four_week_direction"))
    payload = AnalysisGenerationDraft.model_validate({
        "main_keywords": [original.evidence[0].keyword], "trend_direction": str(direction.value),
        "evidence_based_inferences": [
            {"text": f"The observed peak was {peak.value} on {peak.observation_period}.",
             "fact_selections": [selection("observed_peak")]},
            {"text": f"The four-week average moved from {previous.value} to {recent.value}, a change of {change.value} points.",
             "fact_selections": [selection("previous_four_week_average"), selection("recent_four_week_average"),
                 selection("four_week_change")]},
        ],
        "hypotheses": [{"text": "A seasonal campaign may have influenced the peak, but the cause is unverified.",
            "fact_selections": [selection("observed_peak")]}],
        "trend_reasons": [{"text": f"The recent comparison window is {recent.value}, versus {previous.value} previously; change {change.value} points.",
            "fact_selections": [selection("previous_four_week_average"), selection("recent_four_week_average"),
                selection("four_week_change"), selection("four_week_direction")]}],
        "product_ideas": [
            {"what_to_sell": "Cozy indoor scenes coloring book", "intended_customer_hypothesis": "adults who enjoy home décor themes",
             "differentiation_rationale": "room-by-room compositions", "uncertainties": []},
            {"what_to_sell": "Cozy garden and nature coloring book", "intended_customer_hypothesis": "adults interested in gardening",
             "differentiation_rationale": "seasonal botanical scenes", "uncertainties": []},
        ]})
    draft_analysis = materialize_analysis(payload, rows, facts, original.report_language)
    analysis = MarketAnalysis(**draft_analysis.model_dump(), factual_observations=[fact.description for fact in facts])
    from market_research.analysis import attach_python_direction_reference
    attach_python_direction_reference(analysis, facts)
    return original, rows, facts, analysis, by_alias


def test_typed_fact_aliases_map_to_exact_python_ids_and_reject_fake_selection():
    from market_research.analysis import resolve_fact_selections
    from market_research.models import FactSelection
    original, rows, facts, _, choices = _historical_d65_corrected_analysis()
    selection = FactSelection(alias="four_week_change", keyword=choices["four_week_change"].keyword,
        observation_period=choices["four_week_change"].observation_period)
    assert resolve_fact_selections([selection], facts) == [choices["four_week_change"].fact_id]
    fake = FactSelection(alias="four_week_change", keyword=selection.keyword, observation_period="2025-01-01..2025-01-02")
    with pytest.raises(ValueError, match="Unresolvable fact selection"):
        resolve_fact_selections([fake], facts)
    assert original.validation.status == "INVALID" and len(rows) == 53


def test_d65_offline_corrected_analysis_is_valid_and_keeps_two_concepts(tmp_path):
    from market_research.validator import validate_analysis
    original, rows, facts, analysis, _ = _historical_d65_corrected_analysis()
    validation = validate_analysis(analysis, rows, original.metadata.get("live_collection_status"),
        analysis_provenance="ollama", verified_facts=facts)
    assert original.validation.status == "INVALID"  # saved historical artifact remains unchanged
    assert validation.status == "VALID", validation.unsupported_assertions
    assert len(analysis.product_ideas) == 2
    assert all(idea.why_now.fact_ids and idea.why_now.fact_ids == idea.supporting_fact_ids for idea in analysis.product_ideas)
    from market_research.models import ResearchResult
    result = original.model_copy(deep=True)
    result.analysis, result.validation, result.verified_facts = analysis, validation, facts
    report = write_reports(result, tmp_path)
    rendered = __import__("pathlib").Path(report["markdown"]).read_text(encoding="utf-8")
    assert "Ý tưởng sản phẩm — giả thuyết" in rendered
    assert "Cozy indoor scenes coloring book" in rendered and "Cozy garden and nature coloring book" in rendered


@pytest.mark.parametrize("mutation, expected", [
    ("wrong_number", "numbers without matching referenced fact values"),
    ("wrong_date", "dates not present in referenced facts"),
    ("price_tier", "indicative_price_range is present without any verified price facts"),
    ("competition", "unsupported competition-intensity or market-opportunity claim"),
    ("customer_complaint", "presents an unmeasured preference or complaint as observed"),
    ("demand", "product demand or sales"),
    ("confidence", "high confidence is unsupported"),
    ("missing_product_refs", "lacks a relevant keyword-interest fact"),
])
def test_d65_corrected_fixture_rejects_unsupported_market_claims(mutation, expected):
    from market_research.validator import validate_analysis
    _, rows, facts, analysis, choices = _historical_d65_corrected_analysis()
    if mutation == "wrong_number":
        analysis.trend_reasons[0].text = "The recent comparison window is 999, versus 57.5 previously."
    elif mutation == "wrong_date":
        analysis.hypotheses[0].text = "The peak occurred on 2025-12-29, cause unknown."
    elif mutation == "price_tier":
        analysis.indicative_price_range = "Mid to high-end"
    elif mutation == "competition":
        analysis.competition_assessment = "The market opportunity is strong and competition is low."
    elif mutation == "customer_complaint":
        analysis.customer_needs_or_complaints = ["Customers complain about limited time and want quick pages."]
    elif mutation == "demand":
        analysis.hypotheses.append(GroundedClaim(text="Search interest proves consumer demand is rising.",
            fact_ids=[choices["four_week_change"].fact_id]))
    elif mutation == "confidence":
        analysis.confidence = "high"
    elif mutation == "missing_product_refs":
        analysis.product_ideas[0].why_now.fact_ids = []
        analysis.product_ideas[0].supporting_fact_ids = []
    validation = validate_analysis(analysis, rows, analysis_provenance="ollama", verified_facts=facts)
    assert validation.status == "INVALID"
    assert any(expected in item for item in validation.unsupported_assertions), validation.unsupported_assertions


def test_orchestrator_runs_exactly_one_validator_guided_correction_and_keeps_audit(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from market_research.analysis import materialize_analysis
    from market_research.models import AnalysisGenerationDraft
    monkeypatch.chdir(tmp_path)
    csv_path = tmp_path / "trends.csv"
    start = date(2025, 1, 5)
    rows = "\n".join(f"{(start + timedelta(days=7*i)).isoformat()},{30 + 5*i}" for i in range(8))
    csv_path.write_text("Week,cozy coloring book: (United States)\n" + rows + "\n", encoding="utf-8")
    calls = []

    def fake_analyze(self, niche, evidence, report_language, computed_metrics=None, verified_facts=None,
                     correction_diagnostics=None):
        calls.append(correction_diagnostics)
        facts = verified_facts
        by_metric = {fact.metric_name: fact for fact in facts if fact.metric_name in {
            "previous_four_week_average_relative_index", "recent_four_week_average_relative_index",
            "four_week_average_change", "four_week_average_direction"}}
        def selection(alias, metric):
            fact = by_metric[metric]
            return {"alias": alias, "keyword": fact.keyword, "observation_period": fact.observation_period}
        previous, recent, change, direction = (by_metric[key] for key in (
            "previous_four_week_average_relative_index", "recent_four_week_average_relative_index",
            "four_week_average_change", "four_week_average_direction"))
        generated = AnalysisGenerationDraft.model_validate({
            "main_keywords": [evidence[0]["keyword"]], "trend_direction": direction.value,
            "trend_reasons": [{"text": f"Recent average {recent.value} versus previous average {previous.value}; change {change.value} points.",
                "fact_selections": [selection("previous_four_week_average", "previous_four_week_average_relative_index"),
                    selection("recent_four_week_average", "recent_four_week_average_relative_index"),
                    selection("four_week_change", "four_week_average_change"),
                    selection("four_week_direction", "four_week_average_direction")]}],
            "product_ideas": [
                {"what_to_sell": "Cozy indoor scenes", "intended_customer_hypothesis": "adults interested in home themes",
                 "differentiation_rationale": "room-based scenes", "uncertainties": []},
                {"what_to_sell": "Cozy garden scenes", "intended_customer_hypothesis": "adults interested in gardens",
                 "differentiation_rationale": "seasonal plants", "uncertainties": []}]})
        analysis = materialize_analysis(generated, evidence, facts, report_language)
        if len(calls) == 1:
            analysis.indicative_price_range = "Mid to high-end"
            assert correction_diagnostics is None
        else:
            assert correction_diagnostics and any("price facts" in item for item in correction_diagnostics)
            assert len(calls) == 2
        return analysis

    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", fake_analyze)
    result = run_research(ResearchConfig(), "cozy coloring books for adults", mode="csv_import",
        output_dir=tmp_path / "outputs", trends_csv_path=str(csv_path),
        source_url="https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book")
    assert len(calls) == 2
    assert result.validation.status == "VALID"
    recovery = result.metadata["analysis_recovery"]
    assert recovery["attempted"] and recovery["result"] == "accepted"
    assert recovery["original_draft"]["indicative_price_range"] == "Mid to high-end"
    assert recovery["initial_diagnostics"] and recovery["final_diagnostics"] == []
    log_path = next((tmp_path / "logs").glob("*.jsonl"))
    log_entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(item["step_name"] == "correct_analysis" and item["status"] == "success" for item in log_entries)


def test_trend_metrics_compare_completed_four_week_windows_and_keep_keyword():
    from datetime import date, timedelta
    from market_research.analysis import compute_trend_metrics
    start = date(2025, 1, 5)
    values = [10, 20, 30, 40, 50, 60, 70, 80, 99]
    rows = [{"evidence_id": f"e{i}", "keyword": "cozy coloring book", "metric_name": "search_interest_index",
        "metric_value": value, "observation_period": (start + timedelta(days=7*i)).isoformat(),
        "source_url": "https://trends.google.com/trends/explore?geo=US"} for i, value in enumerate(values)]
    summary = compute_trend_metrics(rows, as_of=date(2025, 3, 5))[0]
    assert summary["keyword"] == "cozy coloring book"
    assert summary["status"] == "computed" and summary["direction"] == "increasing"
    assert summary["recent_window"]["average_relative_index"] == 65
    assert summary["previous_window"]["average_relative_index"] == 25
    assert summary["change_in_index_points"] == 40
    assert summary["latest_completed_week"] == "2025-02-23"
    assert "e8" not in summary["evidence_ids"]
    assert summary["peak"]["evidence_id"] == "e7"


def _trend_evidence(dates, values):
    return [{"evidence_id": f"trend-{i}", "keyword": "cozy coloring book", "source_name": "Google Trends (manual CSV export)",
        "source_url": "https://trends.google.com/trends/explore?geo=US&q=cozy", "metric_name": "search_interest_index",
        "metric_value": value, "metric_unit": "relative index", "observation_period": period,
        "raw_observation": {"period_column": "Week"}} for i, (period, value) in enumerate(zip(dates, values))]


def test_numeric_claim_accepts_cited_values_and_does_not_parse_iso_dates_as_metrics():
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="Google Trends recorded 52.0 on 2026-09-13 and 93.0 on 2026-10-11.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256(eid.encode()).hexdigest()[:12]}" for eid in ("trend-0", "trend-1")])]
    result = validate_analysis(analysis, evidence)
    assert result.claims_valid
    assert result.status == "VALID"


def test_fabricated_numeric_value_with_valid_evidence_ids_is_rejected():
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="Google Trends recorded 95.0 on 2026-10-11.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256('trend-1'.encode()).hexdigest()[:12]}"])]
    result = validate_analysis(analysis, evidence)
    assert not result.claims_valid
    assert any("95" in item for item in result.unsupported_assertions)


def test_trends_index_cannot_be_cited_as_price_evidence():
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="The price is $52.0.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256('trend-0'.encode()).hexdigest()[:12]}"])]
    result = validate_analysis(analysis, evidence)
    assert not result.claims_valid
    assert any("without referenced price facts" in item for item in result.unsupported_assertions)


def test_unknown_evidence_reference_is_rejected():
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="Google Trends recorded 52.0 on 2026-09-13.", fact_ids=["missing-id"])]
    result = validate_analysis(analysis, evidence)
    assert result.status == "INVALID"
    assert any("unknown fact IDs" in item for item in result.unsupported_assertions)


def test_monotonic_growth_claim_rejected_when_intermediate_weeks_decline():
    dates = [f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] + ["2025-02-02"]
    evidence = _trend_evidence(dates, [52, 72, 78, 72, 93])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="The series shows a consistent increase from 52 to 93 between 2025-01-05 and 2025-02-02.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256(f'trend-{i}'.encode()).hexdigest()[:12]}" for i in range(5)])]
    result = validate_analysis(analysis, evidence)
    assert result.status == "INVALID"
    assert any("not supported by all cited weekly observations" in item for item in result.unsupported_assertions)


def test_monotonic_growth_claim_accepted_when_every_cited_week_increases():
    dates = [f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] + ["2025-02-02"]
    evidence = _trend_evidence(dates, [52, 60, 70, 80, 93])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.evidence_based_inferences = [GroundedClaim(text="The series shows a monotonic increase from 52 to 93 between 2025-01-05 and 2025-02-02.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256(f'trend-{i}'.encode()).hexdigest()[:12]}" for i in range(5)])]
    result = validate_analysis(analysis, evidence)
    assert result.claims_valid


@pytest.mark.parametrize("text", [
    "consistent growth",
    "not consistent growth",
    "rather than a monotonic trend",
    "không tăng liên tục",
    "tăng liên tục mỗi tuần",
])
def test_monotonic_phrase_detection_respects_negation(text):
    from market_research.validator import _has_monotonic_growth
    assert _has_monotonic_growth(text) is (text == "consistent growth" or text == "tăng liên tục mỗi tuần")


def test_negated_growth_does_not_validate_uncited_fluctuations_or_declines():
    from market_research.analysis import build_verified_facts
    evidence = _trend_evidence([f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] + ["2025-02-02"],
        [52, 72, 78, 72, 93])
    facts = build_verified_facts(evidence)
    ids = [next(f.fact_id for f in facts if f.evidence_ids == [f"trend-{i}"]) for i in range(5)]
    analysis = fallback_analysis("cozy coloring book", evidence, facts)
    claim_text = ("Despite a recent increase in average search interest, weekly values fluctuated, "
        "dropping to 52 on 2025-01-05 before rising to 93 on 2025-02-02, suggesting volatility rather than a consistent or monotonic trend.")
    analysis.evidence_based_inferences = [GroundedClaim(text=claim_text, fact_ids=ids[:2])]
    incomplete = validate_analysis(analysis, evidence, verified_facts=facts)
    assert any("fluctuations without enough cited observations" in item for item in incomplete.unsupported_assertions)
    assert any("decline without cited observations" in item for item in incomplete.unsupported_assertions)
    assert not any("continuous/consistent growth" in item for item in incomplete.unsupported_assertions)
    analysis.evidence_based_inferences = [GroundedClaim(text=claim_text, fact_ids=ids)]
    complete = validate_analysis(analysis, evidence, verified_facts=facts)
    assert complete.claims_valid, complete.unsupported_assertions


def test_computed_metric_numeric_claim_needs_and_accepts_metric_reference():
    from datetime import date, timedelta
    from market_research.analysis import compute_trend_metrics
    start = date(2025, 1, 5)
    evidence = _trend_evidence([(start + timedelta(days=7*i)).isoformat() for i in range(8)],
        [10, 20, 30, 40, 50, 60, 70, 80])
    metric = compute_trend_metrics(evidence, as_of=date(2025, 3, 5))[0]
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    fact_id = f"fact_{metric['metric_id']}_four_week_change"
    analysis.trend_reasons = [GroundedClaim(text="Four-week average changed by 40.0 points.", fact_ids=[fact_id])]
    assert validate_analysis(analysis, evidence).claims_valid
    analysis.trend_reasons = [GroundedClaim(text="Four-week average changed by 41.0 points.", fact_ids=[fact_id])]
    fabricated = validate_analysis(analysis, evidence)
    assert not fabricated.claims_valid and any("41" in item for item in fabricated.unsupported_assertions)
    analysis.trend_reasons = [GroundedClaim(text="Four-week average changed by 41.0 points.", fact_ids=["unknown_metric"])]
    result = validate_analysis(analysis, evidence)
    assert not result.claims_valid
    assert any("unknown fact IDs" in item for item in result.unsupported_assertions)


def test_python_verified_facts_have_stable_ids_and_source_provenance():
    from market_research.analysis import build_verified_facts
    evidence = _trend_evidence([f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] +
        ["2025-02-02", "2025-02-09", "2025-02-16", "2025-02-23"], [10, 20, 30, 40, 50, 60, 70, 80])
    facts = build_verified_facts(evidence)
    point = next(fact for fact in facts if fact.evidence_ids == ["trend-0"])
    change = next(fact for fact in facts if fact.metric_name == "four_week_average_change")
    assert point.value == 10 and point.evidence_ids == ["trend-0"]
    assert change.value == 40 and len(change.evidence_ids) == 8 and change.metric_ids
    assert build_verified_facts(evidence)[0].fact_id == facts[0].fact_id


def test_numeric_scientific_notation_matches_referenced_fact():
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 100.0])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    fact_id = f"fact_e_{__import__('hashlib').sha256('trend-1'.encode()).hexdigest()[:12]}"
    analysis.evidence_based_inferences = [GroundedClaim(text="The index reached 1E+2.", fact_ids=[fact_id])]
    assert validate_analysis(analysis, evidence).claims_valid


def test_trend_direction_requires_matching_computed_fact():
    evidence = _trend_evidence([f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] +
        ["2025-02-02", "2025-02-09", "2025-02-16", "2025-02-23"], [80, 70, 60, 50, 10, 20, 30, 40])
    analysis = fallback_analysis("cozy coloring books for adults", evidence)
    analysis.trend_direction = "increasing"
    from market_research.analysis import build_verified_facts
    analysis.trend_direction_fact_ids = [next(f.fact_id for f in build_verified_facts(evidence)
        if f.metric_name == "four_week_average_direction")]
    result = validate_analysis(analysis, evidence)
    assert not result.claims_valid
    assert any("contradicts its referenced Python-computed direction fact" in item for item in result.unsupported_assertions)


def test_matching_python_direction_fact_reference_is_accepted():
    from datetime import date, timedelta
    from market_research.analysis import build_verified_facts
    start = date(2026, 8, 16)
    evidence = _trend_evidence([(start + timedelta(days=7*i)).isoformat() for i in range(8)],
        [10, 20, 30, 40, 50, 60, 70, 80])
    facts = build_verified_facts(evidence)
    direction = next(fact for fact in facts if fact.metric_name == "four_week_average_direction")
    analysis = fallback_analysis("cozy coloring book", evidence, facts)
    analysis.trend_direction = "increasing"
    analysis.trend_direction_fact_ids = [direction.fact_id]
    analysis.trend_reasons = [GroundedClaim(text="Recent comparison-window average is higher.", fact_ids=[direction.fact_id])]
    result = validate_analysis(analysis, evidence, verified_facts=facts)
    assert not any("trend_direction" in item for item in result.unsupported_assertions)


def test_direction_reference_to_unknown_fact_is_rejected():
    from datetime import date, timedelta
    start = date(2026, 8, 16)
    evidence = _trend_evidence([(start + timedelta(days=7*i)).isoformat() for i in range(8)],
        [10, 20, 30, 40, 50, 60, 70, 80])
    analysis = fallback_analysis("cozy coloring book", evidence)
    analysis.trend_direction = "increasing"
    analysis.trend_direction_fact_ids = ["fabricated-direction-fact"]
    result = validate_analysis(analysis, evidence)
    assert any("trend_direction references unknown fact IDs" in item for item in result.unsupported_assertions)


def test_factual_observations_cannot_be_replaced_by_model_text(sample_evidence):
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    analysis.factual_observations = ["There are 999 records."]
    result = validate_analysis(analysis, sample_evidence)
    assert result.status == "INVALID"
    assert any("Python-generated verified facts" in item for item in result.unsupported_assertions)


def test_vietnamese_decimal_commas_dates_and_window_facts_validate():
    from datetime import date, timedelta
    from market_research.analysis import build_verified_facts
    dates = [(date(2026, 8, 16) + timedelta(days=7*i)).isoformat() for i in range(9)]
    evidence = _trend_evidence(dates, [100.0, 50, 55, 60, 65, 70, 75, 85, 85])
    facts = build_verified_facts(evidence)
    def fact(metric):
        return next(item for item in facts if item.metric_name == metric)
    previous, recent = fact("previous_four_week_average_relative_index"), fact("recent_four_week_average_relative_index")
    change, peak, direction = fact("four_week_average_change"), fact("peak_search_interest_index"), fact("four_week_average_direction")
    analysis = fallback_analysis("cozy coloring book", evidence, facts)
    analysis.trend_direction = "increasing"
    analysis.trend_direction_fact_ids = [direction.fact_id]
    analysis.trend_reasons = [GroundedClaim(
        text="Trung bình chỉ số quan tâm của bốn tuần gần nhất cao hơn giai đoạn bốn tuần trước, nhưng dữ liệu dao động giữa các tuần.",
        fact_ids=[previous.fact_id, recent.fact_id, change.fact_id])]
    analysis.evidence_based_inferences = [
        GroundedClaim(text="Sự gia tăng 21,25 điểm tương đối: trung bình 57,5 lên 78,75.",
            fact_ids=[previous.fact_id, recent.fact_id, change.fact_id]),
        GroundedClaim(text="Chỉ số đạt 100,0 vào ngày 2026-08-16; tương đương 1E+2.",
            fact_ids=[peak.fact_id]),
    ]
    result = validate_analysis(analysis, evidence, verified_facts=facts)
    assert result.claims_valid, result.unsupported_assertions


def test_number_parser_supports_locales_scientific_and_grouping_without_date_fragments():
    from market_research.validator import _decimal
    assert _decimal("57,5") == _decimal("57.5")
    assert _decimal("100,0") == _decimal("100.0")
    assert _decimal("1E+2") == _decimal("100.0")
    assert _decimal("1,234") == _decimal("1234")
    assert _decimal("1,234.5") == _decimal("1234.5")
    assert _decimal("1.234,5") == _decimal("1234.5")


def test_price_claim_detection_distinguishes_gia_tang_from_gia_ban():
    from market_research.analysis import build_verified_facts
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    facts = build_verified_facts(evidence)
    ref = facts[0].fact_id
    analysis = fallback_analysis("cozy coloring book", evidence, facts)
    analysis.evidence_based_inferences = [GroundedClaim(text="Sự gia tăng 52,0 điểm tương đối.", fact_ids=[ref])]
    assert validate_analysis(analysis, evidence, verified_facts=facts).claims_valid
    analysis.evidence_based_inferences = [GroundedClaim(text="Giá bán sản phẩm là $19,99.", fact_ids=[ref])]
    result = validate_analysis(analysis, evidence, verified_facts=facts)
    assert not result.claims_valid
    assert any("price facts" in item for item in result.unsupported_assertions)


def test_non_monotonic_weekly_series_cannot_be_called_consecutive_growth():
    from market_research.analysis import build_verified_facts
    dates = [f"2025-01-{day:02d}" for day in (5, 12, 19, 26)] + ["2025-02-02"]
    evidence = _trend_evidence(dates, [52, 72, 78, 72, 93])
    facts = build_verified_facts(evidence)
    endpoint_facts = [next(item.fact_id for item in facts if item.evidence_ids == [f"trend-{i}"])
        for i in (0, 4)]
    analysis = fallback_analysis("cozy coloring book", evidence, facts)
    analysis.evidence_based_inferences = [GroundedClaim(
        text="Interest increased for 4 consecutive weeks between 2025-01-05 and 2025-02-02.", fact_ids=endpoint_facts)]
    result = validate_analysis(analysis, evidence, verified_facts=facts)
    assert not result.claims_valid
    assert any("continuous/consistent growth not supported" in item or "comparison-window averages" in item
        for item in result.unsupported_assertions)


def test_keyword_normalization_moves_unmeasured_related_terms_to_candidates(sample_evidence):
    from market_research.analysis import normalize_analysis_keywords
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    analysis.related_keywords = ["cozy coloring books for adults", "adult coloring book", "cozy theme coloring"]
    analysis.keyword_research_candidates = []
    normalize_analysis_keywords(analysis, sample_evidence)
    assert analysis.related_keywords == ["cozy coloring books for adults"]
    assert analysis.keyword_research_candidates == ["adult coloring book", "cozy theme coloring"]


def test_invalid_report_quarantines_model_claims_but_keeps_verified_facts(tmp_path):
    from market_research.models import ResearchResult, ValidationResult
    evidence = _trend_evidence(["2026-09-13", "2026-10-11"], [52.0, 93.0])
    analysis = fallback_analysis("cozy coloring book", evidence)
    facts = __import__("market_research.analysis", fromlist=["build_verified_facts"]).build_verified_facts(evidence)
    analysis.factual_observations = [fact.description for fact in facts]
    analysis.evidence_based_inferences = [GroundedClaim(text="Rejected demand claim.", fact_ids=["fake-fact"])]
    result = ResearchResult(run_id="invalid-report", niche="cozy coloring book", category="Coloring books",
        target_region="US", report_language="Vietnamese", mode="replay", started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc), evidence=[Evidence(evidence_id=e["evidence_id"], run_id="r1",
            niche="cozy coloring book", keyword=e["keyword"], source_name=e["source_name"], source_url=e["source_url"],
            collected_at=Evidence.now(), target_region="US", observation_period=e["observation_period"],
            metric_name=e["metric_name"], metric_value=e["metric_value"], metric_unit=e["metric_unit"],
            raw_observation=e["raw_observation"], extraction_method="test fixture", collection_status="success") for e in evidence],
        analysis=analysis, validation=ValidationResult(valid=False, status="INVALID", schema_valid=True,
            claims_valid=False, evidence_sufficient=True, unsupported_assertions=["fake fact ID rejected"]),
        verified_facts=facts)
    report = __import__("pathlib").Path(write_reports(result, tmp_path)["markdown"]).read_text(encoding="utf-8")
    assert report.index("Tóm tắt điều hành") < report.index("Phụ lục kiểm toán")
    assert "Không có phát biểu phân tích nào của Ollama được chấp nhận" in report
    assert "Nội dung Ollama chưa được xác minh" in report and "Rejected demand claim." in report
    assert "Sự thật đã xác minh" not in report
    assert facts[0].fact_id in report and "trends.google.com/trends/explore?geo=US&q=cozy" in report


def _corrected_b900_result():
    original, rows, facts, analysis, choices = _historical_d65_corrected_analysis()
    previous = choices["previous_four_week_average"].fact_id
    recent = choices["recent_four_week_average"].fact_id
    delta = choices["four_week_change"].fact_id
    direction = choices["four_week_direction"].fact_id
    peak = choices["observed_peak"].fact_id
    analysis.trend_reasons = [GroundedClaim(text=(
        f"The recent four-week average search interest increased from {choices['previous_four_week_average'].value} "
        f"to {choices['recent_four_week_average'].value} relative index points; weekly observations fluctuate, "
        "so this is a window comparison, not a sustained weekly trend."),
        fact_ids=[previous, recent, delta, direction])]
    analysis.evidence_based_inferences[1] = GroundedClaim(text=(
        "The synthetic peak was observed on 2025-12-28; one test observation cannot establish seasonality."),
        fact_ids=[peak])
    observation_ids = [fact.fact_id for fact in facts if fact.metric_name == "search_interest_index"]
    analysis.evidence_based_inferences.append(GroundedClaim(
        text="Weekly values fluctuate within this SYNTHETIC test fixture.", fact_ids=observation_ids))
    analysis.hypotheses.append(GroundedClaim(
        text="A fictional seasonal campaign could affect a test series; no real-world cause is measured.",
        fact_ids=[peak]))
    for idea, concept in zip(analysis.product_ideas,
        ["Cozy indoor scenes coloring book for adults", "Cozy nature and garden scenes coloring book"]):
        idea.what_to_sell = "SYNTHETIC test-only: " + concept
        idea.target_customer = "SYNTHETIC test-only customer hypothesis."
        idea.differentiation = "Creative rationale (hypothesis): SYNTHETIC test-only text."
        idea.uncertainties = ["Actual purchases and sales have not been verified; SYNTHETIC test fixture only.",
            "Customer preferences have not been measured directly.", "Product-market fit has not been tested."]
        idea.evidence_status = "hypothesis"
    evidence = [item.model_dump(mode="json") for item in original.evidence]
    corrected = original.model_copy(deep=True)
    corrected.analysis = analysis
    corrected.validation = validate_analysis(analysis, evidence, analysis_provenance="ollama", verified_facts=facts)

    # Deliberately invalid SYNTHETIC starting artifact for validator regression tests.
    original.analysis.trend_reasons[0].text = "The recent four-week average increased to 999 points."
    original.analysis.product_ideas[0].why_now = GroundedClaim(text="Uncited synthetic claim.", fact_ids=[])
    original.analysis.product_ideas[0].supporting_fact_ids = []
    original.analysis.hypotheses.append(GroundedClaim(
        text="A test peak occurred on 2025-12-29; this date is intentionally unsupported.", fact_ids=[peak]))
    return original, corrected


def test_historical_b900_output_reproduces_original_errors_and_stays_invalid():
    original, corrected_copy = _corrected_b900_result()
    original_evidence = [item.model_dump(mode="json") for item in original.evidence]
    historical_validation = validate_analysis(original.analysis, original_evidence,
        original.metadata.get("live_collection_status"), analysis_provenance="ollama",
        verified_facts=original.verified_facts)
    assert original.validation.status == "INVALID"
    assert len(original.evidence) == 53 and len(original.verified_facts) == 58
    assert not any("continuous/consistent growth" in item for item in corrected_copy.validation.unsupported_assertions)
    assert any("numbers without matching referenced fact values" in item for item in historical_validation.unsupported_assertions)
    assert any("why_now has no verified fact references" in item for item in historical_validation.unsupported_assertions)
    assert any("hypotheses[2] includes dates not present" in item for item in historical_validation.unsupported_assertions)
    assert historical_validation.status == "INVALID"


def test_corrected_fixture_from_historical_b900_passes_and_report_keeps_ideas(tmp_path):
    original, corrected = _corrected_b900_result()
    assert original.validation.status == "INVALID"  # historical file was never rewritten
    assert corrected.validation.claims_valid, corrected.validation.unsupported_assertions
    assert corrected.validation.status == "VALID"
    report = write_reports(corrected, tmp_path)
    text = __import__("pathlib").Path(report["markdown"]).read_text(encoding="utf-8")
    assert "Ý tưởng sản phẩm — giả thuyết" in text
    assert "Cozy indoor scenes coloring book" in text and "Cozy nature and garden scenes coloring book" in text
    assert "window comparison, not a sustained weekly trend" in text


@pytest.mark.parametrize("bad_claim, expected", [
    ("date", "dates not present in referenced facts"),
    ("fake_fact", "unknown fact IDs"),
    ("sales", "sales-volume claim"),
    ("price", "price claim"),
    ("demand", "product demand or sales"),
])
def test_corrected_historical_fixture_still_rejects_unsupported_claims(bad_claim, expected):
    _, result = _corrected_b900_result()
    analysis = result.analysis
    peak_id = next(f.fact_id for f in result.verified_facts if f.metric_name == "peak_search_interest_index")
    trend_id = next(f.fact_id for f in result.verified_facts if f.metric_name == "recent_four_week_average_relative_index")
    if bad_claim == "date":
        analysis.hypotheses[1] = GroundedClaim(text="The peak was on 2025-12-29; cause remains unknown.", fact_ids=[peak_id])
    elif bad_claim == "fake_fact":
        analysis.trend_reasons[0] = GroundedClaim(text="Recent average is 78.75.", fact_ids=["invented-average-fact"])
    elif bad_claim == "sales":
        analysis.evidence_based_inferences.append(GroundedClaim(text="The book sold 900 copies.", fact_ids=[trend_id]))
    elif bad_claim == "price":
        analysis.evidence_based_inferences.append(GroundedClaim(text="The book price is $19.99.", fact_ids=[trend_id]))
    elif bad_claim == "demand":
        analysis.product_ideas[0].why_now = GroundedClaim(text=(
            "Google Trends confirms consumer demand is increasing for this specific book."), fact_ids=[trend_id])
    evidence = [item.model_dump(mode="json") for item in result.evidence]
    validation = validate_analysis(analysis, evidence, analysis_provenance="ollama", verified_facts=result.verified_facts)
    assert validation.status == "INVALID"
    assert any(expected in item for item in validation.unsupported_assertions), validation.unsupported_assertions


def test_unsupported_keywords_are_research_candidates_in_report(tmp_path, sample_evidence):
    from market_research.models import ResearchResult
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    analysis.related_keywords = ["adult coloring book"]
    analysis.keyword_research_candidates = ["cozy theme art"]
    analysis.evidence_based_inferences = [GroundedClaim(text="There are 99 catalog results.",
        fact_ids=[f"fact_e_{__import__('hashlib').sha256('r1-e1'.encode()).hexdigest()[:12]}"])]
    result = ResearchResult(run_id="kw", niche="cozy coloring books for adults", category="Coloring books",
        target_region="US", report_language="Vietnamese", mode="replay", started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc), evidence=[Evidence.model_validate(sample_evidence[0])], analysis=analysis,
        validation=validate_analysis(analysis, sample_evidence))
    path = write_reports(result, tmp_path)["markdown"]
    report = __import__("pathlib").Path(path).read_text(encoding="utf-8")
    assert "Keyword ứng viên chưa đo" in report
    assert "adult coloring book" in report and "cozy theme art" in report
    assert "không phải tín hiệu trending/popular" in report
    assert any("numbers without matching referenced fact values" in item for item in result.validation.unsupported_assertions)


def test_fallback_after_model_failure_is_degraded_and_uses_measured_keyword(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    csv_path = tmp_path / "trends.csv"
    csv_path.write_text("Week,cozy coloring book: (United States)\n2025-01-05,32\n2025-01-12,45\n",
        encoding="utf-8")
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid model JSON")))
    result = run_research(ResearchConfig(), "cozy coloring books for adults", mode="csv_import",
        output_dir=tmp_path / "out", trends_csv_path=str(csv_path),
        source_url="https://trends.google.com/trends/explore?geo=US&q=cozy")
    assert result.metadata["analysis_provenance"] == "deterministic_fallback"
    assert result.metadata["analysis_execution_status"] == "failed"
    assert result.validation.status == "DEGRADED" and not result.validation.valid
    assert result.metadata["measured_keywords"] == ["cozy coloring book"]
    assert result.analysis.main_keywords == ["cozy coloring book"]
    assert "SUY GIẢM" in __import__("pathlib").Path(result.metadata["report_paths"]["markdown"]).read_text(encoding="utf-8")


def test_google_trends_csv_normalization_and_utf8(tmp_path):
    csv_path = tmp_path / "trends.csv"
    csv_path.write_text("Category: All categories\nWeek,cozy coloring books for adults: (United States)\n"
        "2025-01-05,32\n2025-01-12,45\n", encoding="utf-8-sig")
    collector = GoogleTrendsCsvCollector(csv_path,
        "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20books%20for%20adults")
    rows = collector.collect(run_id="csv-run", niche="cozy coloring books for adults",
        keyword="cozy coloring books for adults", target_region="United States")
    assert [row.metric_value for row in rows] == [32.0, 45.0]
    assert rows[0].metric_name == "search_interest_index"
    assert rows[0].keyword == "cozy coloring books for adults"
    assert "manual CSV" in rows[0].source_name
    assert rows[0].raw_observation["raw_value"] == "32"


@pytest.mark.parametrize("period_header", ["Tuần", "Tuần"])
def test_google_trends_vietnamese_csv_header_and_metadata(tmp_path, period_header):
    csv_path = tmp_path / "trends-vi.csv"
    csv_path.write_text(
        "Danh mục: Tất cả danh mục\n\n"
        f"{period_header},cozy coloring book: (Hoa Kỳ)\n"
        "2025-10-19,54\n2025-10-26,39\n2025-11-02,80\n"
        "2025-11-09,64\n2025-11-16,<1\n",
        encoding="utf-8-sig",
    )
    source_url = "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book"
    rows = GoogleTrendsCsvCollector(csv_path, source_url).collect(
        run_id="vi-csv-run", niche="cozy coloring book", keyword="cozy coloring book",
        target_region="United States",
    )

    assert [row.observation_period for row in rows] == [
        "2025-10-19", "2025-10-26", "2025-11-02", "2025-11-09", "2025-11-16",
    ]
    assert [row.metric_value for row in rows] == [54.0, 39.0, 80.0, 64.0, None]
    assert rows[0].keyword == "cozy coloring book"
    assert rows[0].source_url == source_url
    assert rows[0].raw_observation["period_column"] == period_header
    assert rows[0].raw_observation["series_column"] == "cozy coloring book: (Hoa Kỳ)"
    assert rows[0].raw_observation["geographic_metadata"] == "Hoa Kỳ"
    assert rows[-1].raw_observation["raw_value"] == "<1"
    assert rows[-1].collection_status == "partial"


def test_evidence_sufficiency_and_empty_evidence_status(sample_evidence):
    analysis = fallback_analysis("cozy coloring books for adults", [])
    empty = validate_analysis(analysis, [], collection_status="completed")
    assert empty.status == "INSUFFICIENT_EVIDENCE"
    assert not empty.valid and empty.schema_valid and not empty.evidence_sufficient

    catalog_analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    catalog_only = validate_analysis(catalog_analysis, sample_evidence, collection_status="completed")
    assert catalog_only.status == "INSUFFICIENT_EVIDENCE"

    trend_rows = [
        {**sample_evidence[0], "evidence_id": "t1", "metric_name": "search_interest_index",
         "metric_value": 32, "observation_period": "2025-01-05"},
        {**sample_evidence[0], "evidence_id": "t2", "metric_name": "search_interest_index",
         "metric_value": 45, "observation_period": "2025-01-12"},
    ]
    trend_analysis = fallback_analysis("cozy coloring books for adults", trend_rows)
    sufficient = validate_analysis(trend_analysis, trend_rows, collection_status="completed")
    assert sufficient.status == "VALID" and sufficient.valid and sufficient.evidence_sufficient


def test_blocked_collection_produces_blocked_validation_and_empty_snapshot(tmp_path, monkeypatch):
    from market_research.collectors import GoogleBooksCollectionError
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.plan",
        lambda self, niche, candidates, allowlist: {"keyword_candidates": candidates,
            "decisions": [{"tool_name": allowlist[0], "keyword": niche, "rationale": "test plan"}],
            "rationale": "mocked plan"})
    def blocked(*args, **kwargs):
        raise GoogleBooksCollectionError("quota", status_code=429, reason="quota_or_rate_limit")
    monkeypatch.setattr("market_research.orchestrator.GoogleBooksCollector.collect", blocked)
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not analyze without evidence")))
    result = run_research(ResearchConfig(), "cozy coloring books for adults", output_dir=tmp_path / "out")
    assert result.metadata["live_collection_status"] == "rate_limited"
    assert result.validation.status == "BLOCKED"
    assert not result.validation.valid and not result.validation.evidence_sufficient
    assert result.metadata["evidence_records_received_by_ollama"] == 0
    assert (tmp_path / result.metadata["snapshot_path"]).read_text(encoding="utf-8") == "[]"


def test_report_outputs(tmp_path, sample_evidence):
    analysis = fallback_analysis("cozy coloring books for adults", sample_evidence)
    from market_research.models import ResearchResult
    result = ResearchResult(run_id="r1", niche="cozy coloring books for adults", category="Coloring & Activity Books",
      target_region="US", report_language="Vietnamese", mode="replay", started_at=datetime.now(timezone.utc),
      finished_at=datetime.now(timezone.utc), evidence=[Evidence.model_validate(sample_evidence[0])], analysis=analysis,
      validation=validate_analysis(analysis, sample_evidence))
    paths = write_reports(result, tmp_path)
    assert all(__import__('pathlib').Path(p).exists() for p in paths.values())
    report_text = __import__('pathlib').Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "CHƯA ĐỦ BẰNG CHỨNG" in report_text


def test_replay_execution_without_network(tmp_path, sample_evidence, monkeypatch):
    from market_research.store import EvidenceStore
    replay = tmp_path / "fixture.json"
    EvidenceStore(replay).save([Evidence.model_validate(sample_evidence[0])])
    monkeypatch.setattr("market_research.orchestrator.OllamaClient.analyze", lambda *a, **k: fallback_analysis("cozy coloring books for adults", sample_evidence))
    result = run_research(ResearchConfig(), "cozy coloring books for adults", mode="replay", replay_path=str(replay), output_dir=tmp_path / "out")
    assert result.mode == "replay" and len(result.evidence) == 1
    assert result.metadata["tool_calls"] == 0
