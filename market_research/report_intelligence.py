"""Read-only, reproducible report calculations; never amend research/validation records."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, timedelta, timezone
from statistics import fmean, pstdev
from urllib.parse import parse_qs, urlparse

from .analysis import compute_trend_metrics
from .trends_csv import GoogleTrendsCsvCollector, is_synthetic_evidence, is_synthetic_result


METHOD = "report_trends_v1"
LIMITATIONS = [
    "Chỉ số Google Trends tương đối trong ngữ cảnh export, không phải search volume, doanh số, tăng trưởng bán hàng hoặc conversion.",
    "CSV và URL do người dùng cung cấp; kiểm tra định dạng không xác thực độc lập tính chân thực của export. Không ghép các export chuẩn hóa riêng.",
    "Không kết luận mùa vụ: một chu kỳ năm không đủ; cần nhiều chu kỳ và đối chiếu cùng điều kiện đo.",
    "Competitive Landscape — Not Yet Verified: phân tích Trends không xác minh độ phủ đối thủ, giá, ratings, review counts hoặc phản hồi khách hàng.",
    "Chưa có evidence về purchase conversion, doanh số và willingness to pay; không thể xác nhận commercial demand hoặc product-market fit.",
]
RECOMMENDATIONS = [
    {"priority": "P1", "question": "Mức quan tâm gần đây có duy trì khi đo lại cùng ngữ cảnh?",
     "evidence": "Export mới cùng keyword, vùng, search type, category và cửa sổ thời gian; đối chiếu các tuần đã hoàn tất.",
     "why": "Phân biệt thay đổi ngắn hạn, đỉnh cục bộ và thay đổi do chuẩn hóa hoặc cách lấy mẫu.",
     "decision": "Quyết định có tiếp tục dành thời gian kiểm chứng niche; chưa phải quyết định sản xuất."},
    {"priority": "P2", "question": "Các lựa chọn cạnh tranh và khoảng giá thực tế là gì?",
     "evidence": "Dữ liệu sản phẩm được phép sử dụng: URL/ID, thời điểm quan sát, listed price/currency, rating/review count và coverage; ghi rõ missing values.",
     "why": "Trends không cung cấp giá hay trải nghiệm khách hàng; catalog sách không xác nhận listing hoặc sales trên Amazon.",
     "decision": "Thiết kế giả thuyết positioning và khoảng giá cần thử nghiệm; không suy sales từ ratings/reviews."},
    {"priority": "P3", "question": "Ai có nhu cầu cụ thể và có sẵn sàng mua concept đề xuất?",
     "evidence": "Phỏng vấn khách hàng có consent, kiểm tra concept và thử nghiệm conversion có cách đo rõ ràng; không thay bằng suy đoán LLM.",
     "why": "Search interest không xác định người mua, vấn đề cần giải quyết hoặc willingness to pay.",
     "decision": "Ưu tiên audience, nội dung và khác biệt sản phẩm sau khi có evidence trực tiếp."},
]


def fmt(value):
    return format(value, ".2f").rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _eligible(item):
    if (is_synthetic_evidence(item) or item.metric_name != "search_interest_index"
            or item.source_name != "Google Trends (manual CSV export)"):
        return False
    if (item.source_url != item.source_url.strip() or "\\" in item.source_url
            or any(ord(char) < 32 or ord(char) == 127 for char in item.source_url)):
        return False
    normalize = GoogleTrendsCsvCollector._normalized_header
    try:
        url = urlparse(item.source_url)
        query = parse_qs(url.query)
        GoogleTrendsCsvCollector("unused.csv", item.source_url)._validate_source_url()
        return (not url.username and not url.password and not url.fragment and url.port in (None, 443)
                and set(query) <= {"q", "geo", "date", "cat", "gprop", "hl", "tz"}
                and query.get("geo") == ["US"] and len(query.get("q", [])) == 1
                and normalize(query["q"][0]) == normalize(item.keyword)
                and normalize(item.target_region) in {"us", "united states", "hoa kỳ"}
                and normalize(str(item.raw_observation.get("geographic_metadata") or "us")) in {"us", "united states", "hoa kỳ"}
                and normalize(str(item.raw_observation.get("period_column", ""))) in {"week", "tuần"})
    except (ValueError, TypeError):
        return False


def _point(item, as_of):
    raw = str(item.raw_observation.get("raw_value", item.metric_value)).strip()
    try:
        period = date.fromisoformat(item.observation_period)
    except ValueError:
        period = None
    value = item.metric_value
    censored = raw.replace(" ", "") == "<1"
    numeric = (not censored and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100)
    if numeric and "raw_value" in item.raw_observation:
        try:
            numeric = float(raw) == value
        except ValueError:
            numeric = False
    return {"record": item, "date": period, "raw": raw, "value": float(value) if numeric else None,
            "censored": censored, "complete": period is not None and period + timedelta(days=6) < as_of}


def _series(records, as_of):
    points = [_point(item, as_of) for item in records]
    dated = sorted((p for p in points if p["date"] is not None), key=lambda p: p["date"])
    completed = [p for p in dated if p["complete"]]
    usable = [p for p in completed if p["value"] is not None]
    dates = [p["date"] for p in points if p["date"] is not None]
    duplicates = len(dates) != len(set(dates)) or len({r.evidence_id for r in records}) != len(records)
    ordered = dates == sorted(dates)
    if duplicates:  # Conflicting or repeated observations must never become extra weight.
        usable = []
    identity = json.dumps([METHOD, as_of.isoformat(), [r.model_dump(mode="json") for r in records]],
                          ensure_ascii=False, sort_keys=True, allow_nan=True)
    prefix = "report_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    measures, findings, caveats = [], [], []

    def measure(key, label, value, unit, selected, formula):
        item = {"metric_id": prefix + "_" + key, "key": key, "label": label, "value": value, "unit": unit,
                "evidence_ids": [p["record"].evidence_id for p in selected], "formula": formula}
        measures.append(item)
        return item

    def finding(observed, support, implication, uncertainty):
        findings.append({"observed": observed, "metric_ids": [m["metric_id"] for m in support],
                         "implication": implication, "uncertainty": uncertainty})

    total = measure("count", "Quan sát nguồn", len(records), "bản ghi", points, "count(source records in this export group)")
    count = measure("usable", "Quan sát số dùng được", len(usable), "tuần hoàn tất", points,
                    "count(unique-date, finite index in [0,100], raw-consistent, completed weeks); duplicate dates block aggregates")
    censored = measure("censored", "Giá trị <1", sum(p["censored"] for p in points), "bản ghi", points, "count(literal <1); never impute zero")
    excluded = measure("excluded", "Kỳ chưa hoàn tất / tương lai", sum(p["date"] is not None and not p["complete"] for p in points),
                       "bản ghi", points, "count(week_start + 6 days >= report UTC date)")
    invalid = measure("missing", "Thiếu / không dùng được (ngoài <1)", sum(not p["censored"] and (p["date"] is None or p["value"] is None) for p in points),
                      "bản ghi", points, "count(invalid date or missing/nonfinite/out-of-range/raw-conflicting value), excluding literal <1")
    gaps = measure("gaps", "Khoảng ngày không đúng nhịp tuần", sum((b["date"] - a["date"]).days != 7 for a, b in zip(dated, dated[1:])),
                   "khoảng", dated, "count(adjacent dated records separated by other than 7 days)")
    period = f"{dated[0]['date']}–{dated[-1]['date']}" if dated else "không có ngày hợp lệ"
    finding(f"{records[0].keyword}: {len(records)} quan sát trong {period}; {len(usable)} quan sát số ở tuần hoàn tất dùng được; "
            f"{censored['value']} giá trị <1 và {excluded['value']} kỳ chưa hoàn tất/tương lai.",
            [total, count, censored, excluded], "Đánh giá độ phủ trước khi dùng thay đổi chỉ số để ưu tiên nghiên cứu.",
            "<1 là quan sát bị giới hạn đo, không phải 0; missing và tuần chưa hoàn tất không được nội suy.")
    if dated:
        latest = dated[-1]
        measure("latest", "Giá trị gốc mới nhất", latest["raw"], f"kỳ {latest['date']} · " + ("đã hoàn tất" if latest["complete"] else "chưa hoàn tất/tương lai; không dùng tính trend"),
                [latest], "raw value at latest dated record; no imputation; not necessarily usable")
    if duplicates: caveats.append("Ngày hoặc evidence ID trùng lặp: không tính aggregate hoặc window; cần kiểm tra export, không gộp/average bản ghi trùng.")
    if not ordered: caveats.append("Ngày nguồn không theo thứ tự: không tính window hoặc diễn giải hướng; audit giữ nguyên thứ tự gốc.")
    if gaps["value"]: caveats.append("Chuỗi có khoảng ngày không đúng nhịp tuần; window chỉ tính khi các tuần cần thiết liên tiếp, không lấp khoảng trống.")
    if invalid["value"]: caveats.append("Có ngày/giá trị thiếu hoặc không hợp lệ; các thống kê chỉ mô tả tập con dùng được, có thể lệch so với toàn chuỗi.")
    if censored["value"]: caveats.append("Giữ nguyên literal <1; không coi là 0, 0.5 hoặc 1 và không bỏ qua để kéo các window cũ lên thay thế.")

    def valid_window(selected, length):
        return (not duplicates and ordered and all(p["date"] is not None for p in points)
                and len(selected) == length and all(p["value"] is not None for p in selected)
                and all((b["date"] - a["date"]).days == 7 for a, b in zip(selected, selected[1:])))

    recent, previous, delta = None, None, None
    last8 = completed[-8:]
    if valid_window(completed[-4:], 4):
        selected = completed[-4:]
        recent = measure("recent4", "Trung bình 4 tuần gần nhất", round(fmean(p["value"] for p in selected), 2),
                         f"điểm index · {selected[0]['date']}–{selected[-1]['date']}", selected, "arithmetic mean of the latest 4 contiguous completed calendar weeks")
    if valid_window(last8, 8):
        # Reuse the engine's calculation; never trust a cached metadata metric as a new finding.
        metric = compute_trend_metrics([p["record"].model_dump(mode="json") for p in completed if p["value"] is not None], as_of=as_of)[0]
        previous = measure("previous4", "Trung bình 4 tuần trước", metric["previous_window"]["average_relative_index"],
                           f"điểm index · {last8[0]['date']}–{last8[3]['date']}", last8[:4], "existing compute_trend_metrics: previous_window.average_relative_index")
        delta = measure("delta4", "Chênh lệch hai cửa sổ", metric["change_in_index_points"], "điểm index; không phải sales growth", last8,
                        "existing compute_trend_metrics: round(unrounded recent mean - unrounded previous mean, 2)")
        word = "cao hơn" if delta["value"] > 0 else "thấp hơn" if delta["value"] < 0 else "bằng"
        implication = ("Mức quan tâm cửa sổ gần đây cao hơn: ưu tiên kiểm chứng xem timing còn phù hợp bằng export tiếp theo trước khi thử concept."
                       if delta["value"] > 0 else "Mức quan tâm cửa sổ gần đây thấp hơn: kiểm tra tính duy trì và thay đổi ngữ cảnh đo trước khi dành thêm nguồn lực cho concept."
                       if delta["value"] < 0 else "Hai cửa sổ có cùng trung bình: chưa có lý do điều chỉnh timing chỉ từ phép so sánh này; vẫn cần nghiên cứu khách hàng.")
        finding(f"Trung bình bốn tuần gần nhất {fmt(recent['value'])}, {word} bốn tuần trước {fmt(previous['value'])}; "
                f"chênh lệch có dấu {fmt(delta['value'])} điểm index.", [recent, previous, delta],
                "INTERPRETATION: " + implication,
                "So sánh hai cửa sổ không chứng minh mọi tuần tăng/giảm, xu hướng dài hạn hoặc thay đổi doanh số.")
    else:
        caveats.append("Chưa đủ hai cửa sổ 4 tuần hoàn tất liên tiếp có số hợp lệ; không tính chênh lệch. Không dùng các quan sát cũ hơn để lấp tuần thiếu/<1.")
    if usable:
        values = [p["value"] for p in usable]
        if len(usable) == 1:
            caveats.append("Chỉ có một quan sát số dùng được: độ lệch chuẩn bằng 0 theo định nghĩa, không xác nhận sự ổn định của mức quan tâm.")
        latest_complete = usable[-1]
        measure("latest_usable", "Index số mới nhất dùng được", latest_complete["value"], f"điểm index · {latest_complete['date']}", [latest_complete], "last usable completed observation")
        high, low = max(values), min(values)
        peaks, lows = [p for p in usable if p["value"] == high], [p for p in usable if p["value"] == low]
        maximum = measure("peak", "Đỉnh quan sát", high, "điểm index · " + ", ".join(str(p["date"]) for p in peaks), peaks, "max(usable completed indices); all tied dates retained")
        minimum = measure("minimum", "Thấp nhất quan sát", low, "điểm index · " + ", ".join(str(p["date"]) for p in lows), lows, "min(usable completed indices); all tied dates retained")
        spread = measure("range", "Biên độ", high - low, "điểm index", usable, "max - min across usable completed observations")
        std = measure("std", "Độ lệch chuẩn tổng thể", round(pstdev(values), 2), "điểm index", usable, "population standard deviation, denominator N; rounded to 2 decimals")
        measure("mean", "Trung bình toàn tập dùng được", round(fmean(values), 2), "điểm index", usable, "arithmetic mean of usable completed indices, not search volume")
        adjacent = [(a, b) for a, b in zip(usable, usable[1:]) if (b["date"] - a["date"]).days == 7]
        if adjacent:
            a, b = max(adjacent, key=lambda pair: abs(pair[1]["value"] - pair[0]["value"]))
            measure("largest_step", "Bước thay đổi tuần lớn nhất theo trị tuyệt đối", b["value"] - a["value"],
                    f"điểm index có dấu · {a['date']}–{b['date']}", [a, b], "signed delta of largest absolute adjacent 7-day change; earliest tie")
        finding(f"Trong các tuần dùng được, đỉnh {fmt(high)}, thấp nhất {fmt(low)}, biên độ {fmt(spread['value'])} "
                f"và độ lệch chuẩn tổng thể {fmt(std['value'])} điểm index.", [maximum, minimum, spread, std],
                "INTERPRETATION: mức phân tán giúp xem đỉnh cao có đại diện cho các tuần khác hay không; không tự xác định audience hoặc positioning.",
                "Chỉ mô tả mẫu CSV; normalization, sampling, ngày thiếu và phạm vi truy vấn có thể ảnh hưởng kết quả.")
    if valid_window(last8, 8):
        moves = [b["value"] - a["value"] for a, b in zip(last8, last8[1:])]
        up, down, flat = sum(v > 0 for v in moves), sum(v < 0 for v in moves), sum(v == 0 for v in moves)
        pattern = "tăng ở mọi bước" if up == len(moves) else "giảm ở mọi bước" if down == len(moves) else "đi ngang" if flat == len(moves) else "dao động/không đơn điệu"
        support = [measure(key, label, value, "bước tuần", last8, formula) for key, label, value, formula in
                   [("up", "Số bước tăng", up, "count(adjacent delta > 0) in latest contiguous 8 completed weeks"),
                    ("down", "Số bước giảm", down, "count(adjacent delta < 0) in latest contiguous 8 completed weeks"),
                    ("flat", "Số bước bằng nhau", flat, "count(adjacent delta == 0) in latest contiguous 8 completed weeks")]]
        finding(f"Cửa sổ tám tuần gần nhất: {up} bước tăng, {down} bước giảm, {flat} bước bằng nhau — {pattern}.", support,
                "INTERPRETATION: kiểm tra hình dạng chuỗi trước khi mô tả tăng trưởng liên tục hoặc chọn thời điểm thử concept.",
                "Mô tả các bước trong cửa sổ quan sát, không dự báo diễn biến tiếp theo hoặc chứng minh nguyên nhân.")
    # A local maximum is a factual shape, never a causal/seasonal explanation or spike forecast.
    if usable:
        peak = max(usable, key=lambda p: p["value"])
        index = dated.index(peak)
        neighbors = dated[max(0, index - 1):index + 2]
        if (len(neighbors) == 3 and neighbors[1] is peak and all(p["complete"] and p["value"] is not None for p in neighbors)
                and all((b["date"] - a["date"]).days == 7 for a, b in zip(neighbors, neighbors[1:]))
                and peak["value"] > neighbors[0]["value"] and peak["value"] > neighbors[2]["value"]):
            m = measure("peak_neighbors", "Đỉnh và hai tuần liền kề", " → ".join(fmt(p["value"]) for p in neighbors),
                        "điểm index · " + " / ".join(str(p["date"]) for p in neighbors), neighbors, "three contiguous completed observations around the earliest maximum")
            caveats.append(f"Đỉnh cục bộ có hai tuần liền kề thấp hơn ({m['metric_id']}); không đồng nghĩa một đợt tăng bền vững hoặc nguyên nhân đã biết.")
        else:
            caveats.append("Chưa có hình dạng đỉnh đơn lẻ với hai tuần hoàn tất liền kề thấp hơn; không gắn nhãn spike hoặc suy nguyên nhân.")
    confidence = "Vừa — mô tả chuỗi CSV, không phải độ tin cậy thương mại" if delta and not invalid["value"] and not censored["value"] and not gaps["value"] else "Thấp — độ phủ/chất lượng hoặc cửa sổ so sánh còn hạn chế"
    return {"keyword": records[0].keyword, "source_url": records[0].source_url,
            "csv_filename": records[0].raw_observation.get("csv_filename"), "period": period,
            "as_of": as_of.isoformat(), "confidence": confidence, "measurements": measures,
            "findings": findings, "caveats": caveats,
            "hypothesis": f"HYPOTHESIS: có thể thử concept theo chủ đề '{records[0].keyword}' với nhóm khách hàng được phỏng vấn; positioning theo keyword chỉ là giả thuyết, audience, khác biệt, giá và khả năng mua vẫn chưa được xác minh."}


def build_report_intelligence(result):
    """Derived view only. Source records, saved metadata, facts and validation stay untouched."""
    groups = defaultdict(list)
    if not is_synthetic_result(result) and not any(item.raw_observation.get("SYNTHETIC") is True for item in result.evidence):
        for item in result.evidence:
            if _eligible(item):
                groups[(item.keyword, item.source_url, item.raw_observation.get("csv_filename", ""))].append(item)
    return {"method": METHOD, "as_of": result.finished_at.astimezone(timezone.utc).date().isoformat(),
            "series": [_series(rows, result.finished_at.astimezone(timezone.utc).date()) for rows in groups.values()],
            "limitations": list(LIMITATIONS), "recommendations": [dict(item) for item in RECOMMENDATIONS]}
