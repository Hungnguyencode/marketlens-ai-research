"""Static, read-only SVG of one existing export; no scripts, resources or new metrics."""
from datetime import date
from html import escape

from .report_intelligence import _point


def render_trend_chart(result, intelligence):
    if not intelligence["series"]:
        return ""
    series = intelligence["series"][0]  # Never overlay independently normalized exports.
    ids = set(series["measurements"][0]["evidence_ids"])
    records = [item for item in result.evidence if item.evidence_id in ids]
    points = [_point(item, date.fromisoformat(series["as_of"])) for item in records]
    if (not points or any(p["date"] is None for p in points)
            or len({p["date"] for p in points}) != len(points)
            or len(ids) != len(records) or not any(p["value"] is not None for p in points)):
        return '<p>Không vẽ biểu đồ: ngày/ID không rõ ràng hoặc không có giá trị số hợp lệ. Xem evidence gốc trong audit.</p>'
    points.sort(key=lambda point: point["date"])
    start, end = points[0]["date"], points[-1]["date"]
    span = max((end - start).days, 1)
    def x(point):
        return 64 + 704 * (point["date"] - start).days / span
    def y(point):
        return 224 - 1.68 * point["value"]
    targets = {item.evidence_id: f"evidence-{index}" for index, item in enumerate(result.evidence)}
    parts = ['<figure class="trend-chart"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 294" role="img" aria-labelledby="trend-title trend-description">',
             '<title id="trend-title">Index Google Trends theo tuần — ' + escape(series["keyword"]) + '</title>',
             '<desc id="trend-description">Thang index 0–100. Điểm đặc: tuần hoàn tất. Vòng rỗng và nét đứt: tuần chưa hoàn tất hoặc tương lai. Khoảng trống không được nội suy; không suy doanh số.</desc>',
             '<text x="64" y="20" font-size="13" fill="#14263e">Index tương đối (0–100), không phải search volume hoặc sales</text>']
    for tick in (0, 25, 50, 75, 100):
        axis_y = 224 - 1.68 * tick
        parts += [f'<line x1="64" y1="{axis_y:.2f}" x2="768" y2="{axis_y:.2f}" stroke="#dce5ed"/>',
                  f'<text x="52" y="{axis_y + 4:.2f}" text-anchor="end" font-size="12" fill="#526176">{tick}</text>']
    for point in points:
        if not point["complete"]:
            parts.append(f'<line class="incomplete-week-marker" data-evidence-id="{escape(point["record"].evidence_id, quote=True)}" x1="{x(point):.2f}" y1="56" x2="{x(point):.2f}" y2="224" stroke="#9b5700" stroke-dasharray="2 4" opacity="0.5"><title>{point["date"]}: chưa hoàn tất/tương lai; raw={escape(point["raw"])}</title></line>')
    for a, b in zip(points, points[1:]):
        if a["value"] is None or b["value"] is None or (b["date"] - a["date"]).days != 7:
            continue
        pending = not a["complete"] or not b["complete"]
        dash = ' stroke-dasharray="5 4"' if pending else ""
        color = "#9b5700" if pending else "#146f69"
        parts.append(f'<line class="trend-segment" x1="{x(a):.2f}" y1="{y(a):.2f}" x2="{x(b):.2f}" y2="{y(b):.2f}" stroke="{color}" stroke-width="2"{dash}/>')
    for point in points:
        if point["value"] is None:
            continue
        pending = not point["complete"]
        status = "chưa hoàn tất/tương lai" if pending else "đã hoàn tất"
        evidence_id = point["record"].evidence_id
        title = escape(f"{point['date']}: {point['raw']} · {status} · {evidence_id}")
        parts.append(f'<a href="#{targets[evidence_id]}"><circle class="trend-point" data-evidence-id="{escape(evidence_id, quote=True)}" data-status="{"incomplete" if pending else "complete"}" cx="{x(point):.2f}" cy="{y(point):.2f}" r="{4 if pending else 2.8}" fill="{"white" if pending else "#146f69"}" stroke="{"#9b5700" if pending else "#146f69"}" stroke-width="1.8"><title>{title}</title></circle></a>')
    labeled = [points[0], points[len(points) // 2], points[-1]] if len(points) > 2 else points
    for index, point in enumerate(labeled):
        anchor = "start" if index == 0 else "end" if index == len(labeled) - 1 else "middle"
        parts.append(f'<text x="{x(point):.2f}" y="246" text-anchor="{anchor}" font-size="12" fill="#526176">{point["date"]}</text>')
    parts += ['<circle cx="70" cy="272" r="3" fill="#146f69"/><text x="82" y="276" font-size="12">Hoàn tất</text>',
              '<line x1="205" y1="272" x2="233" y2="272" stroke="#9b5700" stroke-dasharray="5 4"/><circle cx="220" cy="272" r="4" fill="white" stroke="#9b5700"/><text x="242" y="276" font-size="12">Chưa hoàn tất / tương lai</text></svg>',
              '<figcaption>' + escape(series["keyword"]) + ' · export: ' + escape(str(series["csv_filename"] or "không ghi tên file"))
              + '. Khoảng trống: &lt;1, missing hoặc giá trị không hợp lệ; không nối qua tuần thiếu. Vạch dọc chấm/vòng rỗng đánh dấu kỳ chưa hoàn tất hoặc tương lai. Nhấp điểm để truy evidence. Điểm xếp theo ngày; chỉ vẽ export đầu tiên, không gộp các normalization contexts.</figcaption></figure>']
    return "".join(parts)
