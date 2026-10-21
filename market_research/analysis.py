from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import unicodedata

from .models import (AnalysisDraft, AnalysisGenerationDraft, ComputedTrendMetric, FactSelection,
    GroundedClaim, MarketAnalysis, ProductIdea, VerifiedFact)


FACT_ALIAS_METRICS = {
    "previous_four_week_average": "previous_four_week_average_relative_index",
    "recent_four_week_average": "recent_four_week_average_relative_index",
    "four_week_change": "four_week_average_change",
    "four_week_direction": "four_week_average_direction",
    "observed_peak": "peak_search_interest_index",
    "weekly_observation": "search_interest_index",
}


def fact_selection_options(facts: list[VerifiedFact]) -> list[dict]:
    """Expose semantic choices without exposing canonical IDs to the language model."""
    aliases_by_metric = {metric: alias for alias, metric in FACT_ALIAS_METRICS.items()}
    options = []
    for fact in facts:
        alias = aliases_by_metric.get(fact.metric_name)
        if alias is None or not fact.observation_period:
            continue
        options.append({"alias": alias, "keyword": fact.keyword, "observation_period": fact.observation_period,
            "value": fact.value, "unit": fact.unit, "description": fact.description})
    return options


def resolve_fact_selections(selections: list[FactSelection], facts: list[VerifiedFact]) -> list[str]:
    """Resolve alias + keyword + exact period to canonical fact IDs; reject ambiguous or fake selections."""
    resolved = []
    for selection in selections:
        expected_metric = FACT_ALIAS_METRICS[selection.alias]
        matches = [fact for fact in facts if fact.metric_name == expected_metric
            and fact.keyword.casefold() == selection.keyword.casefold()
            and fact.observation_period == selection.observation_period]
        if len(matches) != 1:
            raise ValueError(f"Unresolvable fact selection: {selection.alias} for {selection.keyword!r} at {selection.observation_period!r}")
        resolved.append(matches[0].fact_id)
    return list(dict.fromkeys(resolved))


def _coverage_confidence(evidence: list[dict]) -> str:
    source_count = len({str(row.get("source_name", "")).casefold() for row in evidence if row.get("source_name")})
    keyword_count = len({str(row.get("keyword", "")).casefold() for row in evidence if row.get("keyword")})
    metrics = {str(row.get("metric_name", "")).casefold() for row in evidence}
    direct = bool(metrics & {"sales_count", "transaction_count", "sales_rank", "review_count",
        "review_rating", "survey_response", "discussion_count", "purchase_count"})
    if source_count >= 3 and keyword_count >= 5 and direct:
        return "high"
    if source_count >= 2 and keyword_count >= 3 and direct:
        return "medium"
    return "low"


def _python_market_context(evidence: list[dict], facts: list[VerifiedFact], report_language: str) -> tuple[str, None, list[str], list[str]]:
    metrics = {str(row.get("metric_name", "")).casefold() for row in evidence}
    price_available = bool(metrics & {"price", "list_price", "sale_price", "indicative_price"})
    competition_available = bool(metrics & {"listing_count", "competitor_count", "competition_count", "competitor_listing_count"})
    feedback_available = bool(metrics & {"review_count", "review_rating", "survey_response", "discussion_count", "customer_feedback"})
    vi = report_language.casefold().startswith("vi")
    if competition_available:
        competition = "Có chỉ số liệt kê/đối thủ trong bằng chứng; mức cạnh tranh cần diễn giải thận trọng." if vi else "Competition/listing metrics are present; intensity still requires cautious interpretation."
    else:
        competition = "Tình trạng cạnh tranh chưa được xác minh; bộ dữ liệu không có độ phủ đối thủ trực tiếp." if vi else "Competition status is unverified; this dataset has no direct competitor coverage."
    customer_notes = ["doanh số và giao dịch thực tế"]
    if not price_available:
        customer_notes.append("giá niêm yết/giá bán từ nguồn trực tiếp")
    if not competition_available:
        customer_notes.append("độ phủ sản phẩm và đối thủ trên thị trường")
    if not feedback_available:
        customer_notes.extend(["đánh giá/khảo sát và phản hồi khách hàng", "nhu cầu và sở thích khách hàng đã đo trực tiếp"])
    customer_notes.append("mức độ phù hợp sản phẩm-thị trường")
    return competition, None, [], customer_notes


def materialize_analysis(draft: AnalysisGenerationDraft, evidence: list[dict], facts: list[VerifiedFact],
                         report_language: str = "Vietnamese") -> AnalysisDraft:
    """Turn alias-based generation into the stable report schema with Python-owned market context."""
    def claim(item):
        return GroundedClaim(text=item.text, fact_ids=resolve_fact_selections(item.fact_selections, facts))

    trend_facts = [fact for fact in facts if fact.metric_name == "search_interest_index"]
    measured = {str(row.get("keyword", "")).casefold() for row in evidence if row.get("keyword")}
    keyword = next((item for item in draft.main_keywords if item.casefold() in measured),
        next((str(row.get("keyword")) for row in evidence if row.get("keyword")), ""))
    keyword_facts = [fact for fact in facts if fact.keyword.casefold() == keyword.casefold()]
    prev = next((fact for fact in keyword_facts if fact.metric_name == FACT_ALIAS_METRICS["previous_four_week_average"]), None)
    recent = next((fact for fact in keyword_facts if fact.metric_name == FACT_ALIAS_METRICS["recent_four_week_average"]), None)
    change = next((fact for fact in keyword_facts if fact.metric_name == FACT_ALIAS_METRICS["four_week_change"]), None)
    point = next((fact for fact in keyword_facts if fact.metric_name == "search_interest_index"), None)
    vi = report_language.casefold().startswith("vi")
    if prev and recent and change:
        why_now_text = (f"Google Trends ghi nhận chỉ số quan tâm tương đối trung bình bốn tuần gần nhất là {recent.value}, "
            f"so với {prev.value} ở bốn tuần trước (thay đổi {change.value} điểm). Chỉ số tìm kiếm không đo lường giao dịch. "
            "Đây là giả thuyết sản phẩm; nhu cầu cụ thể chưa được xác minh và khả năng mua chưa được xác minh." if vi else
            f"Google Trends reports a recent four-week average relative interest index of {recent.value}, compared with "
            f"{prev.value} in the previous four weeks (a change of {change.value} points). Search interest does not measure transactions. "
            "This is a product hypothesis; specific product demand is unverified and purchase potential is unverified.")
        timing_ids = [prev.fact_id, recent.fact_id, change.fact_id]
    elif point:
        why_now_text = (f"Google Trends có quan sát chỉ số tương đối cho từ khóa '{keyword}'. Đây là giả thuyết sản phẩm; "
            "chỉ số tìm kiếm không đo lường giao dịch; nhu cầu cụ thể chưa được xác minh và khả năng mua chưa được xác minh." if vi else
            f"Google Trends has a relative-index observation for the keyword '{keyword}'. This is a product hypothesis; "
            "search interest does not measure transactions; specific product demand is unverified and purchase potential is unverified.")
        timing_ids = [point.fact_id]
    else:
        why_now_text = ("Chưa có chỉ số tìm kiếm trực tiếp phù hợp để biện minh thời điểm ra mắt. Đây là giả thuyết sản phẩm; "
            "nhu cầu cụ thể chưa được xác minh và khả năng mua chưa được xác minh." if vi else
            "No directly relevant search-interest measure supports launch timing. This is a product hypothesis; "
            "specific product demand is unverified and purchase potential is unverified.")
        timing_ids = []

    concepts = []
    for idea in draft.product_ideas:
        customer = idea.intended_customer_hypothesis.strip()
        label = "Giả thuyết khách hàng mục tiêu: " if vi else "Customer hypothesis: "
        customer_text = customer if customer.casefold().startswith(("hypothesis", "giả thuyết")) else label + customer
        uncertainties = list(idea.uncertainties)
        required_uncertainties = (["Chưa xác minh giao dịch mua hoặc doanh số thực tế.",
            "Sở thích khách hàng chưa được đo trực tiếp.", "Chưa kiểm tra mức độ phù hợp sản phẩm-thị trường."] if vi else
            ["Actual purchases and sales have not been verified.", "Customer preferences have not been measured directly.",
             "Product-market fit has not been tested."])
        joined = " ".join(uncertainties).casefold()
        for item in required_uncertainties:
            terms = ("purchase", "sales", "mua", "doanh số") if "purchase" in item.casefold() else (
                "preference", "sở thích", "customer feedback") if "preference" in item.casefold() or "sở thích" in item.casefold() else (
                "product-market fit", "pmf", "phù hợp sản phẩm-thị trường")
            if not any(term in joined for term in terms):
                uncertainties.append(item)
                joined += " " + item.casefold()
        rationale_label = "Lý do sáng tạo (giả thuyết): " if vi else "Creative rationale (hypothesis): "
        concepts.append(ProductIdea(what_to_sell=idea.what_to_sell, target_customer=customer_text,
            why_now=GroundedClaim(text=why_now_text, fact_ids=timing_ids.copy()),
            supporting_fact_ids=timing_ids.copy(), uncertainties=uncertainties,
            evidence_status="hypothesis", differentiation=rationale_label + idea.differentiation_rationale.strip()))

    competition, price, customers, missing = _python_market_context(evidence, facts, report_language)
    target_hypotheses = list(dict.fromkeys(concept.target_customer for concept in concepts))
    if not trend_facts:
        missing.insert(0, "Google Trends search interest")
    return AnalysisDraft(main_keywords=draft.main_keywords, related_keywords=draft.related_keywords,
        keyword_research_candidates=draft.keyword_research_candidates,
        evidence_based_inferences=[claim(item) for item in draft.evidence_based_inferences],
        hypotheses=[claim(item) for item in draft.hypotheses], trend_direction=draft.trend_direction,
        trend_reasons=[claim(item) for item in draft.trend_reasons], target_customers=target_hypotheses,
        customer_needs_or_complaints=customers, competition_assessment=competition,
        indicative_price_range=price, product_ideas=concepts, confidence=_coverage_confidence(evidence),
        missing_or_unavailable_data=missing)


def compute_trend_metrics(evidence: list[dict], *, as_of: date | None = None) -> list[ComputedTrendMetric]:
    """Compute auditable weekly relative-index summaries from imported evidence."""
    as_of = as_of or datetime.now(timezone.utc).date()
    by_keyword: dict[str, list[tuple[date, float, str, str]]] = {}
    for row in evidence:
        if row.get("metric_name") != "search_interest_index" or not isinstance(row.get("metric_value"), (int, float)):
            continue
        period_column = row.get("raw_observation", {}).get("period_column")
        if period_column and unicodedata.normalize("NFC", str(period_column)).strip().casefold() not in {"week", "tuần"}:
            continue
        try:
            period = date.fromisoformat(str(row.get("observation_period", ""))[:10])
        except ValueError:
            continue
        # Google weekly periods are week starts. Exclude future and not-yet-completed weeks.
        if period + timedelta(days=6) >= as_of:
            continue
        keyword = str(row.get("keyword") or "")
        by_keyword.setdefault(keyword, []).append((period, float(row["metric_value"]),
            str(row.get("evidence_id", "")), str(row.get("source_url", ""))))

    summaries: list[ComputedTrendMetric] = []
    for keyword, points in by_keyword.items():
        metric_material = keyword + "|" + "|".join(
            f"{period.isoformat()}:{value}:{evidence_id}" for period, value, evidence_id, _ in points)
        metric_id = f"trend_4w_{sha256(metric_material.encode('utf-8')).hexdigest()[:12]}"
        dates = [point[0] for point in points]
        if dates != sorted(dates):
            summaries.append({"metric_id": metric_id, "keyword": keyword, "status": "insufficient_data", "reason": "weekly dates are not in ascending order"})
            continue
        if len(dates) != len(set(dates)):
            summaries.append({"metric_id": metric_id, "keyword": keyword, "status": "insufficient_data", "reason": "duplicate weekly dates"})
            continue
        recent, previous = points[-4:], points[-8:-4]
        comparison_points = previous + recent
        if len(comparison_points) == 8 and any((comparison_points[i + 1][0] - comparison_points[i][0]).days != 7 for i in range(7)):
            summaries.append({"metric_id": metric_id, "keyword": keyword, "status": "insufficient_data", "reason": "missing or non-weekly dates prevent comparable windows"})
            continue
        recent_avg = sum(point[1] for point in recent) / len(recent) if len(recent) == 4 else None
        previous_avg = sum(point[1] for point in previous) / len(previous) if len(previous) == 4 else None
        delta = round(recent_avg - previous_avg, 2) if recent_avg is not None and previous_avg is not None else None
        direction = "insufficient_data" if delta is None else "increasing" if delta > 0 else "decreasing" if delta < 0 else "stable"
        peak = max(points, key=lambda point: point[1]) if points else None
        summaries.append({
            "metric_id": metric_id, "keyword": keyword, "status": "computed" if delta is not None else "insufficient_data",
            "completed_observation_count": len(points), "latest_completed_week": dates[-1].isoformat() if points else None,
            "recent_window": {"start_period": recent[0][0].isoformat(), "end_period": recent[-1][0].isoformat(),
                "weeks": len(recent), "average_relative_index": round(recent_avg, 2),
                "evidence_ids": [point[2] for point in recent]} if recent_avg is not None else None,
            "previous_window": {"start_period": previous[0][0].isoformat(), "end_period": previous[-1][0].isoformat(),
                "weeks": len(previous), "average_relative_index": round(previous_avg, 2),
                "evidence_ids": [point[2] for point in previous]} if previous_avg is not None else None,
            "change_in_index_points": delta, "direction": direction,
            "peak": {"week": peak[0].isoformat(), "relative_index": peak[1], "evidence_id": peak[2]} if peak else None,
            "seasonality_note": "One annual cycle cannot establish seasonality; year-over-year comparison is unavailable.",
            "evidence_ids": [point[2] for point in points],
            "comparison_evidence_ids": [point[2] for point in comparison_points],
            "recent_evidence": [{"week": point[0].isoformat(), "relative_index": point[1], "evidence_id": point[2], "source_url": point[3]} for point in points[-8:]],
        })
    return summaries


def build_verified_facts(evidence: list[dict], metrics: list[ComputedTrendMetric] | None = None) -> list[VerifiedFact]:
    """Create the deterministic fact layer; every fact maps back to saved evidence or metrics."""
    evidence = [row if isinstance(row, dict) else row.model_dump(mode="json") for row in evidence]
    metrics = metrics if metrics is not None else compute_trend_metrics(evidence)
    facts: list[VerifiedFact] = []
    fact_id_by_evidence: dict[str, str] = {}
    for row in evidence:
        evidence_id = str(row["evidence_id"])
        fact_id = f"fact_e_{sha256(evidence_id.encode('utf-8')).hexdigest()[:12]}"
        fact_id_by_evidence[evidence_id] = fact_id
        raw_value = row.get("metric_value")
        if raw_value is None:
            raw_value = row.get("raw_observation", {}).get("raw_value")
        unit = row.get("metric_unit")
        keyword = str(row.get("keyword") or "")
        period = str(row.get("observation_period") or "")
        value_text = "unavailable" if raw_value is None else str(raw_value)
        description = (f"{row.get('source_name', 'Source')} reports {row.get('metric_name', 'observation')}="
            f"{value_text}{f' {unit}' if unit else ''} for keyword '{keyword}' during {period}.")
        facts.append(VerifiedFact(fact_id=fact_id, keyword=keyword, metric_name=str(row.get("metric_name", "")),
            value=raw_value, unit=unit, observation_period=period, evidence_ids=[evidence_id],
            description=description))

    for metric in metrics:
        if metric.get("status") != "computed":
            continue
        metric_id = metric["metric_id"]
        keyword = metric["keyword"]
        for window_key, suffix, label in (("recent_window", "recent_avg", "recent"),
                ("previous_window", "previous_avg", "previous")):
            window = metric.get(window_key)
            if not window:
                continue
            fact_id = f"fact_{metric_id}_{suffix}"
            facts.append(VerifiedFact(fact_id=fact_id, keyword=keyword,
                metric_name=f"{label}_four_week_average_relative_index",
                value=window["average_relative_index"], unit="Google Trends relative index points; 4 completed weeks",
                observation_period=f"{window['start_period']}..{window['end_period']}",
                evidence_ids=list(window["evidence_ids"]), metric_ids=[metric_id], coverage_count=window["weeks"],
                description=(f"Python computed {label} four-week average relative index for '{keyword}': "
                    f"{window['average_relative_index']} across {window['start_period']} through {window['end_period']} (4 completed weeks).")))
        change = metric.get("change_in_index_points")
        if change is not None:
            recent, previous = metric["recent_window"], metric["previous_window"]
            fact_id = f"fact_{metric_id}_four_week_change"
            facts.append(VerifiedFact(fact_id=fact_id, keyword=keyword, metric_name="four_week_average_change",
                value=change, unit="Google Trends relative index points; recent average minus previous average",
                observation_period=f"{previous['start_period']}..{recent['end_period']}",
                evidence_ids=list(metric["comparison_evidence_ids"]), metric_ids=[metric_id], coverage_count=8,
                description=(f"Python computed the recent versus previous four-week average change for '{keyword}': "
                    f"{change} relative index points ({previous['average_relative_index']} to {recent['average_relative_index']}).")))
            fact_id = f"fact_{metric_id}_direction"
            facts.append(VerifiedFact(fact_id=fact_id, keyword=keyword, metric_name="four_week_average_direction",
                value=metric["direction"], unit="direction of recent versus previous four-week averages",
                observation_period=f"{previous['start_period']}..{recent['end_period']}",
                evidence_ids=list(metric["comparison_evidence_ids"]), metric_ids=[metric_id], coverage_count=8,
                description=(f"Python classified the four-week average comparison for '{keyword}' as {metric['direction']}; "
                    "this does not mean every weekly observation moved in that direction.")))
        peak = metric.get("peak")
        if peak:
            facts.append(VerifiedFact(fact_id=f"fact_{metric_id}_peak", keyword=keyword,
                metric_name="peak_search_interest_index", value=peak["relative_index"],
                unit="Google Trends relative index (0-100; not search volume)", observation_period=peak["week"],
                evidence_ids=[peak["evidence_id"]], metric_ids=[metric_id],
                description=f"Highest observed relative index for '{keyword}' was {peak['relative_index']} in week {peak['week']}."))
    return facts


def select_facts_for_analysis(facts: list[VerifiedFact], metrics: list[ComputedTrendMetric]) -> list[VerifiedFact]:
    selected_evidence_ids = {eid for metric in metrics for eid in metric.get("comparison_evidence_ids", [])}
    selected_evidence_ids.update(metric["peak"]["evidence_id"] for metric in metrics if metric.get("peak"))
    selected_metric_ids = {metric["metric_id"] for metric in metrics}
    return [fact for fact in facts if fact.metric_ids and set(fact.metric_ids) & selected_metric_ids
        or set(fact.evidence_ids) & selected_evidence_ids
        or fact.metric_name != "search_interest_index"
        or not fact.evidence_ids and not fact.metric_ids]


def normalize_analysis_keywords(analysis: MarketAnalysis, evidence: list[dict]) -> None:
    """Keep unmeasured related terms in the candidate field, never as measured signals."""
    measured = {str(row.get("keyword", "")).casefold() for row in evidence if row.get("keyword")}
    existing_candidates = list(analysis.keyword_research_candidates)
    for keyword in analysis.related_keywords:
        if keyword.casefold() not in measured and keyword.casefold() not in {x.casefold() for x in existing_candidates}:
            existing_candidates.append(keyword)
    analysis.related_keywords = [keyword for keyword in analysis.related_keywords if keyword.casefold() in measured]
    analysis.keyword_research_candidates = [keyword for keyword in existing_candidates
        if keyword.casefold() not in measured]


def attach_python_direction_reference(analysis: MarketAnalysis, facts: list[VerifiedFact]) -> None:
    """Attach only the computed direction fact matching the model's direction and main keyword."""
    keyword = analysis.main_keywords[0].casefold() if analysis.main_keywords else ""
    analysis.trend_direction_fact_ids = [fact.fact_id for fact in facts
        if fact.metric_name == "four_week_average_direction" and str(fact.value) == analysis.trend_direction
        and fact.keyword.casefold() == keyword]


def fallback_analysis(niche: str, evidence: list[dict], verified_facts: list[VerifiedFact] | None = None) -> MarketAnalysis:
    has_trends = any(e.get("metric_name") == "search_interest_index" for e in evidence)
    has_catalog = any(e.get("source_name") == "Google Books API" for e in evidence)
    verified_facts = verified_facts if verified_facts is not None else build_verified_facts(evidence)
    fact_refs = [fact.fact_id for fact in verified_facts[:2]]
    first_metric_fact = next((fact.fact_id for fact in verified_facts if fact.metric_name == "four_week_average_direction"), None)
    fallback_ref = [first_metric_fact] if first_metric_fact else fact_refs[:1]
    measured_keywords = list(dict.fromkeys(e.get("keyword", "") for e in evidence if e.get("keyword")))
    display_keyword = measured_keywords[0] if measured_keywords else niche
    if has_trends:
        factual = [fact.description for fact in verified_facts]
        trend_reason = [GroundedClaim(text="Chưa diễn giải xu hướng bằng Ollama; xem các metric đã tính ở phần sự thật đã xác minh.", fact_ids=fallback_ref)] if fallback_ref else []
    elif has_catalog:
        factual = [fact.description for fact in verified_facts]
        trend_reason = [GroundedClaim(text="Metadata thư mục không chứng minh rằng có nhu cầu mua hàng.", fact_ids=fallback_ref)] if fallback_ref else []
    else:
        # Empty evidence is represented by an empty verified-fact list. The report
        # carries the blocked/insufficient status instead of treating this as a fact.
        factual = []
        trend_reason = []
    missing = ["doanh số", "đánh giá mua hàng đã xác minh", "phàn nàn khách hàng", "giá cạnh tranh đầy đủ"]
    if not has_trends:
        missing.insert(0, "Mức quan tâm tìm kiếm Google Trends")
    missing.append("quan sát xu hướng theo chuỗi thời gian đủ dài")
    target_hypothesis = (f"Giả thuyết khách hàng mục tiêu: cần xác thực qua nghiên cứu khách hàng" if display_keyword else
        "Giả thuyết khách hàng mục tiêu: cần xác thực qua nghiên cứu khách hàng")
    return MarketAnalysis(main_keywords=[display_keyword], related_keywords=[], factual_observations=factual,
        evidence_based_inferences=[], hypotheses=["Cần bằng chứng trực tiếp từ khách hàng để xác định phân khúc độc giả và khác biệt sản phẩm."],
        trend_direction="insufficient evidence", trend_reasons=trend_reason,
        target_customers=[], customer_needs_or_complaints=[],
        competition_assessment="Tình trạng cạnh tranh chưa được xác minh; bản ghi thư mục không đại diện cho toàn bộ đối thủ.",
        indicative_price_range=None,
        product_ideas=[ProductIdea(what_to_sell=f"Một concept {display_keyword} theo chủ đề với bố cục trang đa dạng", target_customer=target_hypothesis, why_now=GroundedClaim(text="Đây là giả thuyết sản phẩm; nhu cầu cụ thể chưa được xác minh và khả năng mua chưa được xác minh.", fact_ids=fallback_ref), supporting_fact_ids=fallback_ref.copy(), uncertainties=["Actual purchases and sales have not been verified.", "Customer preferences have not been collected directly.", "Product-market fit has not been tested."], differentiation="Creative rationale (hypothesis): vary page composition and scene density."),
          ProductIdea(what_to_sell=f"Phiên bản quà tặng dành cho nhóm độc giả {display_keyword}", target_customer=target_hypothesis, why_now=GroundedClaim(text="Đây là giả thuyết sản phẩm; nhu cầu cụ thể chưa được xác minh và khả năng mua chưa được xác minh.", fact_ids=fallback_ref), supporting_fact_ids=fallback_ref.copy(), uncertainties=["Actual purchases and sales have not been verified.", "Customer preferences have not been collected directly.", "Product-market fit has not been tested."], differentiation="Creative rationale (hypothesis): organize pages as a giftable themed set.")],
        confidence="low", missing_or_unavailable_data=missing)
