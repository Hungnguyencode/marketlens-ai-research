"""Escaped, workspace-only presentation. No research conclusions are generated here."""
from .dashboard_design import e

STATE_LABELS = {
    "QUEUED": "Đang chờ worker", "RUNNING": "Đang thực hiện", "COMPLETED": "Đã hoàn tất",
    "FAILED": "Thực thi thất bại", "CANCELLED": "Đã hủy", "INTERRUPTED": "Bị gián đoạn",
}
VALIDATION_LABELS = {
    "VALID": "Đạt kiểm định", "DEGRADED": "Còn giới hạn phân tích", "INVALID": "Không đạt kiểm định",
    "BLOCKED": "Bị chặn", "INSUFFICIENT_EVIDENCE": "Chưa đủ bằng chứng",
}
PHASE_LABELS = {
    "QUEUED": "Chờ nhận việc", "STARTING": "Khởi động", "INPUT_VALIDATION": "Kiểm tra input",
    "PLANNING": "Lập kế hoạch", "COLLECTION": "Đọc bằng chứng", "EVIDENCE_PREPARATION": "Chuẩn bị bằng chứng",
    "MODEL_ANALYSIS": "Phân tích với Ollama", "DETERMINISTIC_ANALYSIS": "Tổng hợp bằng Python",
    "CLAIM_VALIDATION": "Kiểm định nội dung", "REPORT_GENERATION": "Tạo báo cáo",
    "ARTIFACT_VERIFICATION": "Kiểm tra artifacts", "READY_TO_PUBLISH": "Sẵn sàng công bố",
    "PUBLICATION": "Đã công bố", "SMOKE": "Kiểm thử hạ tầng", "FINALIZING": "Hoàn tất",
}
PROVENANCE_LABELS = {"ollama": "Ollama", "deterministic_fallback": "Tổng hợp bằng Python",
                     "not_requested": "Không yêu cầu AI", "success": "Model đã thực thi"}


def section_header(title, subtitle="", step=""):
    return f'<div class="rw-section"><span class="rw-step">{e(step)}</span><div><h2>{e(title)}</h2><p>{e(subtitle)}</p></div></div>'


def summary_grid(items):
    return '<dl class="rw-summary">' + ''.join(
        f'<div><dt>{e(name)}</dt><dd>{e(value)}</dd></div>' for name, value in items) + '</dl>'


def job_header(job, created):
    tone = "active" if job.state in {"QUEUED", "RUNNING"} else "limited" if job.state in {"FAILED", "INTERRUPTED"} else "neutral"
    title = getattr(job.request, "topic", "TEST / SMOKE")
    return (f'<div class="rw-job-header"><div><p class="rw-overline">PHIÊN NGHIÊN CỨU · {e(job.job_id[:10])}</p>'
            f'<h2>{e(title)}</h2><p>Tạo lúc {e(created)}</p></div><span class="rw-status rw-{tone}">'
            f'{e(STATE_LABELS[job.state])} · {e(job.state.value)}</span></div>')


def history_cards(jobs, time_label):
    cards = []
    for job in jobs:
        outcome = job.research_outcome
        cards.append('<article class="rw-history-card"><h3>' + e(getattr(job.request, "topic", "TEST / SMOKE"))
                     + '</h3><p>' + e(time_label(job.created_at)) + ' · ' + e(job.job_id[:10]) + '</p>'
                     + summary_grid([("Thực thi", STATE_LABELS[job.state] + " · " + job.state.value),
                                     ("Kiểm định", VALIDATION_LABELS[outcome.validation_status] if outcome else "Chưa có"),
                                     ("Phân tích", PROVENANCE_LABELS.get(outcome.analysis_provenance, outcome.analysis_provenance) if outcome else "Chưa thực thi")])
                     + '</article>')
    return '<div class="rw-history-mobile">' + ''.join(cards) + '</div>'
