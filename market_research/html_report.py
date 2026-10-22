"""Standalone semantic reports from typed records; untrusted text is never HTML/Markdown."""
import html
import json
from datetime import timezone
from urllib.parse import urlsplit

from .analysis import build_verified_facts
from .trends_csv import SYNTHETIC_NOTICE, SYNTHETIC_SOURCE, is_synthetic_result


STYLE = """
:root{color-scheme:light;--ink:#14263e;--muted:#526176;--line:#dce5ed;--teal:#146f69}
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:var(--ink);font:16px/1.7 system-ui,Segoe UI,sans-serif}
main{max-width:1080px;margin:40px auto;padding:40px;background:#fff;border:1px solid var(--line);border-radius:18px}
h1{font-size:clamp(28px,4vw,38px);line-height:1.3;margin:12px 0 22px}h2{font-size:24px;line-height:1.4;margin:0 0 18px}
h3{font-size:19px;line-height:1.5}p{margin:10px 0}a{color:#146f69;text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid #3276e8;outline-offset:4px}
.brand{font-weight:750;letter-spacing:.08em;color:var(--teal);font-size:14px}.kicker,.muted{color:var(--muted);font-size:14px}
header{border-bottom:2px solid var(--teal);padding-bottom:24px}.metadata{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:16px 24px;margin:24px 0 0}
dt{font-size:14px;color:var(--muted)}dd{margin:3px 0 0;font-weight:600}nav{display:flex;flex-wrap:wrap;gap:12px 24px;padding:20px 0}
section{margin-top:32px;padding-top:28px;border-top:1px solid var(--line)}section.executive{background:#f2f8f7;border:1px solid #cee3de;border-radius:12px;padding:24px}
.notice{background:#fff7e8;border-left:4px solid #a86b12;padding:16px 20px;margin:20px 0}.status{display:inline-block;padding:5px 12px;border:1px solid var(--line);border-radius:7px;font-weight:700}
ul,ol{padding-left:24px}li{margin:8px 0}.concepts>li{padding:0 0 20px 12px;margin:24px 0;border-bottom:1px solid var(--line)}.refs{font-size:14px;color:var(--muted)}
.table-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;font-size:14px}caption{text-align:left;padding:16px;font-weight:700;font-size:16px}
th,td{text-align:left;vertical-align:top;padding:12px 14px;border-top:1px solid var(--line)}th{background:#eff4f8}td{min-width:90px}td:first-child{min-width:130px}
code{font-family:ui-monospace,Consolas,monospace;font-size:14px;overflow-wrap:anywhere}.raw-json{display:block;white-space:pre-wrap;background:#f5f7fa;padding:20px;border-radius:10px}
summary{cursor:pointer;font-weight:650;padding:12px 0}h1,h2,h3,p,li,dd,td,a{overflow-wrap:anywhere}footer{margin-top:32px;border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:14px}
@media(max-width:600px){main{margin:0;padding:24px 16px;border:0;border-radius:0}.metadata{grid-template-columns:1fr}h2{font-size:22px}section.executive{padding:18px}.notice{padding:14px}}
@media print{body{background:#fff;color:#000}main{max-width:none;margin:0;padding:0;border:0}nav{display:none}h1,h2,h3{break-after:avoid}tr,.notice{break-inside:avoid}a{color:#000}a[href^="http"]::after{content:" (" attr(href) ")";font-size:11px}.table-scroll{overflow:visible}table{table-layout:fixed;font-size:11px}td{min-width:0;padding:6px}details::details-content{content-visibility:visible;display:block}details>*{display:block!important}.raw-json{font-size:10px}section{margin-top:20px;padding-top:16px}}
"""


def e(value):
    return html.escape(str(value), quote=True)


def source_link(url, label):
    """Allow only absolute HTTP(S) navigation. Unsafe originals remain inert audit text."""
    if url == SYNTHETIC_SOURCE:
        return f'<span>{e(label)} · không có URL nguồn thị trường thật</span>'
    try:
        parsed = urlsplit(url)
        safe = (url == url.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in url)
                and "\\" not in url and parsed.scheme.lower() in {"http", "https"}
                and parsed.hostname and not parsed.username and not parsed.password)
        parsed.port  # Reject malformed ports as well as unsafe schemes.
    except (ValueError, TypeError, AttributeError):
        safe = False
    if not safe:
        return f'{e(label)} <span class="muted">(URL không an toàn, không tạo liên kết)</span> <code>{e(url)}</code>'
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)}</a>'


def render_report(result, summaries, primary_keywords, candidate_keywords):
    analysis, validation = result.analysis, result.validation
    synthetic = is_synthetic_result(result)
    accepted = validation.status == "VALID" and all((validation.valid, validation.schema_valid,
                                                    validation.claims_valid, validation.evidence_sufficient))
    facts = result.verified_facts or build_verified_facts([item.model_dump(mode="json") for item in result.evidence])
    fact_targets = {fact.fact_id: f"fact-{index}" for index, fact in enumerate(facts)}
    evidence_targets = {item.evidence_id: f"evidence-{index}" for index, item in enumerate(result.evidence)}

    def refs(ids, targets=fact_targets):
        return ", ".join(f'<a href="#{targets[value]}"><code>{e(value)}</code></a>'
                         if value in targets else f'<code>{e(value)}</code>' for value in ids) or "không có ID"

    def bullets(values):
        return "<ul>" + "".join(f"<li>{e(value)}</li>" for value in values) + "</ul>"

    def claims(values):
        return "<ul>" + "".join(
            f'<li>{e(item.text if hasattr(item, "text") else item)} '
            f'<span class="refs">(facts: {refs(getattr(item, "fact_ids", []))})</span></li>' for item in values) + "</ul>"

    def section(identifier, title, content, css=""):
        return f'<section id="{identifier}" class="{css}" aria-labelledby="{identifier}-title"><h2 id="{identifier}-title">{title}</h2>{content}</section>'

    mode = {"live": "Thu thập tự động", "csv_import": "Google Trends CSV thủ công", "replay": "Replay snapshot"}[result.mode]
    if synthetic:
        mode = "CSV demo SYNTHETIC"
    metadata = [("Run ID", result.run_id), ("Vùng", result.target_region), ("Chế độ", mode),
                ("Bắt đầu (UTC)", result.started_at.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")),
                ("Hoàn tất (UTC)", result.finished_at.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")),
                ("Analysis provenance", result.metadata.get("analysis_provenance", "N/A")),
                ("Model execution", result.metadata.get("analysis_execution_status", "N/A")),
                ("Model yêu cầu", result.metadata.get("requested_model", "N/A"))]
    parts = ['<!doctype html><html lang="vi"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             f'<title>MarketLens — {e(result.niche)}</title><style>{STYLE}</style></head><body><main>',
             f'<header><p class="brand">MARKETLENS · EVIDENCE-FIRST RESEARCH</p><h1>{e(result.niche)}</h1>',
             f'<p class="status">Kiểm định: {e(validation.status)}</p><dl class="metadata">',
             "".join(f'<div><dt>{e(label)}</dt><dd>{e(value)}</dd></div>' for label, value in metadata), '</dl></header>',
             '<nav aria-label="Mục lục báo cáo"><a href="#summary">Tóm tắt</a><a href="#findings">Nội dung nghiên cứu</a><a href="#limitations">Giới hạn</a><a href="#evidence">Bằng chứng</a><a href="#audit">Audit</a></nav>',
             '<aside class="notice" aria-label="Giới hạn thương mại"><strong>Phạm vi kết luận</strong><p>VALID không xác nhận nhu cầu mua, doanh số, lợi nhuận hoặc product-market fit. Search interest là chỉ số tương đối; metadata thư mục không phải doanh số hay đối thủ đã xác nhận.</p></aside>']
    if synthetic:
        parts.insert(-1, f'<aside class="notice" aria-label="SYNTHETIC provenance"><strong>{e(SYNTHETIC_NOTICE)}</strong></aside>')
    summary = f'<p><strong>{len(result.evidence)}</strong> bản ghi nguồn · phân tích: {e(result.metadata.get("analysis_provenance", "N/A"))}.</p>'
    summary += bullets([paragraph for _, paragraph in summaries])
    if validation.status == "INVALID":
        summary += '<p><strong>Không có phát biểu phân tích nào của Ollama được chấp nhận làm kết luận; xem phụ lục chẩn đoán.</strong></p>'
    parts.append(section("summary", "Tóm tắt điều hành", summary, "executive"))

    keywords = []
    for keyword in primary_keywords:
        matching = [item for item in result.evidence if item.keyword.casefold() == keyword.casefold()]
        keywords.append(f'<li><strong>{e(keyword)}</strong> — {len(matching)} quan sát; nguồn: {source_link(matching[0].source_url, matching[0].source_name)}.</li>')
    keyword_body = '<ul>' + ''.join(keywords) + '</ul>' if keywords else '<p>Chưa có keyword được đo trực tiếp.</p>'
    keyword_body += '<p>Keyword ứng viên chưa đo (không phải tín hiệu trending/popular): ' + e(', '.join(candidate_keywords) or 'không có') + '.</p>'
    parts.append(section("keywords", "Keyword và phạm vi đo lường", keyword_body))

    if accepted:
        body = claims(analysis.evidence_based_inferences)
        body += '<h3>Diễn giải xu hướng</h3>' + claims(analysis.trend_reasons)
        body += '<h3>Giả thuyết nghiên cứu</h3>' + claims(analysis.hypotheses)
        body += '<h3>Ý tưởng sản phẩm — giả thuyết, nhu cầu riêng chưa xác minh</h3><ol class="concepts">'
        for idea in analysis.product_ideas:
            body += (f'<li><h3>{e(idea.what_to_sell)}</h3><p><strong>Đối tượng dự kiến:</strong> {e(idea.target_customer)}</p>'
                     f'<p>{e(idea.why_now.text)}</p><p class="refs">Facts: {refs(idea.why_now.fact_ids)}; '
                     f'fact hỗ trợ: {refs(idea.supporting_fact_ids)}; evidence: {refs(idea.supporting_evidence_ids, evidence_targets)}.</p>'
                     f'<p><strong>Khác biệt sáng tạo:</strong> {e(idea.differentiation)}</p>'
                     '<p><strong>Nhu cầu, mua hàng và product-market fit vẫn cần kiểm chứng.</strong></p>'
                     '<h4>Bất định</h4>' + bullets(idea.uncertainties) + '</li>')
        body += '</ol>'
    else:
        body = '<p>Chỉ các fact do Python tính từ evidence gốc bên dưới được trình bày như dữ kiện.</p>'
        if result.metadata.get("analysis_execution_status") == "not_requested":
            body += '<p>Chưa yêu cầu phân tích Ollama; không có kết luận AI mới.</p>'
        else:
            body += '<p>Phân tích chưa đạt điều kiện trình bày như kết luận; nội dung gốc được giữ trong audit.</p>'
        body += bullets(analysis.factual_observations)
    parts.append(section("findings", "Nội dung nghiên cứu", body))

    limits = [f'Chế độ: {mode}; collection: {result.metadata.get("live_collection_status", "N/A")}; Ollama: {result.metadata.get("analysis_execution_status", "N/A")}.',
              'Search interest là chỉ số tương đối; không chứng minh lượt mua, doanh số hoặc nhu cầu sản phẩm.',
              'Giá/đối thủ/phàn nàn khách hàng chỉ được kết luận khi có fact nguồn trực tiếp.',
              *analysis.missing_or_unavailable_data, *validation.warnings]
    if synthetic:
        limits.append(SYNTHETIC_NOTICE)
    elif result.mode == "csv_import":
        limits.append('CSV xuất thủ công, không phải thu thập Google Trends tự động. URL do người dùng khai báo chưa được xác minh độc lập là URL xuất file.')
    parts.append(section("limitations", "Giới hạn dữ liệu & bất định", bullets(limits)))

    evidence_body = '<div class="table-scroll" tabindex="0" role="region" aria-label="Bảng evidence có thể cuộn"><table><caption>Evidence gốc — giữ nguyên ID, kỳ quan sát và giá trị</caption><thead><tr>'
    evidence_body += ''.join(f'<th scope="col">{label}</th>' for label in ('Evidence ID', 'Nguồn / keyword', 'Kỳ quan sát', 'Metric / giá trị', 'Trích dẫn')) + '</tr></thead><tbody>'
    for index, item in enumerate(result.evidence):
        value = item.raw_observation.get("raw_value", item.metric_value)
        evidence_body += (f'<tr id="evidence-{index}"><th scope="row"><code>{e(item.evidence_id)}</code></th>'
                          f'<td>{source_link(item.source_url, item.source_name)}<p>{e(item.keyword)}</p></td>'
                          f'<td>{e(item.observation_period)}</td><td>{e(item.metric_name)}<br><strong>{e(value)}</strong> {e(item.metric_unit or "")}</td>'
                          f'<td>{e(item.source_excerpt or "")}</td></tr>')
    evidence_body += '</tbody></table></div>'
    if not result.evidence:
        evidence_body += '<p>Không có evidence nguồn. Báo cáo này không phải phân tích thị trường đã đủ bằng chứng.</p>'
    parts.append(section("evidence", "Bằng chứng nguồn", evidence_body))

    audit = '<h3>Facts do Python tạo</h3><ul>'
    for index, fact in enumerate(facts):
        audit += (f'<li id="fact-{index}"><code>{e(fact.fact_id)}</code> — {e(fact.description)}; '
                  f'evidence IDs: {refs(fact.evidence_ids, evidence_targets)}; metric IDs: {e(", ".join(fact.metric_ids) or "none")}.</li>')
    audit += '</ul><h3>Chẩn đoán validation — phát biểu bị từ chối</h3>' + bullets(validation.unsupported_assertions)
    if not accepted:
        audit += '<h3>Nội dung Ollama chưa được xác minh (không dùng làm kết luận)</h3>'
        audit += claims(analysis.evidence_based_inferences) + claims(analysis.trend_reasons) + claims(analysis.hypotheses)
        audit += bullets(analysis.target_customers + analysis.customer_needs_or_complaints)
        audit += f'<p>Cạnh tranh/giá: {e(analysis.competition_assessment)}; {e(analysis.indicative_price_range or "không có giá")}.</p>'
        audit += bullets(f'Ý tưởng {i} ({idea.evidence_status}): {idea.what_to_sell}; {idea.why_now.text}. Nhu cầu riêng cho sản phẩm chưa được xác minh.' for i, idea in enumerate(analysis.product_ideas, 1))
    audit += ('<details><summary>Toàn bộ record gốc · metadata, analysis, raw observations và validation</summary>'
              '<p>Giữ nguyên nội dung để kiểm toán; phần analysis gốc không tự trở thành kết luận đã kiểm định.</p>'
              f'<code class="raw-json">{e(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))}</code></details>')
    parts.append(section("audit", "Phụ lục kiểm toán", audit))
    parts.append('<footer>MarketLens · Báo cáo độc lập, CSS tự chứa, không tải script/font/CDN. Chỉ phản ánh evidence và trạng thái của run được ghi ở trên.</footer></main></body></html>')
    return ''.join(parts)
