"""Five-page app's evidence-aware views; stateful controls stay native Streamlit."""
import streamlit as st

from .dashboard_charts import timeline_chart, weekly_heatmap
from .dashboard_design import badge, date_label, label, matrix_html, metric_cards, note, panel, pipeline_html, section
from .dashboard_insights import (catalog_detail, catalog_table_rows, historical_problem,
                                 idea_references, reviewed_catalog_rows)


def heading(title, description=""):
    st.html(section(title, description))


def kpis(values):
    st.html(metric_cards(values))


def gate_notice(comparison):
    if comparison is None:
        st.html(note("Chưa có comparison tương thích", "Chọn một phiên có joint comparison hợp lệ để xem tín hiệu; không thay bằng CSV standalone.", "neutral"))
        return
    raw = comparison.report["commercial_assessment"]["status"]
    st.html(note(label(raw) + " để xếp hạng thương mại",
        "Evidence gate đang chủ động giữ quyết định. Tín hiệu tìm kiếm và metadata thư mục chưa xác minh được nhu cầu mua.", "amber"))


def time_chart(comparison, selected, include_incomplete=False, focus=False, key="timeline"):
    if comparison is None:
        st.info("Chưa có dataset so sánh chung phù hợp với batch này.")
        return
    try:
        chart = timeline_chart(comparison, selected, include_incomplete, focus)
    except ValueError as exc:
        st.warning(str(exc))
        return
    if chart is None:
        st.info("Chọn ít nhất một keyword có observations để xem biểu đồ.")
        return
    st.altair_chart(chart, width="stretch", key=key, theme=None)
    if focus:
        st.caption("TRỤC Y PHÓNG ĐẠI · Tự chọn miền theo một keyword. Không so trực tiếp độ cao với biểu đồ thang đo chung 0–100.")
    else:
        st.caption("Thang đo chung 0–100 · Kéo/zoom theo thời gian; hover vào các điểm để xem tuần, index và trạng thái kỳ.")


def source_caption(comparison):
    sample = next(iter(comparison.metrics.values()))
    partial = ", ".join(date_label(value) for value in sample.excluded_incomplete_weeks)
    st.caption(f"{sample.completed_weeks}/{sample.observed_weeks} tuần hoàn tất · "
        f"Tuần chưa hoàn tất: {partial or 'Không có'} · Vùng: Hoa Kỳ · Một export so sánh chung.")


def show_ai_insights(run, problem=None, preview=False):
    heading("AI Research Insights", "Phân tích một niche trong lịch sử · tách biệt với batch bốn niche đang chọn.")
    problem = problem or (historical_problem(run) if run else "Không có artifact AI hợp lệ để hiển thị.")
    if problem:
        st.warning(problem)
        return
    st.html(badge("Ollama · thực thi thành công", "teal") + badge("VALID", "teal") + badge("Giả thuyết · cần kiểm chứng", "amber"))
    st.caption(f"Run {run.run_id} · {run.niche} · {date_label(run.finished_at, True)} · provenance: ollama / success. "
               "Đọc kết quả đã lưu; không gọi mô hình. Không áp trạng thái này cho batch bốn niche.")
    st.html(note("Quan sát và giả thuyết có vai trò khác nhau",
        "Thời điểm nghiên cứu dựa trên facts của CSV riêng trong run lịch sử. Khách hàng, concept và khác biệt là giả thuyết; "
        "không phải nhu cầu mua đã xác minh. Không so số index này với joint export bốn niche.", "blue"))
    for i, idea in enumerate(run.analysis.product_ideas, 1):
        st.html(panel(idea.what_to_sell, [idea.target_customer], f"GIẢ THUYẾT SẢN PHẨM {i} · OLLAMA LỊCH SỬ"))
        with st.expander(f"Thời điểm, bất định & nguồn · ý tưởng {i}", expanded=not preview):
            st.html(panel("Căn cứ thời điểm · facts gắn bởi Python", [idea.why_now.text]))
            st.html(panel("Khác biệt đề xuất · giả thuyết", [idea.differentiation]))
            st.html(panel("Điều chưa biết", idea.uncertainties or ["Artifact không ghi bất định; cần rà soát thêm."]))
            facts, sources = idea_references(run, idea)
            st.caption("Facts và evidence gốc dưới đây thuộc run lịch sử, không ghép vào comparison hiện tại.")
            st.dataframe([{"Fact ID": f.fact_id, "Keyword": f.keyword, "Metric": f.metric_name,
                "Giá trị": str(f.value), "Đơn vị": f.unit, "Kỳ": f.observation_period,
                "Evidence IDs": ", ".join(f.evidence_ids)} for f in facts], hide_index=True, width="stretch")
            st.dataframe([{"Evidence ID": item.evidence_id, "Nguồn": item.source_name,
                "URL nguồn": item.source_url, "Kỳ": item.observation_period, "Giá trị": str(item.metric_value),
                "Cách thu thập": item.extraction_method} for item in sources], hide_index=True, width="stretch",
                column_config={"URL nguồn": st.column_config.LinkColumn(display_text="Mở nguồn ↗")})


def coverage_rows(batch, comparison):
    rows = []
    for key, niche in batch.niches.items():
        snapshot = batch.catalogs.get(key)
        catalog = (f"{len(snapshot.items)} bản ghi · {label(niche.catalog.status)}", "teal") if snapshot and snapshot.items else ("Chưa có snapshot hợp lệ", "amber")
        measured = comparison and comparison.metrics[key].completed_weeks > 0
        rows.append((niche.name, [catalog,
            ("Có dữ liệu đo" if measured else "Chưa có comparison đo được", "blue" if measured else "neutral"),
            (label(niche.llm_execution_status), "neutral"), (label(niche.claim_validation_status), "neutral")]))
    return rows


def show_overview(batch, comparison, insights=None, insights_problem=None):
    total = sum(n.catalog.parsed_items for n in batch.niches.values())
    candidates = sum(n.catalog.relevance_candidates for n in batch.niches.values())
    complete = next(iter(comparison.metrics.values())).completed_weeks if comparison else "—"
    kpis([("Niche được theo dõi", len(batch.niches), "Bằng chứng độc lập theo từng niche"),
          ("Bản ghi thư mục", total, "Metadata đã parse trong batch nguồn"),
          ("Ứng viên từ collector", candidates, "Nhãn gốc · cần rà soát liên quan"),
          ("Tuần so sánh hoàn tất", complete, "Được tính trên cùng export context")])
    st.html(pipeline_html(batch, comparison))
    heading("Độ phủ nghiên cứu", "Trạng thái theo nguồn, không phải điểm chất lượng hoặc quy mô thị trường. Bảng có thể cuộn ngang.")
    st.html(matrix_html("Bằng chứng & thực thi trong batch đang chọn",
        ["Niche", "Open Library · metadata", "Google Trends · joint CSV", "Mô hình", "Kiểm định claim"], coverage_rows(batch, comparison)))
    gate_notice(comparison)
    panels = [panel("Open Library", [f"{total} bản ghi / {candidates} candidates trong batch nguồn.",
        "Truy xuất gần nhất: " + date_label(max((n.catalog.retrieved_at for n in batch.niches.values() if n.catalog.retrieved_at), default=None), True),
        "Metadata tác phẩm; chưa xác nhận sản phẩm đang bán tại Hoa Kỳ."], "CATALOG DISCOVERY")]
    if comparison:
        settings = comparison.dataset.declared_export_settings
        panels.append(panel("Google Trends", ["Một joint export trên cùng thang index tương đối.",
            date_label(settings["period_start"]) + " → " + date_label(settings["period_end"]),
            "Index 0 không chứng minh zero searches, buyers hoặc demand."], "SEARCH INTEREST"))
    st.html('<div class="ml-grid">' + "".join(panels) + '</div>')
    with st.expander("Context phiên đang xem · ID đầy đủ có thể sao chép"):
        st.caption("Batch nguồn")
        st.code(batch.run_id, language=None)
        if comparison:
            st.caption("Comparison tương thích · cùng thang đo chung")
            st.code(comparison.run_id, language=None)
            st.code(comparison.dataset.dataset_id, language=None)
    show_ai_insights(insights, insights_problem, preview=True)
    if batch.catalog_issues:
        st.info("Một số snapshot thiếu hoặc không khớp. Counts vẫn ghi theo result; chi tiết trong Bằng chứng & truy vết.")


def show_keywords(comparison):
    if comparison is None:
        gate_notice(None)
        return
    metrics = comparison.metrics
    selected = st.multiselect("Keyword hiển thị", list(metrics), default=list(metrics), format_func=lambda key: metrics[key].keyword)
    include = st.checkbox("Hiện observations chưa hoàn tất trên biểu đồ", value=False)
    with st.container(border=True):
        time_chart(comparison, selected, include, key="keyword-timeline")
        source_caption(comparison)
    heading("Đọc đúng tín hiệu", "Relative search interest không phải absolute search volume hoặc purchase intent.")
    st.dataframe([{"Keyword": m.keyword, "Index trung bình": m.mean_relative_index, "Đỉnh quan sát": m.maximum_relative_index,
        "Tuần dương / hoàn tất": f"{m.positive_completed_weeks}/{m.completed_weeks}",
        "% tuần dương": m.positive_completed_percentage, "Chất lượng tín hiệu": label("SPARSE_SIGNAL") if m.sparse_signal else "Liên tục trong mẫu"}
        for m in metrics.values()], hide_index=True, width="stretch", column_config={
            "Index trung bình": st.column_config.NumberColumn(format="%.2f"), "% tuần dương": st.column_config.NumberColumn(format="%.2f%%")})
    sparse = [m.keyword for m in metrics.values() if m.sparse_signal]
    if sparse:
        st.html(note("Tín hiệu thưa · cần kiểm tra cách viết truy vấn", "; ".join(sparse) +
            ". Ít tuần có index dương; chưa đủ cơ sở diễn giải delta thành tăng trưởng hoặc xem keyword đại diện toàn niche.", "amber"))
    with st.expander("Phóng đại một keyword để quan sát tín hiệu nhỏ"):
        focus_id = st.selectbox("Keyword cần xem kỹ", list(metrics), format_func=lambda key: metrics[key].keyword)
        time_chart(comparison, [focus_id], include, focus=True, key="keyword-focus")
    heading("Hai cửa sổ bốn tuần", "Chỉ hiển thị diễn giải khi metric được đánh dấu hỗ trợ trong artifact.")
    supported = [m for m in metrics.values() if m.window_interpretation == "SUPPORTED"]
    if supported:
        st.dataframe([{"Keyword": m.keyword, "Bốn tuần trước": m.previous_four_week_average,
            "Bốn tuần gần nhất": m.recent_four_week_average, "Thay đổi (điểm index)": m.change_in_index_points,
            "Diễn giải": label(m.direction)} for m in supported], hide_index=True, width="stretch")
    else:
        st.info("Chưa có chuỗi đủ tín hiệu để diễn giải hai cửa sổ.")
    st.caption("Bật tuần chưa hoàn tất chỉ thay phần biểu đồ, không thay metric. Một chu kỳ năm chưa xác nhận seasonality; không trộn CSV cozy standalone.")


def show_niche(batch, comparison, insights=None, insights_problem=None):
    niche_id = st.selectbox("Chọn niche", list(batch.niches), format_func=lambda key: batch.niches[key].name)
    niche = batch.niches[niche_id]
    heading(niche.name, "Đối tượng dự kiến: " + niche.intended_audience)
    st.html(badge(niche.catalog.status, "teal" if niche.catalog.status == "success" else "neutral") +
            badge("DETERMINISTIC_SUMMARY", "blue") + badge("NOT_REQUESTED"))
    metric = comparison.metrics[niche_id] if comparison else None
    mean = f"{metric.mean_relative_index:.2f}" if metric and metric.mean_relative_index is not None else "—"
    kpis([("Bản ghi thư mục", niche.catalog.parsed_items, "Theo catalog snapshot"),
          ("Ứng viên từ collector", niche.catalog.relevance_candidates, "Giữ nguyên nhãn gốc · chưa xác nhận đối thủ"),
          ("Index tìm kiếm trung bình", mean, "Cùng joint export · chỉ tuần hoàn tất")])
    heading("Nhịp quan sát theo tuần", "Mỗi ô là một tuần hoàn tất, từ trái sang phải rồi xuống nhóm tiếp theo; màu dùng thang đo chung 0–100.")
    if comparison:
        chart = weekly_heatmap(comparison, niche_id)
        if chart is not None:
            st.altair_chart(chart, width="stretch", theme=None, key="niche-heatmap")
        source_caption(comparison)
        st.caption("Ô nhạt có thể là index 0 đã quan sát, không phải thiếu dữ liệu. Chỉ số tìm kiếm không đo doanh số.")
        with st.expander("Bảng quan sát và chỉ số của keyword"):
            from .dashboard_data import chart_rows
            st.dataframe(chart_rows(comparison, [niche_id]), hide_index=True, width="stretch")
            st.write("Keyword:", metric.keyword)
            st.write("Đỉnh quan sát:", metric.maximum_relative_index, "· Tuần dương:", metric.positive_completed_weeks,
                     "/", metric.completed_weeks)
    else:
        gate_notice(None)
    st.caption("Độ phủ: metadata thư mục / tín hiệu tìm kiếm nếu có; giá, doanh số và khách hàng chưa được đo trực tiếp.")
    if metric and metric.sparse_signal:
        st.html(note("Tín hiệu thưa", "Chưa đủ độ đại diện để suy ra trend growth hoặc nhu cầu mua.", "amber"))
    heading("Khám phá catalog", f"Query: {niche.catalog.query} · Truy xuất: {date_label(niche.catalog.retrieved_at, True)}")
    st.html(note("Rà soát liên quan dựa trên tiêu đề", "Khớp từ khóa là bước sàng lọc. Chưa xác nhận category sách tô màu hoặc đối thủ bán lẻ; "
        "cần metadata chủ đề/mô tả và kiểm tra thủ công. Counts collector gốc không bị sửa.", "amber"))
    only = st.checkbox("Chỉ hiển thị ứng viên theo nhãn collector gốc", value=False)
    books = reviewed_catalog_rows(batch, niche_id, only)
    if books:
        st.dataframe(catalog_table_rows(books), hide_index=True, width="stretch",
            column_order=["Tên sách", "Rà soát hiển thị", "Tác giả", "Xuất bản đầu", "Ấn bản ghi nhận", "Nguồn"],
            column_config={"Nguồn": st.column_config.LinkColumn("Nguồn", display_text="Mở Open Library ↗"),
                "Xuất bản đầu": st.column_config.NumberColumn("Năm", format="%d"),
                "Ấn bản ghi nhận": st.column_config.NumberColumn("Ấn bản", format="%d")})
        detail_idx = st.selectbox("Xem lý do rà soát", list(range(len(books))),
            format_func=lambda idx: f"Bản ghi {idx + 1} · {books[idx]['Tên sách'][:70]}" + ("…" if len(books[idx]["Tên sách"]) > 70 else ""),
            key="catalog-review-detail-" + niche_id)
        detail = catalog_detail(books, detail_idx)
        with st.expander("Chi tiết căn cứ, metadata và nhãn nguồn", expanded=False):
            st.markdown("**Tên sách đầy đủ**")
            st.write(detail["Tên sách"])
            st.markdown("**Trạng thái rà soát · dựa trên tiêu đề**")
            st.write(detail["Rà soát hiển thị"])
            st.markdown("**Lý do đầy đủ**")
            st.write(detail["Căn cứ"])
            st.markdown("**Nhãn gốc của collector**")
            st.write(detail["Nhãn collector gốc"])
            st.markdown("**Nguồn Open Library · URL đầy đủ**")
            st.code(detail["Nguồn"], language=None, wrap_lines=True)
            st.write("Tác giả:", detail["Tác giả"], "· Xuất bản đầu:", detail["Xuất bản đầu"],
                     "· Ấn bản thư mục:", detail["Ấn bản ghi nhận"])
    else:
        st.info("Chưa có metadata sách phù hợp để hiển thị từ snapshot.")
    if niche_id in batch.catalog_issues:
        st.warning(batch.catalog_issues[niche_id])
    st.caption("Ấn bản là số ghi nhận thư mục, không phải doanh số. Metadata không xác nhận tình trạng bán lẻ tại Hoa Kỳ hoặc giá.")
    heading("Khoảng trống & giả thuyết", "Giữ riêng điều đã quan sát với ý tưởng chưa được xác minh.")
    with st.expander("Dữ liệu còn thiếu", expanded=True):
        for gap in niche.missing_data:
            st.write("•", gap)
    for hypothesis in niche.candidate_product_hypotheses:
        st.html(panel(hypothesis.get("concept", "Giả thuyết chưa kiểm chứng"),
            [hypothesis.get("next_step", "Cần nghiên cứu thêm trước quyết định thương mại.")], "GIẢ THUYẾT · CHƯA KIỂM CHỨNG"))
    if niche_id == "cozy-adults":
        show_ai_insights(insights, insights_problem)
    else:
        st.info("Chưa có phân tích Ollama lịch sử được liên kết cho niche này. Ý tưởng cấu hình ở trên chưa qua mô hình hoặc kiểm định claim.")
    with st.expander("Nguồn và trạng thái raw của niche"):
        st.json({"niche_id": niche_id, "catalog_query": niche.catalog.query, "catalog_provenance": niche.catalog.provenance,
            "retrieved_at": str(niche.catalog.retrieved_at), "standalone_csv_status": niche.trends_status,
            "shared_comparison_id": comparison.run_id if comparison else None, "analysis_status": niche.analysis_status,
            "llm_execution_status": niche.llm_execution_status, "claim_validation_status": niche.claim_validation_status})


def show_opportunity(batch, comparison):
    gate_notice(comparison)
    if comparison is None:
        return
    decision = comparison.report["commercial_assessment"]
    heading("Bằng chứng nào đã sẵn sàng?", "Không dùng trọng số hoặc điểm cơ hội khi bằng chứng thương mại còn thiếu. Bảng có thể cuộn ngang.")
    dimensions = [
        ("Tín hiệu tìm kiếm", [("Có dữ liệu đo" if comparison.metrics[key].completed_weeks else "Chưa có dữ liệu đo", "blue") for key in batch.niches]),
        ("Metadata catalog", [("Có snapshot" if key in batch.catalogs else "Thiếu snapshot", "teal" if key in batch.catalogs else "amber") for key in batch.niches])]
    dimensions += [(commercial_label(name), [("Chưa đủ bằng chứng", "amber") for _ in batch.niches]) for name in decision["missing_dimensions"]]
    st.html(matrix_html("Độ phủ theo khía cạnh · chưa phải xếp hạng", ["Khía cạnh"] + [n.name for n in batch.niches.values()], dimensions))
    st.html('<div class="ml-grid">' + panel("Điều hệ thống biết", [
        "Tín hiệu của bốn exact search phrases trên cùng joint export.",
        "Catalog metadata và relevance candidates từ batch được tham chiếu.",
        "Keyword thưa và giới hạn representativeness đã được đánh dấu."], "BẰNG CHỨNG QUAN SÁT") +
        panel("Điều hệ thống chưa biết", [commercial_label(name) for name in decision["missing_dimensions"]], "CHƯA ĐO LƯỜNG") + '</div>')
    with st.expander("Vì sao evidence gate giữ quyết định?"):
        for reason in decision["reasons"]:
            st.write("•", commercial_text(reason))
        st.json({"commercial_status": decision["status"], "market_opportunity_rank": decision["market_opportunity_rank"]})
    heading("Nghiên cứu tiếp theo", "Các việc cần xác minh, không phải Top 3 cơ hội kinh doanh đã được chứng minh.")
    cards = [panel(commercial_text(action["action"]), ["Niche: " + ", ".join(batch.niches[k].name for k in action["niche_ids"] if k in batch.niches)],
                   "ƯU TIÊN NGHIÊN CỨU") for action in decision["research_priorities"]]
    st.html('<div class="ml-grid">' + "".join(cards) + '</div>')
    with st.expander("Thứ tự mô tả tín hiệu keyword"):
        st.write(" → ".join(comparison.metrics[key].keyword for key in comparison.report["observed_keyword_signal_order"]))
        st.caption("observed_keyword_signal_order theo mean cùng export; không phải market_opportunity_rank. "
                   "Không dùng numFound, edition_count hoặc candidates để chấm điểm thị trường.")


def commercial_label(raw):
    return {"Giá retail quan sát được": "Giá bán lẻ quan sát được", "Bằng chứng sales": "Doanh số",
        "Customer reviews/nhu cầu đã kiểm chứng": "Đánh giá khách hàng / nhu cầu đã kiểm chứng",
        "Cạnh tranh liên quan và US retail availability đã xác minh": "Cạnh tranh liên quan / tình trạng bán lẻ tại Hoa Kỳ"}.get(raw, raw)


def commercial_text(raw):
    """Translate known explanatory fragments, leaving raw audit data untouched."""
    for before, after in [
        ("Search-interest index và catalog metadata không đủ hỗ trợ commercial opportunity ranking.",
         "Chỉ số tìm kiếm và metadata thư mục chưa đủ để xếp hạng cơ hội thương mại."),
        ("Open Library candidates không phải confirmed competitors; counts/edition_count không dùng làm điểm.",
         "Ứng viên Open Library chưa phải đối thủ đã xác nhận; số bản ghi và ấn bản không dùng làm điểm."),
        ("Sparse keyword signals:", "Tín hiệu từ khóa thưa:"),
        ("catalog coverage", "độ phủ thư mục"),
        ("Giá retail quan sát được", "Giá bán lẻ quan sát được"),
        ("Bằng chứng sales", "Doanh số"),
        ("Customer reviews/nhu cầu đã kiểm chứng", "Đánh giá khách hàng / nhu cầu đã kiểm chứng"),
        ("US retail availability", "tình trạng bán lẻ tại Hoa Kỳ"),
        ("availability, giá", "tình trạng bán, giá"),
        ("sales, customer reviews", "doanh số, đánh giá khách hàng"),
    ]:
        raw = raw.replace(before, after)
    return raw
