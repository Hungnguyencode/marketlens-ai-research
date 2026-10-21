import json
from pathlib import Path

from .analysis import build_verified_facts
from .models import ResearchResult


def render_markdown(result: ResearchResult) -> str:
    analysis = result.analysis
    evidence = result.evidence
    evidence_by_id = {item.evidence_id: item for item in evidence}
    facts = result.verified_facts or build_verified_facts([item.model_dump(mode="json") for item in evidence])
    facts_by_id = {fact.fact_id: fact for fact in facts}
    measured = {item.keyword.casefold() for item in evidence}

    def text(claim):
        return claim.text if hasattr(claim, "text") else str(claim)

    def refs(fact_ids):
        return ", ".join(f"`{fact_id}`" for fact_id in fact_ids) or "không có fact ID"

    trend_vi = {"increasing": "tăng", "stable": "ổn định", "decreasing": "giảm", "insufficient evidence": "chưa đủ dữ kiện"}
    status_vi = {"VALID": "HỢP LỆ", "DEGRADED": "SUY GIẢM", "INSUFFICIENT_EVIDENCE": "CHƯA ĐỦ BẰNG CHỨNG",
        "BLOCKED": "BỊ CHẶN", "INVALID": "KHÔNG HỢP LỆ"}
    mode_vi = {"live": "thu thập tự động", "replay": "phát lại snapshot", "csv_import": "nạp CSV thủ công"}

    metrics = result.metadata.get("computed_metrics", [])
    lines = [f"# Báo cáo nghiên cứu: {result.niche}", "", "## Tóm tắt điều hành",
        f"- Trạng thái kiểm định: **{status_vi[result.validation.status]}**.",
        f"- Bằng chứng nguồn: {len(evidence)} bản ghi; phân tích: {result.metadata.get('analysis_provenance', 'N/A')}."]
    if result.validation.status == "INVALID":
        lines.append("- Không có phát biểu phân tích nào của Ollama được chấp nhận làm kết luận; xem phụ lục chẩn đoán.")
    if metrics:
        for metric in metrics:
            recent, previous = metric.get("recent_window") or {}, metric.get("previous_window") or {}
            delta = metric.get("change_in_index_points")
            keyword = metric.get("keyword", "")
            if recent and previous and delta is not None:
                change_word = "cao hơn" if delta > 0 else "thấp hơn" if delta < 0 else "bằng"
                recent_points = [point.get("relative_index") for point in metric.get("recent_evidence", [])]
                fluctuates = any(b < a for a, b in zip(recent_points, recent_points[1:])) and any(
                    b > a for a, b in zip(recent_points, recent_points[1:]))
                fluctuation_note = "; dữ liệu dao động giữa các tuần" if fluctuates else ""
                lines.append(f"- Với **{keyword}**, trung bình chỉ số tương đối của bốn tuần gần nhất "
                    f"({recent.get('start_period')}–{recent.get('end_period')}: {recent.get('average_relative_index')}) "
                    f"{change_word} giai đoạn bốn tuần trước ({previous.get('start_period')}–{previous.get('end_period')}: "
                    f"{previous.get('average_relative_index')}) {abs(delta)} điểm{fluctuation_note}. Đây là so sánh hai cửa sổ, "
                    "không khẳng định tăng liên tục theo tuần, xu hướng dài hạn, doanh số hay sức mua.")
            else:
                lines.append(f"- Chỉ số cho **{keyword}**: {metric.get('reason', 'chưa đủ dữ liệu để so sánh hai cửa sổ')}.")
    else:
        lines.append("- Chưa có metric xu hướng tính được từ dữ liệu.")

    lines += ["", "## Keyword và phạm vi đo lường"]
    primary_keywords = []
    candidate_keywords = list(analysis.keyword_research_candidates)
    for keyword in analysis.main_keywords + analysis.related_keywords:
        if keyword.casefold() in measured:
            if keyword.casefold() not in {x.casefold() for x in primary_keywords}:
                primary_keywords.append(keyword)
        elif keyword.casefold() not in {x.casefold() for x in candidate_keywords}:
            candidate_keywords.append(keyword)
    if primary_keywords:
        for keyword in primary_keywords:
            matching = [item for item in evidence if item.keyword.casefold() == keyword.casefold()]
            first = matching[0]
            lines.append(f"- **{keyword}** — {len(matching)} quan sát; nguồn: [{first.source_name}]({first.source_url}).")
    else:
        lines.append("- Chưa có keyword được đo trực tiếp.")
    lines.append("- Keyword ứng viên chưa đo (không phải tín hiệu trending/popular): " +
        (", ".join(candidate_keywords) if candidate_keywords else "không có"))

    if result.validation.status == "VALID":
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
            "- Diễn giải của Ollama đang bị loại khỏi kết luận vì kiểm định claim chưa đạt."]

    lines += ["", "## Giới hạn dữ liệu",
        f"- Chế độ: {mode_vi[result.mode]}; collection: {result.metadata.get('live_collection_status', 'N/A')}; "
        f"Ollama: {result.metadata.get('analysis_execution_status', 'N/A')}.",
        "- Search interest là chỉ số tương đối; không chứng minh lượt mua, doanh số hoặc nhu cầu sản phẩm.",
        "- Giá/đối thủ/phàn nàn khách hàng chỉ được kết luận khi có fact nguồn trực tiếp."]
    lines.extend(f"- Thiếu/không khả dụng: {item}" for item in analysis.missing_or_unavailable_data)

    lines += ["", "## Phụ lục kiểm toán"]
    lines.append(f"- Run ID: `{result.run_id}`; schema hợp lệ: {result.validation.schema_valid}; "
        f"claims hợp lệ: {result.validation.claims_valid}; evidence đủ: {result.validation.evidence_sufficient}.")
    if result.validation.unsupported_assertions:
        lines += ["", "### Chẩn đoán validation — phát biểu bị từ chối"]
        lines.extend(f"- {item}" for item in result.validation.unsupported_assertions)
    if result.validation.status != "VALID":
        lines += ["", "### Nội dung Ollama chưa được xác minh (không dùng làm kết luận)"]
        lines.extend(f"- Suy luận: {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.evidence_based_inferences)
        lines.extend(f"- Lý do xu hướng: {text(claim)} (facts: {refs(claim.fact_ids)})" for claim in analysis.trend_reasons)
        lines.extend(f"- Giả thuyết: {text(item)} (facts: {refs(getattr(item, 'fact_ids', []))})" for item in analysis.hypotheses)
        lines.extend(f"- Khách hàng/nhu cầu: {item}" for item in analysis.target_customers + analysis.customer_needs_or_complaints)
        lines.append(f"- Cạnh tranh/giá: {analysis.competition_assessment}; {analysis.indicative_price_range or 'không có giá'}.")
        for index, idea in enumerate(analysis.product_ideas, 1):
            lines.append(f"- Ý tưởng {index} ({idea.evidence_status}): {idea.what_to_sell}; {text(idea.why_now)}. "
                "Nhu cầu riêng cho sản phẩm chưa được xác minh.")
    lines += ["", "### Facts do Python tạo"]
    lines.extend(f"- `{fact.fact_id}` — {fact.description}; evidence IDs: {', '.join(fact.evidence_ids) or 'none'}; "
        f"metric IDs: {', '.join(fact.metric_ids) or 'none'}." for fact in facts)
    lines += ["", "### Evidence gốc"]
    lines.extend(f"- `{item.evidence_id}` — [{item.source_name}]({item.source_url}); {item.keyword}; "
        f"{item.metric_name}={item.metric_value} {item.metric_unit or ''}; kỳ={item.observation_period}; "
        f"{item.source_excerpt or ''}" for item in evidence)
    lines.append("")
    return "\n".join(lines)


def write_reports(result: ResearchResult, output_dir: str | Path) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{result.run_id}-{result.niche.replace(' ', '-')[:50]}"
    md, html, js = out / f"{stem}.md", out / f"{stem}.html", out / f"{stem}.json"
    content = render_markdown(result)
    md.write_text(content, encoding="utf-8")
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html.write_text(f"<!doctype html><meta charset='utf-8'><title>Market Research</title>"
        f"<pre style='white-space:pre-wrap;font:16px system-ui'>{escaped}</pre>", encoding="utf-8")
    js.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": str(md), "html": str(html), "json": str(js)}
