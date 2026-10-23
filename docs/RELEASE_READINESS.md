# MarketLens — M3.4 release readiness

## Kết luận và phạm vi

**Local mentor demo: READY**, khi sử dụng workspace thử riêng và gắn nhãn dữ liệu. **Public GitHub release: gate riêng của chủ repository**, cần duyệt diff, giấy phép và quyền chia sẻ dữ liệu. Đây không phải dịch vụ production hoặc hệ thống xác nhận nhu cầu thương mại.

### P0 publication gate — 23/10/2026

Lỗi tự phát được bắt trực tiếp tại rename `_partial → result`: `PermissionError`, errno 13, WinError 5, sau 8/8 checkpoint. Mã cũ gộp lỗi OS với invalid bundle và bỏ qua cơ hội phục hồi denial tạm thời. Bản vá chỉ retry rename Windows 5/32/33 tối đa 4 lần (tổng chờ 0,5 giây), kiểm tra bundle/target/owner mỗi lần, xác minh lại vị trí cuối cùng và live lease trước commit. Năm regression cases thất bại trên mã cũ đã được sửa; không đổi Evidence Validator, provenance, model hoặc SQLite schema. Không xác định hoặc suy đoán process OS gây denial.

Focused: **208 passed**; crash/restart process thật: **1 passed**; full Windows: **491 passed**, không skip/deselect. Harness SYNTHETIC không chèn lỗi: **241/241 COMPLETED**, có một native WinError 5 được xử lý mà không chạy lại research. Lỗi I/O kéo dài vẫn FAILED/PUBLICATION_IO_FAILED; bundle không hợp lệ vẫn FAILED/INVALID_RESULT. Reference/manifest/checksum tiếp tục chặn download sai. Orphan sau lỗi SQLite/process crash không được tự công nhận hoặc replay; request thử lại phải có job/bundle mới. Không gọi live inference/collection; 24/24 historical hashes giữ nguyên.

Các số liệu và cảnh báo publication trong phần M3.4 bên dưới là **biên bản lịch sử trước P0**, được giữ để truy vết; trạng thái publication hiện tại tham chiếu gate P0 và README. Hai incident M3.3.2 cũ thiếu exception trace nên không gán chắc chắn tác nhân cho chúng. Native reproduction có trace và regression/fault injection là bằng chứng của bản vá, không phải lời hứa filesystem không bao giờ thất bại.

M3.4 bắt đầu từ checkpoint `8dfa254` trên `feature/interactive-research`. Không thay đổi sáu trang Streamlit, collector, validator hoặc worker. HTML exporter chỉ đọc các record đã có, escape text và tạo tài liệu tự chứa; JSON audit giữ đầy đủ record gốc. Các report lịch sử và SHA-256 đã công bố được giữ nguyên.

**Hotfix provenance sau QA M3.4:** CSV do script demo tạo phải để trống URL trong Workspace; marker trong nội dung được ghép với `urn:marketlens:synthetic-demo` (định danh nội bộ, không tạo source link). Preview/evidence/reports/audit gắn nhãn SYNTHETIC; metric demo riêng không tính thành market signal, inference bị chặn và không được trộn với evidence thật. Kiểm tra URL/keyword/vùng cho CSV thật giữ nguyên. Các số liệu QA/browser bên dưới mô tả lượt M3.4 trước hotfix; report demo cũ dùng URL Google Trends dựng lại không phải evidence thật và không được viết lại để tránh phá checksum. Tạo job mới để áp dụng provenance đúng.

Nghiệm thu hotfix: **79 focused tests passed; full suite 411 passed trong 75,95 giây**, không skip/deselect. Thêm 23 regression cases so với M3.4. Pipeline SYNTHETIC offline qua Worker/AppTest đạt COMPLETED/DEGRADED, `evidence_sufficient=false`, inference không được yêu cầu; download vẫn kiểm tra hash. **24/24** protected hashes giữ nguyên. Không chạy collection/Ollama thật hoặc browser QA mới trong hotfix này.

QA ngày **22/10/2026**, Python **3.12.6**:

- Bản sao từ tracked HEAD + tám file candidate M3.4, venv mới không dùng system site-packages: **388 passed / 102,39 giây**, không skip/deselect, không MARKETLENS overrides. Streamlit **1.66.1**, pytest **8.4.2**, Pydantic **2.14.1**, httpx **0.28.1**; `pip check` đạt.
- Workspace gốc, venv hiện có (Streamlit **1.66.0**), đủ ba biến DB/input/artifacts trỏ vào workspace QA tồn tại: **388 passed / 94,92 giây**, không skip/deselect. Database QA dùng làm cấu hình kế thừa giữ nguyên SHA-256.
- Focused HTML/portable demo: **25 passed**. Tăng từ baseline 363 lên 388 tests; không loại test cũ.
- Worker ở process độc lập, chặn network/inference: job SYNTHETIC **COMPLETED, 8/8, DEGRADED, 4 observations, not_requested**. Không phải lượt nghiên cứu bằng dữ liệu thật.
- AST syntax check **49 file Python** đạt; `git diff --check` và kiểm tra whitespace tám file candidate đạt. **24/24 protected artifact hashes** và hai file untracked ban đầu giữ nguyên; HEAD/main/index không đổi. Rà soát cấu hình, Git ignore và mẫu secret/path không thấy credential hoặc đường dẫn máy cá nhân mới; đây không phải pentest hay vulnerability audit.

## A. Automated / offline acceptance

| Journey / contract | Phương pháp | Kết quả quan sát | Gate | Hành động còn lại |
|---|---|---|---|---|
| HTML ngữ nghĩa | HTMLParser: headings, paragraphs, lists, table, links; cấm pre dump | Focused report tests đạt | PASS | Xem thêm browser/print bên dưới |
| HTML an toàn | Injection vào text/URL; scheme allowlist; không script/image/iframe/CDN; CSP | Escaping và full-record JSON roundtrip đạt | PASS | Không coi report như sandbox chống người sửa filesystem |
| Evidence / số liệu / `<1` | So sánh JSON gốc và audit HTML, raw observations | Giá trị và ID được giữ | PASS | Không suy ra sales từ index |
| CSV sai/lớn, mismatch keyword/vùng/hash | Regression suite workspace/engine | Hai full suites đạt | PASS | Giữ error states trung thực |
| Dedupe, quyền inference OFF và xác nhận hai bước | Regression workspace/jobs | Không submit do rerun/polling | PASS | Inference chỉ do người dùng chủ động bật |
| QUEUED khi thiếu worker | Store/Workspace tests, workflow SYNTHETIC | Không tự khởi chạy worker | PASS | Cùng DB/input/artifacts trong hai terminal |
| Completion/cancel/failure/interruption, restart/lease | Worker, process, publication regression | State và ownership được bảo toàn trong regression | PASS | Không auto replay; incident publication cũ vẫn WARNING |
| Missing/corrupt artifact, unsafe path, downloads | Verifier/manifest/checksum tests | Chặn partial, checksum sai và path không hợp lệ | PASS | Giữ quyền filesystem local |
| History / monitor refresh, draft | Native fragment/AppTest regressions | Giữ lựa chọn và draft, không phát sinh job | PASS | Browser nền có thể throttle |
| Năm trang analytics và Workspace | AppTest với fixture SYNTHETIC tạm | Hai full suites bao gồm sáu trang đạt | PASS | Clone mới không có AI Insights lịch sử |
| Clone không có CSV/DB/artifact/env riêng | Nguồn từ tracked HEAD + whitelist thay đổi M3.4; venv mới | Không copy TASK-1/fixtures/output riêng; 388 tests đạt | PASS | Thay đổi M3.4 chưa nằm trong commit cho tới khi chủ repo duyệt |
| Package/setup Windows | Venv mới, pip editable extras, pip check | Cài từ PyPI chính thức thành công | PASS | Ghi lại phiên bản dependency của môi trường demo |
| Offline demo | Script tạo CSV UTF-8/BOM có nhãn SYNTHETIC, không ghi đè | Tái dùng importer/JobStore/worker thật, inference/network OFF | PASS | Không giới thiệu là market evidence thật |

Kết quả test cuối, thời gian, versions và checksum được ghi trong biên bản QA local `.tmp/m34/`; thư mục đó bị Git ignore. Không lấy việc tests pass để khẳng định rủi ro publication đã hết.

## B. Actual Windows browser verification

| Journey | Phương pháp | Gate / giới hạn |
|---|---|---|
| HTML standalone: header, findings, uncertainty, evidence/audit | IAB trên Windows, ảnh thực 1440/390px; DOM width tại 768/1024px | PASS: không tràn toàn trang. Dùng cả report pipeline DEGRADED và presentation fixture SYNTHETIC; không có inference mới |
| Link tới fact/evidence, bảng cuộn trong vùng riêng | Click anchor, mở audit JSON, screenshot/DOM | PASS: fact/evidence đúng đích, `<1` giữ nguyên; bảng cuộn riêng. Không click/fetch nguồn ngoài |
| Tải HTML từ Workspace | Click download thật, đối chiếu bytes với manifest và bản HTML đã render qua loopback | PASS: SHA-256 trùng cả ba; chưa mở file trực tiếp bằng Edge/Chrome |
| Startup sạch và history | Streamlit 1.66.1 từ clone, analytics root rỗng; mở job SYNTHETIC đã lưu | PASS: empty state trung thực; history một job, COMPLETED/DEGRADED, ba nút report |
| Workspace mobile | Viewport 390px, mở rồi collapse sidebar, đọc main/download controls | PASS trong phạm vi kiểm tra này; không lặp lại toàn bộ browser QA sáu trang |
| Print preview | CSS print + automated assertions | WARNING cho đến khi kiểm tra preview/in thật trên Edge/Chrome |
| UI desktop/390/768/1024 và inference thật trước M3.4 | Windows QA đã được người dùng xác nhận ở M3.3 | Không phải kết quả chạy mới của M3.4 |

Ảnh QA thực nằm trong `.tmp/m34/`, không đưa vào Git. Report presentation có VALID chỉ để kiểm tra bố cục giả thuyết/fact links; nhãn SYNTHETIC và provenance fixture rõ ràng, không phải kết quả inference hoặc claim-validation mới.

## C. Real Ollama E2E đã hoàn thành trước M3.4

Người dùng xác nhận trên Windows: `qwen3:4b-instruct` hoạt động, nghiên cứu từ Streamlit qua worker đạt **COMPLETED + VALID**. Đây là bằng chứng do người dùng cung cấp, không phải một lượt model mới được thực hiện trong M3.4. Không suy ra sales/product-market fit từ VALID. M3.4 chỉ thực hiện pipeline SYNTHETIC no-inference và test offline; giữ nguyên artifact thật cũ.

## D. Các kiểm tra cần chủ repo tham gia

1. Mở report HTML mới tải trong Edge/Chrome ở desktop và mobile; kiểm tra headings/list/table, source/fact links, `<1`, audit và **Ctrl+P**. Không ghi đè HTML cũ vì sẽ phá manifest checksum.
2. Tự chọn nếu cần một lượt **CSV thật + Ollama** với exporter mới: xác nhận hai checkbox, cùng cấu hình worker, theo dõi state/validation và tải ba reports. Đây là inference thật có chi phí local; không cần cho demo OFF.
3. Nếu publication lỗi: giữ DB/bundle/log và exception chain gốc để điều tra; không retry mù hoặc tự replay model. Hướng dẫn gate ở README; trace M3.3.3 local được giữ nguyên.
4. Trước GitHub public: chỉ đưa file source/test/docs đã duyệt; giữ CSV riêng, `.env`, DB, outputs, logs, snapshots, QA screenshots ngoài Git. `TASK-1.md` và `tests/fixtures/` hiện chủ ý untracked. Chọn giấy phép phù hợp trước khi mời tái sử dụng; chưa có dependency lock bảo đảm tái tạo bit-for-bit.

## Known risks và mức độ

| Rủi ro | Mức độ | Đánh giá / biện pháp |
|---|---|---|
| Intermittent FAILED/INVALID_RESULT tại publication | HIGH cho release ổn định; WARNING cho demo có giám sát | Root cause chưa xác định. Windows file handle chặn rename chỉ là cơ chế được tái hiện có kiểm soát; không chứng minh giải thích incident cũ. Không sửa bằng blind retry. |
| Nguồn dữ liệu hạn chế | MEDIUM | Trends import thủ công, URL do người dùng khai báo; catalog không phải verified competitors/sales/prices. Nguồn unavailable phải hiển thị thiếu dữ liệu. |
| Không có dữ liệu riêng trên clone | EXPECTED | Analytics empty state; dùng Workspace với dữ liệu riêng hoặc demo gắn nhãn SYNTHETIC. Niche example không chứa mapping CSV riêng. |
| Phiên bản dependency theo range | MEDIUM | Venv mới đã kiểm tra; chưa có lockfile/hash-pinned install hoặc audit lỗ hổng độc lập. |
| Local trust model | MEDIUM | Loopback, không authentication đa người dùng; người có quyền sửa filesystem/SQLite vẫn nằm ngoài ranh giới bảo vệ ứng dụng. |
| HTML mới, report cũ | LOW | Exporter mới chỉ áp dụng cho output mới. Report cũ được giữ nguyên, kể cả định dạng pre cũ, để bảo toàn provenance/checksum. |
| Print/desktop zoom trên browser thực | LOW | Cần chủ repo kiểm tra print preview và zoom thật; không suy ra từ CSS/AppTest. |

## Demo mentor 3–5 phút

1. **0:00–0:40 — Bài toán và ranh giới:** giải thích evidence → fact → claim, phân biệt execution với validation. Cho xem sơ đồ README.
2. **0:40–1:20 — Nguồn và quyền:** Workspace, topic SYNTHETIC mentor demo, để trống URL, CSV preview có nhãn SYNTHETIC, `<1`, inference OFF. Không dựng URL Google Trends cho demo. Nếu dùng dữ liệu thật sẵn có, nói rõ nguồn/phạm vi và quyền chia sẻ.
3. **1:20–2:10 — Job bền vững:** gửi một lần, QUEUED trước worker, chạy worker độc lập; xem checkpoint thật, monitor/history cập nhật không F5.
4. **2:10–3:30 — Kết quả và audit:** COMPLETED/DEGRADED ở demo OFF; giải thích không có AI mới. Tải HTML mới, xem summary/limitations/table/source/fact links và full audit. Không gọi fixture VALID là model result thật.
5. **3:30–4:30 — Sáu trang và tradeoffs:** nếu có batch/comparison được phép dùng, cho xem analytics/heatmap/evidence matrix; nếu clone trống, trình bày empty state có chủ đích. Kết thúc với publication risk, test results và phạm vi phát triển tiếp.

Chuẩn bị trước một report mới đã verify trong workspace demo để có phương án trình bày nếu worker gặp lỗi. Không đổi FAILED thành thành công hoặc che state lỗi trong buổi demo.
