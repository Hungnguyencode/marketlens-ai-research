"""Streamlit artifact viewer. Start with: python -m streamlit run dashboard.py"""
import os
from pathlib import Path

import streamlit as st

from market_research.dashboard_data import ArtifactError, ArtifactStore, default_selection
from market_research.dashboard_design import badge, date_label, hero, run_label
from market_research.dashboard_insights import load_golden_insights
from market_research.dashboard_ui import show_ai_insights, show_overview, show_keywords, show_niche, show_opportunity


SOURCE_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY = Path(os.environ.get("MARKETLENS_ARTIFACTS_DIR", SOURCE_DIRECTORY)).resolve()
PAGES = ["Tổng quan", "Phân tích từ khóa", "Khám phá niche", "Đánh giá cơ hội", "Bằng chứng & truy vết", "Không gian nghiên cứu"]


@st.cache_data(ttl=10, max_entries=4, show_spinner=False)
def cached_index(repository: str, fingerprint: tuple):
    return ArtifactStore(repository).discover()


@st.cache_data(ttl=10, max_entries=4, show_spinner=False)
def cached_historical(repository: str, run_id: str, fingerprint: tuple):
    return ArtifactStore(repository).load_historical(run_id)


@st.cache_data(ttl=10, max_entries=4, show_spinner=False)
def cached_insights(repository: str, fingerprint: tuple):
    return load_golden_insights(ArtifactStore(repository))


def download(store, relative, label, key):
    try:
        content = store.read(relative)
        mime = "application/json" if relative.endswith(".json") else "text/markdown" if relative.endswith(".md") else "application/octet-stream"
        st.download_button(label, content, file_name=Path(relative).name, mime=mime, key=key, width="content")
    except (OSError, ArtifactError):
        st.caption(f"Không có file tải xuống: {Path(relative).name}")


def download_group(store, items, columns=2):
    """Keep related downloads compact while allowing Streamlit to stack on phones."""
    for start in range(0, len(items), columns):
        slots = st.columns(min(columns, len(items) - start), gap="small")
        for slot, (relative, title, key) in zip(slots, items[start:start + columns]):
            with slot:
                download(store, relative, "Tải " + title, key)


def show_historical(store, index, fingerprint):
    st.markdown("**Research Engine v1 — phân tích lịch sử riêng**")
    choices = ["Không mở"] + list(index.historical)
    run_id = st.selectbox("Mở historical single-niche run", choices)
    if run_id == "Không mở":
        return
    try:
        run = cached_historical(str(store.repository), run_id, fingerprint)
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f"Run lịch sử không đọc được: {str(exc)[:300]}")
        return
    st.info("Đây là historical single-niche analysis riêng. Các trạng thái này không áp dụng cho batch bốn niche đang chọn.")
    st.write(f"Run {run.run_id} · {run.niche}")
    st.write(f"Validation: {run.validation.status} · claims_valid={run.validation.claims_valid}")
    st.caption(f"Analysis provenance: {run.metadata.get('analysis_provenance', 'Không ghi')} · "
               f"Execution: {run.metadata.get('analysis_execution_status', 'Không ghi')} · {run.finished_at.isoformat()}")
    show_ai_insights(run)
    path = index.historical[run_id]
    download(store, path, "Tải historical JSON", "historical-json")
    download(store, str(Path(path).with_suffix(".md")), "Tải historical Markdown", "historical-md")


def show_audit(store, index, batch, comparison, fingerprint):
    st.html(badge("DETERMINISTIC_SUMMARY", "blue") + badge("NOT_REQUESTED") + badge("NOT_RUN"))
    st.write("Đã đối chiếu ID, schema và checksum trước khi ghép batch với comparison. "
             "Kiểm tra tính toàn vẹn artifact không đồng nghĩa kiểm định claim bởi mô hình.")
    with st.expander("Context & ID đầy đủ · sao chép bằng nút trên khối mã"):
        st.caption("Batch ID")
        st.code(batch.run_id, language=None, wrap_lines=True)
        if comparison:
            st.caption("Comparison ID")
            st.code(comparison.run_id, language=None, wrap_lines=True)
            st.caption("Dataset ID")
            st.code(comparison.dataset.dataset_id, language=None, wrap_lines=True)
        st.json({key: {"analysis_status": niche.analysis_status, "llm_execution_status": niche.llm_execution_status,
            "claim_validation_status": niche.claim_validation_status} for key, niche in batch.niches.items()})
    st.subheader("Tải artifacts gốc")
    batch_tab, comparison_tab = st.tabs(["Batch & tổng hợp", "Comparison & dữ liệu nguồn"])
    with batch_tab:
        download_group(store, [
            (f"batches/{batch.run_id}/summary.json", "Batch JSON", "batch-json"),
            (f"batches/{batch.run_id}/summary.md", "Batch Markdown", "batch-md")])
    if comparison:
        downloads = []
        with st.expander("Checksum, export settings và nguồn khai báo"):
            st.caption("SHA-256 CSV gốc; checksum các batch artifacts cũng đã đối chiếu trước khi ghép.")
            st.code(comparison.dataset.csv_sha256, language=None)
            st.json(comparison.dataset.declared_export_settings)
            st.json(comparison.report["commercial_assessment"])
            st.caption("Các thiết lập do người dùng khai báo không được coi là xác thực độc lập từ CSV. "
                       "URL nguồn: " + (comparison.dataset.source_url or "Chưa được cung cấp"))
        for name, title in [("comparison", "Comparison JSON"), ("report", "Report Markdown"),
                            ("dataset", "Dataset audit JSON"), ("source_csv", "CSV gốc")]:
            path = store.child(f"comparisons/{comparison.run_id}", comparison.report["artifacts"][name])
            downloads.append((path, title, "comparison-" + name))
        with comparison_tab:
            download_group(store, downloads)
    else:
        with comparison_tab:
            st.info("Chưa có comparison tương thích để tải.")
    with st.expander("Nguồn từng niche và file audit"):
        for niche_id, niche in batch.niches.items():
            st.markdown("**" + niche.name + "**")
            st.json({"query": niche.catalog.query, "source_url": niche.catalog.request_url,
                     "retrieved_at": str(niche.catalog.retrieved_at), "provenance": niche.catalog.provenance,
                     "missing_data": niche.missing_data})
            niche_downloads = [(batch.paths[niche_id], "Result JSON", niche_id + "-result")]
            for name in ("catalog_snapshot", "trends_evidence", "verified_facts", "summary"):
                if name in niche.artifacts:
                    relative = store.child(str(Path(batch.paths[niche_id]).parent.as_posix()), niche.artifacts[name])
                    niche_downloads.append((relative, name, niche_id + "-" + name))
            download_group(store, niche_downloads)
    with st.expander("Research Engine v1 / historical Ollama"):
        show_historical(store, index, fingerprint)
    if index.issues:
        with st.expander("Artifact bị loại khỏi lựa chọn"):
            for issue in index.issues:
                st.write(issue)


def select_page(page):
    st.session_state["page"] = page
    if page == "Không gian nghiên cứu":
        st.query_params["workspace"] = "research"
    elif "workspace" in st.query_params:
        del st.query_params["workspace"]


def main():
    # Streamlit's integer form preserves the 288px desktop width and keeps AUTO
    # behavior on narrow viewports; unlike the old CSS width override it stays
    # inside the native responsive sidebar.
    st.set_page_config(page_title="MarketLens · Evidence Research", page_icon="📊", layout="wide",
                       initial_sidebar_state=288)
    st.html("<style>" + (SOURCE_DIRECTORY / "dashboard.css").read_text(encoding="utf-8") + "</style>")
    st.sidebar.html('<div class="ml-brand">Market<span>Lens</span></div><div class="ml-brand-sub">EVIDENCE-FIRST RESEARCH</div>')
    if st.session_state.get("page") not in PAGES:
        st.session_state["page"] = PAGES[-1] if st.query_params.get("workspace") == "research" else PAGES[0]
    st.sidebar.html('<div class="ml-sidebar-label">KHÔNG GIAN NGHIÊN CỨU</div>')
    for name in PAGES:
        st.sidebar.button(name, key="nav-" + name, width="stretch",
            type="primary" if st.session_state["page"] == name else "secondary", on_click=select_page, args=(name,))
    page = st.session_state["page"]
    if page == "Không gian nghiên cứu":
        from market_research.dashboard_workspace import show_workspace
        show_workspace()
        return
    try:
        store = ArtifactStore(REPOSITORY)
        fingerprint = store.fingerprint()
        if st.sidebar.button("Đọc lại artifacts", width="stretch", help="Xóa cache và đọc lại file có sẵn; không chạy nghiên cứu mới."):
            st.cache_data.clear()
        index = cached_index(str(REPOSITORY), fingerprint)
    except (OSError, ArtifactError) as exc:
        st.error(f"Không thể đọc thư mục artifacts: {exc}")
        return
    if not index.batches:
        st.title("Chưa có batch nghiên cứu hợp lệ")
        st.info("Đặt batch artifacts tại outputs/batches/. Dashboard chỉ đọc file có sẵn; không tự gọi collector hoặc Ollama.")
        for issue in index.issues:
            st.warning(issue)
        if page == "Bằng chứng & truy vết" and index.historical:
            show_historical(store, index, fingerprint)
        return
    preferred, _ = default_selection(index)
    ids = list(index.batches)
    with st.sidebar.expander("Phiên dữ liệu & nguồn", expanded=False):
        batch_id = st.selectbox("Batch được chọn", ids, index=ids.index(preferred), format_func=run_label,
            help="ID đầy đủ nằm dưới selector và trong Audit. Dashboard luôn kiểm tra ID/checksum tham chiếu.")
        batch = index.batches[batch_id]
        compatible = [key for key, view in index.comparisons.items() if view.batch_id == batch_id]
        comparison_id = st.selectbox("Comparison tương thích", compatible, format_func=run_label) if compatible else None
        st.caption("Batch ID đầy đủ")
        st.code(batch_id, language=None, wrap_lines=True)
        if comparison_id:
            st.caption("Comparison ID đầy đủ")
            st.code(comparison_id, language=None, wrap_lines=True)
    comparison = index.comparisons.get(comparison_id)
    st.sidebar.caption("Chỉ đọc artifacts · không chạy inference hoặc collection")
    st.sidebar.caption(f"Cache tối đa 10 giây · Đọc lúc {date_label(index.loaded_at, True)}")
    introductions = {
        "Tổng quan": "Trung tâm nghiên cứu bốn niche: quan sát tín hiệu, theo dõi độ phủ và quyết định dựa trên bằng chứng.",
        "Phân tích từ khóa": "Khám phá cùng một joint export: thang so sánh chung, độ thưa và những diễn giải được hỗ trợ.",
        "Khám phá niche": "Từ tín hiệu tìm kiếm đến nguồn thư mục: xem dữ liệu thật và nhận diện điều còn cần xác minh.",
        "Đánh giá cơ hội": "Phân biệt bằng chứng quan sát, giả thuyết sản phẩm và điều kiện để đánh giá thương mại.",
        "Bằng chứng & truy vết": "Kiểm tra nguồn, context, raw statuses và tải artifacts; historical Ollama được giữ riêng."}
    st.html(hero(page, introductions[page]))
    if index.issues:
        st.caption(f"{len(index.issues)} artifact không tương thích đã bị loại; xem Bằng chứng & truy vết.")
    insights, insights_problem = cached_insights(str(REPOSITORY), fingerprint) if page in {"Tổng quan", "Khám phá niche"} else (None, None)
    if page == "Tổng quan":
        show_overview(batch, comparison, insights, insights_problem)
    elif page == "Phân tích từ khóa":
        show_keywords(comparison)
    elif page == "Khám phá niche":
        show_niche(batch, comparison, insights, insights_problem)
    elif page == "Đánh giá cơ hội":
        show_opportunity(batch, comparison)
    else:
        show_audit(store, index, batch, comparison, fingerprint)


if __name__ == "__main__":
    main()
