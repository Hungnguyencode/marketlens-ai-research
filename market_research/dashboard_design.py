"""Escaped presentation components and Vietnamese display labels. Raw enums remain in artifacts/audit."""
from datetime import datetime, timezone
from html import escape
import re


STATUS_LABELS = {
    "DETERMINISTIC_SUMMARY": "Tổng hợp bằng Python", "DETERMINISTIC_COMPARISON": "So sánh bằng Python",
    "NOT_REQUESTED": "Chưa yêu cầu chạy mô hình", "NOT_RUN": "Chưa thực hiện kiểm định",
    "CATALOG_ONLY": "Khám phá thư mục", "CATALOG_AND_TRENDS": "Thư mục & tín hiệu tìm kiếm",
    "TRENDS_ONLY": "Chỉ có tín hiệu tìm kiếm", "NO_EVIDENCE": "Chưa có bằng chứng",
    "SPARSE_SIGNAL": "Tín hiệu thưa", "INSUFFICIENT_EVIDENCE": "Chưa đủ bằng chứng",
    "success": "Đã thu thập", "partial": "Thu thập một phần", "empty": "Nguồn trả kết quả rỗng",
    "failed": "Thu thập chưa thành công", "disabled": "Đã tắt nguồn", "not_configured": "Chưa cấu hình nguồn",
    "available": "Có dữ liệu", "missing": "Chưa có dataset", "live": "Thu từ API tại thời điểm ghi nhận",
    "cache_replay": "Đọc lại snapshot", "not_collected": "Chưa thu thập",
    "increasing": "Trung bình hai cửa sổ tăng", "decreasing": "Trung bình hai cửa sổ giảm",
    "stable": "Trung bình hai cửa sổ ổn định", "insufficient evidence": "Chưa đủ tín hiệu để diễn giải",
    "VALID": "Đạt kiểm định", "PARTIAL_EVIDENCE": "Bằng chứng chưa đầy đủ", "BLOCKED": "Nguồn đang bị chặn"}


def label(raw):
    return STATUS_LABELS.get(raw, raw)


def e(value):
    return escape(str(value), quote=True)


def badge(raw, tone="neutral"):
    tone = tone if tone in {"neutral", "blue", "teal", "amber"} else "neutral"
    return f'<span class="ml-badge ml-{tone}" title="{e(raw)}">{e(label(raw))}</span>'


def date_label(value, with_time=False):
    if not value:
        return "Chưa ghi nhận"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if with_time:
            return dt.astimezone(timezone.utc).strftime("%d/%m/%Y · %H:%M UTC")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return str(value)


def run_label(run_id):
    match = re.search(r"-(\d{8})T(\d{6})", run_id)
    if not match:
        return run_id
    dt = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    return dt.strftime("%d/%m/%Y · %H:%M UTC") + " · " + run_id.rsplit("-", 1)[-1]


def hero(title, description):
    return f'<header class="ml-hero"><div class="ml-eyebrow">MARKET INTELLIGENCE / RESEARCH WORKSPACE</div><h1>{e(title)}</h1><p>{e(description)}</p></header>'


def section(title, description=""):
    return f'<div class="ml-section"><h2>{e(title)}</h2><p>{e(description)}</p></div>'


def metric_cards(values):
    return '<div class="ml-metrics">' + "".join(
        f'<article class="ml-metric"><div class="ml-metric-label">{e(title)}</div><strong>{e(value)}</strong><p>{e(detail)}</p></article>'
        for title, value, detail in values) + '</div>'


def note(title, description, tone="blue"):
    tone = tone if tone in {"blue", "amber", "neutral", "teal"} else "neutral"
    return f'<div class="ml-note ml-{tone}"><strong>{e(title)}</strong><p>{e(description)}</p></div>'


def panel(title, items, eyebrow=""):
    return f'<article class="ml-panel"><div class="ml-eyebrow">{e(eyebrow)}</div><h3>{e(title)}</h3><ul>' + "".join(
        f'<li>{e(item)}</li>' for item in items) + '</ul></article>'


def pipeline_stages(batch, comparison):
    successful = sum(n.catalog.status in {"success", "partial"} for n in batch.niches.values())
    catalog_present = any(n.catalog.parsed_items for n in batch.niches.values())
    standalone_present = any(n.trend_observations for n in batch.niches.values())
    evidence = ("Catalog + CSV so sánh chung" if comparison and catalog_present else "CSV so sánh chung" if comparison else
                "Catalog + CSV standalone riêng" if catalog_present and standalone_present else "Metadata thư mục" if catalog_present else
                "CSV standalone · chưa so sánh chung" if standalone_present else "Chưa có bằng chứng")
    return [
        ("Thu thập", f"{successful}/{len(batch.niches)} nguồn catalog có kết quả", "teal" if successful else "amber"),
        ("Bằng chứng", evidence, "teal" if catalog_present or standalone_present or comparison else "neutral"),
        ("Phân tích", label("DETERMINISTIC_SUMMARY") + " · " + label("NOT_REQUESTED"), "blue"),
        ("Kiểm định", label("NOT_RUN") + (" · checksum nguồn đã đối chiếu" if comparison else ""), "neutral"),
        ("Báo cáo", "Artifacts JSON đã được tải", "blue")]


def matrix_html(title, columns, rows):
    """Accessible source-status table; words remain visible without color."""
    header = "".join(f'<th scope="col">{e(column)}</th>' for column in columns)
    body = []
    for name, cells in rows:
        values = "".join(f'<td><span class="ml-{tone if tone in {"teal", "amber", "neutral", "blue"} else "neutral"}">{e(text)}</span></td>'
                         for text, tone in cells)
        body.append(f'<tr><th scope="row">{e(name)}</th>{values}</tr>')
    return f'<div class="ml-table-wrap" tabindex="0" role="region" aria-label="{e(title)}"><table class="ml-matrix"><caption>{e(title)}</caption><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def pipeline_html(batch, comparison):
    return '<div class="ml-flow">' + "".join(
        f'<div class="ml-stage"><span class="ml-stage-dot ml-{tone}"></span><strong>{e(title)}</strong><p>{e(detail)}</p></div>'
        for title, detail, tone in pipeline_stages(batch, comparison)) + '</div>'
