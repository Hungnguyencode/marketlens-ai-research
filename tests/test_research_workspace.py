"""SYNTHETIC CSV only. No live collection, model calls or user historical artifacts."""
import hashlib
import json
import os
import inspect
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from market_research.job_config import open_job_store
from market_research.job_store import JobState, JobStore, JobStoreError, ResearchInputError
from market_research.job_worker import Worker
from market_research.jobs import main
from market_research.research_workspace import (MAX_UPLOAD_BYTES, WorkspaceError, original_filename,
                                                persist_upload, preview_upload, safe_child, verified_reports)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://trends.google.com/trends/explore?geo=US&q=cozy%20coloring%20book"
CSV = ("SYNTHETIC TEST FIXTURE — NOT MARKET EVIDENCE\nDanh mục: Tất cả danh mục\n\n"
       "Tuần,cozy coloring book: (Hoa Kỳ)\n2025-10-19,54\n2025-10-26,<1\n"
       "2025-11-02,80\n2025-11-09,64\n").encode("utf-8-sig")


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("external operation")
        raise AssertionError("Live collection/inference forbidden")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.plan", forbidden)
    monkeypatch.setattr("market_research.ollama.OllamaClient.analyze", forbidden)
    monkeypatch.setattr("market_research.collectors.GoogleBooksCollector.collect", forbidden)
    return calls


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETLENS_JOBS_DB", str(tmp_path / "runtime/jobs.sqlite3"))
    monkeypatch.setenv("MARKETLENS_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("MARKETLENS_JOB_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MARKETLENS_ARTIFACTS_DIR", str(tmp_path / "empty-dashboard"))
    return open_job_store()


def preview(store, **kwargs):
    return preview_upload(store, "SYNTHETIC-vietnamese.csv", CSV, topic="Cozy coloring books",
                          source_url=SOURCE, **kwargs)


def queued(store, **kwargs):
    view = preview(store, **kwargs)
    return store.submit("research", view.payload()), view


def completed(store):
    job, view = queued(store)
    with Worker(store) as worker:
        done = worker.run_once()
    assert done.state == JobState.COMPLETED
    return done


def app():
    return AppTest.from_string("from market_research.dashboard_workspace import show_workspace\nshow_workspace()").run(timeout=20)


def button(application, text):
    return next(item for item in application.button if item.label == text)


def all_text(application):
    return "\n".join(str(item.value) for kind in ("text", "caption", "info", "warning", "error", "success")
                     for item in application.get(kind))


def test_upload_preserves_bom_bytes_and_provenance(runtime):
    view = preview(runtime)
    path = runtime.input_root / view.request.input_path
    assert path.read_bytes() == CSV
    assert path.name != view.filename and len(path.stem) == 32
    assert original_filename(runtime, view.request) == "SYNTHETIC-vietnamese.csv"
    assert view.request.input_sha256 == hashlib.sha256(CSV).hexdigest()
    assert view.keyword == "cozy coloring book" and view.geography == "Hoa Kỳ"
    assert view.observations[1] == {"Kỳ": "2025-10-26", "Index gốc": "<1"}
    assert view.request.source_url == SOURCE
    assert not view.request.allow_inference and not view.request.allow_network
    reopened = JobStore(runtime.database, artifact_root=runtime.artifact_root)
    assert (reopened.input_root / view.request.input_path).read_bytes() == CSV
    assert not reopened.list_jobs()


@pytest.mark.parametrize("filename,raw", [
    ("../escape.csv", CSV), (r"C:\private\x.csv", CSV), ("fake.json", CSV),
    ("empty.csv", b""), ("binary.csv", b"\xff\x00"), ("secret.csv", b"api_key=redacted"),
    ("large.csv", b"x" * (MAX_UPLOAD_BYTES + 1)),
], ids=["traversal", "absolute", "wrong-type", "empty", "binary", "credential", "oversized"])
def test_reject_invalid_upload(runtime, filename, raw):
    with pytest.raises(WorkspaceError):
        persist_upload(runtime, filename, raw)
    assert not runtime.list_jobs()


@pytest.mark.parametrize("replacement", [
    b"not a time series", CSV.replace(b"cozy coloring book:", b"other keyword:"),
    CSV.replace("Hoa Kỳ".encode(), b"Canada"),
    CSV.replace(b"2025-10-19,54", b"2025-10-19,54,5").replace(
        "cozy coloring book: (Hoa Kỳ)".encode(), "cozy coloring book: (Hoa Kỳ),other: (Hoa Kỳ)".encode()),
], ids=["no-header", "wrong-keyword", "wrong-region", "multiple-keywords"])
def test_block_invalid_or_mismatched_csv(runtime, replacement):
    with pytest.raises(WorkspaceError):
        preview_upload(runtime, "input.csv", replacement, topic="Cozy books", source_url=SOURCE)


def test_inference_requires_separate_confirmation(runtime):
    with pytest.raises(WorkspaceError, match="xác nhận"):
        preview(runtime, allow_inference=True)
    request = preview(runtime, allow_inference=True, inference_confirmed=True).request
    assert request.allow_inference and not request.allow_network
    assert request.model == "qwen3:4b-instruct"


@pytest.mark.parametrize("url", ["", SOURCE.replace("geo=US", "geo=CA"), "https://example.com/export", SOURCE + "&token=private"])
def test_invalid_source_url_cannot_create_preview_or_job(runtime, url):
    with pytest.raises(WorkspaceError):
        preview_upload(runtime, "input.csv", CSV, topic="Cozy books", source_url=url)
    assert not runtime.list_jobs()


def test_shared_config_cli_worker_and_dashboard(runtime, capsys, monkeypatch):
    job, _ = queued(runtime)
    assert main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["job_id"] == job.job_id
    assert open_job_store().input_root == runtime.input_root
    assert open_job_store().artifact_root == runtime.artifact_root
    monkeypatch.delenv("MARKETLENS_INPUT_ROOT")
    assert open_job_store().input_root == runtime.input_root  # persisted across processes
    explicit = open_job_store(runtime.database, input_root=runtime.input_root)
    assert explicit.database == runtime.database


def test_upload_names_never_overwrite(runtime):
    first, second = preview(runtime), preview(runtime)
    assert first.request.input_path != second.request.input_path
    assert (runtime.input_root / first.request.input_path).read_bytes() == CSV


def test_preview_checksum_change_blocks_before_insert(runtime):
    view = preview(runtime)
    (runtime.input_root / view.request.input_path).write_bytes(CSV + b"\n")
    with pytest.raises(ResearchInputError):
        runtime.submit("research", view.payload(), expected_input_sha256=view.request.input_sha256)
    assert not runtime.list_jobs()


def test_atomic_dedup_across_sessions_and_upload_names(runtime):
    views = [preview(runtime), preview(runtime)]
    def submit(index):
        return runtime.submit("research", views[index % 2].payload(), deduplicate=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(submit, range(8)))
    assert len({job.job_id for job in jobs}) == len(runtime.list_jobs()) == 1
    # Similar active job reuses original permissions instead of silently enqueueing inference.
    changed = views[0].payload() | {"allow_inference": True}
    assert runtime.submit("research", changed, deduplicate=True).job_id == jobs[0].job_id
    assert not runtime.get(jobs[0].job_id).request.allow_inference
    runtime.request_cancel(jobs[0].job_id)
    assert runtime.submit("research", views[1].payload(), deduplicate=True).job_id == jobs[0].job_id


def test_empty_workspace_and_five_page_navigation(runtime):
    application = AppTest.from_file(str(ROOT / "dashboard.py")).run(timeout=20)
    assert not application.exception
    for name in ("Tổng quan", "Phân tích từ khóa", "Khám phá niche", "Đánh giá cơ hội", "Bằng chứng & truy vết"):
        assert button(application, name)
    button(application, "Không gian nghiên cứu").click().run(timeout=20)
    assert not application.exception
    assert "Chưa có job" in all_text(application)
    assert len(application.tabs) == 3
    assert all(not widget.value for widget in application.checkbox)
    for _ in range(2):
        application.run(timeout=20)
    button(application, "Làm mới trạng thái").click().run(timeout=20)
    assert not runtime.list_jobs() and not application.exception


def test_ui_upload_preview_explicit_submit_refresh_and_reload(runtime, monkeypatch):
    fake = SimpleNamespace(size=len(CSV), name="SYNTHETIC-vietnamese.csv", getvalue=lambda: CSV)
    monkeypatch.setattr("streamlit.file_uploader", lambda *args, **kwargs: fake)
    application = app()
    application.text_input[0].set_value("Cozy coloring books")
    application.text_input[1].set_value(SOURCE)
    button(application, "Kiểm tra CSV & xem trước").click().run(timeout=20)
    assert not application.exception and not runtime.list_jobs()
    assert "chưa được xác minh độc lập" in all_text(application)
    assert "Keyword trong file: cozy coloring book" in all_text(application)
    assert not any(widget.value for widget in application.checkbox)
    button(application, "Gửi yêu cầu đã xem trước").click().run(timeout=20)
    job = runtime.list_jobs()[0]
    assert len(runtime.list_jobs()) == 1 and job.state == JobState.QUEUED
    assert not job.request.allow_inference and not job.request.allow_network
    button(application, "Gửi yêu cầu đã xem trước").click().run(timeout=20)
    button(application, "Làm mới trạng thái").click().run(timeout=20)
    assert len(runtime.list_jobs()) == 1 and not application.exception
    assert job.job_id in [code.value for code in application.code]
    reloaded = AppTest.from_file(str(ROOT / "dashboard.py"))
    reloaded.query_params.update({"workspace": "research", "job": job.job_id})
    reloaded.run(timeout=20)
    assert not reloaded.exception and job.job_id in [code.value for code in reloaded.code]


def test_ui_blocks_unconfirmed_inference(runtime, monkeypatch):
    monkeypatch.setattr("streamlit.file_uploader", lambda *args, **kwargs:
                        SimpleNamespace(size=len(CSV), name="input.csv", getvalue=lambda: CSV))
    application = app()
    application.text_input[0].set_value("Cozy coloring books")
    application.text_input[1].set_value(SOURCE)
    application.checkbox[0].check()
    button(application, "Kiểm tra CSV & xem trước").click().run(timeout=20)
    assert application.error and not runtime.list_jobs()
    assert not any(item.label == "Gửi yêu cầu đã xem trước" for item in application.button)
    application.checkbox[1].check()
    button(application, "Kiểm tra CSV & xem trước").click().run(timeout=20)
    assert not application.exception
    button(application, "Gửi yêu cầu đã xem trước").click().run(timeout=20)
    assert runtime.list_jobs()[0].request.allow_inference


@pytest.mark.parametrize("state", list(JobState))
def test_all_persisted_states_and_cancellation_controls(runtime, state):
    if state == JobState.COMPLETED:
        job = completed(runtime)
    else:
        job, _ = queued(runtime)
        if state == JobState.CANCELLED:
            job = runtime.request_cancel(job.job_id)
        elif state != JobState.QUEUED:
            worker_id = runtime.register_worker()
            job = runtime.claim_next(worker_id)
            if state == JobState.FAILED:
                job = runtime.finish(job.job_id, worker_id, state, error_code="HANDLER_FAILED")
            elif state == JobState.INTERRUPTED:
                runtime.stop_worker(worker_id)
                job = runtime.get(job.job_id)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert not application.exception and state.value in all_text(application)
    cancel = [item for item in application.button if item.label == "Yêu cầu hủy job"]
    assert bool(cancel) == (state in {JobState.QUEUED, JobState.RUNNING})
    if cancel:
        cancel[0].click().run(timeout=20)
        updated = runtime.get(job.job_id)
        assert updated.cancellation_requested_at
        assert updated.state == (JobState.CANCELLED if state == JobState.QUEUED else JobState.RUNNING)
    if state == JobState.COMPLETED:
        assert "DEGRADED" in all_text(application) and "COMPLETED" in all_text(application)
        assert len(application.get("download_button")) == 3
    else:
        assert not application.get("download_button")


def test_publication_io_failure_stays_failed_in_monitor_history_and_download(runtime, monkeypatch):
    job, _ = queued(runtime)
    rename = Path.rename

    def denied(path, target):
        if path.name == "_partial" and Path(target).name == "result":
            raise OSError(5, "SYNTHETIC private filesystem error")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", denied)
    with Worker(runtime) as worker:
        failed = worker.run_once()
    assert failed.state == JobState.FAILED and failed.error_code == "PUBLICATION_IO_FAILED"
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    text = all_text(application)
    assert not application.exception and "FAILED" in text and "PUBLICATION_IO_FAILED" in text
    assert "SYNTHETIC private filesystem error" not in text
    assert any("operation=rename; errno=5; winerror=None; attempts=1" in item.value for item in application.code)
    assert not application.get("download_button")
    history = runtime.list_jobs()
    assert len(history) == 1 and history[0].job_id == job.job_id and history[0].state == JobState.FAILED
    with pytest.raises(WorkspaceError):
        verified_reports(runtime, job.job_id)


def test_publication_diagnostics_never_render_arbitrary_exception_text(runtime):
    import sqlite3
    job, _ = queued(runtime)
    owner = runtime.register_worker()
    runtime.claim_next(owner)
    runtime.finish(job.job_id, owner, JobState.FAILED, error_code="PUBLICATION_IO_FAILED")
    with sqlite3.connect(runtime.database) as db:
        db.execute("UPDATE jobs SET error_message=? WHERE job_id=?", ("SYNTHETIC private exception text", job.job_id))
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert not application.exception and "SYNTHETIC private exception text" not in all_text(application)
    assert all("SYNTHETIC private exception text" not in item.value for item in application.code)
    assert not application.get("download_button")


def test_history_selection_and_bad_id(runtime):
    first, _ = queued(runtime)
    second, _ = queued(runtime)
    application = app()
    application.selectbox[0].set_value(first.job_id)
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert first.job_id in [item.value for item in application.code]
    application.selectbox[0].set_value(second.job_id)
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert second.job_id in [item.value for item in application.code]
    application.session_state["workspace_job"] = "../../secret"
    application.run(timeout=20)
    assert not application.exception and application.warning


def test_manual_refresh_updates_history_after_external_worker_transition(runtime):
    job, _ = queued(runtime)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    worker_id = runtime.register_worker()
    runtime.claim_next(worker_id)
    button(application, "Làm mới trạng thái").click().run(timeout=20)
    history = next(frame.value for frame in application.dataframe if "Trạng thái" in frame.value.columns)
    assert history.iloc[0]["Trạng thái"] == "RUNNING"
    assert application.selectbox[0].value == job.job_id
    assert job.job_id not in application.selectbox[0].options[0]  # Full ID lives in copy/details.
    runtime.stop_worker(worker_id)
    button(application, "Làm mới trạng thái").click().run(timeout=20)
    history = next(frame.value for frame in application.dataframe if "Trạng thái" in frame.value.columns)
    assert history.iloc[0]["Trạng thái"] == "INTERRUPTED"
    assert "INTERRUPTED" in all_text(application) and not application.exception


def test_verified_download_bytes_and_separate_validation(runtime):
    job = completed(runtime)
    verified = verified_reports(runtime, job.job_id)
    manifest = json.loads((runtime.artifact_root / job.result_path).read_text(encoding="utf-8"))
    assert verified.result.validation.status == "DEGRADED" and job.state == JobState.COMPLETED
    for role, (filename, raw) in verified.downloads.items():
        assert filename == Path(manifest["artifacts"][role]["path"]).name
        assert hashlib.sha256(raw).hexdigest() == manifest["artifacts"][role]["sha256"]


def test_result_status_typography_is_scoped_and_uses_readable_words(runtime):
    completed(runtime)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    markup = "\n".join(element.proto.body for element in application.get("html"))
    assert "<strong>Đã hoàn tất</strong>" in markup and "<p>COMPLETED</p>" in markup
    assert "<strong>Còn giới hạn phân tích</strong>" in markup and "<p>DEGRADED</p>" in markup
    css = (ROOT / "dashboard.css").read_text(encoding="utf-8")
    assert ".st-key-workspace-result-summary .ml-metric strong" in css
    assert "minmax(min(100%,260px),1fr)" in css


@pytest.mark.parametrize("damage", ["missing", "checksum", "traversal", "reference", "partial"])
def test_damaged_or_unpublished_results_block_downloads(runtime, damage):
    job = completed(runtime)
    path = runtime.artifact_root / job.result_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    target = path.parent / manifest["artifacts"]["html"]["path"]
    if damage == "missing":
        target.unlink()
    elif damage == "checksum":
        target.write_bytes(b"tampered")
    elif damage == "traversal":
        manifest["artifacts"]["html"]["path"] = "../outside.html"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    elif damage == "reference":
        with runtime._connection(write=True) as db:
            db.execute("UPDATE jobs SET result_path='../outside/manifest.json' WHERE job_id=?", (job.job_id,))
    else:
        path.parent.rename(path.parent.with_name("_partial"))
    with pytest.raises(WorkspaceError):
        verified_reports(runtime, job.job_id)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert not application.exception and application.error
    assert not application.get("download_button")


@pytest.mark.parametrize("name", ["../secret", "C:/secret.csv", "a/../../secret", r"a\secret", "a:stream"])
def test_safe_child_rejects_unsafe_paths(runtime, name):
    with pytest.raises(WorkspaceError):
        safe_child(runtime.input_root, name)


def test_upload_rejects_symlink_without_following_it(runtime, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = runtime.input_root / ".marketlens-uploads"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows without symlink privilege: exercise the identical guard with a test double.
        from unittest.mock import patch
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda self: self == link or original(self)):
            with pytest.raises(WorkspaceError):
                persist_upload(runtime, "input.csv", CSV)
    else:
        with pytest.raises(WorkspaceError):
            persist_upload(runtime, "input.csv", CSV)
    assert not list(outside.iterdir())


def test_inherited_marketlens_configuration_is_removed():
    assert not any(name.startswith("MARKETLENS_") for name in os.environ)


def test_environment_isolation_covers_unknown_future_settings(monkeypatch):
    from conftest import clear_marketlens_environment
    for name in ("MARKETLENS_JOBS_DB", "MARKETLENS_INPUT_ROOT", "MARKETLENS_JOB_ARTIFACT_ROOT", "MARKETLENS_FUTURE"):
        monkeypatch.setenv(name, "SYNTHETIC inherited application configuration")
    monkeypatch.setenv("UNRELATED_TEST_SETTING", "retained")
    clear_marketlens_environment(monkeypatch)
    assert not any(name.startswith("MARKETLENS_") for name in os.environ)
    assert os.environ["UNRELATED_TEST_SETTING"] == "retained"


def test_persisted_artifact_root_guard_still_rejects_environment_mismatch(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("MARKETLENS_JOB_ARTIFACT_ROOT", str(tmp_path / "different-artifacts"))
    with pytest.raises(JobStoreError, match="artifact directory differs"):
        open_job_store()


def fragment_ids(application):
    """Test adapter for native fragment storage; production uses no private APIs."""
    return application._fragment_storage.ids_registered_after(0)


def tick_fragment(application, fragment_id, monkeypatch, *, manual=False):
    """Deliver the same RerunData that the browser timer/manual fragment event sends.

    AppTest does not run a browser clock; it otherwise uses the real Streamlit runner.
    """
    from streamlit.runtime.scriptrunner_utils.script_requests import ScriptRequests
    original = ScriptRequests.request_rerun

    def scoped(runner, data):
        return original(runner, replace(data, fragment_id_queue=[fragment_id], is_auto_rerun=not manual))

    with monkeypatch.context() as patch:
        # Also scope the runner's initial request; otherwise its default full-app
        # request wins coalescing before AppTest delivers our timer event.
        patch.setattr(ScriptRequests, "request_rerun", scoped)
        application.run(timeout=20)
    assert not application.exception
    return application


def monitored_app(runtime, monkeypatch):
    import market_research.dashboard_workspace as workspace
    calls = []
    original = workspace.show_new_research

    def form(store):
        calls.append("form")
        original(store)

    monkeypatch.setattr(workspace, "show_new_research", form)
    application = app()
    application.text_input[0].set_value("Bản nháp cần giữ nguyên")
    application.text_input[1].set_value(SOURCE)
    button(application, "Xem chi tiết job").click().run(timeout=20)
    parent = application._fragment_storage._parent_by_id
    child = next(key for key in fragment_ids(application) if parent[key] is not None
                 and inspect.getclosurevars(application._fragment_storage._fragments[key]).nonlocals["non_optional_func"].__name__ == "poll")
    return application, child, parent[child], calls


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED])
def test_native_auto_refresh_terminal_transition_no_form_or_service_calls(runtime, monkeypatch, terminal, no_services):
    job, _ = queued(runtime)
    application, child, parent, form_calls = monitored_app(runtime, monkeypatch)
    initial_calls = len(form_calls)
    def forbidden_submit(*args, **kwargs):
        raise AssertionError("Refresh must never submit a request")
    monkeypatch.setattr(JobStore, "submit", forbidden_submit)
    tick_fragment(application, child, monkeypatch)
    assert "QUEUED" in all_text(application)
    if terminal == JobState.COMPLETED:
        with Worker(runtime) as worker:
            assert worker.run_once().state == terminal  # Local SYNTHETIC CSV, inference OFF.
    elif terminal == JobState.CANCELLED:
        runtime.request_cancel(job.job_id)
    else:
        owner = runtime.register_worker()
        runtime.claim_next(owner)
        runtime.finish(job.job_id, owner, terminal, error_code="HANDLER_FAILED")
    tick_fragment(application, child, monkeypatch)
    assert terminal.value in all_text(application)
    assert len(application.get("download_button")) == (3 if terminal == JobState.COMPLETED else 0)
    # Terminal child snapshots stop querying; the slow parent will retire the child.
    original_get = runtime.get
    def forbidden_get(*args, **kwargs):
        raise AssertionError("A terminal child must not poll SQLite")
    monkeypatch.setattr(JobStore, "get", forbidden_get)
    tick_fragment(application, child, monkeypatch)
    monkeypatch.setattr(JobStore, "get", lambda self, key: original_get(key))
    tick_fragment(application, parent, monkeypatch)
    assert child not in fragment_ids(application)
    assert terminal.value in all_text(application)
    assert len(form_calls) == initial_calls and len(runtime.list_jobs()) == 1
    assert not runtime.get(job.job_id).request.allow_inference
    assert len(application.get("download_button")) == (3 if terminal == JobState.COMPLETED else 0)
    assert not no_services


def test_fragment_manual_refresh_preserves_form_and_poll_intervals(runtime, monkeypatch):
    from market_research.dashboard_workspace import ACTIVE_REFRESH_SECONDS, TERMINAL_REFRESH_SECONDS
    job, _ = queued(runtime)
    application, child, parent, calls = monitored_app(runtime, monkeypatch)
    draft_values = {widget.id: widget.value for widget in application.text_input}
    initial = len(calls)
    owner = runtime.register_worker()
    runtime.claim_next(owner)
    tick_fragment(application, child, monkeypatch)
    assert "RUNNING" in all_text(application)
    runtime.request_cancel(job.job_id)
    runtime.finish(job.job_id, owner, JobState.CANCELLED)
    # Parent fragment event is what the manual button sends in a browser. AppTest
    # exposes only the latest partial tree after a child-only run.
    tick_fragment(application, parent, monkeypatch, manual=True)
    assert "CANCELLED" in all_text(application) and child not in fragment_ids(application)
    assert len(calls) == initial
    # No form script executed; its persisted widget values are unchanged.
    assert all(application.session_state[key] == value for key, value in draft_values.items())
    assert ACTIVE_REFRESH_SECONDS == 2.5 and TERMINAL_REFRESH_SECONDS == 30
    assert len(runtime.list_jobs()) == 1


def test_setup_has_grouped_source_execution_and_safe_defaults(runtime):
    application = app()
    markup = "\n".join(item.proto.body for item in application.get("html"))
    assert "Thiết lập nghiên cứu" in markup
    assert any(item.value == "Nguồn dữ liệu" for item in application.subheader)
    assert any(item.value == "Tùy chọn thực thi" for item in application.subheader)
    assert len(application.checkbox) == 2 and not any(item.value for item in application.checkbox)
    assert not runtime.list_jobs()


def test_verified_display_snapshot_skips_unchanged_hashing_and_invalidates_changed_files(runtime, monkeypatch):
    import market_research.workspace_state as state
    job = completed(runtime)
    original = state.verified_reports
    calls = []
    def tracked(*args):
        calls.append("verify")
        return original(*args)
    monkeypatch.setattr(state, "verified_reports", tracked)
    snapshot = state.ResultSnapshot().load(runtime, job)
    assert snapshot.verified and calls == ["verify"]
    snapshot.load(runtime, job)
    snapshot.load(runtime, job)
    assert calls == ["verify"]
    manifest_path = runtime.artifact_root / job.result_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (manifest_path.parent / manifest["artifacts"]["html"]["path"]).write_bytes(b"SYNTHETIC corruption")
    snapshot.load(runtime, job)
    assert snapshot.verified is None and snapshot.error and len(calls) == 2


def test_missing_bundle_has_bounded_verification_and_explicit_retry(runtime, monkeypatch):
    import market_research.workspace_state as state
    job = completed(runtime)
    published = (runtime.artifact_root / job.result_path).parent
    held = published.with_name("SYNTHETIC-test-held")
    published.rename(held)
    snapshot = state.ResultSnapshot()
    for _ in range(3):
        snapshot.load(runtime, job)
    assert snapshot.attempts == 3 and snapshot.verified is None
    held.rename(published)
    snapshot.load(runtime, job)
    assert snapshot.attempts == 3 and snapshot.verified is None
    assert state.ResultSnapshot().load(runtime, job).verified is not None


def test_download_reverifies_all_files_even_when_display_snapshot_exists(runtime):
    from market_research.workspace_state import ResultSnapshot, verified_download
    job = completed(runtime)
    snapshot = ResultSnapshot().load(runtime, job)
    filename, content = snapshot.verified.downloads["json"]
    digest = hashlib.sha256(content).hexdigest()
    assert verified_download(runtime, job.job_id, "json", digest) == content
    manifest_path = runtime.artifact_root / job.result_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    html = manifest_path.parent / manifest["artifacts"]["html"]["path"]
    html.write_bytes(b"SYNTHETIC changed HTML")
    with pytest.raises(WorkspaceError):
        verified_download(runtime, job.job_id, "json", digest)


def test_download_buttons_defer_fresh_verification(runtime, monkeypatch):
    import streamlit as st
    completed(runtime)
    captured = []
    original = st.download_button
    def record(label, data, **kwargs):
        captured.append(data)
        return original(label, data, **kwargs)
    monkeypatch.setattr(st, "download_button", record)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert not application.exception and len(captured) == 3
    assert all(callable(data) and isinstance(data(), bytes) for data in captured)


def test_history_and_monitor_sync_without_full_page_or_selection_reset(runtime, monkeypatch):
    job, _ = queued(runtime)
    application, monitor, _, form_calls = monitored_app(runtime, monkeypatch)
    parents = application._fragment_storage._parent_by_id
    history = next(key for key in fragment_ids(application) if parents[key] is not None
                   and inspect.getclosurevars(application._fragment_storage._fragments[key]).nonlocals["non_optional_func"].__name__ == "poll_history")
    initial_calls = len(form_calls)
    with Worker(runtime) as worker:
        assert worker.run_once().state == JobState.COMPLETED
    tick_fragment(application, monitor, monkeypatch)
    assert "COMPLETED" in all_text(application) and len(application.get("download_button")) == 3
    tick_fragment(application, history, monkeypatch)
    frame = next(item.value for item in application.dataframe if "Trạng thái" in item.value.columns)
    assert frame.iloc[0]["Trạng thái"] == "COMPLETED" and frame.iloc[0]["Kiểm định"] == "DEGRADED"
    assert application.selectbox[0].value == job.job_id
    assert application.session_state["workspace_job"] == job.job_id and len(form_calls) == initial_calls
    monkeypatch.setattr(JobStore, "list_jobs", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Terminal history must stop fast queries")))
    tick_fragment(application, history, monkeypatch)
    assert len(form_calls) == initial_calls


def test_manual_history_refresh_is_available_and_does_not_submit(runtime):
    queued(runtime)
    application = app()
    button(application, "Làm mới lịch sử").click().run(timeout=20)
    assert not application.exception and len(runtime.list_jobs()) == 1


@pytest.mark.parametrize("status", ["VALID", "DEGRADED", "INVALID"])
def test_result_status_copy_is_distinct_in_presentation_fixture(runtime, monkeypatch, status):
    """SYNTHETIC presentation double only, not a new research result or validator rule."""
    import market_research.dashboard_workspace as workspace
    job = completed(runtime)
    verified = verified_reports(runtime, job.job_id)
    outcome = job.research_outcome.model_copy(update={"validation_status": status, "claims_valid": status != "INVALID"})
    display_job = job.model_copy(update={"research_outcome": outcome})
    monkeypatch.setattr(workspace, "result_snapshot", lambda *args, **kwargs:
                        SimpleNamespace(verified=verified, checked_at=job.finished_at))
    monkeypatch.setattr(workspace, "_test_result_job", display_job, raising=False)
    application = AppTest.from_string('''
from market_research.dashboard_workspace import show_result, _test_result_job
from market_research.job_config import open_job_store
show_result(open_job_store(), _test_result_job)
''').run(timeout=20)
    markup = "\n".join(item.proto.body for item in application.get("html"))
    assert not application.exception and f"<p>{status}</p>" in markup
    if status == "INVALID":
        assert application.warning and not any("AI analysis chưa" in str(item.value) for item in application.subheader)


def test_history_mobile_pagination_retains_all_rows_and_full_id_detail(runtime):
    for _ in range(21):
        runtime.submit("smoke")
    application = app()
    assert not application.exception and len(application.selectbox[0].options) == 21
    assert len(application.selectbox[1].options) == 3
    application.selectbox[1].set_value(2).run(timeout=20)
    assert not application.exception
    oldest = runtime.list_jobs(200)[-1]
    markup = "\n".join(item.proto.body for item in application.get("html"))
    assert oldest.job_id[:10] in markup and "rw-history-mobile" in markup
    application.selectbox[0].set_value(oldest.job_id)
    button(application, "Xem chi tiết job").click().run(timeout=20)
    assert oldest.job_id in [item.value for item in application.code]


def test_snapshot_binds_display_to_authoritative_outcome(runtime):
    from market_research.workspace_state import ResultSnapshot
    job = completed(runtime)
    changed = job.model_copy(update={"research_outcome": job.research_outcome.model_copy(update={"validation_status": "INVALID"})})
    snapshot = ResultSnapshot().load(runtime, changed)
    assert snapshot.verified is None and snapshot.error


def test_manual_refresh_retries_exhausted_missing_bundle(runtime):
    job = completed(runtime)
    published = (runtime.artifact_root / job.result_path).parent
    held = published.with_name("SYNTHETIC-held-for-manual-refresh")
    published.rename(held)
    application = app()
    button(application, "Xem chi tiết job").click().run(timeout=20)
    application.run(timeout=20)
    application.run(timeout=20)
    assert application.error and not application.get("download_button")
    held.rename(published)
    button(application, "Làm mới trạng thái").click().run(timeout=20)
    assert not application.exception and len(application.get("download_button")) == 3


def test_workspace_helpers_escape_metadata_and_use_scoped_css(runtime):
    from market_research.workspace_design import job_header, summary_grid
    job, _ = queued(runtime)
    unsafe = '<script>alert("SYNTHETIC")</script>'
    altered = job.model_copy(update={"request": job.request.model_copy(update={"topic": unsafe})})
    markup = job_header(altered, "UTC") + summary_grid([("Nguồn", unsafe)])
    assert "<script>" not in markup and "&lt;script&gt;" in markup
    css = (ROOT / "dashboard.css").read_text(encoding="utf-8")
    assert ".st-key-research-workspace .rw-job-header" in css
    assert ".st-key-research-workspace .st-key-workspace-history-table" in css


def test_download_refuses_different_valid_bytes_than_display_digest(runtime):
    from market_research.workspace_state import verified_download
    job = completed(runtime)
    with pytest.raises(WorkspaceError, match="đổi so với bản"):
        verified_download(runtime, job.job_id, "json", "0" * 64)
