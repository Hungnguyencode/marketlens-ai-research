"""Auditable comparison within one joint export; commercial ranking fails closed."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import Field, model_validator

from .multi_niche import NICHE_KEYWORDS, NicheResult, StrictModel, _cell, _json, _normalized
from .trends_csv import GoogleTrendsCsvCollector


class ComparisonConfig(StrictModel):
    csv_path: Path
    batch_directory: Path
    source_url: str | None = None
    joint_export_confirmed: Literal[True]
    settings_origin: Literal["user_declared"] = "user_declared"
    region: Literal["US"] = "US"
    category: Literal["All categories"] = "All categories"
    search_type: Literal["Web Search"] = "Web Search"
    period_start: date
    period_end: date
    as_of: date
    keyword_by_niche: dict[str, str]
    sparse_positive_fraction_threshold: float = Field(default=0.2, gt=0, le=1)
    minimum_positive_weeks_per_window: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def validate_context(self):
        if set(self.keyword_by_niche) != set(NICHE_KEYWORDS):
            raise ValueError("Comparison requires the four explicit stable niche mappings")
        for niche_id, keyword in self.keyword_by_niche.items():
            if _normalized(keyword) not in {_normalized(k) for k in NICHE_KEYWORDS[niche_id]}:
                raise ValueError(f"Keyword mapped to the wrong niche: {niche_id}")
        if len({_term(k) for k in self.keyword_by_niche.values()}) != 4:
            raise ValueError("Comparison keywords must be distinct")
        if self.period_end < self.period_start or (self.period_end - self.period_start).days % 7:
            raise ValueError("Observation window must contain contiguous weekly periods")
        if self.period_end > self.as_of:
            raise ValueError("Observation starts after the requested as_of date")
        if self.source_url is not None:
            GoogleTrendsCsvCollector(self.csv_path, self.source_url)._validate_source_url()
            query = parse_qs(urlparse(self.source_url).query)
            if query.get("geo") != ["US"] or len(query.get("q", [])) != 1:
                raise ValueError("Known source URL must identify US and all four comparison terms")
            if {_term(k) for k in query["q"][0].split(",")} != {_term(k) for k in self.keyword_by_niche.values()}:
                raise ValueError("Known source URL keywords differ from comparison mapping")
        return self


def _term(value: str) -> str:
    return GoogleTrendsCsvCollector._normalized_header(value)


class ComparisonObservation(StrictModel):
    evidence_id: str
    period_start: date
    raw_value: str
    relative_index: float = Field(ge=0, le=100)
    complete_week: bool


class ComparisonSeries(StrictModel):
    dataset_id: str
    niche_id: str
    keyword: str
    original_header: str
    geographic_metadata: str
    observations: list[ComparisonObservation]


class JointComparisonDataset(StrictModel):
    dataset_id: str
    source_name: Literal["Google Trends (manual joint-comparison CSV)"] = "Google Trends (manual joint-comparison CSV)"
    normalization_context: Literal["single_joint_export"] = "single_joint_export"
    source_path: str
    csv_sha256: str
    source_url: str | None
    parsed_at: datetime
    as_of: date
    declared_export_settings: dict
    metadata_preamble: list[list[str]]
    series: list[ComparisonSeries] = Field(min_length=4, max_length=4)


class KeywordMetrics(StrictModel):
    dataset_id: str
    niche_id: str
    keyword: str
    observed_weeks: int
    completed_weeks: int
    excluded_incomplete_weeks: list[date]
    completed_period_start: date | None
    completed_period_end: date | None
    mean_relative_index: float | None
    maximum_relative_index: float | None
    positive_completed_weeks: int
    positive_completed_percentage: float | None
    positive_observed_weeks_including_partial: int
    data_completeness: float
    sparse_signal: bool
    recent_four_week_average: float | None
    previous_four_week_average: float | None
    change_in_index_points: float | None
    window_interpretation: Literal["SUPPORTED", "INSUFFICIENT_PERIODS", "NOT_INTERPRETABLE_SPARSE_SIGNAL"]
    direction: Literal["increasing", "decreasing", "stable", "insufficient evidence"]
    supporting_evidence_ids: list[str]
    window_evidence_ids: list[str]
    warnings: list[str]


class CommercialDecision(StrictModel):
    status: Literal["RANKABLE", "PROVISIONAL", "INSUFFICIENT_EVIDENCE"]
    assessment_scope: str = "commercial opportunity"
    reasons: list[str]
    missing_dimensions: list[str]
    market_opportunity_rank: list[str] | None = None
    research_priorities: list[dict] = Field(max_length=3)


def load_comparison_config(path: str | Path) -> ComparisonConfig:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"Comparison configuration not found: {path}. Copy comparison.example.json to a local config first."
        )
    config = ComparisonConfig.model_validate_json(path.read_text(encoding="utf-8"))
    config.csv_path = (path.parent / config.csv_path).resolve()
    config.batch_directory = (path.parent / config.batch_directory).resolve()
    if not config.csv_path.is_file():
        raise FileNotFoundError(
            f"Joint Google Trends CSV not found: {config.csv_path}. Export one joint US CSV for all four configured keywords and update csv_path."
        )
    if not config.batch_directory.is_dir():
        raise FileNotFoundError(
            f"Compatible batch directory not found: {config.batch_directory}. Generate or select a four-niche batch and update batch_directory."
        )
    return config


def parse_comparison(config: ComparisonConfig, *, csv_bytes: bytes | None = None) -> JointComparisonDataset:
    """Only numeric, complete rows from one explicitly declared joint export are accepted."""
    config = ComparisonConfig.model_validate(config.model_dump())
    raw = config.csv_path.read_bytes() if csv_bytes is None else csv_bytes
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("Comparison CSV is not valid UTF-8 CSV") from exc
    headers = [i for i, row in enumerate(rows) if row and _term(row[0]) in {"week", "tuần"}]
    if len(headers) != 1:
        raise ValueError("Comparison CSV must contain exactly one Week/Tuần header")
    index = headers[0]
    header = rows[index]
    if len(header) != 5:
        raise ValueError("One joint export with exactly four keyword columns is required; independent exports cannot be mixed")
    column_mapping = {}
    expected = {_term(keyword): niche_id for niche_id, keyword in config.keyword_by_niche.items()}
    for column, label in enumerate(header[1:], 1):
        keyword = GoogleTrendsCsvCollector._keyword_from_header(label)
        region = GoogleTrendsCsvCollector._geography_from_header(label)
        if region is None or _term(region) not in {"hoa kỳ", "united states", "us"}:
            raise ValueError("Every comparison series must explicitly identify the US region")
        if _term(keyword) not in expected or _term(keyword) in column_mapping:
            raise ValueError("Wrong, duplicate or ambiguous comparison keyword columns")
        column_mapping[_term(keyword)] = (column, keyword, region)
    for row in rows[:index]:
        line = _term(",".join(row))
        if line.startswith(("danh mục:", "category:")) and line.split(":", 1)[1].strip() not in {"tất cả danh mục", "all categories"}:
            raise ValueError("CSV category preamble contradicts declared All categories setting")
    settings = config.model_dump(mode="json", exclude={"csv_path", "batch_directory", "as_of",
        "sparse_positive_fraction_threshold", "minimum_positive_weeks_per_window"})
    digest = hashlib.sha256(raw).hexdigest()
    dataset_id = "joint-" + hashlib.sha256((digest + json.dumps(settings, sort_keys=True)).encode("utf-8")).hexdigest()[:20]
    series = [ComparisonSeries(dataset_id=dataset_id, niche_id=niche_id,
        keyword=column_mapping[_term(keyword)][1], original_header=header[column_mapping[_term(keyword)][0]],
        geographic_metadata=column_mapping[_term(keyword)][2], observations=[])
        for niche_id, keyword in config.keyword_by_niche.items()]
    dates = []
    for row in rows[index + 1:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != 5:
            raise ValueError("Incomplete comparison row: expected date and four numeric values")
        try:
            period = date.fromisoformat(row[0].strip())
        except ValueError as exc:
            raise ValueError(f"Malformed comparison date: {row[0]!r}") from exc
        if period.isoformat() != row[0].strip():
            raise ValueError("Comparison dates must use YYYY-MM-DD")
        if dates and period != dates[-1] + timedelta(days=7):
            raise ValueError("Comparison dates must be unique, ascending and contiguous weekly periods")
        dates.append(period)
        for item in series:
            column = column_mapping[_term(item.keyword)][0]
            raw_value = row[column].strip()
            if not raw_value or raw_value.casefold() in {"n/a", "na", "null"}:
                raise ValueError(f"Missing observation for {item.keyword} at {period}; zero is distinct from missing")
            if raw_value.replace(" ", "") == "<1":
                raise ValueError("Censored <1 observations cannot be imputed as zero in numeric comparison")
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise ValueError(f"Non-numeric comparison value {raw_value!r} at {period}") from exc
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"Comparison index must be finite and in 0-100 at {period}")
            item.observations.append(ComparisonObservation(evidence_id=f"{dataset_id}:{item.niche_id}:{period}",
                period_start=period, raw_value=raw_value, relative_index=value,
                complete_week=period + timedelta(days=6) < config.as_of))
    if not dates or dates[0] != config.period_start or dates[-1] != config.period_end:
        raise ValueError("CSV observation window is incomplete or contradicts configuration")
    return JointComparisonDataset(dataset_id=dataset_id, source_path=str(config.csv_path), csv_sha256=digest,
        source_url=config.source_url, parsed_at=datetime.now(timezone.utc), as_of=config.as_of,
        declared_export_settings=settings, metadata_preamble=rows[:index], series=series)


def compare_keyword_signals(dataset: JointComparisonDataset, *, sparse_threshold: float = 0.2,
                            minimum_positive_per_window: int = 2) -> list[KeywordMetrics]:
    """Reject mixed contexts, including independently normalized standalone exports."""
    if dataset.normalization_context != "single_joint_export" or len(dataset.series) != 4:
        raise ValueError("Comparison requires one joint export, not independent datasets")
    if not 0 < sparse_threshold <= 1 or not 1 <= minimum_positive_per_window <= 4:
        raise ValueError("Invalid explicit sparse-signal policy")
    mapping = dataset.declared_export_settings.get("keyword_by_niche", {})
    if (dataset.declared_export_settings.get("joint_export_confirmed") is not True or
            set(mapping) != set(NICHE_KEYWORDS) or {s.niche_id for s in dataset.series} != set(NICHE_KEYWORDS)):
        raise ValueError("Dataset must retain four distinct explicit niche identities and joint-export declaration")
    reference_dates = [point.period_start for point in dataset.series[0].observations]
    if not reference_dates or any(b != a + timedelta(days=7) for a, b in zip(reference_dates, reference_dates[1:])):
        raise ValueError("Dataset must retain contiguous unique weekly dates")
    results = []
    for series in dataset.series:
        if series.dataset_id != dataset.dataset_id or [p.period_start for p in series.observations] != reference_dates:
            raise ValueError("Independent export contexts or date coverage cannot be mixed")
        if _term(series.keyword) != _term(mapping[series.niche_id]) or any(
                p.evidence_id != f"{dataset.dataset_id}:{series.niche_id}:{p.period_start}" for p in series.observations):
            raise ValueError("Series keyword or evidence identity belongs to a different niche/context")
        if any(p.complete_week != (p.period_start + timedelta(days=6) < dataset.as_of) for p in series.observations):
            raise ValueError("Inconsistent completed-week coverage")
        points = [point for point in series.observations if point.complete_week]
        values = [point.relative_index for point in points]
        positive = sum(value > 0 for value in values)
        fraction = positive / len(points) if points else 0.0
        sparse = fraction < sparse_threshold
        recent, previous = points[-4:], points[-8:-4]
        enough = len(previous) == len(recent) == 4
        recent_average = sum(p.relative_index for p in recent) / 4 if len(recent) == 4 else None
        previous_average = sum(p.relative_index for p in previous) / 4 if len(previous) == 4 else None
        delta = recent_average - previous_average if enough else None
        interpretable = enough and not sparse and all(sum(p.relative_index > 0 for p in window) >= minimum_positive_per_window
                                                     for window in (previous, recent))
        warnings = ["Index 0 là giá trị báo cáo, không có nghĩa zero searches, zero buyers hay zero demand.",
                    "Một chu kỳ năm chưa đủ để xác nhận seasonality; search interest không đo purchase intent."]
        if sparse:
            warnings.append("SPARSE_SIGNAL: ít tuần có index dương; keyword có thể không đại diện niche và nhạy với phép đo.")
        if enough and not interpretable:
            warnings.append("Không diễn giải chênh lệch hai cửa sổ thành trend growth do tín hiệu quá thưa trong chuỗi/cửa sổ.")
        results.append(KeywordMetrics(dataset_id=dataset.dataset_id, niche_id=series.niche_id, keyword=series.keyword,
            observed_weeks=len(series.observations), completed_weeks=len(points),
            excluded_incomplete_weeks=[p.period_start for p in series.observations if not p.complete_week],
            completed_period_start=points[0].period_start if points else None,
            completed_period_end=points[-1].period_start if points else None,
            mean_relative_index=sum(values) / len(values) if values else None,
            maximum_relative_index=max(values) if values else None,
            positive_completed_weeks=positive, positive_completed_percentage=100 * fraction if points else None,
            positive_observed_weeks_including_partial=sum(p.relative_index > 0 for p in series.observations),
            data_completeness=1.0, sparse_signal=sparse, recent_four_week_average=recent_average,
            previous_four_week_average=previous_average, change_in_index_points=delta,
            window_interpretation="SUPPORTED" if interpretable else "NOT_INTERPRETABLE_SPARSE_SIGNAL" if enough else "INSUFFICIENT_PERIODS",
            direction=("increasing" if delta > 0 else "decreasing" if delta < 0 else "stable") if interpretable else "insufficient evidence",
            supporting_evidence_ids=[p.evidence_id for p in points],
            window_evidence_ids=[p.evidence_id for p in previous + recent] if enough else [], warnings=warnings))
    return results


def _load_batch(directory: Path) -> tuple[str, dict[str, NicheResult], dict[str, str], dict[str, str]]:
    directory = directory.resolve()
    summary_path = directory / "summary.json"
    summary_bytes = summary_path.read_bytes()
    summary = json.loads(summary_bytes.decode("utf-8"))
    if not isinstance(summary, dict) or not isinstance(summary.get("batch_id"), str):
        raise ValueError("Malformed batch summary identity")
    entries = summary.get("niches", [])
    if (not isinstance(entries, list) or len(entries) != 4 or any(not isinstance(row, dict) for row in entries)
            or {row.get("niche_id") for row in entries} != set(NICHE_KEYWORDS)):
        raise ValueError("Existing batch must contain exactly the four distinct stable niche IDs")
    results, hashes = {}, {"summary.json": hashlib.sha256(summary_bytes).hexdigest()}
    paths = {}
    for row in entries:
        if not isinstance(row.get("result_path"), str):
            raise ValueError("Batch entry must declare its result artifact path")
        path = (directory / row["result_path"]).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Batch result path escapes its batch directory")
        raw = path.read_bytes()
        result = NicheResult.model_validate_json(raw)
        if result.niche_id != row["niche_id"] or result.run_id != f"{summary['batch_id']}-{result.niche_id}":
            raise ValueError("Batch result identity disagrees with its explicit stable niche mapping")
        catalog = result.catalog
        if not 0 <= catalog.relevance_candidates <= catalog.parsed_items <= catalog.raw_items_returned:
            raise ValueError("Batch catalog counts are inconsistent")
        if len(catalog.record_ids) != catalog.parsed_items or any(
                record_id != f"{result.run_id}-catalog-{work_id}" for work_id, record_id in catalog.record_ids.items()):
            raise ValueError("Catalog record identities or parsed counts do not belong to this niche")
        results[result.niche_id] = result
        paths[result.niche_id] = path.relative_to(directory).as_posix()
        hashes[paths[result.niche_id]] = hashlib.sha256(raw).hexdigest()
    return summary["batch_id"], results, hashes, paths


def assess_commercial_opportunity(metrics: list[KeywordMetrics], batch: dict[str, NicheResult]) -> CommercialDecision:
    """Current supported sources cannot satisfy commercial scoring criteria, regardless of their counts."""
    missing = ["Giá retail quan sát được", "Bằng chứng sales", "Customer reviews/nhu cầu đã kiểm chứng",
               "Cạnh tranh liên quan và US retail availability đã xác minh"]
    sparse = [item.keyword for item in metrics if item.sparse_signal]
    reasons = ["Search-interest index và catalog metadata không đủ hỗ trợ commercial opportunity ranking.",
               "Thiếu bằng chứng trực tiếp cho toàn bộ tiêu chí thương mại: " + "; ".join(missing) + ".",
               "Open Library candidates không phải confirmed competitors; counts/edition_count không dùng làm điểm."]
    if sparse:
        reasons.append("Sparse keyword signals: " + ", ".join(sparse) + "; chưa xác nhận tính đại diện của cách viết truy vấn.")
    if any(result.catalog.status not in {"success", "partial"} for result in batch.values()):
        reasons.append("Một số niche còn thiếu catalog coverage thành công.")
    priorities = [
        {"label": "RESEARCH_PRIORITY", "action": "Kiểm tra cách viết keyword và tính đại diện bằng export so sánh chung mới.",
         "niche_ids": [item.niche_id for item in metrics if item.sparse_signal] or list(batch)},
        {"label": "RESEARCH_PRIORITY", "action": "Thu thập nguồn retail Hoa Kỳ cho availability, giá và cạnh tranh liên quan.",
         "niche_ids": list(batch)},
        {"label": "RESEARCH_PRIORITY", "action": "Xác minh sales, customer reviews và nhu cầu khách hàng trước khi chấm cơ hội thương mại.",
         "niche_ids": list(batch)},
    ]
    return CommercialDecision(status="INSUFFICIENT_EVIDENCE", reasons=reasons, missing_dimensions=missing,
                              market_opportunity_rank=None, research_priorities=priorities)


def run_comparison(config: ComparisonConfig | str | Path, *, output_dir: str | Path = "outputs/comparisons",
                   as_of: date | None = None) -> dict:
    config = load_comparison_config(config) if isinstance(config, (str, Path)) else ComparisonConfig.model_validate(config.model_dump())
    if as_of:
        config.as_of = as_of
    raw = config.csv_path.read_bytes()
    dataset = parse_comparison(config, csv_bytes=raw)
    metrics = compare_keyword_signals(dataset, sparse_threshold=config.sparse_positive_fraction_threshold,
        minimum_positive_per_window=config.minimum_positive_weeks_per_window)
    batch_id, batch, hashes, paths = _load_batch(config.batch_directory)
    decision = assess_commercial_opportunity(metrics, batch)
    joined = []
    for item in metrics:
        original = batch[item.niche_id]
        joined.append({"niche_id": item.niche_id, "name": original.name,
            "keyword_metrics": item.model_dump(mode="json"), "catalog": original.catalog.model_dump(mode="json"),
            "catalog_source": {"batch_id": batch_id, "artifact": paths[item.niche_id],
                               "collection_method": "existing_batch_artifact; no new live request"},
            "standalone_trends_excluded_from_comparison": True,
            "product_hypotheses": [{**hypothesis, "status": "UNTESTED_HYPOTHESIS", "source_batch_id": batch_id,
                                    "comparison_role": "existing hypothesis; not independent confirmation"}
                                   for hypothesis in original.candidate_product_hypotheses],
            "missing_commercial_data": decision.missing_dimensions,
            "limitations": item.warnings + original.missing_data})
    ordered = sorted(metrics, key=lambda item: (-(item.mean_relative_index or 0), item.keyword))
    run_id = f"comparison-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
    directory = Path(output_dir).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    report = {"run_id": run_id, "output_directory": str(directory), "dataset_id": dataset.dataset_id,
        "analysis_status": "DETERMINISTIC_COMPARISON", "llm_execution_status": "NOT_REQUESTED",
        "claim_validation_status": "NOT_RUN", "normalization_context": dataset.normalization_context,
        "dataset_provenance": dataset.model_dump(mode="json", exclude={"series"}),
        "batch_provenance": {"directory": str(config.batch_directory), "batch_id": batch_id, "artifact_sha256": hashes},
        "metric_policy": {"complete_week_rule": "week_start + 6 days < as_of", "as_of": str(config.as_of),
            "sparse_positive_fraction_threshold": config.sparse_positive_fraction_threshold,
            "minimum_positive_weeks_per_window": config.minimum_positive_weeks_per_window,
            "comparison_basis": "mean relative index in completed weeks, within this joint export only",
            "tie_policy": "Equal means carry no evidenced difference; keyword lexical tie-break is presentation only."},
        "observed_keyword_signal_order": [item.niche_id for item in ordered],
        "commercial_assessment": decision.model_dump(mode="json"), "niches": joined,
        "artifacts": {"dataset": "dataset.json", "source_csv": "source.csv", "config": "config.json",
                      "comparison": "comparison.json", "report": "comparison.md"}}
    _json(directory / "config.json", config.model_dump(mode="json"))
    _json(directory / "dataset.json", dataset.model_dump(mode="json"))
    with (directory / "source.csv").open("xb") as output:
        output.write(raw)
    _json(directory / "comparison.json", report)
    _write_report(directory / "comparison.md", report)
    return report


def _number(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _write_report(path: Path, report: dict) -> None:
    provenance = report["dataset_provenance"]
    decision = report["commercial_assessment"]
    lines = ["# So sánh tín hiệu keyword và điều kiện xếp hạng", "",
        f"Commercial assessment: **{decision['status']}**. Không tạo Top 3 cơ hội thị trường.",
        "Inference: NOT_REQUESTED; claim validation: NOT_RUN; DETERMINISTIC_COMPARISON.", "",
        f"Dataset: `{report['dataset_id']}`; as_of: {provenance['as_of']}; CSV SHA-256: `{provenance['csv_sha256']}`.",
        f"Source URL: {provenance['source_url'] or 'Chưa biết; không tự dựng URL export.'}",
        "US locale/keyword được kiểm tra từ header; joint export, category và Web Search được khai báo bởi người dùng, "
        "không được coi là xác thực độc lập. Giữ preamble gốc trong dataset.json.",
        "Index chỉ so sánh trong cùng joint export. Không trộn CSV cozy riêng; không coi đó là xác nhận độc lập.", "",
        "## Bốn keyword trên cùng scale", "",
        "Mean/max/% positive và hai cửa sổ dùng cùng tập tuần hoàn tất. Tuần chưa hoàn tất giữ trong audit, loại khỏi metric. "
        "Số tuần dương raw trong ngoặc chỉ mô tả dữ liệu, có bao gồm tuần chưa hoàn tất.",
        "| Keyword | Tuần hoàn tất/tổng | Mean | Max | Tuần dương complete [raw/tổng] | % dương complete | Previous 4w | Recent 4w | Diễn giải | Catalog parsed/candidate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for row in report["niches"]:
        m, c = row["keyword_metrics"], row["catalog"]
        lines.append("| " + " | ".join(map(_cell, [m["keyword"], f"{m['completed_weeks']}/{m['observed_weeks']}",
            _number(m["mean_relative_index"]), _number(m["maximum_relative_index"]),
            f"{m['positive_completed_weeks']} [{m['positive_observed_weeks_including_partial']}/{m['observed_weeks']}]",
            _number(m["positive_completed_percentage"]), _number(m["previous_four_week_average"]),
            _number(m["recent_four_week_average"]), m["direction"] + ("; SPARSE_SIGNAL" if m["sparse_signal"] else ""),
            f"{c['parsed_items']}/{c['relevance_candidates']}"])) + " |")
    lines += ["", "observed_keyword_signal_order (theo mean, không phải market_opportunity_rank): " +
        " → ".join(report["observed_keyword_signal_order"]),
        "Khác biệt có thể do cách viết keyword và độ phủ. Index 0 không có nghĩa zero searches, buyers hay demand. "
        "Sparse series không được diễn giải delta thành tăng trưởng. Một chu kỳ năm không chứng minh seasonality.",
        "", "## Provenance và hạn chế từng niche", ""]
    for row in report["niches"]:
        c, m = row["catalog"], row["keyword_metrics"]
        lines += [f"- **{_cell(row['name'])}**: catalog={c['status']}, {c['provenance']} @ {c['retrieved_at']}; "
            f"request={c['request_url']}; completed window={m['completed_period_start']}..{m['completed_period_end']}; "
            f"loại tuần chưa hoàn tất={', '.join(m['excluded_incomplete_weeks']) or 'không có'}; "
            f"numeric data completeness={m['data_completeness']:.0%}."]
        lines.extend(f"  - Product hypothesis chưa kiểm chứng: {_cell(h.get('concept', ''))}; "
                     f"{_cell(h.get('intended_audience', ''))}." for h in row["product_hypotheses"])
    lines += ["", "Open Library không xác nhận currently available products, direct competitors, sales, prices hoặc US retail availability. "
        "numFound, edition_count và candidate counts không tham gia điểm/xếp hạng.", "", "## Decision gate và dữ liệu cần bổ sung", "",
        *[f"- {reason}" for reason in decision["reasons"]], "", "## Research priorities — chưa phải business opportunities", "",
        *[f"- {priority['label']}: {priority['action']} ({', '.join(priority['niche_ids'])})"
          for priority in decision["research_priorities"]], "",
        "Audit: [comparison.json](comparison.json), [dataset.json](dataset.json), [source.csv](source.csv), [config.json](config.json).", ""]
    with path.open("x", encoding="utf-8") as output:
        output.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline comparison of one shared Trends export and an existing batch")
    parser.add_argument("--config", default="comparison.json")
    parser.add_argument("--output-dir", default="outputs/comparisons")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Reproducible completed-week cutoff")
    args = parser.parse_args()
    try:
        report = run_comparison(args.config, output_dir=args.output_dir, as_of=args.as_of)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Comparison input/output error: {exc}\n")
    print(json.dumps({"run_id": report["run_id"], "output_directory": report["output_directory"],
        "dataset_id": report["dataset_id"], "commercial_assessment": report["commercial_assessment"]["status"],
        "observed_keyword_signal_order": report["observed_keyword_signal_order"],
        "llm_execution_status": report["llm_execution_status"], "claim_validation_status": report["claim_validation_status"]},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
