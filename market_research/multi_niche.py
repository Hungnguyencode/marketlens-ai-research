"""Four independent evidence collections; no LLM calls or opportunity ranking."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analysis import build_verified_facts, compute_trend_metrics
from .logging import ExecutionLogger
from .models import ComputedTrendMetric, Evidence
from .openlibrary_collector import CollectionResult, OpenLibraryCollector, _replay_snapshot
from .tools import ToolRegistry
from .trends_csv import GoogleTrendsCsvCollector


NICHE_KEYWORDS = {
    "cozy-adults": ("cozy coloring books for adults", "cozy coloring book"),
    "bold-easy": ("bold and easy coloring books",),
    "christmas-adults": ("Christmas coloring books for adults",),
    "kids-christmas": ("kids Christmas activity books",),
}


def _normalized(value: str) -> str:
    return re.sub(r"\bbooks\b", "book", " ".join(unicodedata.normalize("NFC", value).casefold().split()))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrendsDataset(StrictModel):
    niche_id: str
    csv_path: Path
    keyword: str = Field(min_length=1)
    source_url: str
    region: Literal["US"] = "US"
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def check_dataset(self):
        if self.period_end < self.period_start:
            raise ValueError("Trends period_end precedes period_start")
        parsed = urlparse(self.source_url)
        params = parse_qs(parsed.query)
        if parsed.scheme != "https" or parsed.hostname != "trends.google.com" or parsed.path != "/trends/explore":
            raise ValueError("Trends source_url must be the actual HTTPS Trends explore URL")
        if params.get("geo") != [self.region] or len(params.get("q", [])) != 1:
            raise ValueError("Trends source_url must declare exactly one keyword and the configured US region")
        if _normalized(params["q"][0]) != _normalized(self.keyword):
            raise ValueError("Trends URL keyword does not match the configured dataset keyword")
        if "date" in params and params["date"] != [f"{self.period_start} {self.period_end}"]:
            raise ValueError("Use a fixed Trends URL date range matching the configured observation window")
        return self


class NicheConfig(StrictModel):
    niche_id: str
    name: str = Field(min_length=1)
    intended_audience: str = Field(min_length=1)
    catalog_query: str = Field(min_length=1)
    catalog_enabled: bool = True
    trends_enabled: bool = True
    catalog_snapshot: Path | None = None
    trends: TrendsDataset | None = None

    @model_validator(mode="after")
    def check_identity(self):
        if self.niche_id not in NICHE_KEYWORDS:
            raise ValueError(f"Unknown niche identity: {self.niche_id}")
        expected = NICHE_KEYWORDS[self.niche_id]
        if _normalized(self.name) != _normalized(expected[0]):
            raise ValueError("Niche name and stable identity disagree")
        if not self.catalog_query.strip():
            raise ValueError("Catalog query must not be blank")
        if self.trends:
            if self.trends.niche_id != self.niche_id:
                raise ValueError("Trends dataset belongs to a different niche")
            if _normalized(self.trends.keyword) not in {_normalized(k) for k in expected}:
                raise ValueError("Trends keyword does not belong to this niche")
        return self


class BatchConfig(StrictModel):
    catalog_limit: int = Field(default=10, ge=1, le=20)
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    niches: list[NicheConfig] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def check_four_distinct_niches(self):
        if {n.niche_id for n in self.niches} != set(NICHE_KEYWORDS):
            raise ValueError("Exactly the four distinct configured niche identities are required")
        datasets = [n.trends.csv_path.resolve() for n in self.niches if n.trends]
        if len(datasets) != len(set(datasets)):
            raise ValueError("One Trends CSV cannot be mapped to multiple niches")
        return self


class CatalogCoverage(StrictModel):
    status: Literal["success", "empty", "partial", "failed", "disabled", "not_configured"]
    query: str
    provenance: Literal["live", "cache_replay", "not_collected"] = "not_collected"
    retrieved_at: datetime | None = None
    request_url: str | None = None
    raw_items_returned: int = 0
    parsed_items: int = 0
    relevance_candidates: int = 0
    source_total_matches: int | None = None
    record_ids: dict[str, str] = Field(default_factory=dict)


class TrendInterpretation(StrictModel):
    direction: str = "insufficient evidence"
    keyword: str | None = None
    region: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    supporting_fact_ids: list[str] = Field(default_factory=list)
    note: str = "Chưa có dữ liệu Trends phù hợp để diễn giải xu hướng."


class NicheResult(StrictModel):
    run_id: str
    niche_id: str
    name: str
    intended_audience: str
    catalog: CatalogCoverage
    trends_status: Literal["available", "missing", "disabled", "failed"]
    trends_provenance: dict = Field(default_factory=dict)
    trend_observations: int = 0
    trend_metrics: list[ComputedTrendMetric] = Field(default_factory=list)
    trend_interpretation: TrendInterpretation = Field(default_factory=TrendInterpretation)
    evidence_coverage: Literal["CATALOG_AND_TRENDS", "CATALOG_ONLY", "TRENDS_ONLY", "NO_EVIDENCE"]
    trend_sufficient: bool = False
    llm_execution_status: Literal["NOT_REQUESTED"] = "NOT_REQUESTED"
    claim_validation_status: Literal["NOT_RUN"] = "NOT_RUN"
    analysis_status: Literal["DETERMINISTIC_SUMMARY"] = "DETERMINISTIC_SUMMARY"
    completeness: Literal["PARTIAL_EVIDENCE", "BLOCKED", "INSUFFICIENT_EVIDENCE"]
    supported_metrics: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    candidate_product_hypotheses: list[dict] = Field(default_factory=list)
    ranking_status: Literal["NOT_YET_RANKABLE"] = "NOT_YET_RANKABLE"
    cross_niche_trends_comparable: Literal[False] = False


def load_batch_config(path: str | Path) -> BatchConfig:
    path = Path(path).resolve()
    config = BatchConfig.model_validate_json(path.read_text(encoding="utf-8"))
    for niche in config.niches:
        if niche.catalog_snapshot is not None:
            niche.catalog_snapshot = (path.parent / niche.catalog_snapshot).resolve()
        if niche.trends:
            niche.trends.csv_path = (path.parent / niche.trends.csv_path).resolve()
    return BatchConfig.model_validate(config.model_dump())


class DatasetMappingError(ValueError):
    """Configuration/data mismatch: reject before performing any provider requests."""


def _load_trends(niche: NicheConfig, run_id: str) -> list[Evidence]:
    dataset = niche.trends
    if dataset is None or not niche.trends_enabled:
        return []
    rows = GoogleTrendsCsvCollector(dataset.csv_path, dataset.source_url).collect(
        run_id=run_id, niche=niche.name, keyword=dataset.keyword, target_region="United States")
    if {_normalized(row.keyword) for row in rows} != {_normalized(dataset.keyword)}:
        raise DatasetMappingError(f"{niche.niche_id}: CSV keyword series does not match the configured dataset")
    regions = {_normalized(str(row.raw_observation.get("geographic_metadata", ""))) for row in rows}
    if not regions or not regions <= {"hoa kỳ", "united states", "us"}:
        raise DatasetMappingError(f"{niche.niche_id}: CSV region is missing or does not match US")
    try:
        periods = [date.fromisoformat(row.observation_period) for row in rows]
    except ValueError as exc:
        raise DatasetMappingError(f"{niche.niche_id}: CSV has an invalid observation date") from exc
    if min(periods) != dataset.period_start or max(periods) != dataset.period_end:
        raise DatasetMappingError(f"{niche.niche_id}: CSV time window does not match the configured dataset")
    if len({row.evidence_id for row in rows}) != len(rows):
        raise DatasetMappingError(f"{niche.niche_id}: duplicate CSV observations are ambiguous")
    if any(row.metric_value is not None and
           (not math.isfinite(float(row.metric_value)) or not 0 <= float(row.metric_value) <= 100) for row in rows):
        raise DatasetMappingError(f"{niche.niche_id}: index outside the Google Trends 0-100 range")
    return rows


def _catalog_result(niche: NicheConfig, registry: ToolRegistry, live: bool, limit: int) -> CollectionResult | None:
    if not niche.catalog_enabled:
        return None
    if live:
        return registry.call("openlibrary_search", budget=4, query=niche.catalog_query, limit=limit)
    if niche.catalog_snapshot is None:
        return None
    result = _replay_snapshot(niche.catalog_snapshot)
    if _normalized(result.query) != _normalized(niche.catalog_query):
        raise ValueError("Cached catalog query does not match this niche's configured query")
    if any(_normalized(item.original_query) != _normalized(result.query) for item in result.items):
        raise ValueError("Cached item query does not match snapshot provenance")
    request = urlparse(result.request_url)
    params = parse_qs(request.query)
    if (request.scheme != "https" or request.hostname != "openlibrary.org" or request.path != "/search.json"
            or len(params.get("q", [])) != 1 or _normalized(params["q"][0]) != _normalized(result.query)):
        raise ValueError("Cached request URL does not match Open Library query provenance")
    if any(item.request_url != result.request_url or item.retrieved_at != result.retrieved_at or
           item.source_name != "Open Library Search API" or not re.fullmatch(r"OL[0-9]+W", item.work_id) or
           item.work_url != f"https://openlibrary.org/works/{item.work_id}" for item in result.items):
        raise ValueError("Cached record attribution does not match snapshot provenance")
    # Relevance candidates must be a subset of unique parsed works, including for older caches.
    unique = {item.work_id: item for item in result.items}
    result.duplicate_records_skipped += len(result.items) - len(unique)
    result.items = list(unique.values())
    for item in result.items:
        item.relevance_score, item.relevance_status = OpenLibraryCollector.relevance(result.query, item.title)
    result.relevant_items = [item for item in result.items if item.relevance_status == "candidate"]
    if result.duplicate_records_skipped and result.status == "success":
        result.status = "partial"
    return result


def _json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def _summarize(niche: NicheConfig, run_id: str, catalog: CollectionResult | None,
               rows: list[Evidence], trends_error: dict | None, catalog_error: dict | None,
               live: bool, csv_digest: str | None) -> NicheResult:
    coverage = CatalogCoverage(status="disabled" if not niche.catalog_enabled else
        "failed" if catalog_error else "not_configured", query=niche.catalog_query)
    errors = [e for e in (catalog_error, trends_error) if e]
    warnings = []
    if catalog:
        coverage = CatalogCoverage(status=catalog.status, query=catalog.query, provenance=catalog.provenance,
            retrieved_at=catalog.retrieved_at, request_url=catalog.request_url,
            raw_items_returned=catalog.raw_items_returned, parsed_items=len(catalog.items),
            relevance_candidates=len(catalog.relevant_items), source_total_matches=catalog.source_total_matches,
            record_ids={item.work_id: f"{run_id}-catalog-{item.work_id}" for item in catalog.items})
        errors.extend({"source": "Open Library", **error} for error in catalog.errors)
        warnings.extend(catalog.warnings)
    elif live and niche.catalog_enabled:
        coverage.provenance = "live"
    elif niche.catalog_enabled and niche.catalog_snapshot is not None:
        coverage.provenance = "cache_replay"
    trends_status = ("disabled" if not niche.trends_enabled else "failed" if trends_error else
                     "available" if rows else "missing")
    metrics = compute_trend_metrics([row.model_dump(mode="json") for row in rows])
    # The engine's legacy peak dict omits source_url although its shared typed contract requires it.
    # Bind it from the same niche's evidence without changing the golden engine's metric output.
    source_urls = {row.evidence_id: row.source_url for row in rows}
    for metric in metrics:
        if metric.get("peak"):
            metric["peak"]["source_url"] = source_urls[metric["peak"]["evidence_id"]]
    facts = build_verified_facts([row.model_dump(mode="json") for row in rows], metrics)
    supported = next((metric for metric in metrics if metric["status"] == "computed"), None)
    interpretation = TrendInterpretation()
    provenance = {}
    if rows and niche.trends:
        dataset = niche.trends
        provenance = {**dataset.model_dump(mode="json"), "collection_method": "manual_csv_import",
            "imported_at": rows[0].collected_at.isoformat(),
            "csv_sha256": csv_digest,
            "freshness_note": "Kỳ quan sát CSV được giữ nguyên; thời điểm import không làm mới dữ liệu nguồn."}
        interpretation = TrendInterpretation(keyword=dataset.keyword, region=dataset.region,
            period_start=dataset.period_start, period_end=dataset.period_end,
            direction=supported["direction"] if supported else "insufficient evidence",
            supporting_fact_ids=[fact.fact_id for fact in facts if fact.metric_name == "four_week_average_direction"],
            note=("Python so sánh trung bình hai cửa sổ 4 tuần hoàn tất trong cùng chuỗi; "
                  "index tương đối không đo sales hay search volume; không so trực tiếp CSV chuẩn hóa riêng giữa các niche.")
                 if supported else "Có CSV nhưng chưa đủ hai cửa sổ tuần hoàn tất hợp lệ để so sánh xu hướng.")
    has_catalog = bool(catalog and catalog.items)
    category = ("CATALOG_AND_TRENDS" if has_catalog and rows else "CATALOG_ONLY" if has_catalog else
                "TRENDS_ONLY" if rows else "NO_EVIDENCE")
    missing = ["Sales", "Giá", "Reviews/thảo luận khách hàng", "US retail availability",
               "Đối thủ cạnh tranh trực tiếp đã xác minh", "Nhu cầu mua và product-market fit"]
    if not supported:
        missing.append("Chưa có Trends dataset phù hợp/đủ kỳ để diễn giải xu hướng")
    if not has_catalog:
        missing.append("Chưa có catalog metadata")
    if has_catalog and not coverage.relevance_candidates:
        missing.append("Chưa có catalog candidate vượt ngưỡng relevance")
    return NicheResult(run_id=run_id, niche_id=niche.niche_id, name=niche.name,
        intended_audience=niche.intended_audience, catalog=coverage, trends_status=trends_status,
        trends_provenance=provenance, trend_observations=len(rows), trend_metrics=metrics,
        trend_interpretation=interpretation, evidence_coverage=category, trend_sufficient=bool(supported),
        completeness="PARTIAL_EVIDENCE" if rows or has_catalog else "BLOCKED"
            if coverage.status == "failed" or trends_status == "failed" else "INSUFFICIENT_EVIDENCE",
        supported_metrics=(["catalog_metadata", "title_relevance_candidates"] if has_catalog else []) +
            (["within_series_four_week_relative_interest_comparison"] if supported else []),
        missing_data=missing, errors=errors, warnings=warnings,
        candidate_product_hypotheses=[{"concept": niche.name, "intended_audience": niche.intended_audience,
            "status": "UNTESTED_HYPOTHESIS", "origin": "configured niche; not a market conclusion",
            "discovery_source_urls": [item.work_url for item in catalog.relevant_items[:3]] if catalog else [],
            "next_step": "Kiểm chứng concept với khách hàng và nguồn retail Hoa Kỳ; thu thập dữ liệu còn thiếu."}])


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _write_niche(directory: Path, result: NicheResult, catalog: CollectionResult | None, rows: list[Evidence]) -> None:
    facts = build_verified_facts([row.model_dump(mode="json") for row in rows], result.trend_metrics)
    evidence_ids = {row.evidence_id for row in rows}
    if any(not set(fact.evidence_ids) <= evidence_ids for fact in facts):
        raise ValueError("Fact references evidence outside its niche")
    artifacts = {"result": "result.json", "summary": "summary.md", "trends_evidence": "trends-evidence.json",
                 "verified_facts": "verified-facts.json"}
    if catalog:
        artifacts["catalog_snapshot"] = "catalog-snapshot.json"
        _json(directory / artifacts["catalog_snapshot"], catalog.model_dump(mode="json"))
    result.artifacts = artifacts
    _json(directory / artifacts["trends_evidence"], [row.model_dump(mode="json") for row in rows])
    _json(directory / artifacts["verified_facts"], [fact.model_dump(mode="json") for fact in facts])
    _json(directory / artifacts["result"], result.model_dump(mode="json"))
    lines = [f"# {result.name}", "", f"Niche ID: {result.niche_id}; run: {result.run_id}", "",
        f"Coverage: {result.evidence_coverage}; completeness: {result.completeness}.",
        f"Catalog: {result.catalog.status}; raw={result.catalog.raw_items_returned}; parsed={result.catalog.parsed_items}; "
        f"candidates={result.catalog.relevance_candidates}; provenance={result.catalog.provenance}.",
        f"Catalog retrieved_at: {result.catalog.retrieved_at}; request: {result.catalog.request_url}",
        f"Trends: {result.trends_status}; observations={result.trend_observations}; "
        f"supported direction={result.trend_interpretation.direction}.", result.trend_interpretation.note,
        f"LLM: {result.llm_execution_status}; claim validation: {result.claim_validation_status}; "
        f"analysis: {result.analysis_status}.", "", "## Catalog discovery", ""]
    if catalog:
        lines.extend(f"- [{_cell(item.title).replace('[', '(').replace(']', ')')}]({item.work_url}) — "
                     f"candidate, chưa xác minh là sản phẩm đang bán/đối thủ."
                     for item in catalog.relevant_items[:5])
    lines.extend(["", "## Nguồn và giới hạn", "",
        "Open Library: metadata thư mục; numFound/edition_count không đo market size, sales hoặc số đối thủ. "
        "Coverage có thể thưa; không suy ra US retail availability.",
        f"Trends provenance: {json.dumps(result.trends_provenance, ensure_ascii=False)}", "",
        *[f"- Thiếu: {missing}" for missing in result.missing_data],
        *[f"- Lỗi: {_cell(error)}" for error in result.errors],
        "", f"Ranking: {result.ranking_status}. CSV chuẩn hóa riêng không so index trực tiếp giữa các niche.",
        "", "## Hướng nghiên cứu", "", f"Concept chưa kiểm chứng: {result.name}; {result.intended_audience}.",
        "Xác minh concept với khách hàng và nguồn retail Hoa Kỳ trước khi đưa ra kết luận thị trường.", "",
        *[f"- [{name}]({path})" for name, path in artifacts.items() if name != "summary"], ""])
    with (directory / artifacts["summary"]).open("x", encoding="utf-8") as output:
        output.write("\n".join(lines))


def run_batch(config: BatchConfig | str | Path, *, output_dir: str | Path = "outputs/batches",
              collect_live: bool = False, collector: OpenLibraryCollector | None = None) -> dict:
    """Network is opt-in; LLM inference is intentionally outside this batch contract."""
    config = load_batch_config(config) if isinstance(config, (str, Path)) else BatchConfig.model_validate(config.model_dump())
    batch_id = f"batch-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
    # Preflight dataset mappings before any live collection or output creation.
    prepared = {}
    for niche in config.niches:
        run_id = f"{batch_id}-{niche.niche_id}"
        try:
            dataset = niche.trends if niche.trends_enabled else None
            digest = hashlib.sha256(dataset.csv_path.read_bytes()).hexdigest() if dataset else None
            rows = _load_trends(niche, run_id)
            if dataset and digest != hashlib.sha256(dataset.csv_path.read_bytes()).hexdigest():
                raise DatasetMappingError(f"{niche.niche_id}: CSV changed during import")
            prepared[niche.niche_id] = (rows, None, digest)
        except DatasetMappingError:
            raise
        except (OSError, ValueError) as exc:
            prepared[niche.niche_id] = ([], {"source": "Google Trends CSV", "kind": type(exc).__name__, "message": str(exc)}, None)
    directory = Path(output_dir).resolve() / batch_id
    directory.mkdir(parents=True, exist_ok=False)
    _json(directory / "config.json", config.model_dump(mode="json"))
    logger = ExecutionLogger(directory / "execution.jsonl", batch_id)
    collector = collector or OpenLibraryCollector(timeout=config.request_timeout_seconds)
    registry = ToolRegistry({collector.name: collector.collect})
    results = []
    for niche in config.niches:
        niche_dir = directory / niche.niche_id
        niche_dir.mkdir(exist_ok=False)
        rows, trends_error, digest = prepared[niche.niche_id]
        catalog = None
        catalog_error = None
        try:
            catalog = _catalog_result(niche, registry, collect_live, config.catalog_limit)
        except Exception as exc:
            # Transport, snapshot and provider errors are independent per niche.
            catalog_error = {"source": "Open Library", "kind": type(exc).__name__, "message": str(exc)}
        result = _summarize(niche, f"{batch_id}-{niche.niche_id}", catalog, rows,
                            trends_error, catalog_error, collect_live, digest)
        _write_niche(niche_dir, result, catalog, rows)
        logger.step("collect_niche", "MultiNiche", result.completeness,
            f"{niche.niche_id}; catalog={result.catalog.status}; trends={result.trends_status}",
            json.dumps(result.errors, ensure_ascii=False) if result.errors else None)
        summary = result.model_dump(mode="json", exclude={"trend_metrics", "trends_provenance", "artifacts", "warnings",
                                                          "candidate_product_hypotheses"})
        summary["catalog"].pop("record_ids")
        summary["result_path"] = f"{niche.niche_id}/result.json"
        summary["summary_path"] = f"{niche.niche_id}/summary.md"
        results.append(summary)
    batch = {"batch_id": batch_id, "output_directory": str(directory), "execution_status": "COMPLETED",
        "collection_mode": "live" if collect_live else "offline", "llm_execution_status": "NOT_REQUESTED",
        "claim_validation_status": "NOT_RUN", "ranking_status": "NOT_YET_RANKABLE",
        "cross_niche_trends_comparable": False, "niches": results}
    _json(directory / "summary.json", batch)
    header = ["# So sánh độ phủ bằng chứng của bốn niche", "",
        "Bảng này so sánh coverage, chưa xếp hạng cơ hội thị trường. LLM: NOT_REQUESTED; claim validation: NOT_RUN.",
        "Không so trực tiếp index từ các CSV chuẩn hóa riêng. Open Library không xác nhận sales, giá, market size hay US retail availability.", "",
        "| Niche | Parsed | Candidates | Trends | Diễn giải trend | Coverage | Collection | Analysis | Thiếu | Freshness |",
        "|---|---:|---:|---|---|---|---|---|---|---|"]
    for row in results:
        c = row["catalog"]
        freshness = f"catalog: {c['provenance']} @ {c['retrieved_at']}; Trends: " + (
            f"{row['trend_interpretation']['period_start']}..{row['trend_interpretation']['period_end']} (manual CSV)"
            if row["trends_status"] == "available" else row["trends_status"])
        header.append("| " + " | ".join(map(_cell, [f"[{row['name']}]({row['summary_path']})", c["parsed_items"],
            c["relevance_candidates"], row["trends_status"], row["trend_interpretation"]["direction"],
            row["evidence_coverage"], c["status"], row["analysis_status"], "; ".join(row["missing_data"]), freshness])) + " |")
    with (directory / "summary.md").open("x", encoding="utf-8") as output:
        output.write("\n".join(header) + "\n")
    return batch


def main() -> None:
    parser = argparse.ArgumentParser(description="Four-niche evidence collection without Ollama or ranking")
    parser.add_argument("--config", default="niches.json")
    parser.add_argument("--output-dir", default="outputs/batches")
    parser.add_argument("--collect-live", action="store_true", help="Opt in to actual Open Library HTTP calls")
    parser.add_argument("--no-ollama", action="store_true", help="Explicitly acknowledge the default: no LLM execution")
    args = parser.parse_args()
    try:
        batch = run_batch(args.config, output_dir=args.output_dir, collect_live=args.collect_live)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Batch configuration/output error: {exc}\n")
    print(json.dumps({"batch_id": batch["batch_id"], "output_directory": batch["output_directory"],
        "execution_status": batch["execution_status"], "niches": [{"niche_id": n["niche_id"],
            "catalog": n["catalog"]["status"], "parsed": n["catalog"]["parsed_items"],
            "candidates": n["catalog"]["relevance_candidates"], "trends": n["trends_status"],
            "coverage": n["evidence_coverage"], "analysis": n["analysis_status"]} for n in batch["niches"]]},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
