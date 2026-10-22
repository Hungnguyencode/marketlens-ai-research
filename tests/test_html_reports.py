"""SYNTHETIC structured report fixtures only; no collection, model or historical files."""
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from market_research.models import GroundedClaim, ResearchResult
from market_research.reports import render_html, render_markdown, write_reports


class Document(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.nodes, self.text, self.raw = [], [], []
        self.raw_depth = 0
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.nodes.append((tag, attrs))
        if tag == "code" and attrs.get("class") == "raw-json":
            self.raw_depth = 1

    def handle_endtag(self, tag):
        if tag == "code":
            self.raw_depth = 0

    def handle_data(self, data):
        self.text.append(data)
        if self.raw_depth:
            self.raw.append(data)


@pytest.fixture
def report(synthetic_historical_result_json):
    return ResearchResult.model_validate(synthetic_historical_result_json)


def test_html_is_semantic_standalone_not_a_markdown_pre_dump(report):
    content = render_html(report)
    doc = Document(content)
    tags = [tag for tag, _ in doc.nodes]
    assert content.startswith("<!doctype html>") and tags.count("h1") == 1
    assert tags.count("h2") == 6
    assert {"main", "header", "nav", "section", "p", "ul", "ol", "li", "table", "caption", "thead", "tbody", "th", "td", "dl", "strong"} <= set(tags)
    assert not {"pre", "script", "iframe", "img", "object", "embed", "form", "link"} & set(tags)
    assert any(tag == "meta" and attrs.get("name") == "viewport" for tag, attrs in doc.nodes)
    assert any(tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy" for tag, attrs in doc.nodes)
    assert "@media print" in content and "@media(max-width:600px)" in content
    assert "# Báo cáo nghiên cứu:" not in " ".join(doc.text)


def test_html_preserves_all_original_data_and_clickable_references(report):
    doc = Document(render_html(report))
    assert json.loads("".join(doc.raw)) == report.model_dump(mode="json")
    hrefs = [attrs["href"] for tag, attrs in doc.nodes if tag == "a"]
    assert report.evidence[0].source_url in hrefs
    assert "#fact-0" in hrefs and "#evidence-0" in hrefs
    for tag, attrs in doc.nodes:
        if tag == "a" and attrs.get("href", "").startswith("http"):
            assert attrs["rel"] == "noopener noreferrer" and attrs["target"] == "_blank"
    text = " ".join(doc.text)
    assert report.evidence[0].evidence_id in text and report.verified_facts[0].fact_id in text
    assert report.analysis.product_ideas[0].uncertainties[0] in text
    assert "VALID không xác nhận nhu cầu mua" in text


@pytest.mark.parametrize("url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x",
                                    "file:///C:/private", "//example.invalid/path", "vbscript:msgbox(1)",
                                    "https://user:password@example.invalid/", "https://example.invalid:bad/",
                                    " https://example.invalid/", "https://example.invalid/\npath"])
def test_unsafe_source_urls_stay_inert_but_original_is_preserved(report, url):
    report.evidence[0].source_url = url
    doc = Document(render_html(report))
    assert url not in [attrs.get("href") for tag, attrs in doc.nodes if tag == "a"]
    assert json.loads("".join(doc.raw))["evidence"][0]["source_url"] == url
    assert "URL không an toàn" in " ".join(doc.text)


def test_untrusted_fields_cannot_create_tags_attributes_or_remote_resources(report):
    payload = '</code><script>alert(1)</script><img src="https://example.invalid/x" onerror="alert(2)"> & **literal**'
    report.niche = payload
    report.analysis.product_ideas[0].what_to_sell = payload
    report.analysis.product_ideas[0].why_now.text = payload
    report.evidence[0].source_excerpt = payload
    report.evidence[0].source_name = payload
    report.verified_facts[0].description = payload
    doc = Document(render_html(report))
    assert not {"script", "img", "iframe", "object", "svg"} & {tag for tag, _ in doc.nodes}
    assert not any(key.startswith("on") or key in {"src", "srcdoc"} for _, attrs in doc.nodes for key in attrs)
    assert payload in " ".join(doc.text)
    assert json.loads("".join(doc.raw)) == report.model_dump(mode="json")


@pytest.mark.parametrize("status", ["VALID", "DEGRADED", "INVALID", "BLOCKED", "INSUFFICIENT_EVIDENCE"])
def test_validation_outcomes_do_not_promote_rejected_claims(report, status):
    report.validation.status = status
    report.validation.valid = status == "VALID"
    report.validation.claims_valid = status == "VALID"
    marker = "SYNTHETIC unique model conclusion"
    report.analysis.evidence_based_inferences = [GroundedClaim(text=marker, fact_ids=[report.verified_facts[0].fact_id])]
    content = render_html(report)
    primary = content.split('<section id="audit"', 1)[0]
    assert (marker in primary) == (status == "VALID")
    assert marker in " ".join(Document(content).text)
    assert f"Kiểm định: {status}" in " ".join(Document(content).text)


def test_no_inference_report_is_honest_and_preserves_less_than_one(report):
    report.validation.status = "DEGRADED"
    report.validation.valid = False
    report.metadata["analysis_execution_status"] = "not_requested"
    report.evidence[0].metric_value = "<1"
    report.evidence[0].raw_observation = {"raw_value": "<1", "SYNTHETIC": True}
    content = render_html(report)
    assert "&lt;1" in content and "Chưa yêu cầu phân tích Ollama" in content
    assert "Chưa yêu cầu phân tích Ollama" in render_markdown(report)
    assert json.loads("".join(Document(content).raw))["evidence"][0]["metric_value"] == "<1"


def test_empty_blocked_report_remains_useful_without_market_findings(report):
    report.evidence = []
    report.verified_facts = []
    report.validation.status = "BLOCKED"
    report.validation.valid = report.validation.claims_valid = report.validation.evidence_sufficient = False
    content = render_html(report)
    assert "Không có evidence nguồn" in content
    assert "SYNTHETIC test-only concept" not in content.split('<section id="audit"', 1)[0]


def test_written_html_and_json_preserve_traceability_and_values(report, tmp_path):
    paths = write_reports(report, tmp_path)
    content = Path(paths["html"]).read_text(encoding="utf-8")
    assert content == render_html(report)
    assert Path(paths["markdown"]).read_text(encoding="utf-8") == render_markdown(report)
    assert json.loads(Path(paths["json"]).read_text(encoding="utf-8")) == report.model_dump(mode="json")
    assert json.loads("".join(Document(content).raw)) == report.model_dump(mode="json")


def test_contradictory_validation_flags_cannot_expose_primary_model_claims(report):
    report.validation.claims_valid = False
    report.analysis.evidence_based_inferences = [GroundedClaim(text="SYNTHETIC inconsistent claim", fact_ids=[])]
    primary = render_html(report).split('<section id="audit"', 1)[0]
    assert "SYNTHETIC inconsistent claim" not in primary


def test_report_filenames_cannot_escape_output_root(report, tmp_path):
    report.niche = "SYNTHETIC / ../../ unsafe: title"
    outputs = write_reports(report, tmp_path / "reports")
    assert all(Path(name).parent == tmp_path / "reports" for name in outputs.values())
    report.run_id = "../SYNTHETIC-outside"
    with pytest.raises(ValueError, match="run ID"):
        write_reports(report, tmp_path / "other")
    assert not (tmp_path / "other").exists()
