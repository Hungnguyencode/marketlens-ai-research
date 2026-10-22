"""Presentation snapshots only. SQLite and the existing bundle verifier stay authoritative."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from .research_workspace import WorkspaceError, VerifiedReports, safe_child, verified_reports
from .research_contract import ResearchOutcome

MAX_VERIFICATION_ATTEMPTS = 3


def bundle_fingerprint(store, job):
    """Cheap invalidation for displayed snapshots; this does NOT approve downloads."""
    if job.result_path != f"{job.job_id}/result/manifest.json":
        raise WorkspaceError("Tham chiếu kết quả chưa hợp lệ.")
    manifest_path = safe_child(store.artifact_root, job.result_path)
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    paths = [manifest_path] + [safe_child(manifest_path.parent, item["path"])
                               for item in manifest["artifacts"].values()]
    return (hashlib.sha256(raw).hexdigest(), tuple(
        (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        for path in paths for stat in [path.stat()]))


@dataclass
class ResultSnapshot:
    verified: VerifiedReports | None = None
    fingerprint: tuple | None = None
    attempts: int = 0
    error: str | None = None
    checked_at: datetime | None = None

    def load(self, store, job):
        if self.verified is None and self.attempts >= MAX_VERIFICATION_ATTEMPTS:
            return self
        try:
            current = bundle_fingerprint(store, job)
        except (OSError, ValueError, KeyError, TypeError):
            current = None
        if self.verified is not None and current == self.fingerprint:
            return self
        if self.verified is not None:
            self.verified, self.attempts = None, 0
        self.attempts += 1
        try:
            self.verified = verified_reports(store, job.job_id)
            if ResearchOutcome.from_result(self.verified.result, job.request.model) != job.research_outcome:
                raise WorkspaceError("Job đã đổi sau lần đọc trạng thái; cần xác minh lại.")
            self.fingerprint = bundle_fingerprint(store, job)
            self.checked_at = datetime.now(timezone.utc)
            self.error = None
        except (OSError, ValueError, KeyError, TypeError):
            self.verified = None
            self.error = "Chưa thể xác minh bundle: file thiếu, đường dẫn không hợp lệ hoặc checksum không khớp. Tải xuống đang bị chặn."
        return self


def verified_download(store, job_id, role, expected_digest):
    """Deferred download: verify the whole bundle again, then deliver the exact shown bytes."""
    if role not in {"markdown", "html", "json"}:
        raise WorkspaceError("Loại báo cáo không hợp lệ.")
    _, content = verified_reports(store, job_id).downloads[role]
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise WorkspaceError("Báo cáo đã đổi so với bản đang xem. Hãy làm mới trước khi tải.")
    return content
