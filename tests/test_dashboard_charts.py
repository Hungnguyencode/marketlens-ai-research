"""Presentation regressions, including the actual Vega rendering failure hidden by loader tests."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("altair")
pd = pytest.importorskip("pandas")

from market_research.dashboard_charts import SERIES_COLORS, chart_frame, timeline_chart
from market_research.dashboard_data import ArtifactStore
from market_research.dashboard_design import STATUS_LABELS, badge, hero, label, metric_cards, pipeline_stages


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def comparison(synthetic_dashboard_artifacts):
    return synthetic_dashboard_artifacts["comparison"]


def test_typed_chart_frame_real_completed_coverage_and_keyword_filters(comparison):
    frame = chart_frame(comparison, list(comparison.metrics))
    expected = sum(m.completed_weeks for m in comparison.metrics.values())
    assert len(frame) == expected and frame["keyword"].nunique() == 4
    assert pd.api.types.is_datetime64_any_dtype(frame["date"])
    assert frame["relative_index"].dtype == "float64" and frame["complete_week"].dtype == "bool"
    assert frame["complete_week"].all() and frame["relative_index"].between(0, 100).all()
    partial = chart_frame(comparison, list(comparison.metrics), True)
    assert (~partial["complete_week"]).sum() == sum(len(m.excluded_incomplete_weeks) for m in comparison.metrics.values())
    assert set(partial.loc[~partial["complete_week"], "period_label"]) == {"Chưa hoàn tất"}
    assert (partial["relative_index"] == 0).any()  # zero observations survive, never replaced by missing
    for niche_id, metric in comparison.metrics.items():
        selected = chart_frame(comparison, [niche_id])
        assert set(selected["keyword"]) == {metric.keyword} and len(selected) == metric.completed_weeks
    assert chart_frame(comparison, []).empty and timeline_chart(comparison, []) is None
    with pytest.raises(ValueError):
        chart_frame(comparison, ["unrelated-niche"])


def test_chart_spec_has_one_scoped_zoom_not_duplicate_layer_signals(comparison):
    spec = timeline_chart(comparison, list(comparison.metrics)).to_dict(validate=True)
    assert len(spec["params"]) == 1
    assert spec["params"][0]["views"] == ["completed_timeline"]
    assert spec["params"][0]["select"]["encodings"] == ["x"]
    encoding = spec["layer"][0]["encoding"]
    assert encoding["y"]["scale"]["domain"] == [0, 100]
    assert encoding["color"]["scale"]["range"] == [SERIES_COLORS[key] for key in comparison.metrics]
    assert {entry["field"] for entry in encoding["tooltip"]} == {"date", "keyword", "relative_index", "period_label"}
    focus = timeline_chart(comparison, ["bold-easy"], focus=True).to_dict(validate=True)
    assert "domain" not in focus["layer"][0]["encoding"]["y"]["scale"]
    assert "phóng đại" in focus["layer"][0]["encoding"]["y"]["title"]
    with pytest.raises(ValueError):
        timeline_chart(comparison, list(comparison.metrics), focus=True)


@pytest.mark.parametrize("selected,partial,focus", [(None, False, False), (None, True, False),
                                                    (["bold-easy"], False, False), (["bold-easy"], False, True)])
def test_vega_really_renders_shared_partial_niche_and_focus_charts(comparison, selected, partial, focus):
    compiler = pytest.importorskip("vl_convert", reason="Optional dashboard-test extra enables real offline Vega render checks")
    selected = list(comparison.metrics) if selected is None else selected
    svg = compiler.vegalite_to_svg(timeline_chart(comparison, selected, partial, focus).to_dict(validate=True))
    ET.fromstring(svg)
    assert 'role-mark' in svg and len(svg) > 5000
    for key in selected:
        assert comparison.metrics[key].keyword in svg


@pytest.mark.parametrize("raw", ["DETERMINISTIC_SUMMARY", "NOT_REQUESTED", "NOT_RUN", "CATALOG_ONLY", "SPARSE_SIGNAL", "INSUFFICIENT_EVIDENCE"])
def test_vietnamese_labels_keep_raw_enum_available_in_tooltips(raw):
    assert label(raw) == STATUS_LABELS[raw] and label(raw) != raw
    assert f'title="{raw}"' in badge(raw)
    assert label("UNKNOWN_STATUS") == "UNKNOWN_STATUS"


def test_custom_html_escapes_artifact_text_and_pipeline_preserves_provenance(comparison, synthetic_dashboard_artifacts):
    assert "<script>" not in hero("<script>", 'title="bad"')
    assert "&lt;script&gt;" in metric_cards([("<script>", 1, "fixture")])
    batch = synthetic_dashboard_artifacts["batch"]
    stages = pipeline_stages(batch, comparison)
    assert [name for name, _, _ in stages] == ["Thu thập", "Bằng chứng", "Phân tích", "Kiểm định", "Báo cáo"]
    assert label("NOT_REQUESTED") in stages[2][1] and label("NOT_RUN") in stages[3][1]
    assert "VALID" not in str(stages)


def test_apptest_emitted_chart_renders_and_empty_selection_is_informative(comparison, app_artifact_root):
    pytest.importorskip("streamlit")
    compiler = pytest.importorskip("vl_convert")
    import pyarrow as pa
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "dashboard.py")).run(timeout=30)
    next(b for b in app.sidebar.button if b.label == "Phân tích từ khóa").click().run(timeout=30)
    assert not app.exception
    charts = app.get("vega_lite_chart")
    assert len(charts) == 2  # shared chart plus optional focus panel
    def emitted_spec(chart):
        spec = json.loads(chart.proto.spec)
        spec.setdefault("datasets", {})
        for dataset in chart.proto.datasets:
            frame = pa.ipc.open_stream(dataset.data.data).read_all().to_pandas()
            spec["datasets"][dataset.name] = json.loads(frame.to_json(orient="records", date_format="iso"))
        if chart.proto.data.data:
            frame = pa.ipc.open_stream(chart.proto.data.data).read_all().to_pandas()
            spec["data"] = {"values": json.loads(frame.to_json(orient="records", date_format="iso"))}
        return spec
    for chart in charts:
        assert 'role-mark' in compiler.vegalite_to_svg(emitted_spec(chart))
    assert len(app.multiselect[0].value) == 4
    app.checkbox[0].check().run(timeout=30)
    assert not app.exception
    partial_spec = emitted_spec(app.get("vega_lite_chart")[0])
    records = next(iter(partial_spec["datasets"].values()))
    assert len(records) == sum(m.observed_weeks for m in comparison.metrics.values())
    assert sum(not row["complete_week"] for row in records) == 4
    app.multiselect[0].set_value([]).run(timeout=30)
    assert not app.exception
    assert any("Chọn ít nhất một keyword" in message.value for message in app.info)
    next(b for b in app.sidebar.button if b.label == "Khám phá niche").click().run(timeout=30)
    for niche_id in comparison.metrics:
        next(w for w in app.selectbox if w.label == "Chọn niche").set_value(niche_id).run(timeout=30)
        assert not app.exception
        assert 'role-mark' in compiler.vegalite_to_svg(emitted_spec(app.get("vega_lite_chart")[0]))
