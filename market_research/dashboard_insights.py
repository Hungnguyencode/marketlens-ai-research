"""Read-only presentation checks for saved AI output and conservative title review.

These guards do not rerun or replace the research claim validator. Saved validation
is historical; nothing here promotes it to the selected batch/comparison.
"""
import re
import unicodedata
from urllib.parse import urlparse

from .dashboard_data import ArtifactError, catalog_rows


GOLDEN_RUN_ID = "a7c48c0031e4"
GOLDEN_NICHE = "cozy coloring books for adults"
CATALOG_COLUMNS = ("Tên sách", "Rà soát hiển thị", "Tác giả", "Xuất bản đầu", "Ấn bản ghi nhận", "Nguồn")


def safe_source_url(value):
    try:
        url = urlparse(value)
        return url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def historical_problem(run):
    if (run.metadata.get("analysis_provenance") != "ollama" or
            run.metadata.get("analysis_execution_status") != "success"):
        return "Artifact không ghi nhận Ollama thực thi thành công."
    validation = run.validation
    if (validation.status != "VALID" or not all((validation.valid, validation.schema_valid,
            validation.claims_valid, validation.evidence_sufficient))):
        return "Artifact chưa đạt kiểm định VALID với bằng chứng đầy đủ."
    facts = {fact.fact_id: fact for fact in run.verified_facts}
    evidence = {item.evidence_id: item for item in run.evidence}
    for idea in run.analysis.product_ideas:
        refs = set(idea.why_now.fact_ids + idea.supporting_fact_ids)
        if not refs or not refs <= facts.keys():
            return "Tham chiếu fact của ý tưởng bị thiếu hoặc không khớp."
        sources = set(idea.supporting_evidence_ids)
        for ref in refs:
            if not facts[ref].evidence_ids:
                return "Fact không có đường dẫn truy vết tới evidence."
            sources.update(facts[ref].evidence_ids)
        if not sources <= evidence.keys() or any(
            evidence[ref].run_id != run.run_id or evidence[ref].collection_status not in {"success", "partial"}
            or not safe_source_url(evidence[ref].source_url) for ref in sources
        ):
            return "Nguồn evidence bị thiếu, không an toàn hoặc không thuộc run lịch sử."
    return None


def load_golden_insights(store):
    try:
        run = store.load_historical(GOLDEN_RUN_ID)
        if run.niche != GOLDEN_NICHE:
            raise ArtifactError("Niche của golden run không khớp")
        problem = historical_problem(run)
        return (None, problem) if problem else (run, None)
    except (OSError, ValueError, KeyError) as exc:
        return None, f"Chưa thể hiển thị AI Insights: {str(exc)[:220]}"


def idea_references(run, idea):
    ids = set(idea.why_now.fact_ids + idea.supporting_fact_ids)
    facts = [fact for fact in run.verified_facts if fact.fact_id in ids]
    evidence_ids = set(idea.supporting_evidence_ids)
    for fact in facts:
        evidence_ids.update(fact.evidence_ids)
    return facts, [item for item in run.evidence if item.evidence_id in evidence_ids]


def review_title(title, niche_id):
    """Title-only screening, never confirmation of retail competition or category."""
    text = unicodedata.normalize("NFC", title).casefold()
    words = set(re.findall(r"[a-z]+", text))
    coloring = bool(words & {"coloring", "colouring"})
    context = {
        "cozy-adults": bool(words & {"cozy", "cosy"}),
        "bold-easy": {"bold", "easy"} <= words,
        "christmas-adults": "christmas" in words and bool(words & {"adult", "adults"}),
        "kids-christmas": "christmas" in words and bool(words & {"kids", "kid", "children", "child"}),
    }.get(niche_id, False)
    if coloring and context:
        return ("Khớp chủ đề rõ trong tiêu đề", "Tiêu đề nêu coloring/colouring và dấu hiệu niche; chưa xác nhận phân loại hoặc đối thủ bán lẻ.")
    if coloring or context:
        return ("Ứng viên sơ bộ · cần rà soát", "Tiêu đề chỉ khớp một phần; chưa đủ metadata xác nhận sách tô màu thuộc niche.")
    return ("Chưa đủ bằng chứng liên quan", "Không thấy đồng thời hoạt động tô màu và dấu hiệu niche trong tiêu đề; cần metadata bổ sung.")


def reviewed_catalog_rows(batch, niche_id, candidates_only=False):
    rows = catalog_rows(batch, niche_id, candidates_only)
    result = []
    for row in rows:
        status, reason = review_title(row["Tên sách"], niche_id)
        result.append({"Tên sách": row["Tên sách"], "Rà soát hiển thị": status, "Căn cứ": reason,
                       "Tác giả": row["Tác giả"], "Xuất bản đầu": row["Xuất bản đầu"],
                       "Ấn bản ghi nhận": row["Editions ghi nhận"], "Nguồn": row["Nguồn"],
                       "Nhãn collector gốc": row["Phân loại"]})
    return result


def catalog_table_rows(rows):
    """Keep the discovery table concise; full rationale stays in the detail view."""
    return [{column: row[column] for column in CATALOG_COLUMNS} for row in rows]


def catalog_detail(rows, index):
    """Return the complete, original title rationale, source, and collector label."""
    if not isinstance(index, int) or index < 0 or index >= len(rows):
        raise IndexError("Chỉ mục bản ghi catalog không hợp lệ")
    row = rows[index]
    return {"Tên sách": row["Tên sách"], "Rà soát hiển thị": row["Rà soát hiển thị"],
            "Căn cứ": row["Căn cứ"], "Nhãn collector gốc": row["Nhãn collector gốc"],
            "Nguồn": row["Nguồn"], "Tác giả": row["Tác giả"],
            "Xuất bản đầu": row["Xuất bản đầu"], "Ấn bản ghi nhận": row["Ấn bản ghi nhận"]}
