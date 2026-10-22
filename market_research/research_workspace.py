"""Local workspace operations; no collector HTTP, inference, worker startup or Streamlit."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .job_store import JobState, JobStore, JobStoreError
from .models import ResearchResult
from .research_contract import (SECRET_PATTERN, ResearchRequest, ResearchSubmission,
                                prepare_request, resolve_input, verify_bundle)
from .trends_csv import GoogleTrendsCsvCollector, SYNTHETIC_SOURCE, is_synthetic_csv

MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class WorkspaceError(ValueError):
    """Only fixed safe messages are displayed to the user."""


def safe_child(root: Path, name: str) -> Path:
    relative = Path(name)
    if (relative.is_absolute() or PureWindowsPath(name).drive or ":" in name or "\\" in name
            or any(part in {"..", "."} for part in relative.parts)):
        raise WorkspaceError("Đường dẫn artifact không hợp lệ.")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise WorkspaceError("Không chấp nhận liên kết file hoặc thư mục.")
    if not current.resolve().is_relative_to(root.resolve()):
        raise WorkspaceError("File nằm ngoài workspace được phép.")
    return current


def persist_upload(store: JobStore, filename: str, raw: bytes) -> str:
    if (not filename or Path(filename).name != filename or PureWindowsPath(filename).name != filename
            or Path(filename).suffix.lower() != ".csv" or SECRET_PATTERN.search(filename)):
        raise WorkspaceError("Chọn một file .csv với tên file thông thường.")
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise WorkspaceError("CSV phải có dữ liệu và không vượt quá 32 MiB.")
    try:
        text = raw.decode("utf-8-sig")
        if "\x00" in text or SECRET_PATTERN.search(text):
            raise ValueError()
        directory = safe_child(store.input_root, ".marketlens-uploads")
        directory.mkdir(exist_ok=True)
        name = uuid4().hex + ".csv"
        path = safe_child(store.input_root, ".marketlens-uploads/" + name)
        with path.open("xb") as handle:
            handle.write(raw)
        # Display-only metadata, never a filesystem path supplied by the client.
        with path.with_suffix(".json").open("x", encoding="utf-8") as handle:
            json.dump({"original_filename": filename}, handle, ensure_ascii=False)
        return path.relative_to(store.input_root).as_posix()
    except (OSError, UnicodeError, ValueError):
        raise WorkspaceError("Không thể lưu CSV UTF-8 an toàn trong input workspace.") from None


@dataclass(frozen=True)
class CsvPreview:
    request: ResearchRequest
    filename: str
    keyword: str
    geography: str
    observations: list[dict]

    def payload(self):
        return self.request.model_dump(exclude={"input_sha256"})


def preview_upload(store: JobStore, filename: str, raw: bytes, *, topic: str, source_url: str,
                   model: str = "qwen3:4b-instruct", allow_inference: bool = False,
                   inference_confirmed: bool = False) -> CsvPreview:
    if allow_inference and not inference_confirmed:
        raise WorkspaceError("Cần xác nhận riêng trước khi cho phép Ollama inference.")
    try:
        if is_synthetic_csv(raw):
            if source_url.strip() not in {"", SYNTHETIC_SOURCE} or allow_inference:
                raise WorkspaceError("SYNTHETIC demo: để trống URL nguồn và tắt inference; không dùng URL Google Trends cho dữ liệu được tạo.")
            source_url = SYNTHETIC_SOURCE
        submission = ResearchSubmission(topic=topic, mode="csv_import", input_path="pending.csv",
                                        source_url=source_url, model=model, allow_network=False,
                                        allow_inference=allow_inference)
        name = persist_upload(store, filename, raw)
        payload = submission.model_dump()
        payload["input_path"] = name
        request = prepare_request(payload, store.input_root)
        path = resolve_input(store.input_root, request.input_path, request.mode)
        rows = GoogleTrendsCsvCollector(path, source_url).collect(
            run_id="workspace-preview", niche=topic, keyword=topic, target_region="United States")
        normalize = GoogleTrendsCsvCollector._normalized_header
        keywords = {normalize(row.keyword) for row in rows}
        declared = parse_qs(urlparse(source_url).query).get("q", [])
        if len(keywords) != 1 or (source_url != SYNTHETIC_SOURCE and
                                 (len(declared) != 1 or normalize(declared[0]) not in keywords)):
            raise WorkspaceError("CSV phải có đúng một keyword và khớp q trong URL nguồn.")
        geographies = {row.raw_observation.get("geographic_metadata", "") for row in rows}
        if any(value and normalize(value) not in {"us", "united states", "hoa kỳ"} for value in geographies):
            raise WorkspaceError("Vùng ghi trong CSV không khớp United States.")
        return CsvPreview(request, filename, rows[0].keyword,
                          ", ".join(sorted(value for value in geographies if value)) or "CSV không ghi vùng",
                          [{"Kỳ": row.observation_period, "Index gốc": row.raw_observation["raw_value"]}
                           for row in rows])
    except WorkspaceError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkspaceError("CSV hoặc yêu cầu không hợp lệ. Kiểm tra UTF-8, chủ đề, model và URL Google Trends geo=US.") from None


def original_filename(store: JobStore, request: ResearchRequest) -> str:
    try:
        path = resolve_input(store.input_root, request.input_path, request.mode)
        if path.parent.name == ".marketlens-uploads":
            metadata = safe_child(store.input_root, path.with_suffix(".json").relative_to(store.input_root).as_posix())
            value = json.loads(metadata.read_text(encoding="utf-8"))["original_filename"]
            if (isinstance(value, str) and Path(value).name == value and PureWindowsPath(value).name == value
                    and not SECRET_PATTERN.search(value)):
                return value
        return path.name
    except (OSError, ValueError, KeyError, TypeError):
        return "Tên file gốc không còn truy xuất được"


@dataclass(frozen=True)
class VerifiedReports:
    result: ResearchResult
    downloads: dict[str, tuple[str, bytes]]


def verified_reports(store: JobStore, job_id: str) -> VerifiedReports:
    try:
        job = store.get(job_id)  # SQLite, not a browser-supplied artifact path.
        if (job.job_type != "research" or job.state != JobState.COMPLETED or not job.research_outcome
                or job.result_path != f"{job.job_id}/result/manifest.json"):
            raise ValueError()
        directory = safe_child(store.artifact_root, job.job_id)
        manifest_path = safe_child(store.artifact_root, job.result_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stage = manifest_path.parent
        for entry in manifest["artifacts"].values():
            safe_child(stage, entry["path"])
        verify_bundle(directory, job.job_id, job.request, job.research_outcome, directory_name="result")
        downloads = {}
        for role in ("markdown", "html", "json"):
            entry = manifest["artifacts"][role]
            path = safe_child(stage, entry["path"])
            content = path.read_bytes()
            # Hash the exact bytes offered, rather than relying on a previous stat/read.
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError()
            downloads[role] = (path.name, content)
        return VerifiedReports(ResearchResult.model_validate_json(downloads["json"][1]), downloads)
    except (OSError, ValueError, KeyError, TypeError, JobStoreError):
        raise WorkspaceError("Kết quả chưa công bố, file bị thiếu, đường dẫn không an toàn hoặc checksum không khớp. Không cung cấp tải xuống.") from None
