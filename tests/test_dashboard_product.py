"""Product presentation regressions. No source collection or inference."""
import copy
import hashlib
import json
import tomllib
from pathlib import Path

import httpx
import pytest

from market_research.dashboard_charts import chart_frame, weekly_heatmap
from market_research.dashboard_data import ArtifactStore
from market_research.dashboard_design import hero, label, matrix_html, section
from market_research.dashboard_insights import (CATALOG_COLUMNS, GOLDEN_RUN_ID, catalog_detail,
    catalog_table_rows, historical_problem, idea_references, load_golden_insights,
    review_title, reviewed_catalog_rows)
from market_research.dashboard_ui import commercial_label, coverage_rows


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only dashboard must not collect or infer")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.__init__", forbidden)


@pytest.fixture
def views(synthetic_dashboard_artifacts):
    return (synthetic_dashboard_artifacts["store"],
            synthetic_dashboard_artifacts["batch"],
            synthetic_dashboard_artifacts["comparison"])


def test_heading_structure_escaping_and_typography_tokens():
    markup = hero("Tổng quan · <script>", "Bằng chứng & kiểm định")
    assert markup.startswith('<header class="ml-hero">') and markup.count('<h1>') == 1
    assert "&lt;script&gt;" in markup and "<script>" not in markup
    assert '<h2>' in section("Nguồn dữ liệu")
    css = (ROOT / "dashboard.css").read_text(encoding="utf-8")
    assert 'padding:5.5rem 2.5rem 3rem' in css and 'padding:5.5rem 1rem 2rem' in css
    for token in ['--ml-body:16px', '--ml-secondary:14px', 'font-size:24px', 'font-size:18px',
                  'clamp(32px,2.6vw,38px)', 'line-height:1.4', 'max-width:768px', 'max-width:420px']:
        assert token in css
    assert 'overflow:hidden' not in css and 'height:100%' not in css
    config = tomllib.loads((ROOT / '.streamlit/config.toml').read_text(encoding='utf-8'))
    assert config['theme']['baseFontSize'] == 16
    assert config['theme']['sidebar']['backgroundColor'] == '#0C1B2F'
    assert config['server']['address'] == '127.0.0.1'
    assert '[data-testid="stSidebar"] {width:288px' not in css
    assert 'data-testid="stCodeBlock"' in css and 'overflow-wrap:anywhere' in css
    import ast
    tree = ast.parse((ROOT / 'dashboard.py').read_text(encoding='utf-8'))
    page_config = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and
        isinstance(node.func, ast.Attribute) and node.func.attr == 'set_page_config')
    # Streamlit's integer form uses AUTO behavior with a fixed initial desktop width.
    assert next(k.value.value for k in page_config.keywords if k.arg == 'initial_sidebar_state') == 288
    group = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'download_group')
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'columns'
               for node in ast.walk(group))


def test_source_matrix_escapes_and_preserves_status_words(views):
    _, batch, comparison = views
    rows = coverage_rows(batch, comparison)
    assert len(rows) == 4
    assert all(cells[1][0] == 'Có dữ liệu đo' for _, cells in rows)
    assert all(cells[2][0] == 'Chưa yêu cầu chạy mô hình' for _, cells in rows)
    assert all(cells[3][0] == 'Chưa thực hiện kiểm định' for _, cells in rows)
    markup = matrix_html('Coverage <img>', ['Niche', 'Nguồn'], [('Niche', [('<script>', 'evil')])])
    assert '<caption>' in markup and 'scope="row"' in markup and 'tabindex="0"' in markup
    assert '<script>' not in markup and 'ml-evil' not in markup
    no_sources = copy.deepcopy(batch)
    no_sources.catalogs.clear()
    assert all(cells[0][1] == 'amber' and cells[1][1] == 'neutral' for _, cells in coverage_rows(no_sources, None))


@pytest.mark.parametrize('niche_id', ['cozy-adults', 'bold-easy', 'christmas-adults', 'kids-christmas'])
def test_heatmap_is_exact_completed_joint_data_and_renders(views, niche_id):
    compiler = pytest.importorskip('vl_convert')
    _, _, comparison = views
    chart = weekly_heatmap(comparison, niche_id)
    spec = chart.to_dict(validate=True)
    assert spec['mark']['type'] == 'rect' and spec['encoding']['color']['scale']['domain'] == [0, 100]
    assert 'params' not in spec and 'layer' not in spec
    data = next(iter(spec['datasets'].values()))
    expected = chart_frame(comparison, [niche_id])
    assert [p['relative_index'] for p in data] == expected.relative_index.tolist()
    assert len(data) == comparison.metrics[niche_id].completed_weeks
    assert {p['keyword'] for p in data} == {comparison.metrics[niche_id].keyword}
    assert all(p['complete_week'] for p in data) and 'role-mark' in compiler.vegalite_to_svg(spec)


def test_missing_golden_does_not_create_ai_insights_from_test_data(views):
    store, _, _ = views
    run, problem = load_golden_insights(store)
    assert run is None and problem and "SYNTHETIC_TEST_FIXTURE" not in problem
    assert GOLDEN_RUN_ID not in store.historical_paths()


@pytest.mark.parametrize('mutation', ['missing', 'malformed', 'invalid', 'false_claims', 'no_execution',
                                    'wrong_niche', 'broken_fact', 'broken_evidence', 'unsafe_url'])
def test_unavailable_or_invalid_golden_never_becomes_ai_preview(tmp_path, mutation,
                                                                 synthetic_historical_result_json):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    path = outputs / f'{GOLDEN_RUN_ID}-SYNTHETIC-test-only.json'
    raw = copy.deepcopy(synthetic_historical_result_json)
    if mutation == 'invalid':
        raw['validation']['status'] = 'INVALID'
    elif mutation == 'false_claims':
        raw['validation']['claims_valid'] = False
    elif mutation == 'no_execution':
        raw['metadata']['analysis_execution_status'] = 'NOT_REQUESTED'
    elif mutation == 'wrong_niche':
        raw['niche'] = 'Unrelated niche'
    elif mutation == 'broken_fact':
        raw['analysis']['product_ideas'][0]['why_now']['fact_ids'] = ['fake']
    elif mutation == 'broken_evidence':
        raw['evidence'] = []
    elif mutation == 'unsafe_url':
        for evidence in raw['evidence']:
            evidence['source_url'] = 'javascript:alert(1)'
    if mutation != 'missing':
        path.write_text('{' if mutation == 'malformed' else json.dumps(raw), encoding='utf-8')
    run, problem = load_golden_insights(ArtifactStore(tmp_path))
    assert run is None and problem


@pytest.mark.parametrize('title,niche,status', [
    ('Cozy Critter Caravan', 'cozy-adults', 'Ứng viên sơ bộ · cần rà soát'),
    ('Catholic Girl: Cute Cozy', 'cozy-adults', 'Ứng viên sơ bộ · cần rà soát'),
    ('Cozy Coloring Book for Adults', 'cozy-adults', 'Khớp chủ đề rõ trong tiêu đề'),
    ('Bold & Easy Colouring Book', 'bold-easy', 'Khớp chủ đề rõ trong tiêu đề'),
    ('Christmas Coloring for Adults', 'christmas-adults', 'Khớp chủ đề rõ trong tiêu đề'),
    ('Christmas Coloring Book for Kids', 'kids-christmas', 'Khớp chủ đề rõ trong tiêu đề'),
    ('History of Ships', 'kids-christmas', 'Chưa đủ bằng chứng liên quan'),
    ('Cozy History', 'bold-easy', 'Chưa đủ bằng chứng liên quan'),
])
def test_conservative_title_review_never_confirms_competitors(title, niche, status):
    observed, reason = review_title(title, niche)
    assert observed == status and reason
    assert 'đối thủ đã xác nhận' not in observed


def test_review_is_presentation_only_and_original_counts_remain(views):
    _, batch, _ = views
    original = copy.deepcopy(batch)
    for key in batch.niches:
        rows = reviewed_catalog_rows(batch, key)
        assert len(rows) == len(batch.catalogs[key].items)
        assert all(row['Nguồn'].startswith('https://openlibrary.org/works/') for row in rows)
    assert batch == original


def test_catalog_table_is_concise_and_full_relevance_reason_is_retrievable(views):
    _, batch, _ = views
    for niche_id in batch.niches:
        rows = reviewed_catalog_rows(batch, niche_id)
        table = catalog_table_rows(rows)
        assert all(tuple(row) == CATALOG_COLUMNS for row in table)
        assert all('Căn cứ' not in row and 'Nhãn collector gốc' not in row for row in table)
        for i, row in enumerate(rows):
            detail = catalog_detail(rows, i)
            assert detail['Tên sách'] == row['Tên sách'] and detail['Căn cứ'] == row['Căn cứ']
            assert detail['Nhãn collector gốc'] == row['Nhãn collector gốc']
            assert detail['Nguồn'] == row['Nguồn'] and detail['Căn cứ']
    with pytest.raises(IndexError):
        catalog_detail([], 0)


def test_temporary_artifact_bytes_and_download_paths_are_valid(views):
    store, batch, comp = views
    assert store.read(f'batches/{batch.run_id}/summary.json') == (store.outputs / 'batches' / batch.run_id / 'summary.json').read_bytes()
    source_path = store.child(f'comparisons/{comp.run_id}', comp.report['artifacts']['source_csv'])
    assert hashlib.sha256(store.read(source_path)).hexdigest() == comp.dataset.csv_sha256


def html_content(app):
    return '\n'.join(element.proto.body for element in app.get('html'))


def test_distinct_pages_sidebar_context_and_missing_historical_ai_apptest(views, app_artifact_root):
    from streamlit.testing.v1 import AppTest
    _, batch, comparison = views
    app = AppTest.from_file(str(ROOT / 'dashboard.py')).run(timeout=30)
    assert not app.exception and not app.get('vega_lite_chart')
    markup = html_content(app)
    assert 'AI Research Insights' in markup and '<table class="ml-matrix">' in markup
    assert app.warning and 'provenance: ollama / success' not in '\n'.join(c.value for c in app.caption)
    assert 'cozy coloring books for adults' in markup
    assert batch.run_id in [c.value for c in app.code]
    assert comparison.run_id in [c.value for c in app.code]
    assert comparison.dataset.dataset_id in [c.value for c in app.code]
    assert next(b for b in app.sidebar.button if b.label == 'Tổng quan').proto.type == 'primary'
    assert any(b.label == 'Đọc lại artifacts' for b in app.sidebar.button)
    selector = next(widget for widget in app.sidebar.selectbox if widget.label == 'Comparison tương thích')
    assert selector.value == comparison.run_id
    if len(selector.options) > 1:
        choices = [key for key, view in ArtifactStore(app_artifact_root).discover().comparisons.items() if view.batch_id == batch.run_id]
        selector.set_value(choices[-1]).run(timeout=30)
        assert not app.exception and choices[-1] in [c.value for c in app.code]
    for key in comparison.metrics:
        next(b for b in app.sidebar.button if b.label == 'Khám phá niche').click().run(timeout=30)
        next(w for w in app.selectbox if w.label == 'Chọn niche').set_value(key).run(timeout=30)
        assert not app.exception
        spec = json.loads(app.get('vega_lite_chart')[0].proto.spec)
        assert spec['mark']['type'] == 'rect'
        assert ('AI Research Insights' in html_content(app)) == (key == 'cozy-adults')
    next(w for w in app.selectbox if w.label == 'Chọn niche').set_value('cozy-adults').run(timeout=30)
    catalog_table = next(frame.value for frame in app.dataframe if 'Rà soát hiển thị' in frame.value.columns)
    assert list(catalog_table.columns) == list(CATALOG_COLUMNS)
    assert 'Căn cứ' not in catalog_table.columns
    assert next(w for w in app.selectbox if w.label == 'Xem lý do rà soát').value == 0
    assert any(expander.label == 'Chi tiết căn cứ, metadata và nhãn nguồn' for expander in app.expander)
    next(b for b in app.sidebar.button if b.label == 'Đánh giá cơ hội').click().run(timeout=30)
    assert not app.exception and not app.get('vega_lite_chart')
    assert 'Chưa đủ bằng chứng để xếp hạng' in html_content(app)
    assert comparison.report['commercial_assessment']['market_opportunity_rank'] is None
    next(b for b in app.sidebar.button if b.label == 'Bằng chứng & truy vết').click().run(timeout=30)
    assert not app.exception and len(app.get('download_button')) >= 6
    assert batch.run_id in [c.value for c in app.code]
    raw_json = '\n'.join(element.value for element in app.json)
    assert 'NOT_REQUESTED' in raw_json and 'NOT_RUN' in raw_json


def test_standardized_display_labels_keep_commercial_raw_unchanged(views):
    _, _, comparison = views
    original = copy.deepcopy(comparison.report)
    labels = [commercial_label(v) for v in comparison.report['commercial_assessment']['missing_dimensions']]
    assert 'Doanh số' in labels and any('tình trạng bán lẻ tại Hoa Kỳ' in v for v in labels)
    assert label('NOT_REQUESTED') == 'Chưa yêu cầu chạy mô hình'
    assert label('NOT_RUN') == 'Chưa thực hiện kiểm định'
    assert comparison.report == original


def test_empty_heatmap_and_broken_ai_preview_are_graceful(views):
    from streamlit.testing.v1 import AppTest
    _, _, comparison = views
    empty = copy.deepcopy(comparison)
    for series in empty.dataset.series:
        series.observations = []
    assert weekly_heatmap(empty, 'cozy-adults') is None
    app = AppTest.from_string('''
from market_research.dashboard_ui import show_ai_insights
show_ai_insights(None, "Artifact lịch sử bị thiếu hoặc không hợp lệ")
''').run(timeout=30)
    assert not app.exception and len(app.warning) == 1
    assert not app.dataframe and 'Ollama · thực thi thành công' not in html_content(app)
