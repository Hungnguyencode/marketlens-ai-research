import json
import re
from pathlib import Path

from .analysis import build_verified_facts
from .models import ResearchResult
from .report_intelligence import build_report_intelligence, fmt
from .trends_csv import SYNTHETIC_NOTICE, SYNTHETIC_SOURCE, is_synthetic_result


def unverified_analysis_heading(result):
    if result.metadata.get("analysis_execution_status") == "not_requested":
        return "Nội dung Python tạo theo quy tắc — không chạy Ollama; giả thuyết chưa xác minh"
    if result.metadata.get("analysis_provenance") == "deterministic_fallback":
        return "Nội dung fallback bằng Python — giả thuyết chưa xác minh"
    if result.metadata.get("analysis_provenance") == "ollama":
        return "Nội dung Ollama chưa được xác minh (không dùng làm kết luận)"
    return "Nội dung phân tích gốc chưa được xác minh — nguồn xem metadata"


def rejected_analysis_notice(result):
    if (result.metadata.get("analysis_provenance") == "ollama"
            and result.metadata.get("analysis_execution_status") != "not_requested"):
        return "Không có phát biểu phân tích nào của Ollama được chấp nhận làm kết luận; xem phụ lục chẩn đoán."
    return "Các phát biểu phân tích chưa đạt kiểm định không được chấp nhận làm kết luận; xem phụ lục chẩn đoán."


def trend_summaries(result, intelligence=None):
    intelligence = intelligence if intelligence is not None else build_report_intelligence(result)
    return [(series["keyword"], finding["observed"]) for series in intelligence["series"]
            for finding in series["findings"][:4]] or [("", "Chưa có chuỗi Google Trends thật đủ điều kiện cho phân tích định lượng báo cáo.")]


def intelligence_markdown(intelligence):
    if not intelligence["series"]:
        return []
    lines = ["", "## Phân tích định lượng & phát hiện có căn cứ",
             "Tính lại từ source records; không dùng số liệu do LLM hoặc cached metadata cung cấp. Index points không phải sales growth."]
    for series in intelligence["series"]:
        lines += ["", "### " + series["keyword"], "- Độ tin cậy mô tả: " + series["confidence"],
                  "- Export tách riêng: " + str(series["csv_filename"] or "không ghi tên file") + "; không so index trực tiếp với export khác.",
                  "- Mốc loại tuần chưa hoàn tất (UTC): " + series["as_of"],
                  "", "| Phép đo | Giá trị / đơn vị | Metric ID |", "|---|---|---|"]
        cell = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
        lines += [f"| {cell(m['label'])} | {cell(fmt(m['value']))} {cell(m['unit'])} | `{m['metric_id']}` |"
                  for m in series["measurements"]]
        for finding in series["findings"]:
            lines += ["", "- **OBSERVED FACT:** " + finding["observed"],
                      "  - Metrics: " + ", ".join("`" + value + "`" for value in finding["metric_ids"]),
                      "  - **INTERPRETATION:** " + finding["implication"].removeprefix("INTERPRETATION: "),
                      "  - **Bất định:** " + finding["uncertainty"]]
        lines += ["", "- **" + series["hypothesis"] + "**"]
        lines += ["- Giới hạn phép tính: " + value for value in series["caveats"]]
    lines += ["", "## Hành động nghiên cứu ưu tiên", "Đây là đề xuất thu thập/kiểm chứng evidence, không phải kết luận thương mại."]
    for action in intelligence["recommendations"]:
        lines += ["", "### " + action["priority"] + " — " + action["question"],
                  "- Evidence cần: " + action["evidence"], "- Vì sao: " + action["why"], "- Quyết định hỗ trợ: " + action["decision"]]
    return lines


def keyword_groups(result):
    analysis = result.analysis
    measured = {item.keyword.casefold() for item in result.evidence}
    primary_keywords = []
    candidate_keywords = list(analysis.keyword_research_candidates)
    for keyword in analysis.main_keywords + analysis.related_keywords:
        if keyword.casefold() in measured:
            if keyword.casefold() not in {x.casefold() for x in primary_keywords}:
                primary_keywords.append(keyword)
        elif keyword.casefold() not in {x.casefold() for x in candidate_keywords}:
            candidate_keywords.append(keyword)
    return primary_keywords, candidate_keywords


def render_html(result: ResearchResult) -> str:
    from .html_report import render_report
    intelligence = build_report_intelligence(result)
    return render_report(result, trend_summaries(result, intelligence), *keyword_groups(result),
                         intelligence=intelligence, unverified_heading=unverified_analysis_heading(result),
                         rejection_notice=rejected_analysis_notice(result))


def render_markdown(result: ResearchResult) -> str:
    analysis = result.analysis
    evidence = result.evidence
    facts = result.verified_facts or build_verified_facts([item.model_dump(mode="json") for item in evidence])
    synthetic = is_synthetic_result(result)
    intelligence = build_report_intelligence(result)
    accepted = (not synthetic and result.validation.status == "VALID" and all((result.validation.valid,
                result.validation.schema_valid, result.validation.claims_valid, result.validation.evidence_sufficient)))

    def source(item):
        return item.source_name if item.source_url == SYNTHETIC_SOURCE else f"[{item.source_name}]({item.source_url})"

    def text(claim):
        return claim.text if hasattr(claim, "text") else str(claim)

    def refs(fact_ids):
        return ", ".join(f"`{fact_id}`" for fact_id in fact_ids) or "không có fact ID"

    status_vi = {"VALID": "HỢP LỆ", "DEGRADED": "SUY GIẢM", "INSUFFICIENT_EVIDENCE": "CHƯA ĐỦ BẰNG CHỨNG",
        "BLOCKED": "BỊ CHẶN", "INVALID": "KHÔNG HỢP LỆ"}
    mode_vi = {"live": "thu thập tự động", "replay": "phát lại snapshot", "csv_import": "nạp CSV thủ công"}
    if synthetic:
        mode_vi[result.mode] = "CSV demo SYNTHETIC"

    lines = [f"# Báo cáo nghiên cứu: {result.niche}", "", "## Tóm tắt điều hành",
        f"- Trạng thái kiểm định: **{status_vi[result.validation.status]}**.",
        f"- Bằng chứng nguồn: {len(evidence)} bản ghi; phân tích: {result.metadata.get('analysis_provenance', 'N/A')}."]
    if synthetic:
        lines.insert(2, "> **" + SYNTHETIC_NOTICE + "**")
    if result.validation.status == "INVALID":
        lines.append("- " + rejected_analysis_notice(result))
    lines.extend("- " + (paragraph.replace(keyword, f"**{keyword}**", 1) if keyword else paragraph)
                 for keyword, paragraph in trend_summaries(result, intelligence))
    if intelligence["series"]:
        lines.extend("- Độ tin cậy mô tả " + series["keyword"] + ": " + series["confidence"] for series in intelligence["series"])
        lines.append("- Hàm ý thực hành: kiểm tra tính duy trì của mức quan tâm trước khi thử positioning; chưa có evidence về mua hàng, giá hoặc conversion.")
        lines.append("- Bất định chính: chuẩn hóa và ngữ cảnh export, kỳ chưa hoàn tất/thiếu giá trị, nguồn do người dùng khai báo; chưa xác minh mùa vụ hoặc sức mua.")
    lines += intelligence_markdown(intelligence)

    lines += ["", "## Keyword và phạm vi đo lường"]
    primary_keywords, candidate_keywords = keyword_groups(result)
    if primary_keywords:
        for keyword in primary_keywords:
            matching = [item for item in evidence if item.keyword.casefold() == keyword.casefold()]
            first = matching[0]
            lines.append(f"- **{keyword}** — {len(matching)} quan sát; nguồn: {source(first)}.")
    else:
        lines.append("- Chưa có keyword được đo trực tiếp.")
    lines.append("- Keyword ứng viên chưa đo (không phải tín hiệu trending/popular): " +
        (", ".join(candidate_keywords) if candidate_keywords else "không có"))

    if accepted:
        lines += ["", "## Diễn giải đã qua kiểm định"]
        lines.extend(f"- {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.evidence_based_inferences)
        lines.extend(f"- Xu hướng: {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.trend_reasons)
        lines.extend(f"- Giả thuyết nghiên cứu: {text(hypothesis)} "
            f"(fact tham chiếu: {refs(getattr(hypothesis, 'fact_ids', []))})" for hypothesis in analysis.hypotheses)
        lines.append("### Ý tưởng sản phẩm — giả thuyết, nhu cầu riêng chưa xác minh")
        for index, idea in enumerate(analysis.product_ideas, 1):
            lines.append(f"{index}. **{idea.what_to_sell}** — {idea.target_customer}. {text(idea.why_now)} "
                f"(facts: {refs(idea.why_now.fact_ids)}). Nhu cầu, mua hàng và product-market fit vẫn cần kiểm chứng.")
            if idea.differentiation:
                lines.append(f"   - Khác biệt sáng tạo: {idea.differentiation}")
        if not (analysis.evidence_based_inferences or analysis.trend_reasons or analysis.hypotheses):
            lines.append("- Không có diễn giải bổ sung.")
    else:
        lines += ["", "## Kết quả đã xác minh", "- Chỉ các fact do Python tính từ evidence gốc bên dưới được trình bày như dữ kiện.",
            ("- Chưa yêu cầu phân tích Ollama; không có kết luận AI mới."
             if result.metadata.get("analysis_execution_status") == "not_requested" else
             "- Nội dung phân tích chưa đạt điều kiện trình bày như kết luận; xem nguồn trong metadata và audit.")]

    lines += ["", "## Giới hạn dữ liệu",
        f"- Chế độ: {mode_vi[result.mode]}; collection: {result.metadata.get('live_collection_status', 'N/A')}; "
        f"Ollama: {result.metadata.get('analysis_execution_status', 'N/A')}.",
        "- Search interest là chỉ số tương đối; không chứng minh lượt mua, doanh số hoặc nhu cầu sản phẩm.",
        "- Giá/đối thủ/phàn nàn khách hàng chỉ được kết luận khi có fact nguồn trực tiếp."]
    lines.extend(f"- Thiếu/không khả dụng: {item}" for item in analysis.missing_or_unavailable_data)
    lines.extend("- " + value for value in intelligence["limitations"])

    lines += ["", "## Phụ lục kiểm toán"]
    lines.append(f"- Run ID: `{result.run_id}`; schema hợp lệ: {result.validation.schema_valid}; "
        f"claims hợp lệ: {result.validation.claims_valid}; evidence đủ: {result.validation.evidence_sufficient}.")
    if result.validation.unsupported_assertions:
        lines += ["", "### Chẩn đoán validation — phát biểu bị từ chối"]
        lines.extend(f"- {item}" for item in result.validation.unsupported_assertions)
    if not accepted:
        lines += ["", "<details>", "<summary>" + unverified_analysis_heading(result) + "</summary>", ""]
        lines.extend(f"- Suy luận: {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.evidence_based_inferences)
        lines.extend(f"- Lý do xu hướng: {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.trend_reasons)
        lines.extend(f"- Giả thuyết: {text(item)} (facts: {refs(getattr(item, 'fact_ids', []))})" for item in analysis.hypotheses)
        lines.extend(f"- Khách hàng/nhu cầu: {item}" for item in analysis.target_customers + analysis.customer_needs_or_complaints)
        lines.append(f"- Cạnh tranh/giá: {analysis.competition_assessment}; {analysis.indicative_price_range or 'không có giá'}.")
        for index, idea in enumerate(analysis.product_ideas, 1):
            lines.append(f"- Ý tưởng {index} ({idea.evidence_status}): {idea.what_to_sell}; {text(idea.why_now)}. "
                "Nhu cầu riêng cho sản phẩm chưa được xác minh.")
        lines += ["", "</details>"]
    lines += ["", "<details>", "<summary>Facts Python, evidence từng kỳ và trace đầy đủ</summary>", ""]
    lines += ["", "### Facts do Python tạo"]
    lines.extend(f"- `{fact.fact_id}` — {fact.description}; evidence IDs: {', '.join(fact.evidence_ids) or 'none'}; "
        f"metric IDs: {', '.join(fact.metric_ids) or 'none'}." for fact in facts)
    lines += ["", "### Evidence gốc"]
    lines.extend(f"- `{item.evidence_id}` — {source(item)}; {item.keyword}; "
        f"{item.metric_name}={item.metric_value} {item.metric_unit or ''}; kỳ={item.observation_period}; "
        f"{item.source_excerpt or ''}" for item in evidence)
    if intelligence["series"]:
        lines += ["", "### Audit phép tính report · " + intelligence["method"]]
        for series in intelligence["series"]:
            for m in series["measurements"]:
                lines.append(f"- `{m['metric_id']}`: {m['formula']}; evidence IDs: " + ", ".join(f"`{value}`" for value in m["evidence_ids"]))
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def write_reports(result: ResearchResult, output_dir: str | Path) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result.run_id):
        raise ValueError("Unsafe report run ID.")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "-", result.niche.replace(" ", "-"))[:50].strip(".-") or "research"
    stem = f"{result.run_id}-{slug}"
    md, html, js = out / f"{stem}.md", out / f"{stem}.html", out / f"{stem}.json"
    content = render_markdown(result)
    md.write_text(content, encoding="utf-8")
    html.write_text(render_html(result), encoding="utf-8")
    js.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": str(md), "html": str(html), "json": str(js)}
