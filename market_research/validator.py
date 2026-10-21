from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .analysis import build_verified_facts, compute_trend_metrics
from .models import GroundedClaim, MarketAnalysis, ValidationResult, VerifiedFact


DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?%?(?![A-Za-z0-9_])")
WINDOW_PATTERN = re.compile(r"\b\d+\s*(?:[- ]\s*weeks?|tuần)\b", re.I)
WINDOW_STREAK_PATTERN = re.compile(r"\b(?:\d+|four)\s+(?:consecutive|successive)\s+weeks?\b|\b\d+\s+tuần\s+liên\s+tiếp\b", re.I)
PRICE_CLAIM_PATTERN = re.compile(
    r"(?:[$€£]|\b(?:usd|eur|gbp)\b|\bprice\b|\bcost\b|\bgiá\b|"
    r"\bmức giá\b|\bgiá bán\b|\bgiá niêm yết\b|\bgiá sản phẩm\b|\bgiá tham khảo\b|"
    r"\bgia\s+(?:ban|niem yet|san pham|tham khao)\b)", re.I)
MARKET_DEMAND_PATTERN = re.compile(r"\b(?:market demand|consumer demand|demand|purchases?|buyers?|sold|sales|"
    r"nhu cầu(?:\s+(?:thị trường|sản phẩm))?|doanh số|mua hàng|bán được)\b", re.I)
NEGATED_DEMAND_PATTERN = re.compile(r"\b(?:not|does not|doesn't|cannot|can't|no evidence|không|chưa|không thể)\b", re.I)
DEMAND_NEGATION_BEFORE = re.compile(r"(?:not|does not|doesn't|cannot|can't|no evidence(?: of)?|without|không|chưa|không thể|does not establish|cannot establish)(?:\W+\w+){0,8}\W*$", re.I)
DEMAND_NEGATION_AFTER = re.compile(r"^\W*(?:(?:specific|product-specific|cụ thể|riêng|sản phẩm|potential|khả năng)\W+){0,3}(?:(?:is|are|was|were|has|have|remains?)\W+)?(?:unverified|not verified|not proven|not established|not measured|not been verified|not been measured|chưa(?:\W+được)?\W+(?:xác minh|xác thực|kiểm chứng|đo lường)|không\W+(?:chứng minh|đo lường))\b", re.I)
COORDINATED_DEMAND_NEGATION_AFTER = re.compile(r"^\W+and\W+(?:(?:actual|verified)\W+)?(?:purchases?|sales|buyers?|mua hàng|doanh số)\W+(?:(?:is|are|has|have)\W+)?(?:not\W+been\W+verified|unverified|not\W+verified|chưa(?:\W+được)?\W+(?:xác minh|xác thực|kiểm chứng))\b", re.I)
MONOTONIC_GROWTH_PATTERN = re.compile(
    r"\b(?:consistent(?:ly)?\s+(?:increase|increasing|growth|growing|rise|rising)|"
    r"monotonic(?:ally)?|continuous(?:ly)?\s+(?:increase|growth|grow|rise)|"
    r"steady\s+(?:increase|growth|rise)|every\s+week\s+(?:increased|rose))\b|"
    r"(?:tăng\s+(?:liên tục|đều đặn|nhất quán)|liên tục\s+tăng|tăng đều)|"
    r"\b(?:increase|increased|increasing|rise|rose|growth|growing).{0,40}\b(?:\d+|four)\s+(?:consecutive|successive)\s+weeks?\b"
)
FLUCTUATION_PATTERN = re.compile(r"\b(?:fluctuat\w*|volatil\w*)\b|dao động", re.I)
DECLINE_PATTERN = re.compile(r"\b(?:declin\w*|drop(?:ped|ping)?|fell|falling|decreas\w*)\b|\b(?:giảm|sụt|đi xuống)\b", re.I)
GROWTH_NEGATION_PREFIX = re.compile(
    r"(?:\bnot\b(?:\s+\w+){0,3}\s*|\bno\b(?:\s+\w+){0,3}\s*|\bwithout\b(?:\s+\w+){0,3}\s*|"
    r"\brather\s+than\b.{0,40}$|\bneither\b.{0,40}\bnor\b.{0,15}$|"
    r"\b(?:không|chưa)\b(?:\s+\w+){0,3}\s*)$", re.I)
GROWTH_NEGATION_SUFFIX = re.compile(r"^\s*(?:(?:is|was|were|has been|have been)\s+)?(?:not|never|không|chưa)\b", re.I)


def _decimal(value) -> Decimal | None:
    try:
        token = str(value).strip().replace("%", "")
        if "," in token and "." in token:
            decimal_mark = "," if token.rfind(",") > token.rfind(".") else "."
            grouping_mark = "." if decimal_mark == "," else ","
            token = token.replace(grouping_mark, "").replace(decimal_mark, ".")
        elif "," in token:
            groups = token.split(",")
            if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
                token = "".join(groups)  # conventional thousands grouping: 1,234
            else:
                token = token.replace(",", ".")  # locale decimal comma: 57,5 / 78,75
        elif token.count(".") > 1:
            groups = token.split(".")
            if all(len(group) == 3 for group in groups[1:]):
                token = "".join(groups)
            else:
                token = "".join(groups[:-1]) + "." + groups[-1]
        return Decimal(token).normalize()
    except (InvalidOperation, ValueError):
        return None


def _legacy_claim_refs(claim: str, evidence_to_fact: dict[str, str], metric_to_facts: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Read old string claims for backward compatibility; new drafts use GroundedClaim."""
    fact_ids: list[str] = []
    def replace(match):
        for token in (part.strip() for part in match.group(1).split(",")):
            if token.startswith("metric:"):
                fact_ids.extend(metric_to_facts.get(token.removeprefix("metric:"), []))
            elif token in evidence_to_fact:
                fact_ids.append(evidence_to_fact[token])
            else:
                fact_ids.append(token)
        return ""
    text = re.sub(r"\[([^\]]+)\]", replace, claim)
    return text.strip(), list(dict.fromkeys(fact_ids))


def _claim_text_and_refs(claim, evidence_to_fact, metric_to_facts):
    if isinstance(claim, GroundedClaim):
        return claim.text, list(claim.fact_ids)
    if isinstance(claim, str):
        return _legacy_claim_refs(claim, evidence_to_fact, metric_to_facts)
    return str(claim), []


def _has_monotonic_growth(text: str) -> bool:
    for match in MONOTONIC_GROWTH_PATTERN.finditer(text):
        left = text[max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start()),
            text.rfind("!", 0, match.start()), text.rfind("?", 0, match.start())) + 1:match.start()]
        right = text[match.end():match.end() + 30]
        if GROWTH_NEGATION_PREFIX.search(left[-80:]) or GROWTH_NEGATION_SUFFIX.search(right):
            continue
        return True
    return False


def _has_unqualified_demand_claim(text: str) -> bool:
    for match in MARKET_DEMAND_PATTERN.finditer(text):
        before = text[max(0, match.start() - 64):match.start()]
        after = text[match.end():match.end() + 48]
        if (DEMAND_NEGATION_BEFORE.search(before) or DEMAND_NEGATION_AFTER.search(after)
                or COORDINATED_DEMAND_NEGATION_AFTER.search(after)):
            continue
        return True
    return False


def _cited_trend_series(cited_facts: list[VerifiedFact], evidence_by_id: dict[str, dict],
                        metrics_by_id: dict[str, dict]) -> list[list[tuple[date, float]]]:
    evidence_ids = {eid for fact in cited_facts for eid in fact.evidence_ids}
    for fact in cited_facts:
        for metric_id in fact.metric_ids:
            metric = metrics_by_id.get(metric_id, {})
            evidence_ids.update(metric.get("comparison_evidence_ids", []))
            evidence_ids.update(metric.get("evidence_ids", []))
    rows = [evidence_by_id[eid] for eid in evidence_ids if eid in evidence_by_id
        and evidence_by_id[eid].get("metric_name") == "search_interest_index"
        and isinstance(evidence_by_id[eid].get("metric_value"), (int, float))]
    grouped: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for row in rows:
        try:
            point_date = date.fromisoformat(str(row.get("observation_period", ""))[:10])
            grouped.setdefault((str(row.get("source_name", "")), str(row.get("keyword", ""))), []).append(
                (point_date, float(row["metric_value"])))
        except (ValueError, TypeError, KeyError):
            continue
    return [sorted(points) for points in grouped.values()]


def _trajectory_supported(text: str, cited_facts: list[VerifiedFact], evidence_by_id: dict[str, dict],
                          metrics_by_id: dict[str, dict]) -> tuple[bool, bool]:
    series = _cited_trend_series(cited_facts, evidence_by_id, metrics_by_id)
    fluctuations_claimed = bool(FLUCTUATION_PATTERN.search(text))
    declines_claimed = bool(DECLINE_PATTERN.search(text))
    fluctuations_supported = any(len(points) >= 3 and any(b[1] > a[1] for a, b in zip(points, points[1:]))
        and any(b[1] < a[1] for a, b in zip(points, points[1:])) for points in series)
    declines_supported = any(len(points) >= 2 and any(b[1] < a[1] for a, b in zip(points, points[1:]))
        for points in series)
    return (not fluctuations_claimed or fluctuations_supported, not declines_claimed or declines_supported)


def _monotonic_supported(text: str, cited_facts: list[VerifiedFact], facts_by_id: dict[str, VerifiedFact],
                         evidence_by_id: dict[str, dict], metrics_by_id: dict[str, dict]) -> bool:
    if not _has_monotonic_growth(text):
        return True
    series = _cited_trend_series(cited_facts, evidence_by_id, metrics_by_id)
    if WINDOW_STREAK_PATTERN.search(text) and any(len(points) < 4 for points in series):
        return False
    return bool(series) and all(len(points) >= 2 and all(b[1] > a[1] for a, b in zip(points, points[1:]))
        for points in series)


def validate_analysis(analysis: MarketAnalysis, evidence: list[dict], collection_status: str | None = None,
                     analysis_provenance: str = "ollama", verified_facts: list[VerifiedFact] | None = None) -> ValidationResult:
    evidence_by_id = {str(row["evidence_id"]): row for row in evidence}
    computed_metrics = compute_trend_metrics(evidence)
    facts = verified_facts if verified_facts is not None else build_verified_facts(evidence, computed_metrics)
    facts_by_id = {fact.fact_id: fact for fact in facts}
    evidence_to_fact = {eid: fact.fact_id for fact in facts for eid in fact.evidence_ids
        if fact.fact_id.startswith("fact_e_")}
    metric_to_facts: dict[str, list[str]] = {}
    for fact in facts:
        for metric_id in fact.metric_ids:
            metric_to_facts.setdefault(metric_id, []).append(fact.fact_id)
    metrics_by_id = {metric["metric_id"]: metric for metric in computed_metrics if metric.get("metric_id")}
    unsupported: list[str] = []
    warnings: list[str] = []

    expected_facts = [fact.description for fact in facts]
    if analysis.factual_observations != expected_facts:
        unsupported.append("Factual observations differ from Python-generated verified facts")
    for fact in facts:
        unknown_evidence = set(fact.evidence_ids) - set(evidence_by_id)
        if unknown_evidence:
            unsupported.append(f"Verified fact {fact.fact_id} references unknown evidence IDs: {', '.join(sorted(unknown_evidence))}")
        unknown_metrics = set(fact.metric_ids) - set(metrics_by_id)
        if unknown_metrics:
            unsupported.append(f"Verified fact {fact.fact_id} references unknown metric IDs: {', '.join(sorted(unknown_metrics))}")

    def validate_claim(label: str, claim, *, require_fact: bool = True):
        text, fact_ids = _claim_text_and_refs(claim, evidence_to_fact, metric_to_facts)
        unknown = set(fact_ids) - set(facts_by_id)
        if unknown:
            unsupported.append(f"{label} references unknown fact IDs: {', '.join(sorted(unknown))}")
        cited_facts = [facts_by_id[fact_id] for fact_id in dict.fromkeys(fact_ids) if fact_id in facts_by_id]
        if evidence and require_fact and not cited_facts:
            unsupported.append(f"{label} has no verified fact references")
        if WINDOW_STREAK_PATTERN.search(text) and any(fact.metric_name in {
                "recent_four_week_average_relative_index", "previous_four_week_average_relative_index",
                "four_week_average_change"} for fact in cited_facts):
            unsupported.append(f"{label} describes comparison-window averages as weekly movement across consecutive weeks")
        if not _monotonic_supported(text, cited_facts, facts_by_id, evidence_by_id, metrics_by_id):
            unsupported.append(f"{label} claims continuous/consistent growth not supported by all cited weekly observations")
        fluctuation_supported, decline_supported = _trajectory_supported(text, cited_facts, evidence_by_id, metrics_by_id)
        if not fluctuation_supported:
            unsupported.append(f"{label} claims weekly fluctuations without enough cited observations showing both rises and falls")
        if not decline_supported:
            unsupported.append(f"{label} claims a decline without cited observations showing a decrease")

        dates = set(DATE_PATTERN.findall(text))
        supported_dates: set[str] = set()
        for fact in cited_facts:
            period = fact.observation_period
            supported_dates.update(DATE_PATTERN.findall(period))
        if dates - supported_dates:
            unsupported.append(f"{label} includes dates not present in referenced facts: {', '.join(sorted(dates - supported_dates))}")
        # Years in human prose are temporal labels, not market measurements. Their dates
        # still need to occur in the cited fact periods.
        years = set(YEAR_PATTERN.findall(text))
        supported_years = {period[:4] for fact in cited_facts for period in DATE_PATTERN.findall(fact.observation_period)}
        if years - supported_years:
            unsupported.append(f"{label} includes unsupported years: {', '.join(sorted(years - supported_years))}")
        measurement_text = WINDOW_PATTERN.sub("", text)
        measurement_text = DATE_PATTERN.sub("", measurement_text)
        measurement_text = YEAR_PATTERN.sub("", measurement_text)
        numbers = {_decimal(value) for value in NUMBER_PATTERN.findall(measurement_text)}
        numbers.discard(None)
        supported_values = set()
        for fact in cited_facts:
            if fact.value is not None and (value := _decimal(fact.value)) is not None:
                supported_values.add(value)
            if fact.coverage_count is not None and (value := _decimal(fact.coverage_count)) is not None:
                supported_values.add(value)
        unmatched = numbers - supported_values
        if unmatched:
            unsupported.append(f"{label} contains numbers without matching referenced fact values: "
                + ", ".join(str(value) for value in sorted(unmatched)))

        cited_rows = [evidence_by_id[eid] for fact in cited_facts for eid in fact.evidence_ids if eid in evidence_by_id]
        metric_names = {str(row.get("metric_name", "")).casefold() for row in cited_rows}
        units = {str(row.get("metric_unit", "")).casefold() for row in cited_rows}
        if _has_unqualified_demand_claim(text):
            direct_demand_metrics = {"sales_count", "transaction_count", "sales_rank", "purchase_count"}
            if not (metric_names & direct_demand_metrics):
                unsupported.append(f"{label} treats search interest or non-purchase evidence as evidence of product demand or sales")
        if (numbers or PRICE_CLAIM_PATTERN.search(text)) and PRICE_CLAIM_PATTERN.search(text):
            if not (metric_names & {"price", "list_price", "sale_price", "indicative_price"}
                    or units & {"usd", "eur", "gbp"}):
                unsupported.append(f"{label} makes a price claim without referenced price facts")
        if numbers and re.search(r"\b(?:sales volume|sales|sold|units sold|doanh số|bán được)\b", text, re.I):
            if not (metric_names & {"sales_count", "transaction_count"}):
                unsupported.append(f"{label} makes a sales-volume claim without referenced sales-count facts")
        if numbers and re.search(r"\b(?:listings?|product listings?|number of products|số listing)\b", text, re.I):
            if "listing_count" not in metric_names:
                unsupported.append(f"{label} makes a listing-count claim without referenced listing facts")

    for index, claim in enumerate(analysis.evidence_based_inferences, 1):
        validate_claim(f"evidence_based_inferences[{index}]", claim)
    for index, claim in enumerate(analysis.trend_reasons, 1):
        validate_claim(f"trend_reasons[{index}]", claim)
    direction_facts = [facts_by_id[fact_id] for fact_id in analysis.trend_direction_fact_ids if fact_id in facts_by_id]
    unknown_direction_refs = set(analysis.trend_direction_fact_ids) - set(facts_by_id)
    if unknown_direction_refs:
        unsupported.append("trend_direction references unknown fact IDs: " + ", ".join(sorted(unknown_direction_refs)))
    if analysis.trend_direction != "insufficient evidence":
        if not direction_facts:
            expected_keyword = analysis.main_keywords[0].casefold() if analysis.main_keywords else ""
            computed_directions = [fact for fact in facts
                if fact.metric_name == "four_week_average_direction" and fact.keyword.casefold() == expected_keyword]
            if computed_directions and all(str(fact.value) != analysis.trend_direction for fact in computed_directions):
                unsupported.append(f"trend_direction {analysis.trend_direction} contradicts the Python-computed direction fact")
            else:
                unsupported.append(f"trend_direction {analysis.trend_direction} has no Python-generated direction fact reference")
        elif any(fact.metric_name != "four_week_average_direction" or str(fact.value) != analysis.trend_direction
                 for fact in direction_facts):
            unsupported.append(f"trend_direction {analysis.trend_direction} contradicts its referenced Python-computed direction fact")
    elif direction_facts:
        unsupported.append("trend_direction is insufficient evidence but references a computed direction fact")
    for index, idea in enumerate(analysis.product_ideas, 1):
        if len(idea.what_to_sell.strip()) < 8:
            unsupported.append(f"Product idea {index} does not contain a meaningful product concept")
        if len(idea.target_customer.strip()) < 5:
            unsupported.append(f"Product idea {index} does not describe an intended customer hypothesis")
        if len(idea.differentiation.strip()) < 5:
            unsupported.append(f"Product idea {index} lacks a differentiation or creative rationale")
        fact_refs = set(idea.supporting_fact_ids)
        unknown = fact_refs - set(facts_by_id)
        if unknown:
            unsupported.append(f"Product idea {index} references unknown fact IDs: {', '.join(sorted(unknown))}")
        unknown_evidence = set(idea.supporting_evidence_ids) - set(evidence_by_id)
        if unknown_evidence:
            unsupported.append(f"Product idea {index} references unknown evidence IDs: {', '.join(sorted(unknown_evidence))}")
        if idea.evidence_status == "evidence_backed" and not (fact_refs or idea.supporting_evidence_ids):
            unsupported.append(f"Product idea {index} is labeled evidence-backed without supporting facts")
        available_search_facts = [fact for fact in facts if fact.metric_name == "search_interest_index"]
        validate_claim(f"product_ideas[{index}].why_now", idea.why_now, require_fact=bool(available_search_facts))
        why_fact_refs = [facts_by_id[fact_id] for fact_id in idea.why_now.fact_ids if fact_id in facts_by_id]
        if idea.evidence_status == "hypothesis":
            if idea.supporting_fact_ids != idea.why_now.fact_ids:
                unsupported.append(f"Product idea {index} supporting facts do not match its Python-checked timing rationale")
            trend_facts = available_search_facts
            if trend_facts:
                timing_search_facts = [facts_by_id[fact_id] for fact_id in idea.why_now.fact_ids if fact_id in facts_by_id
                    and (facts_by_id[fact_id].metric_name == "search_interest_index"
                        or facts_by_id[fact_id].metric_name.endswith("four_week_average_relative_index")
                        or facts_by_id[fact_id].metric_name in {"four_week_average_change", "four_week_average_direction"})]
                if not timing_search_facts:
                    unsupported.append(f"Product idea {index} lacks a relevant keyword-interest fact for its timing rationale")
                primary_keywords = {keyword.casefold() for keyword in analysis.main_keywords}
                if any(fact.keyword.casefold() not in primary_keywords for fact in timing_search_facts):
                    unsupported.append(f"Product idea {index} timing rationale cites unrelated keyword facts")
            if not re.search(r"\b(?:hypothesis|giả thuyết)\b", idea.why_now.text, re.I):
                unsupported.append(f"Product idea {index} must be explicitly labeled as a product hypothesis")
            if not re.search(r"(?:specific|product-specific) demand|purchase potential|nhu cầu(?: (?:riêng|cụ thể))?", idea.why_now.text, re.I) \
                    or not re.search(r"(?:not verified|unverified|not been verified|chưa (?:được )?(?:xác minh|xác thực|kiểm chứng))", idea.why_now.text, re.I):
                unsupported.append(f"Product idea {index} must state that its specific demand or purchase potential is unverified")
            if any(fact.metric_name == "search_interest_index" for fact in facts) and not any(
                    fact.metric_name == "search_interest_index" or fact.metric_name.endswith("four_week_average_relative_index")
                    or fact.metric_name in {"four_week_average_change", "four_week_average_direction"} for fact in why_fact_refs):
                unsupported.append(f"Product idea {index} does not cite a keyword-interest fact for its timing rationale")
            uncertainties = " ".join(idea.uncertainties).casefold()
            if not re.search(r"purchase|sales|mua hàng|doanh số", uncertainties):
                unsupported.append(f"Product idea {index} lacks uncertainty about actual purchases or sales")
            if not re.search(r"preference|customer feedback|preferences|sở thích|khẩu vị|phản hồi khách hàng", uncertainties):
                unsupported.append(f"Product idea {index} lacks uncertainty about customer preferences")
            if not re.search(r"product.market fit|\bpmf\b|phù hợp (?:sản phẩm[- ]?)?thị trường", uncertainties):
                unsupported.append(f"Product idea {index} lacks uncertainty about product-market fit")
            if not re.search(r"hypothesis|giả thuyết", idea.target_customer, re.I):
                unsupported.append(f"Product idea {index} target customer is not labeled as a hypothesis")
            if idea.differentiation and not re.search(r"hypothesis|giả thuyết", idea.differentiation, re.I):
                unsupported.append(f"Product idea {index} differentiation is not labeled as a creative hypothesis")
    distinct_concepts = {re.sub(r"\W+", " ", idea.what_to_sell.casefold()).strip() for idea in analysis.product_ideas}
    if len(analysis.product_ideas) < 2 or len(distinct_concepts) < 2:
        unsupported.append("Analysis must retain at least two distinct meaningful product concepts")
    for field, claims in (("hypotheses", analysis.hypotheses), ("target_customers", analysis.target_customers),
                          ("customer_needs_or_complaints", analysis.customer_needs_or_complaints)):
        for index, claim in enumerate(claims, 1):
            validate_claim(f"{field}[{index}]", claim, require_fact=False)
    metric_names_all = {str(row.get("metric_name", "")).casefold() for row in evidence}
    price_metrics = {"price", "list_price", "sale_price", "indicative_price"}
    competition_metrics = {"listing_count", "competitor_count", "competition_count", "competitor_listing_count"}
    feedback_metrics = {"review_count", "review_rating", "survey_response", "discussion_count", "customer_feedback"}
    customer_data_available = bool(metric_names_all & feedback_metrics)
    if analysis.indicative_price_range and not (metric_names_all & price_metrics):
        unsupported.append("indicative_price_range is present without any verified price facts")
    competition_unverified = re.search(r"unverif|not (?:known|measured|assessed|verified)|no direct|chưa (?:được )?xác minh|chưa có bằng chứng", analysis.competition_assessment, re.I)
    if not (metric_names_all & competition_metrics):
        if not competition_unverified:
            unsupported.append("competition_assessment must explicitly say competition is unverified when no competition facts exist")
        if re.search(r"market opportunity|opportunity for|high competition|intense competition|low competition|limited competition|saturated|crowded market|cạnh tranh (?:cao|thấp|gay gắt)|cơ hội thị trường", analysis.competition_assessment, re.I):
            unsupported.append("competition_assessment makes an unsupported competition-intensity or market-opportunity claim")
    if not customer_data_available:
        for index, item in enumerate(analysis.customer_needs_or_complaints, 1):
            if not re.search(r"hypothesis|hypothesized|assumption|may |might |could |possible|giả thuyết|có thể|giả định", item, re.I):
                unsupported.append(f"customer_needs_or_complaints[{index}] presents an unmeasured preference or complaint as observed")
        for index, item in enumerate(analysis.target_customers, 1):
            if not re.search(r"hypothesis|giả thuyết", item, re.I):
                unsupported.append(f"target_customers[{index}] is not labeled as a hypothesis without direct customer evidence")
    if analysis.confidence == "high":
        source_count = len({str(row.get("source_name", "")).casefold() for row in evidence if row.get("source_name")})
        keyword_count = len({str(row.get("keyword", "")).casefold() for row in evidence if row.get("keyword")})
        direct_metrics = {"sales_count", "transaction_count", "sales_rank", "review_count", "review_rating", "survey_response", "discussion_count", "purchase_count"}
        if source_count < 3 or keyword_count < 5 or not (metric_names_all & direct_metrics):
            unsupported.append("high confidence is unsupported by the available source, keyword, and direct-market-signal coverage")
    validate_claim("competition_assessment", analysis.competition_assessment, require_fact=False)
    if analysis.indicative_price_range:
        validate_claim("indicative_price_range", analysis.indicative_price_range, require_fact=False)
    for index, idea in enumerate(analysis.product_ideas, 1):
        validate_claim(f"product_ideas[{index}].what_to_sell", idea.what_to_sell, require_fact=False)
        validate_claim(f"product_ideas[{index}].target_customer", idea.target_customer, require_fact=False)
        if idea.differentiation:
            validate_claim(f"product_ideas[{index}].differentiation", idea.differentiation, require_fact=False)
        for uncertainty_index, claim in enumerate(idea.uncertainties, 1):
            validate_claim(f"product_ideas[{index}].uncertainties[{uncertainty_index}]", claim, require_fact=False)

    measured_keywords = {str(row.get("keyword", "")).casefold() for row in evidence if row.get("keyword")}
    for keyword in analysis.main_keywords:
        if keyword.casefold() not in measured_keywords and evidence:
            unsupported.append(f"Main keyword is not measured in the supplied evidence: {keyword}")
    for keyword in analysis.related_keywords:
        if keyword.casefold() not in measured_keywords:
            warnings.append(f"Unmeasured keyword must be treated as a research candidate, not a market signal: {keyword}")
    if not analysis.missing_or_unavailable_data:
        warnings.append("Missing/unavailable data section is empty")

    signals: dict[tuple[str, str, str], set[str]] = {}
    signal_metrics = {"search_interest_index", "sales_rank", "sales_count", "discussion_count"}
    for row in evidence:
        if row.get("metric_name") not in signal_metrics or not isinstance(row.get("metric_value"), (int, float)):
            continue
        key = (str(row.get("source_name", "")), str(row.get("metric_name", "")), str(row.get("keyword", "")))
        period = str(row.get("observation_period", "")).strip()
        if period:
            signals.setdefault(key, set()).add(period)
    evidence_sufficient = any(len(periods) >= 2 for periods in signals.values())
    claims_valid = not unsupported
    blocked_statuses = {"blocked", "rate_limited", "quota_or_permission_denied"}
    if not claims_valid:
        status = "INVALID"
    elif not evidence and collection_status in blocked_statuses:
        status = "BLOCKED"
    elif evidence and analysis_provenance == "deterministic_fallback":
        status = "DEGRADED"
    elif not evidence_sufficient:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "VALID"
    return ValidationResult(valid=status == "VALID", status=status, schema_valid=True,
        claims_valid=claims_valid, evidence_sufficient=evidence_sufficient,
        unsupported_assertions=list(dict.fromkeys(unsupported)), warnings=list(dict.fromkeys(warnings)))
