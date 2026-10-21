"""Typed chart data and scoped Altair interaction; no metric recalculation."""
import altair as alt
import pandas as pd

from .dashboard_data import chart_rows


SERIES_COLORS = {"cozy-adults": "#3276E8", "bold-easy": "#0B9D91",
                 "christmas-adults": "#C4892F", "kids-christmas": "#8C6BD1"}


def chart_frame(comparison, selected, include_incomplete=False):
    if not set(selected) <= set(comparison.metrics):
        raise ValueError("Bộ lọc keyword không thuộc comparison được chọn")
    rows = chart_rows(comparison, selected, include_incomplete)
    frame = pd.DataFrame(rows).rename(columns={"Ngày": "date", "Keyword": "keyword",
        "Index tương đối": "relative_index", "Kỳ quan sát": "period_label"})
    if frame.empty:
        return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "keyword": pd.Series(dtype="str"),
            "relative_index": pd.Series(dtype="float64"), "complete_week": pd.Series(dtype="bool"),
            "period_label": pd.Series(dtype="str"), "partial_segment": pd.Series(dtype="bool")})
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
    frame["relative_index"] = pd.to_numeric(frame["relative_index"], errors="raise").astype("float64")
    frame["complete_week"] = frame["complete_week"].astype("bool")
    frame = frame.sort_values(["keyword", "date"]).reset_index(drop=True)
    frame["partial_segment"] = ~frame["complete_week"]
    for _, group in frame.groupby("keyword", sort=False):
        if (~group["complete_week"]).any() and group["complete_week"].any():
            frame.loc[group[group["complete_week"]].index[-1], "partial_segment"] = True
    return frame


def timeline_chart(comparison, selected, include_incomplete=False, focus=False):
    frame = chart_frame(comparison, selected, include_incomplete)
    if frame.empty:
        return None
    if focus and len(selected) != 1:
        raise ValueError("Focus mode chỉ dành cho một keyword")
    active = [comparison.metrics[key].keyword for key in comparison.metrics if key in selected]
    colors = {comparison.metrics[key].keyword: SERIES_COLORS[key] for key in comparison.metrics}
    yscale = alt.Scale(zero=True, nice=True) if focus else alt.Scale(domain=[0, 100], nice=False)
    base = alt.Chart(frame).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%m/%Y", labelAngle=0, tickCount=7)),
        y=alt.Y("relative_index:Q", title="Index tương đối" + (" · trục phóng đại" if focus else " · thang đo chung 0–100"), scale=yscale),
        color=alt.Color("keyword:N", title=None, scale=alt.Scale(domain=active, range=[colors[k] for k in active]),
                        legend=alt.Legend(orient="bottom", columns=1, labelLimit=360)),
        tooltip=[alt.Tooltip("keyword:N", title="Keyword"), alt.Tooltip("date:T", title="Tuần", format="%d/%m/%Y"),
                 alt.Tooltip("relative_index:Q", title="Index", format=".2f"),
                 alt.Tooltip("period_label:N", title="Trạng thái kỳ")])
    # Attach the interval to ONE named unit. A global interval on multiple layers produced duplicate Vega signals.
    zoom = alt.selection_interval(name="timeline_zoom", bind="scales", encodings=["x"])
    line = base.transform_filter(alt.datum.complete_week).mark_line(strokeWidth=2.7, clip=True).properties(
        name="completed_timeline").add_params(zoom)
    points = base.transform_filter(alt.datum.complete_week).mark_circle(size=26, opacity=0.65, clip=True)
    partial = base.transform_filter(alt.datum.partial_segment).mark_line(strokeWidth=2, strokeDash=[5, 4], clip=True)
    partial_points = base.transform_filter(~alt.datum.complete_week).mark_point(
        shape="diamond", filled=False, size=130, strokeWidth=2.5, clip=True)
    return (line + points + partial + partial_points).properties(height=360).configure_view(stroke=None).configure_axis(
        gridColor="#EAF0F6", domain=False, tickColor="#D6E0EB", labelColor="#64748B", titleColor="#64748B",
        labelFont="Segoe UI", titleFont="Segoe UI", labelFontSize=13, titleFontSize=14, labelPadding=9, titlePadding=15).configure_legend(
        labelFont="Segoe UI", labelFontSize=13, labelColor="#51637B", symbolType="stroke", symbolStrokeWidth=3)


def weekly_heatmap(comparison, niche_id):
    """One cell per completed observation, chronological 13-column grid.

    No aggregation, interpolation, missing-cell filling or independent normalization.
    Cell coordinates express observation order, not a fabricated calendar weekday.
    """
    frame = chart_frame(comparison, [niche_id])
    if frame.empty:
        return None
    frame["position"] = frame.index % 13 + 1
    frame["block"] = [f"Nhóm {i // 13 + 1}" for i in frame.index]
    return alt.Chart(frame).mark_rect(cornerRadius=4, stroke="#fff", strokeWidth=3).encode(
        x=alt.X("position:O", title="Thứ tự tuần trong nhóm (từ cũ đến mới)", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("block:O", title=None),
        color=alt.Color("relative_index:Q", title="Index · 0–100", scale=alt.Scale(domain=[0, 100], range=["#eaf0f8", "#2459a5"]),
                        legend=alt.Legend(orient="bottom", gradientLength=160)),
        tooltip=[alt.Tooltip("keyword:N", title="Keyword"), alt.Tooltip("date:T", title="Tuần", format="%d/%m/%Y"),
                 alt.Tooltip("relative_index:Q", title="Index"), alt.Tooltip("period_label:N", title="Kỳ")]
    ).properties(height=210).configure_view(stroke=None).configure_axis(
        labelFont="Segoe UI", titleFont="Segoe UI", labelFontSize=13, titleFontSize=14,
        labelPadding=8, titlePadding=14, domain=False).configure_legend(labelFontSize=13, titleFontSize=14)
