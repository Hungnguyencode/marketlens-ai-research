"""Read-only dashboard adapters. No collectors, inference, database or artifact writes."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .comparison import (CommercialDecision, ComparisonConfig, JointComparisonDataset, KeywordMetrics,
                         assess_commercial_opportunity, compare_keyword_signals, parse_comparison)
from .models import ResearchResult
from .multi_niche import NICHE_KEYWORDS, CatalogCoverage, NicheResult
from .openlibrary_collector import CollectionResult


class ArtifactError(ValueError):
    pass


@dataclass
class BatchView:
    run_id: str
    summary: dict
    niches: dict[str, NicheResult]
    paths: dict[str, str]
    catalogs: dict[str, CollectionResult] = field(default_factory=dict)
    catalog_issues: dict[str, str] = field(default_factory=dict)


@dataclass
class ComparisonView:
    run_id: str
    batch_id: str
    report: dict
    dataset: JointComparisonDataset
    metrics: dict[str, KeywordMetrics]


@dataclass
class DashboardIndex:
    batches: dict[str, BatchView] = field(default_factory=dict)
    comparisons: dict[str, ComparisonView] = field(default_factory=dict)
    historical: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    loaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ArtifactStore:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, repository: str | Path):
        self.repository = Path(repository).resolve()
        self.outputs = (self.repository / "outputs").resolve()
        if not self.outputs.is_relative_to(self.repository):
            raise ArtifactError("Thư mục outputs nằm ngoài repository")

    def safe_path(self, relative: str) -> Path:
        part = Path(relative)
        if part.is_absolute() or part.drive or ".." in part.parts or not part.parts:
            raise ArtifactError("Đường dẫn artifact không an toàn")
        path = (self.outputs / part).resolve()
        if not path.is_relative_to(self.outputs):
            raise ArtifactError("Artifact nằm ngoài outputs")
        return path

    def child(self, base: str, relative: str) -> str:
        base_path = self.safe_path(base)
        path = self.safe_path(f"{base}/{relative}")
        if Path(relative).is_absolute() or not path.is_relative_to(base_path):
            raise ArtifactError("Tham chiếu artifact nằm ngoài run được chọn")
        return path.relative_to(self.outputs).as_posix()

    def read(self, relative: str) -> bytes:
        path = self.safe_path(relative)
        if not path.is_file() or path.stat().st_size > self.MAX_BYTES:
            raise ArtifactError(f"Artifact thiếu hoặc quá lớn: {relative}")
        return path.read_bytes()

    def json(self, relative: str) -> dict:
        try:
            result = json.loads(self.read(relative).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ArtifactError(f"JSON không hợp lệ: {relative}") from exc
        if not isinstance(result, dict):
            raise ArtifactError(f"Artifact phải là JSON object: {relative}")
        return result

    @staticmethod
    def run_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,160}", value):
            raise ArtifactError("Run ID không hợp lệ")
        return value

    def folders(self, kind: str) -> list[str]:
        root = self.safe_path(kind)
        if not root.exists():
            return []
        candidates = []
        for path in root.iterdir():
            try:
                self.run_id(path.name)
                safe = self.safe_path(f"{kind}/{path.name}")
                if safe.is_dir():
                    candidates.append((safe.stat().st_mtime_ns, path.name))
            except (ArtifactError, OSError):
                continue
        return [name for _, name in sorted(candidates, reverse=True)]

    def historical_paths(self) -> dict[str, str]:
        result = {}
        if self.outputs.is_dir():
            for path in sorted(self.outputs.glob("*.json"), reverse=True):
                if re.fullmatch(r"[a-f0-9]{12}-.+\.json", path.name):
                    key = path.name.split("-", 1)[0]
                    if key not in result:
                        result[key] = path.name
        return result

    def fingerprint(self) -> tuple:
        """Metadata-only cache key; includes new runs, source changes and removals."""
        files = set(self.historical_paths().values())
        for kind, filenames in [("batches", ("summary.json",)),
                                ("comparisons", ("comparison.json", "dataset.json", "source.csv", "comparison.md"))]:
            for run_id in self.folders(kind):
                files.add(f"{kind}/{run_id}")
                files.update(f"{kind}/{run_id}/{name}" for name in filenames)
                if kind == "batches":
                    files.update(f"{kind}/{run_id}/{niche_id}/{name}" for niche_id in NICHE_KEYWORDS
                                 for name in ("result.json", "catalog-snapshot.json"))
        stamps = []
        for relative in sorted(files):
            try:
                stat = self.safe_path(relative).stat()
                stamps.append((relative, stat.st_mtime_ns, stat.st_size))
            except (OSError, ArtifactError):
                stamps.append((relative, None, None))
        return tuple(stamps)

    def load_batch(self, run_id: str) -> BatchView:
        base = f"batches/{self.run_id(run_id)}"
        summary = self.json(f"{base}/summary.json")
        entries = summary.get("niches")
        if (summary.get("batch_id") != run_id or not isinstance(entries, list) or len(entries) != 4 or
                any(not isinstance(row, dict) for row in entries) or
                {row.get("niche_id") for row in entries} != set(NICHE_KEYWORDS)):
            raise ArtifactError("Batch phải có đúng bốn stable niche ID và batch ID khớp thư mục")
        view = BatchView(run_id, summary, {}, {})
        for entry in entries:
            niche_id = entry["niche_id"]
            path = self.child(base, entry["result_path"])
            result = NicheResult.model_validate(self.json(path))
            if result.niche_id != niche_id or result.run_id != f"{run_id}-{niche_id}":
                raise ArtifactError("Identity của niche không khớp batch")
            c = result.catalog
            if not 0 <= c.relevance_candidates <= c.parsed_items <= c.raw_items_returned:
                raise ArtifactError("Catalog counts không nhất quán")
            view.niches[niche_id], view.paths[niche_id] = result, path
            try:
                relative = result.artifacts.get("catalog_snapshot")
                if relative is None:
                    raise ArtifactError("Run không có catalog snapshot")
                snapshot = self.child(str(Path(path).parent.as_posix()), relative)
                catalog = CollectionResult.model_validate(self.json(snapshot))
                if (catalog.query != c.query or catalog.provenance != c.provenance or catalog.retrieved_at != c.retrieved_at or
                        len(catalog.items) != c.parsed_items or len(catalog.relevant_items) != c.relevance_candidates or
                        catalog.raw_items_returned != c.raw_items_returned or catalog.request_url != c.request_url):
                    raise ArtifactError("Catalog snapshot không khớp metadata của niche")
                ids = {item.work_id for item in catalog.items}
                if len(ids) != len(catalog.items) or not {item.work_id for item in catalog.relevant_items} <= ids:
                    raise ArtifactError("Catalog snapshot có ID trùng hoặc candidate không tồn tại")
                request = urlparse(catalog.request_url)
                if (request.scheme != "https" or request.hostname != "openlibrary.org" or request.path != "/search.json" or
                        parse_qs(request.query).get("q") != [catalog.query]):
                    raise ArtifactError("Catalog request URL không khớp query/provider")
                for item in catalog.items:
                    if (not re.fullmatch(r"OL[0-9]+W", item.work_id) or
                            item.work_url != f"https://openlibrary.org/works/{item.work_id}" or
                            item.original_query != catalog.query or item.request_url != catalog.request_url or
                            item.retrieved_at != catalog.retrieved_at or item.provenance != catalog.provenance):
                        raise ArtifactError("Provenance của book record không hợp lệ")
                view.catalogs[niche_id] = catalog
            except (OSError, ValueError, KeyError, TypeError) as exc:
                view.catalog_issues[niche_id] = str(exc)[:300]
        return view

    def load_comparison(self, run_id: str, batches: dict[str, BatchView]) -> ComparisonView:
        base = f"comparisons/{self.run_id(run_id)}"
        report = self.json(f"{base}/comparison.json")
        batch_id = report.get("batch_provenance", {}).get("batch_id")
        if report.get("run_id") != run_id or batch_id not in batches:
            raise ArtifactError("Comparison không tham chiếu batch hợp lệ đang có trong repository")
        batch = batches[batch_id]
        paths = {"summary.json": f"batches/{batch_id}/summary.json"}
        paths.update({Path(path).relative_to(f"batches/{batch_id}").as_posix(): path for path in batch.paths.values()})
        hashes = report["batch_provenance"].get("artifact_sha256", {})
        if set(hashes) != set(paths) or any(hashlib.sha256(self.read(path)).hexdigest() != hashes[name]
                                          for name, path in paths.items()):
            raise ArtifactError("STALE_REFERENCE: batch đã thay đổi so với checksum comparison; không ghép dữ liệu")
        artifact = report.get("artifacts", {})
        dataset = JointComparisonDataset.model_validate(self.json(self.child(base, artifact["dataset"])))
        source = self.read(self.child(base, artifact["source_csv"]))
        if (dataset.dataset_id != report.get("dataset_id") or report.get("normalization_context") != "single_joint_export" or
                hashlib.sha256(source).hexdigest() != dataset.csv_sha256 or
                dataset.model_dump(mode="json", exclude={"series"}) != report.get("dataset_provenance")):
            raise ArtifactError("Dataset context/checksum không khớp comparison")
        policy = report["metric_policy"]
        if str(dataset.as_of) != policy.get("as_of"):
            raise ArtifactError("Completed-week cutoff không khớp dataset")
        config = ComparisonConfig.model_validate({**dataset.declared_export_settings,
            "csv_path": self.safe_path(self.child(base, artifact["source_csv"])),
            "batch_directory": self.safe_path(f"batches/{batch_id}"), "as_of": dataset.as_of})
        reparsed = parse_comparison(config, csv_bytes=source)
        if reparsed.dataset_id != dataset.dataset_id or reparsed.series != dataset.series or reparsed.metadata_preamble != dataset.metadata_preamble:
            raise ArtifactError("Observations trong dataset không khớp CSV gốc")
        computed = compare_keyword_signals(dataset, sparse_threshold=policy["sparse_positive_fraction_threshold"],
                                          minimum_positive_per_window=policy["minimum_positive_weeks_per_window"])
        rows = report.get("niches", [])
        if len(rows) != 4 or {row["niche_id"] for row in rows} != set(NICHE_KEYWORDS):
            raise ArtifactError("Comparison phải có đúng bốn niche độc lập")
        by_id = {item.niche_id: item for item in computed}
        for row in rows:
            niche_id = row["niche_id"]
            actual = KeywordMetrics.model_validate(row["keyword_metrics"])
            catalog = CatalogCoverage.model_validate(row["catalog"])
            if actual != by_id[niche_id] or catalog != batch.niches[niche_id].catalog:
                raise ArtifactError("Metric hoặc catalog không khớp nguồn tham chiếu")
            if row["catalog_source"]["batch_id"] != batch_id or not row.get("standalone_trends_excluded_from_comparison"):
                raise ArtifactError("Comparison trộn normalization context/batch khác")
            if row["catalog_source"]["artifact"] != Path(batch.paths[niche_id]).relative_to(f"batches/{batch_id}").as_posix():
                raise ArtifactError("Catalog artifact không khớp niche tham chiếu")
        expected_order = [m.niche_id for m in sorted(computed, key=lambda item: (-(item.mean_relative_index or 0), item.keyword))]
        if report.get("observed_keyword_signal_order") != expected_order:
            raise ArtifactError("Observed keyword order không khớp metric nguồn")
        decision = CommercialDecision.model_validate(report["commercial_assessment"])
        if decision != assess_commercial_opportunity(computed, batch.niches):
            raise ArtifactError("Ranking decision không khớp phạm vi bằng chứng hỗ trợ")
        if (report.get("llm_execution_status") != "NOT_REQUESTED" or report.get("claim_validation_status") != "NOT_RUN" or
                report.get("analysis_status") != "DETERMINISTIC_COMPARISON"):
            raise ArtifactError("Analysis provenance không tương thích dashboard comparison")
        return ComparisonView(run_id, batch_id, report, dataset, by_id)

    def discover(self) -> DashboardIndex:
        index = DashboardIndex(historical=self.historical_paths())
        for run_id in self.folders("batches"):
            try:
                index.batches[run_id] = self.load_batch(run_id)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                index.issues.append(f"{run_id}: {str(exc)[:350]}")
        for run_id in self.folders("comparisons"):
            try:
                index.comparisons[run_id] = self.load_comparison(run_id, index.batches)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                index.issues.append(f"{run_id}: {str(exc)[:350]}")
        return index

    def load_historical(self, run_id: str) -> ResearchResult:
        path = self.historical_paths().get(self.run_id(run_id))
        if not path:
            raise ArtifactError("Không tìm thấy run lịch sử được chọn")
        result = ResearchResult.model_validate(self.json(path))
        if result.run_id != run_id:
            raise ArtifactError("Historical run ID không khớp filename")
        return result


def overview_rows(batch: BatchView, comparison: ComparisonView | None) -> list[dict]:
    if comparison and comparison.batch_id != batch.run_id:
        raise ArtifactError("Không thể hiển thị comparison thuộc batch khác")
    return [{"Niche": result.name, "Catalog": result.catalog.status,
             "Parsed": result.catalog.parsed_items, "Candidates": result.catalog.relevance_candidates,
             "CSV standalone trong batch": result.trends_status,
             "Comparison chung": "Có" if comparison else "Chưa có comparison tương thích",
             "Truy xuất catalog (UTC)": str(result.catalog.retrieved_at or "Chưa thu"),
             "Analysis": result.analysis_status, "LLM": result.llm_execution_status,
             "Claim validation": result.claim_validation_status}
            for result in batch.niches.values()]


def default_selection(index: DashboardIndex) -> tuple[str | None, str | None]:
    if index.comparisons:
        view = next(iter(index.comparisons.values()))
        return view.batch_id, view.run_id
    return (next(iter(index.batches)), None) if index.batches else (None, None)


def keyword_rows(comparison: ComparisonView) -> list[dict]:
    return [{"Keyword": m.keyword, "Mean index": m.mean_relative_index, "Max index": m.maximum_relative_index,
             "Tuần dương hoàn tất": m.positive_completed_weeks, "% tuần dương": m.positive_completed_percentage,
             "Tuần hoàn tất": m.completed_weeks, "Tổng tuần raw": m.observed_weeks,
             "Sparse signal": "Có" if m.sparse_signal else "Không", "Diễn giải": m.direction}
            for m in comparison.metrics.values()]


def chart_rows(comparison: ComparisonView, niche_ids: list[str], include_incomplete: bool = False) -> list[dict]:
    return [{"Ngày": str(p.period_start), "Keyword": s.keyword, "Index tương đối": p.relative_index,
             "complete_week": p.complete_week, "Kỳ quan sát": "Hoàn tất" if p.complete_week else "Chưa hoàn tất"}
            for s in comparison.dataset.series if s.niche_id in niche_ids for p in s.observations
            if p.complete_week or include_incomplete]


def catalog_rows(batch: BatchView, niche_id: str, candidates_only: bool = True) -> list[dict]:
    catalog = batch.catalogs.get(niche_id)
    if not catalog:
        return []
    candidates = {item.work_id for item in catalog.relevant_items}
    return [{"Tên sách": item.title, "Tác giả": ", ".join(item.authors) or "Chưa có metadata",
             "Xuất bản đầu": item.first_publish_year, "Editions ghi nhận": item.edition_count,
             "Phân loại": "Relevance candidate" if item.work_id in candidates else "Low relevance",
             "Nguồn": item.work_url} for item in catalog.items
            if not candidates_only or item.work_id in candidates]
