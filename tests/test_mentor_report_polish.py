"""Offline presentation checks with SYNTHETIC unit fixtures emulating real-format records."""
import json
import xml.etree.ElementTree as ET

import pytest

from market_research.html_report import STYLE
from market_research.report_chart import render_trend_chart
from market_research.report_intelligence import build_report_intelligence
from market_research.reports import render_html, render_markdown
from test_html_reports import Document
from test_report_intelligence import make_result  # Reuse the existing isolated format fixture.

NS = {"s": "http://www.w3.org/2000/svg"}


def svg(result):
    content = render_trend_chart(result, build_report_intelligence(result))
    return ET.fromstring(content[content.index("<svg"):content.index("</svg>") + 6])


@pytest.mark.parametrize("execution,provenance,expected", [
    ("not_requested", "deterministic_fallback", "Nội dung Python tạo theo quy tắc"),
    ("failed", "deterministic_fallback", "Nội dung fallback bằng Python"),
    ("success", "ollama", "Nội dung Ollama chưa được xác minh"),
    ("failed", "unknown", "Nội dung phân tích gốc chưa được xác minh"),
])
def test_origin_labels_are_accurate_in_html_and_markdown(make_result, execution, provenance, expected):
    result = make_result()
    result.metadata.update(analysis_execution_status=execution, analysis_provenance=provenance)
    result.validation.status = "INVALID"
    for content in (render_html(result), render_markdown(result)):
        assert expected in content
        if provenance != "ollama":
            assert "Nội dung Ollama chưa được xác minh" not in content
        if execution == "not_requested" or provenance == "deterministic_fallback":
            assert "Không có phát biểu phân tích nào của Ollama được chấp nhận" not in content
        if provenance == "ollama":
            assert "Không có phát biểu phân tích nào của Ollama được chấp nhận" in content


def test_repetitive_observations_and_all_trace_targets_stay_in_collapsed_audit(make_result):
    result = make_result()
    result.analysis.factual_observations = ["PER_WEEK_REPEAT_" + str(i) for i in range(53)]
    before = result.model_dump(mode="json")
    view = build_report_intelligence(result)
    html = render_html(result)
    doc = Document(html)
    assert "PER_WEEK_REPEAT_0" not in html.split('<section id="audit"', 1)[0]
    assert all(value in html for value in result.analysis.factual_observations)
    heavy = [attrs for tag, attrs in doc.nodes if tag == "details" and "audit-heavy" in attrs.get("class", "")]
    assert len(heavy) >= 6 and all("open" not in attrs for attrs in heavy)
    ids = {attrs.get("id") for _, attrs in doc.nodes}
    for index, item in enumerate(result.evidence):
        assert f"evidence-{index}" in ids and item.evidence_id in html
    for series in view["series"]:
        for measure in series["measurements"]:
            assert measure["metric_id"] in ids and "audit_" + measure["metric_id"] in ids
    assert json.loads("".join(doc.raw)) == before
    assert result.model_dump(mode="json") == before and build_report_intelligence(result) == view
    assert "<details>" in render_markdown(result)


def test_print_css_does_not_expand_raw_or_verbose_audit_even_when_open():
    print_css = STYLE.split("@media print", 1)[1]
    assert "details>*{display:block!important}" not in print_css
    assert "content-visibility:visible" not in print_css
    assert "details.audit-heavy>:not(summary){display:none!important}" in print_css
    assert ".raw-json,.calculation-json{display:none!important}" in print_css
    assert ".trend-chart svg{min-width:0;max-height:230px}" in print_css


def test_chart_uses_original_values_and_clearly_marks_incomplete_week(make_result):
    result = make_result()
    result.evidence[-1].metric_value = 91
    result.evidence[-1].raw_observation["raw_value"] = "91"
    before = result.model_dump(mode="json")
    root = svg(result)
    points = root.findall('.//s:circle[@class="trend-point"]', NS)
    assert len(points) == 53
    pending = [point for point in points if point.get("data-status") == "incomplete"]
    assert len(pending) == 1 and pending[0].get("data-evidence-id") == result.evidence[-1].evidence_id
    assert pending[0].get("fill") == "white" and float(pending[0].get("cy")) == pytest.approx(224 - 1.68 * 91)
    assert root.findall('.//s:line[@class="incomplete-week-marker"]', NS)
    assert any(line.get("stroke-dasharray") for line in root.findall('.//s:line[@class="trend-segment"]', NS))
    hrefs = [element.get("href") for element in root.iter() if element.get("href")]
    assert len(hrefs) == 53 and all(href.startswith("#evidence-") for href in hrefs)
    assert result.model_dump(mode="json") == before


def test_missing_or_censored_values_and_calendar_gaps_are_not_bridged(make_result):
    result = make_result([10, "<1", 30, 40, 50, 60, 70, 80])
    root = svg(result)
    assert len(root.findall('.//s:circle[@class="trend-point"]', NS)) == 7
    assert len(root.findall('.//s:line[@class="trend-segment"]', NS)) == 5
    assert result.evidence[1].raw_observation["raw_value"] == "<1"
    result = make_result([10, 20, 30, 40, 50, 60, 70, 80])
    del result.evidence[2]
    assert len(svg(result).findall('.//s:line[@class="trend-segment"]', NS)) == 5


def test_incomplete_censored_week_is_marked_without_inventing_a_point(make_result):
    result = make_result()
    result.evidence[-1].metric_value = None
    result.evidence[-1].raw_observation["raw_value"] = "<1"
    root = svg(result)
    assert len(root.findall('.//s:circle[@class="trend-point"]', NS)) == 52
    assert len(root.findall('.//s:line[@class="incomplete-week-marker"]', NS)) == 1


@pytest.mark.parametrize("case", ["empty", "all-missing", "invalid-date", "duplicate-date", "synthetic"])
def test_unusable_or_synthetic_input_is_never_a_real_market_chart(make_result, case):
    result = make_result()
    if case == "empty": result.evidence = []
    if case == "all-missing": result = make_result([None] * 8)
    if case == "invalid-date": result.evidence[0].observation_period = "bad"
    if case == "duplicate-date": result.evidence[0].observation_period = result.evidence[1].observation_period
    if case == "synthetic": result.metadata["data_provenance"] = "SYNTHETIC"
    assert "<svg" not in render_trend_chart(result, build_report_intelligence(result))


def test_one_chart_never_overlays_independently_normalized_exports(make_result):
    result = make_result([10, 20, 30, 40])
    other = make_result([60, 70, 80, 90])
    for item in other.evidence:
        item.evidence_id += "-other"
        item.raw_observation["csv_filename"] = "another-export.csv"
    result.evidence += other.evidence
    html = render_html(result)
    assert html.count("<svg ") == 1
    root = svg(result)
    assert len(root.findall('.//s:circle[@class="trend-point"]', NS)) == 4
    assert not any("-other" in point.get("data-evidence-id", "") for point in root.findall('.//s:circle[@class="trend-point"]', NS))


def test_svg_is_static_offline_safe_and_does_not_change_measurements(make_result):
    result = make_result()
    result.evidence[0].raw_observation["csv_filename"] = '</figcaption><script>alert(1)</script>'
    view = build_report_intelligence(result)
    chart = render_trend_chart(result, view)
    root = svg(result)
    allowed = {"svg", "title", "desc", "text", "line", "circle", "a"}
    assert {element.tag.rsplit("}", 1)[-1] for element in root.iter()} <= allowed
    assert not any(key.startswith("on") or key in {"src", "style"} for element in root.iter() for key in element.attrib)
    assert "<script>" not in chart and "&lt;script&gt;" in chart
    assert build_report_intelligence(result) == view
    html = render_html(result)
    assert "Content-Security-Policy" in html and "Phạm vi kết luận" in html
