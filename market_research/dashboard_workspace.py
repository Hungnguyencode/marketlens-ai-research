"""Intentional job submission and verified presentation; research runs only in the worker."""
import streamlit as st
from uuid import uuid4
from functools import partial
import hashlib
from datetime import timezone

from .dashboard_design import hero
from .dashboard_ui import heading, kpis
from .job_config import open_job_store
from .job_store import ERRORS, JobState, JobStoreError
from .research_workspace import (WorkspaceError, original_filename, preview_upload,
                                 verified_reports)
from .workspace_design import (STATE_LABELS, VALIDATION_LABELS, PHASE_LABELS, PROVENANCE_LABELS,
                               job_header, section_header, summary_grid, history_cards)
from .workspace_state import ResultSnapshot, MAX_VERIFICATION_ATTEMPTS, verified_download

ACTIVE_STATES = {JobState.QUEUED, JobState.RUNNING}
ACTIVE_REFRESH_SECONDS = 2.5
TERMINAL_REFRESH_SECONDS = 30


def choose_job(job_id):
    st.session_state["workspace_job"] = job_id
    st.query_params["job"] = job_id


def open_monitor(job_id):
    choose_job(job_id)
    st.session_state["workspace_destination"] = "Theo dõi job"
    st.session_state["workspace_navigation"] = st.session_state.get("workspace_navigation", 0) + 1
    st.rerun()


def open_history_selection():
    # Read the current widget value in the callback, never a previous render's args.
    open_monitor(st.session_state["workspace-history"])


def time_label(value):
    return value.astimezone(timezone.utc).strftime("%d/%m/%Y · %H:%M UTC") if value else "Chưa ghi nhận"


def result_entry(store, job, reset=False):
    cache = st.session_state.setdefault("workspace_verified_results", {})
    contract = job.request.model_dump_json() + (job.research_outcome.model_dump_json() if job.research_outcome else "") + str(job.result_path)
    key = (str(store.database), str(store.artifact_root), job.job_id, hashlib.sha256(contract.encode("utf-8")).hexdigest())
    if reset or key not in cache:
        while len(cache) >= 3:
            cache.pop(next(iter(cache)))
        cache[key] = ResultSnapshot()
    return cache[key]


def result_snapshot(store, job, reset=False):
    return result_entry(store, job, reset).load(store, job)


def show_new_research(store):
    st.html(section_header("Thiết lập nghiên cứu", "Một chủ đề, một keyword và nguồn CSV bạn có thể truy vết.", "01"))
    with st.form("workspace-request"):
        topic = st.text_input("Chủ đề nghiên cứu", max_chars=200)
        source, execution = st.columns([1.35, 1], gap="large")
        with source:
            st.subheader("Nguồn dữ liệu")
            uploaded = st.file_uploader("CSV Google Trends (UTF-8, tối đa 32 MiB)", type=["csv"], max_upload_size=32)
            source_url = st.text_input("URL Google Trends thực tế đã dùng để xuất CSV",
                                       help="HTTPS, geo=US, q khớp keyword. Không nhập token hoặc mật khẩu.")
            st.caption("United States · import cục bộ. Index 0–100 là mức quan tâm tương đối, không phải doanh số hoặc search volume.")
        with execution:
            st.subheader("Tùy chọn thực thi")
            model = st.text_input("Model Ollama được yêu cầu", value="qwen3:4b-instruct", max_chars=100,
                                  help="Tên model không xác nhận đã cài. Không gọi Ollama ở bước kiểm tra CSV.")
            allow_inference = st.checkbox("Cho phép Ollama inference", value=False)
            confirmed = st.checkbox("Xác nhận chạy Ollama cho yêu cầu này", value=False,
                                    help="Tôi cho phép worker dùng tài nguyên máy để inference cho đúng yêu cầu đang xem trước.")
            st.caption("Hai lựa chọn trên cùng bật mới cấp quyền AI. Inference mặc định tắt; chỉ worker độc lập thực thi.")
            st.html(summary_grid([("Chế độ", "CSV import · cục bộ"), ("Thu thập mạng", "TẮT")]))
        checked = st.form_submit_button("Kiểm tra CSV & xem trước",
                                       type="secondary" if st.session_state.get("workspace_preview") else "primary")
    if checked:
        st.session_state.pop("workspace_preview", None)
        try:
            if uploaded is None:
                raise WorkspaceError("Hãy tải CSV trước khi kiểm tra.")
            if uploaded.size > 32 * 1024 * 1024:
                raise WorkspaceError("CSV không được vượt quá 32 MiB.")
            st.session_state["workspace_preview"] = preview_upload(
                store, uploaded.name, uploaded.getvalue(), topic=topic, source_url=source_url,
                model=model, allow_inference=allow_inference, inference_confirmed=confirmed)
        except WorkspaceError as exc:
            st.error(str(exc))
    preview = st.session_state.get("workspace_preview")
    if preview is None:
        return
    request = preview.request
    st.html(section_header("Kiểm tra nguồn & xác nhận", "Bản xem trước đã chốt; kiểm tra lại nếu bạn sửa form.", "02"))
    with st.container(border=True, key="workspace-input-summary"):
        st.html(summary_grid([("File gốc", preview.filename), ("Keyword", preview.keyword),
                              ("Vùng trong CSV", preview.geography), ("Phạm vi", "United States"),
                              ("Quan sát", len(preview.observations)), ("Toàn vẹn input", "Đã chốt SHA-256")]))
        st.caption("Keyword trong file: " + preview.keyword)
        st.caption("URL chưa được xác minh độc lập là URL xuất file; đã đối chiếu keyword và vùng cục bộ.")
        with st.expander("URL nguồn & checksum input"):
            st.text("URL do người dùng cung cấp: " + request.source_url)
            st.code(request.input_sha256, language=None, wrap_lines=True)
        with st.expander("Xem trước quan sát · tối đa 10 dòng", expanded=True):
            st.dataframe(preview.observations[:10], hide_index=True, width="stretch")
            st.caption("Giữ nguyên ngày và giá trị CSV, kể cả <1. Không suy ra lượng tìm kiếm tuyệt đối.")
        st.html(summary_grid([("Yêu cầu sẽ gửi", request.topic), ("Nguồn", "Google Trends CSV thủ công"),
                              ("Inference", "BẬT · đã xác nhận" if request.allow_inference else "TẮT"),
                              ("Thu thập mạng", "TẮT"), ("Model", request.model)]))
        if st.button("Gửi yêu cầu đã xem trước", key="workspace-submit", type="primary"):
            try:
                submission_id = uuid4().hex
                job = store.submit("research", preview.payload(), job_id=submission_id, deduplicate=True,
                                   expected_input_sha256=request.input_sha256)
                choose_job(job.job_id)
                st.session_state["workspace_receipt"] = job.job_id
                if job.job_id != submission_id:
                    st.warning("Đã có job trùng hoặc đang chờ/chạy với cùng chủ đề và input. Dùng lại job đã lưu; "
                               "quyền và model giữ theo job đó. Không tạo job mới, không chạy lại inference.")
                else:
                    st.success("Đã gửi job vào SQLite; chỉ worker độc lập mới thực thi.")
                st.caption("Chọn tab Theo dõi job để xem trạng thái và quyền/model của yêu cầu đã lưu.")
            except (JobStoreError, ValueError, OSError):
                st.error("Không thể gửi yêu cầu an toàn. Kiểm tra lại input và cấu hình workspace; không tự thử lại.")
        receipt = st.session_state.get("workspace_receipt")
        if receipt:
            st.caption("Job ID đã lưu · sao chép để truy vết")
            st.code(receipt, language=None, wrap_lines=True)
            st.button("Mở Theo dõi job", key="workspace-open-monitor", on_click=open_monitor, args=(receipt,))


def show_result(store, job):
    snapshot = result_snapshot(store, job)
    if snapshot.verified is None:
        st.error(snapshot.error or "Đang kiểm tra bundle đã công bố.")
        if snapshot.attempts < MAX_VERIFICATION_ATTEMPTS:
            st.caption(f"Đang xác minh {snapshot.attempts}/{MAX_VERIFICATION_ATTEMPTS}; thử lại ở nhịp 2,5 giây tiếp theo. Không chạy lại nghiên cứu.")
        else:
            st.caption("Đã dừng kiểm tra tự động sau 3 lần. Kiểm tra file rồi bấm Làm mới trạng thái để thử lại.")
        return
    verified = snapshot.verified
    result = verified.result
    outcome = job.research_outcome
    st.html(section_header("Kết quả đã công bố", "Kết quả workflow và kiểm định được trình bày riêng; không xác nhận nhu cầu mua."))
    with st.container(key="workspace-result-summary"):
        kpis([("Thực thi", STATE_LABELS[job.state], job.state.value),
              ("Kiểm định", VALIDATION_LABELS[outcome.validation_status], outcome.validation_status),
              ("Bằng chứng", outcome.evidence_count, "Quan sát nguồn · không phải doanh số")])
    interpretation = {
        "VALID": "Phân tích đạt kiểm định trên bằng chứng của phiên. Chưa xác minh nhu cầu mua, doanh số hoặc lợi nhuận.",
        "DEGRADED": "Workflow đã hoàn tất; phân tích vẫn có giới hạn. Đọc provenance và phần bất định trước khi sử dụng.",
        "INVALID": "Workflow đã hoàn tất nhưng phân tích không đạt kiểm định. Không dùng nội dung này như kết luận đã xác minh.",
    }.get(outcome.validation_status, "Bằng chứng hoặc nguồn còn thiếu. Kết quả chưa đủ để đưa ra kết luận thị trường.")
    st.info(interpretation)
    st.caption(PROVENANCE_LABELS.get(outcome.analysis_provenance, outcome.analysis_provenance)
               + " · " + PROVENANCE_LABELS.get(outcome.model_execution_status, outcome.model_execution_status))
    show_findings(result, outcome)
    show_evidence(result, job.job_id)
    st.html(section_header("Báo cáo & tải xuống", "Bundle đã xác minh. Mỗi lần tải đều kiểm tra lại manifest và SHA-256."))
    for slot, role, title, mime in zip(st.columns(3), ("markdown", "html", "json"),
                                     ("Markdown", "HTML", "JSON"),
                                     ("text/markdown", "text/html", "application/json")):
        filename, content = verified.downloads[role]
        with slot:
            st.download_button("Tải " + title,
                               partial(verified_download, store, job.job_id, role, hashlib.sha256(content).hexdigest()),
                               file_name=filename, mime=mime, key=f"workspace-download-{job.job_id}-{role}",
                               width="stretch", on_click="ignore")
    with st.expander("Audit kết quả · nguồn, IDs & nội dung gốc"):
        st.caption("Research run ID")
        st.code(result.run_id, language=None, wrap_lines=True)
        st.text(f"Analysis provenance: {outcome.analysis_provenance}\nModel execution: {outcome.model_execution_status}\nModel yêu cầu: {outcome.requested_model}")
        st.caption("Bản hiển thị đã kiểm tra lúc " + time_label(snapshot.checked_at)
                   + ". Download luôn xác minh lại bytes; không nhúng HTML report vào trang.")
        for source in dict.fromkeys(item.source_url for item in result.evidence):
            st.code(source, language=None, wrap_lines=True)
        st.json(result.validation.model_dump(mode="json"))
        with st.expander("Analysis JSON gốc · bao gồm nội dung chưa đạt nếu có"):
            st.json(result.analysis.model_dump(mode="json"))


def show_findings(result, outcome):
    st.html(section_header("Nội dung nghiên cứu", "Chỉ đọc dữ liệu trong report đã xác minh; dashboard không tạo thêm phân tích."))
    if outcome.validation_status == "INVALID" or not outcome.claims_valid:
        st.warning("Nội dung phân tích chưa đạt kiểm định; giữ trong audit để rà soát, không trình bày như kết luận.")
        return
    if outcome.analysis_provenance != "ollama" or outcome.model_execution_status != "success":
        with st.container(border=True):
            st.subheader("AI analysis chưa được yêu cầu hoặc chưa thực thi thành công")
            st.write("Bạn vẫn có thể kiểm tra quan sát nguồn và tải báo cáo. Không có giả thuyết AI mới được tạo ở dashboard.")
            for observation in result.analysis.factual_observations[:5]:
                st.text(observation)
        return
    for index, idea in enumerate(result.analysis.product_ideas, 1):
        with st.container(border=True):
            st.caption(f"GIẢ THUYẾT SẢN PHẨM {index} · TỪ REPORT")
            st.subheader(idea.what_to_sell)
            st.text(idea.target_customer)
            st.text(idea.differentiation)
            with st.expander("Căn cứ, bất định & tham chiếu"):
                st.text(idea.why_now.text)
                for uncertainty in idea.uncertainties:
                    st.text("• " + uncertainty)
                st.code("\n".join(idea.supporting_fact_ids + idea.supporting_evidence_ids), language=None, wrap_lines=True)


def show_evidence(result, job_id):
    st.html(section_header("Khám phá bằng chứng", "Quan sát nguyên gốc; index tương đối không phải lượng tìm kiếm tuyệt đối hoặc doanh số."))
    keywords = ", ".join(dict.fromkeys(item.keyword for item in result.evidence))
    st.caption(f"{keywords} · {result.target_region} · {len(result.evidence)} quan sát")
    if result.mode == "csv_import":
        st.caption("Google Trends CSV thủ công · URL do người dùng khai báo, chưa được xác minh độc lập.")
    full = st.checkbox("Xem toàn bộ quan sát", key=f"workspace-all-evidence-{job_id}", value=False)
    rows = result.evidence if full else result.evidence[:10]
    st.dataframe([{"Kỳ quan sát": item.observation_period,
                   "Index gốc": str(item.raw_observation.get("raw_value", item.metric_value))} for item in rows],
                 hide_index=True, width="stretch")
    if not full and len(result.evidence) > 10:
        st.caption(f"Đang hiển thị 10/{len(result.evidence)} quan sát. JSON report giữ đầy đủ evidence và tham chiếu nguồn.")


def cancel_monitored_job(store, job_id):
    try:
        store.request_cancel(job_id)
    except JobStoreError:
        st.session_state["workspace_cancel_notice"] = True


def show_monitor(store, job, *, include_result=True):
    st.html(job_header(job, time_label(job.created_at)))
    if job.cancellation_requested_at:
        st.warning("Đã ghi nhận yêu cầu hủy. " + ("Chờ checkpoint kế tiếp; HTTP/Ollama có thể cần trả về hoặc hết timeout." if job.state == JobState.RUNNING else "Job đã kết thúc."))
    if job.state in {JobState.QUEUED, JobState.RUNNING}:
        st.button("Yêu cầu hủy job", key="workspace-cancel", disabled=job.cancellation_requested_at is not None,
                  on_click=cancel_monitored_job, args=(store, job.job_id))
    if st.session_state.pop("workspace_cancel_notice", False):
        st.warning("Trạng thái job đã đổi hoặc DB đang bận; hãy làm mới.")
    with st.container(border=True, key="workspace-progress"):
        outcome = job.research_outcome
        st.html(summary_grid([("Thực thi", STATE_LABELS[job.state]),
                              ("Kiểm định", VALIDATION_LABELS[outcome.validation_status] if outcome else "Chưa có kết quả"),
                              ("Giai đoạn", PHASE_LABELS.get(job.phase, job.phase))]))
        st.progress(job.completed_steps / job.total_steps, text=f"{job.completed_steps}/{job.total_steps} checkpoint hoàn tất")
        st.caption("Tiến độ workflow đã lưu, không phải token Ollama hoặc ước tính thời gian còn lại.")
        guidance = {
            JobState.QUEUED: "Đang chờ worker độc lập nhận việc. QUEUED không chứng minh worker đang tắt.",
            JobState.RUNNING: "Worker đang thực hiện yêu cầu đã lưu. Hủy là hợp tác, không dừng HTTP/Ollama ngay lập tức.",
            JobState.COMPLETED: "Workflow đã hoàn tất. Kết quả kiểm định và giới hạn bằng chứng được trình bày riêng bên dưới.",
            JobState.FAILED: "Thực thi gặp lỗi; chưa có báo cáo đã công bố. Xem mã lỗi trong chi tiết để xử lý nguyên nhân.",
            JobState.CANCELLED: "Job đã hủy. Output một phần không được trình bày là báo cáo hoàn tất.",
            JobState.INTERRUPTED: "Job gián đoạn. Không tự thử lại hoặc chạy lại inference; cần rà soát trước lần gửi mới.",
        }
        st.write(guidance[job.state])
    show_job_details(store, job)
    if include_result and job.state == JobState.COMPLETED and job.job_type == "research":
        show_result(store, job)


def show_job_details(store, job):
    with st.expander("Chi tiết thực thi · IDs, nguồn & quyền chạy"):
        st.caption("Job ID đầy đủ · dùng nút copy")
        st.code(job.job_id, language=None, wrap_lines=True)
        st.text(f"Thực thi: {job.state.value}\nGiai đoạn: {job.phase}\nExecution provenance: {job.execution_provenance}\nSố lần thực thi: {job.attempt_count}")
        st.html(summary_grid([("Tạo", time_label(job.created_at)), ("Bắt đầu", time_label(job.started_at)),
                              ("Kết thúc", time_label(job.finished_at))]))
        if job.worker_id:
            st.caption("Worker ID")
            st.code(job.worker_id, language=None, wrap_lines=True)
        if job.job_type == "research":
            st.html(summary_grid([("File gốc", original_filename(store, job.request)), ("Vùng", job.request.target_region),
                                  ("Inference", "BẬT" if job.request.allow_inference else "TẮT"),
                                  ("Thu thập mạng", "BẬT" if job.request.allow_network else "TẮT"), ("Model yêu cầu", job.request.model)]))
            st.caption("URL khai báo · chưa xác minh độc lập")
            st.code(job.request.source_url or "Không áp dụng", language=None, wrap_lines=True)
            st.caption("Input SHA-256")
            st.code(job.request.input_sha256 or "Không áp dụng", language=None, wrap_lines=True)
        if job.research_outcome:
            st.text("Kết quả kiểm định: " + job.research_outcome.validation_status + " · Model execution: " + job.research_outcome.model_execution_status)
        if job.error_code:
            st.warning((job.error_code if job.error_code in ERRORS else "JOB_ERROR") + ": "
                       + ERRORS.get(job.error_code, "Thực thi gặp lỗi; chi tiết nhạy cảm được giữ kín."))
        if job.state == JobState.QUEUED:
            with st.expander("Khởi chạy worker độc lập"):
                st.caption("Dùng cùng cấu hình DB/input/artifacts trong terminal thứ hai. Dashboard không tạo worker.")
                st.code(".\\.venv\\Scripts\\python.exe -m market_research.jobs worker", language="powershell")


def active_monitor(store, initial_job):
    """A short-lived child timer; a terminal snapshot never polls the DB again."""
    latest = initial_job

    @st.fragment(run_every=ACTIVE_REFRESH_SECONDS)
    def poll():
        nonlocal latest
        try:
            if latest.state in ACTIVE_STATES:
                latest = store.get(initial_job.job_id)
            # A terminal read is authoritative; reveal its verified bundle in this tick.
            show_monitor(store, latest)
        except (OSError, ValueError, JobStoreError):
            st.warning("Chưa đọc được trạng thái job; thử lại sau 2,5 giây hoặc bấm Làm mới. Không chạy lại nghiên cứu.")

    poll()


@st.fragment(run_every=TERMINAL_REFRESH_SECONDS)
def show_live_monitor(store):
    """Only this region refreshes. Parent refresh removes a completed child's timer."""
    with st.container(key="workspace-monitor-toolbar"):
        summary, action = st.columns([3, 1])
        with summary:
            st.caption("Theo dõi tự động · 2,5 giây khi đang xử lý")
        with action:
            refresh = st.button("Làm mới trạng thái", key="workspace-refresh", width="stretch",
                                help="Đọc trạng thái và kiểm tra lại kết quả; không gửi job hay gọi inference.")
    selected = st.session_state.get("workspace_job", st.query_params.get("job"))
    if not selected:
        st.info("Chọn Xem chi tiết job từ lịch sử hoặc gửi yêu cầu mới.")
        return
    try:
        job = store.get(selected)
        pending_result = False
        if job.state == JobState.COMPLETED and job.job_type == "research":
            entry = result_entry(store, job, reset=refresh)
            pending_result = entry.verified is None and entry.attempts < MAX_VERIFICATION_ATTEMPTS
        if job.state in ACTIVE_STATES or pending_result:
            active_monitor(store, job)
        else:
            show_monitor(store, job)
    except (OSError, ValueError, JobStoreError):
        st.warning("Job chưa đọc được trong DB đang cấu hình hoặc ID không hợp lệ. Hãy làm mới hoặc chọn lại từ lịch sử.")


def render_history(store, jobs):
    if not jobs:
        st.info("Chưa có job trong workspace này. Tải CSV và xem trước để bắt đầu.")
        return
    st.html(summary_grid([("Phiên đang hiển thị", len(jobs)),
                          ("Đang chờ / thực hiện", sum(job.state in ACTIVE_STATES for job in jobs)),
                          ("Đã kết thúc", sum(job.state not in ACTIVE_STATES for job in jobs))]))
    with st.container(key="workspace-history-table"):
        st.dataframe([{"Chủ đề": getattr(job.request, "topic", "TEST / SMOKE"),
                       "Tạo (UTC)": time_label(job.created_at), "Trạng thái": job.state.value,
                       "Kiểm định": job.research_outcome.validation_status if job.research_outcome else "Chưa có",
                       "Phân tích": PROVENANCE_LABELS.get(job.research_outcome.analysis_provenance,
                                                         job.research_outcome.analysis_provenance) if job.research_outcome else "Chưa thực thi"}
                      for job in jobs], hide_index=True, width="stretch")
    ids = [job.job_id for job in jobs]
    titles = {job.job_id: getattr(job.request, "topic", "TEST / SMOKE") for job in jobs}
    selected = st.session_state.get("workspace_job", st.query_params.get("job"))
    history_id = st.selectbox("Chọn job trong lịch sử", ids, index=ids.index(selected) if selected in ids else 0,
        format_func=lambda key: f"{titles[key]} · {key[:10]}", key="workspace-history",
        help="ID đầy đủ có thể copy trong Chi tiết thực thi. Không ghép golden artifacts vào lịch sử.")
    st.button("Xem chi tiết job", key="workspace-details", on_click=open_history_selection)
    pages = max(1, (len(jobs) + 9) // 10)
    with st.container(key="workspace-history-pagination"):
        page = st.selectbox("Trang lịch sử", list(range(pages)),
                            format_func=lambda value: f"Trang {value + 1}/{pages} · 10 phiên mỗi trang",
                            key="workspace-history-page")
        st.html(history_cards(jobs[page * 10:(page + 1) * 10], time_label))


def active_history(store, initial_jobs):
    latest = initial_jobs

    @st.fragment(run_every=ACTIVE_REFRESH_SECONDS)
    def poll_history():
        nonlocal latest
        try:
            if any(job.state in ACTIVE_STATES for job in latest):
                latest = store.list_jobs(200)
            render_history(store, latest)
        except (OSError, ValueError, JobStoreError):
            st.warning("Chưa đọc được lịch sử. Thử lại ở nhịp kế tiếp hoặc bấm Làm mới lịch sử.")

    poll_history()


@st.fragment(run_every=TERMINAL_REFRESH_SECONDS)
def show_live_history(store):
    st.html(section_header("Lịch sử nghiên cứu", "Tối đa 200 phiên mới nhất trong workspace cục bộ · thời gian UTC."))
    st.button("Làm mới lịch sử", key="workspace-history-refresh")
    try:
        jobs = store.list_jobs(200)
        if any(job.state in ACTIVE_STATES for job in jobs):
            active_history(store, jobs)
        else:
            render_history(store, jobs)
    except (OSError, ValueError, JobStoreError):
        st.warning("Chưa thể đọc lịch sử trong DB đang cấu hình. Hãy làm mới khi DB khả dụng.")


def show_workspace():
    with st.container(key="research-workspace"):
        render_workspace()


def render_workspace():
    st.html(hero("Không gian nghiên cứu", "Từ CSV nguồn đến job bền vững và báo cáo có bằng chứng. Worker chạy độc lập với trình duyệt."))
    try:
        store = open_job_store()
        create, monitor, history = st.tabs(["Tạo nghiên cứu", "Theo dõi job", "Lịch sử & kết quả"],
            default=st.session_state.get("workspace_destination", "Tạo nghiên cứu"),
            key=f"workspace-tabs-{st.session_state.get('workspace_navigation', 0)}")
        with create:
            show_new_research(store)
        # Query after submission; SQLite remains authoritative after browser reload.
        with history:
            show_live_history(store)
        with monitor:
            show_live_monitor(store)
        st.caption("Dữ liệu cục bộ · worker độc lập · cập nhật nhanh 2,5 giây khi có job đang chạy, 30 giây khi nhàn rỗi.")
    except (OSError, ValueError, JobStoreError):
        st.error("Không mở được job workspace. Kiểm tra quyền thư mục và cấu hình DB/input/artifacts của cả dashboard lẫn worker. Chi tiết đường dẫn được giữ kín.")
