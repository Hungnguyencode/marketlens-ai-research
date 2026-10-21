# Evidence-First AI Market Research Agent

## Phạm vi hiện tại

Repository hiện gồm research engine cho niche đơn, tích hợp Ollama và kiểm định claim, Open Library collector, batch/comparison bốn niche cùng dashboard Streamlit năm trang. Chỉ run lịch sử cozy được lưu với provenance Ollama; batch/comparison bốn niche hiện không chạy inference và không đưa ra xếp hạng thương mại nếu thiếu evidence. Báo cáo tiếng Việt; keyword tiếng Anh.

## Dữ liệu cục bộ và kiểm thử tái lập

CSV Google Trends do người dùng xuất, snapshots, logs và outputs nghiên cứu được giữ cục bộ và `.gitignore` loại khỏi baseline Git mặc định. Chưa xác nhận quyền tái phân phối các dữ liệu nguồn bên ngoài này; hãy chỉ chia sẻ chúng khi đã kiểm tra quyền. Test offline tạo CSV tổng hợp tạm thời được ghi rõ là fixture, chỉ để kiểm tra parser/comparison, không phải dữ liệu thị trường và không được dùng trong live mode. Dashboard khởi động khi chưa có artifacts và hiển thị trạng thái trống; cần chạy/import dữ liệu của riêng bạn để xem kết quả.

## Nguồn, tín hiệu và giới hạn

### Google Books Volumes API (đang tích hợp)

Collector lấy kết quả thư mục API, title/author/publication metadata và giá niêm yết khi có. `totalItems` là số bản ghi thư mục API báo cho truy vấn; nó không đo search interest, số listing đầy đủ, doanh số, nhu cầu hay market share. Giá niêm yết không phải giá giao dịch. Google Books trả HTTP 429 trên lần kiểm tra Windows do người dùng thực hiện; nguyên nhân chính xác (rate quota, daily/project quota, burst limit hoặc chính sách khác) chưa xác minh. API key tùy chọn nhận diện project/quota cho public-data requests nhưng không đảm bảo giải quyết lỗi. [Google Books API key guide](https://developers.google.com/books/docs/v1/using)

Collector giới hạn tối đa hai retry cho HTTP 429, tôn trọng `Retry-After` (giây hoặc HTTP-date), và dùng exponential backoff nhỏ kèm jitter khi header không có. Nếu máy chủ yêu cầu chờ lâu hơn giới hạn cấu hình, collector dừng và báo rõ thời gian chờ thay vì retry sớm. HTTP 403/quota được báo riêng và không tự retry. Dedupe keyword để tránh request lặp. API key lấy từ `GOOGLE_BOOKS_API_KEY`, không xuất hiện trong evidence URL hoặc log.

### Nguồn thay thế được đánh giá

- **Google Trends API alpha** có thể cung cấp search interest tương đối theo thời gian và vùng, nhưng Google đang giới hạn quyền truy cập cho nhóm alpha tester; chưa có quyền truy cập xác nhận cho dự án này, nên chưa thể triển khai/kiểm chứng collector. Dữ liệu Trends là search interest, không phải số tìm kiếm tuyệt đối. [Tài liệu API alpha](https://developers.google.com/search/apis/trends)
- **Amazon Creators API** có tài liệu cho tìm listing, offer price và sales rank. Đây là tín hiệu availability/giá/rank, không đồng nghĩa số sales cụ thể. Cần quyền truy cập chương trình/credentials; dự án chưa có tài khoản/key được xác nhận và chưa kiểm tra được live access, nên không đưa vào tool registry. [Tài liệu SearchItems](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference/operations/search-items), [tài liệu SalesRank và giá](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference/operations/get-variations)
- Etsy Open API cũng yêu cầu app credentials. Chưa có key trong project; không scrape search page hoặc vượt hạn chế truy cập.

### Open Library Search API (collector metadata độc lập)

Milestone 2.1 bổ sung collector gọi API tìm kiếm chính thức của [Open Library](https://openlibrary.org/dev/docs/api/search) qua `market_research.openlibrary_collector`. Nguồn này trả metadata tác phẩm (title, author, năm xuất bản đầu, số edition được ghi nhận) và work URL. Nó **không** cung cấp bằng chứng về xu hướng tìm kiếm, số listing sản phẩm đầy đủ, giá, doanh số, bestseller hay nhu cầu. `numFound` là số kết quả thư mục theo truy vấn, không phải market size; `edition_count` là số edition gắn với work, không phải doanh số hoặc số đối thủ.

Collector độc lập với Ollama và luồng `main.py`, có thể gọi bằng `collect_openlibrary(...)` hoặc `openlibrary_tool_registry()` theo bounded `ToolRegistry` hiện có. Nó có giới hạn tối đa 20 kết quả, timeout, pacing và tối đa một retry ngắn cho HTTP 429/5xx (tôn trọng `Retry-After`, dừng nếu server yêu cầu chờ vượt ngưỡng). Mọi bản ghi giữ query gốc, ID/URL work, metadata thực API trả về, thời điểm lấy, request URL và raw record trong một catalog-item model riêng, không ép thành numeric market evidence. Bộ lọc giải thích được dựa trên token title/query chỉ gắn nhãn `candidate` để sàng lọc thủ công; nhãn này không xác nhận đó là listing cạnh tranh. Các snapshot replay giữ thời gian truy xuất ban đầu và được đánh dấu `cache_replay`, không giả làm truy vấn live mới.

Smoke test live, không gọi Ollama hoặc main research workflow:

```powershell
python -m market_research.openlibrary_collector --query "cozy coloring book" --limit 10
```

Lưu snapshot và log collector tùy chọn:

```powershell
python -m market_research.openlibrary_collector --query "cozy coloring book" --limit 10 `
  --snapshot .\snapshots\openlibrary-cozy.json
```

Replay snapshot offline (kết quả ghi `provenance=cache_replay` và giữ timestamp cũ):

```powershell
python -m market_research.openlibrary_collector --query "cozy coloring book" --mode replay `
  --snapshot .\snapshots\openlibrary-cozy.json
```

Collector metadata này được dùng trong batch bốn niche bên dưới; luồng nghiên cứu `main.py` vẫn dùng Google Books/CSV importer. Catalog không đi vào fact logic của Trends hay claim validator. Open Library có thể thiếu coverage hoặc phân loại/ghi metadata không đầy đủ; kết quả relevance thưa phải được giữ nguyên, không bù bằng ví dụ tổng hợp.

### Google Trends chart CSV (đường nạp evidence thủ công)

Google xác nhận chart dữ liệu Trends có thể export CSV và chia sẻ link của trang kết quả. CSV importer trong ứng dụng nhận file do người dùng tải từ UI, kèm **URL thật** của trang Trends; không gọi Trends API, không tự tải hay scrape. Nguồn này cung cấp search-interest index tương đối theo kỳ (không phải số lượt tìm kiếm tuyệt đối). Nó không cung cấp listing, giá, doanh số hay lời phàn nàn của khách hàng. [Hướng dẫn export/cite CSV](https://support.google.com/trends/answer/4365538?hl=en).

Người dùng đã xác minh Open Library trên Windows: query `cozy coloring book` trả 10 record, 10 parsed, 8 relevance candidate. Kết quả này xác nhận metadata của một query trên host, chưa xác nhận bốn niche hoặc quyền truy cập từ runner Codex. Không tạo dữ liệu tổng hợp ở live mode.

## Cấu hình và cài đặt Windows PowerShell

Yêu cầu Python 3.11+, Ollama đang chạy trên Windows host, model `qwen3:4b-instruct` đã cài, và outbound access tới `www.googleapis.com`.

```powershell
$repo = Join-Path $HOME "ai-market-research-agent"
Set-Location $repo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Nếu PowerShell chặn activate script, có thể gọi trực tiếp `\.venv\Scripts\python.exe` và `\.venv\Scripts\pytest.exe`.

Các biến cấu hình được đọc từ process environment (ứng dụng hiện không tự parse `.env`):

```powershell
$env:OLLAMA_BASE_URL = "http://localhost:11434"
$env:OLLAMA_MODEL = "qwen3:4b-instruct"
$env:OLLAMA_TIMEOUT_SECONDS = "120"
$env:REQUEST_TIMEOUT_SECONDS = "15"
# Chỉ đặt nếu bạn có Google Books API key; đừng commit/đăng key.
$env:GOOGLE_BOOKS_API_KEY = "<your-key>"
```

Không cần key để thử collector; API key không đảm bảo xử lý được quota 429.

## Kiểm tra Ollama inference

`/api/tags` chỉ xác nhận server trả lời. Lệnh này gửi một generation request thực tới `/api/generate` và in kết quả:

```powershell
python main.py --check-ollama
```

Kết quả đã được người dùng xác nhận trên Windows host: model `qwen3:4b-instruct` trả `OK` qua `/api/generate`. Kết nối từ Codex sandbox có thể khác kết nối Windows host; lỗi sandbox không chứng minh host bị lỗi. Ứng dụng giữ nguyên `localhost`, không đổi host binding hay mở cổng mạng.

## Chạy nghiên cứu và replay

Live run gọi Google Books, lưu snapshot thật (hoặc snapshot rỗng kèm lỗi nếu bị chặn), gửi evidence thu được cho Ollama, validate và sinh ba định dạng:

```powershell
python main.py --niche "cozy coloring books for adults" --mode live
```

Nếu Google Books trả 429 và bạn cần chạy có keyword trend, có thể tải chart CSV thật từ Google Trends UI (chọn United States và time range phù hợp), copy URL trang kết quả thực từ address bar, rồi import thủ công:

```powershell
python main.py --niche "cozy coloring books for adults" --mode csv_import `
  --trends-csv .\data\google-trends.csv `
  --source-url "<paste the actual Google Trends page URL used for this export>"
```

CSV phải có cột thời gian `Week`/`Tuần`, `Day`/`Ngày`, `Month`/`Tháng` hoặc `Date` và một hay nhiều cột keyword. Header hỗ trợ Unicode NFC/NFD, UTF-8/BOM, preamble metadata và dòng trống. Importer ghi rõ `Google Trends (manual CSV export)` và `manual export, not autonomous collection`; hai kỳ có giá trị số trở lên trên cùng một chuỗi là điều kiện tối thiểu để evidence được xem là đủ cho xu hướng. Các giá trị `<1` được lưu dạng raw observation và không đổi thành số. Khi có dữ liệu này, engine gửi evidence kèm source URL đến Ollama và lưu snapshot để replay.

Trước khi phân tích, Python tính trung bình hai cửa sổ 4 tuần đã hoàn tất, mức thay đổi theo **điểm index tương đối**, tuần đỉnh và evidence IDs tương ứng. Tuần hiện tại chưa hoàn tất bị loại khỏi so sánh; ngày trùng, sai thứ tự hoặc thiếu tuần làm phép so sánh trả `insufficient_data`. Một chu kỳ đơn lẻ không đủ để khẳng định tính mùa vụ. Snapshot vẫn giữ toàn bộ observations gốc.

Replay chỉ đọc snapshot đã lưu, không gọi collector live:

```powershell
python main.py --niche "cozy coloring books for adults" --mode replay --replay snapshots\<run-id>.json
```

Các tùy chọn: `--config config.json`, `--output-dir outputs`. Snapshot ở `snapshots/`, báo cáo ở `outputs/`, log JSONL ở `logs/`. Log lưu status, duration, lỗi và diagnostics quota nhưng không chứa API key.

### Ý nghĩa trạng thái validation

- `VALID`: Ollama tạo phân tích schema-valid, kiểm tra claim đạt và có tối thiểu hai kỳ quan sát số trên một chuỗi market-signal metric (ví dụ Trends search-interest index).
- `DEGRADED`: có evidence nhưng model analysis lỗi/không hợp lệ và báo cáo dùng deterministic fallback; trạng thái này không được coi là phân tích AI thành công.
- `INSUFFICIENT_EVIDENCE`: schema hợp lệ nhưng chưa đủ chuỗi bằng chứng theo thời gian; catalog metadata đơn lẻ luôn vào trạng thái này.
- `BLOCKED`: live collection bị chặn hoặc rate-limited và không có evidence.
- `INVALID`: có claim không được hỗ trợ hoặc ID evidence không tồn tại.

`analysis_provenance` (`ollama`/`deterministic_fallback`), `analysis_execution_status`, `schema_valid`, `claims_valid`, `evidence_sufficient` và `status` được lưu tách biệt, hiển thị trong CLI/báo cáo/JSON. Python tạo fact records ổn định từ evidence và các metric tính được; mỗi fact giữ keyword, chỉ số, giá trị, kỳ quan sát, evidence IDs/metric IDs và mô tả. Ollama nhận các lựa chọn tham chiếu dạng alias có kiểu (ví dụ `previous_four_week_average`, `four_week_change`, `observed_peak`) cùng keyword và kỳ quan sát, không nhận canonical fact IDs. Python ánh xạ alias được chọn sang đúng fact ID của run hiện tại; validator vẫn đối chiếu ý nghĩa, số và ngày trong câu với fact đó. Python dựng phần timing cho ý tưởng sản phẩm từ fact interest phù hợp, đồng thời giữ nhu cầu cụ thể, sở thích khách hàng và product-market fit là chưa xác minh. Trường giá, cạnh tranh và khách hàng chưa có dữ liệu trực tiếp sẽ được để trống hoặc gắn nhãn chưa xác minh; confidence do Python giới hạn theo độ phủ nguồn, keyword và tín hiệu thị trường trực tiếp. Nếu claim không đạt, orchestration có thể gửi đúng một lượt sửa có giới hạn, chỉ với evidence/facts ban đầu và diagnostics; JSON lưu bản nháp đầu, trạng thái sửa và diagnostics trước/sau để audit. Nếu sửa không đạt hoặc lỗi, kết quả vẫn `INVALID`.

Validator hỗ trợ số thập phân dấu phẩy theo ngữ cảnh (ví dụ `57,5`), số thập phân dấu chấm và scientific notation; ngày ISO và dấu phân cách hàng nghìn được xử lý riêng. Nhận diện giá dùng cụm từ/đơn vị giá cụ thể để tránh nhầm “gia tăng” với “giá bán”. Validator kiểm tra provenance, số liệu, ngày/kỳ và direction fact; nó chặn mô tả trung bình hai cửa sổ như mức tăng liên tục theo tuần khi chuỗi có dao động. Search interest không được coi là doanh số hay nhu cầu mua. Report INVALID chỉ trình bày facts Python ở phần kết luận, giữ nội dung Ollama bị từ chối trong phụ lục chẩn đoán và giữ nguyên JSON audit. Keyword chưa đo được được chuyển thành ứng viên nghiên cứu, không phải tín hiệu thị trường. Khi không có evidence, hệ thống không gửi yêu cầu phân tích rỗng cho Ollama nhưng vẫn tạo report ghi rõ blocker.

## HTTP 429: xử lý và chẩn đoán

Collector sẽ retry tối đa hai lần, ghi HTTP status/reason/attempts/Retry-After vào log và metadata. Nếu nhận 429 lần nữa, báo cáo mang trạng thái `rate_limited`; nếu Retry-After vượt giới hạn chờ nhỏ, collector dừng sớm để tôn trọng yêu cầu server. HTTP 403 có thể là quota hoặc quyền và được ghi riêng.

Trên Windows, kiểm tra lại một request đơn giản và quota project/API trong Google Cloud Console. Đợi quota reset nếu server báo daily limit; giảm số lần chạy; chỉ thử key hợp lệ thuộc project đã bật API. Không lặp lệnh liên tục, không proxy/rotate key để né quota. Nếu nguồn vẫn bị giới hạn, báo cáo giữ trạng thái blocked/rate-limited và không thay bằng dữ liệu giả. Codex sandbox và Windows host có thể có kết quả DNS/network khác nhau.

## Batch bốn niche — Milestone 2.2

Registry [niches.json](niches.json) tách stable niche ID, tên, đối tượng dự kiến và catalog query. Batch dùng lại Open Library collector/`ToolRegistry`, CSV importer, hàm tính trend/fact và `ExecutionLogger`. Không thực hiện inference; `--no-ollama` ghi rõ chính sách mặc định. Claim validation là `NOT_RUN`, analysis là `DETERMINISTIC_SUMMARY`; catalog request thành công không làm nghiên cứu được gắn `VALID`.

Một lệnh PowerShell chạy đủ bốn niche với collector online, không gọi Ollama:

```powershell
.\.venv\Scripts\python.exe -m market_research.multi_niche --config .\niches.json --collect-live --no-ollama
```

API tương ứng: `run_batch("niches.json", collect_live=True)` từ `market_research.multi_niche`. Một collector instance dùng chung giữ pacing giữa các request; budget tối đa bốn tool call, mỗi niche độc lập khi nguồn lỗi. `execution_status=COMPLETED` nghĩa vòng batch đã xử lý xong, không có nghĩa mọi collection hoặc phân tích thị trường thành công.

Chỉ cozy có Trends dataset thật được cấu hình. Các niche còn lại thiếu Trends và không nhận observations/facts của cozy. Dataset phải khai báo đúng `niche_id`, keyword, URL nguồn có `q`/`geo=US`, region và `period_start`/`period_end` khớp CSV. CSV phải có đúng một chuỗi keyword/vùng tương ứng. Mapping sai bị từ chối trước khi gửi request hoặc tạo batch artifacts; đường dẫn file tương đối được tính từ thư mục config. Nếu URL có `date`, dùng khoảng ngày cố định khớp window. Dataset được gắn SHA-256 và giữ riêng observation period với import time.

Mỗi niche có `catalog_enabled`/`trends_enabled` (mặc định `true`), tùy chọn `catalog_snapshot`, và `trends` (không có dataset thì bỏ trường này). Registry giới hạn đúng bốn danh tính đã định nghĩa; cho phép keyword cozy dạng singular như CSV thực, nhưng chặn keyword thuộc niche khác. Khi bổ sung dataset, khai báo mapping tường minh và kiểm tra keyword/source trước khi chạy.

Chạy offline: bỏ `--collect-live`. Batch đọc `catalog_snapshot` nếu cấu hình riêng cho niche; nếu không, catalog là `not_configured`. Không tự tìm snapshot cũ hoặc tải dữ liệu thay thế. Ví dụ một mục registry có thể bổ sung `"catalog_snapshot": "snapshots/openlibrary-cozy.json"` nếu file này thực sự tồn tại và đúng query. Replay giữ retrieval timestamp cũ, đánh dấu `cache_replay` ở cả summary và từng record. Không dùng fixture test để giả làm live data.

```powershell
.\.venv\Scripts\python.exe -m market_research.multi_niche --config .\niches.json --no-ollama
```

Output mặc định ở `outputs/batches/batch-<timestamp>-<unique-id>/` (đổi bằng `--output-dir`):

- `summary.json`, `summary.md`, `config.json`, `execution.jsonl` của batch.
- Mỗi niche có `result.json`, `summary.md`, `trends-evidence.json`, `verified-facts.json`, và `catalog-snapshot.json` nếu nguồn trả kết quả.
- Raw catalog và evidence nằm trong audit artifacts; bảng batch chỉ so sánh coverage/count/status/freshness và link chi tiết.

Coverage tách `CATALOG_AND_TRENDS`, `CATALOG_ONLY`, `TRENDS_ONLY`, `NO_EVIDENCE`; `trend_sufficient` riêng với việc dataset có tồn tại. Lỗi toàn bộ nguồn và không có evidence cho niche là `BLOCKED`; có evidence vẫn là `PARTIAL_EVIDENCE` vì còn thiếu các chiều thị trường. Catalog-only giữ `insufficient evidence` cho trend. Replay metadata, CSV thời kỳ cũ và lần lấy online có provenance/freshness riêng. Trends chỉ được diễn giải trong cùng keyword/vùng/window; các CSV chuẩn hóa riêng không so index trực tiếp giữa niche. Open Library không xác nhận US retail availability, đối thủ trực tiếp, sales, giá hay market size. `ranking_status=NOT_YET_RANKABLE`; Milestone 2.3 cần dữ liệu so sánh phù hợp trước khi xếp hạng.

## Joint-export comparison — Milestone 2.3

`market_research.comparison` đọc **một** CSV so sánh chung bốn keyword và batch 2.2 tương thích, không gọi mạng hoặc Ollama. [comparison.json](comparison.json) vẫn là config cục bộ hiện có của chủ repo; file này bị loại khỏi Git vì chứa đường dẫn dữ liệu và batch ID cục bộ. [comparison.example.json](comparison.example.json) là mẫu portable cho clone mới, chỉ chứa đường dẫn tương đối placeholder, không đi kèm CSV hay batch. Đường dẫn tương đối tính từ thư mục config. CSV cozy riêng và các metric/facts cũ không được đưa vào phép tính này.

**Tạo input thật cho clone mới:** tải từ Google Trends UI một joint CSV duy nhất gồm đủ bốn keyword ở cùng vùng Hoa Kỳ và cùng time range. Không dùng bốn export độc lập. Đặt CSV được phép sử dụng trong `data/`, copy mẫu thành config riêng, rồi mở `comparison.local.json` để điền đúng `csv_path`, khoảng ngày/`as_of` theo export và thêm `"joint_export_confirmed": true` chỉ sau khi đã kiểm tra CSV thực sự có đủ bốn keyword trong cùng export. Mẫu cố ý không chứa cờ xác nhận nên không chạy được cho tới khi người dùng xác nhận. Tạo batch tương thích bằng registry bốn niche; lệnh sau không gọi Ollama hoặc collector mạng và sẽ ghi rõ nguồn nào còn thiếu:

```powershell
Copy-Item .\comparison.example.json .\comparison.local.json
New-Item -ItemType Directory -Force .\data | Out-Null
# Chép CSV Google Trends thật do bạn tải vào .\data\ rồi mở config để sửa csv_path, ngày và cờ xác nhận.
notepad .\comparison.local.json
python -m market_research.multi_niche --config .\niches.json --no-ollama
```

Sau khi chọn đúng thư mục batch vừa tạo, sửa `batch_directory` trong `comparison.local.json` thành đường dẫn `outputs/batches/<batch-id>` tương ứng rồi chạy:

```powershell
python -m market_research.comparison --config .\comparison.local.json
```

Muốn lấy metadata Open Library thật thì có thể chủ động thêm `--collect-live` vào lệnh batch; điều đó không bổ sung sales, giá hay search trends. Mẫu portable chứa ngày ví dụ chỉ để minh họa schema, phải thay theo CSV thật. CLI báo riêng khi thiếu CSV hoặc batch, kèm hành động cần làm. Config mẫu không tạo evidence.

Chạy offline trên Windows từ repository:

```powershell
.\.venv\Scripts\python.exe -m market_research.comparison --config .\comparison.json --as-of 2026-10-20
```

API: `run_comparison("comparison.json")`. Có thể dùng riêng `parse_comparison(config)` và `compare_keyword_signals(dataset)` với typed dataset. Parser hỗ trợ UTF-8/BOM, header `Week`/`Tuần` dạng NFC/NFD, metadata preamble và suffix US/Hoa Kỳ. Nó yêu cầu bốn keyword được map tường minh, ngày tuần liên tục/duy nhất và giá trị hữu hạn trong 0–100; từ chối cột/dòng thiếu, sai vùng hoặc window. `0` được giữ là giá trị báo cáo; ô trống/N/A là thiếu và bị từ chối. `<1` bị báo là censored, không tự chuyển thành zero để tính comparison.

`joint_export_confirmed=true`, category và Web Search là khai báo của người dùng; file/preamble/header được giữ để audit, không coi đó là xác thực độc lập cấu hình export. URL export gốc chưa được cung cấp, nên `source_url=null`. Nếu biết URL thực, có thể bổ sung URL đúng cả bốn keyword và geo=US; ứng dụng không tự dựng URL giả. Dataset ID gắn với SHA-256 của CSV và context khai báo; metric từ context hoặc date coverage khác bị chặn.

CSV hiện có 53 dòng tuần. Với `as_of=2026-10-20`, tuần bắt đầu `2026-10-18` được giữ với `complete_week=false`; mean/max, tỷ lệ tuần dương và hai cửa sổ chỉ dùng 52 tuần hoàn tất. JSON giữ riêng số tuần dương toàn bộ raw observations và số tuần dương hoàn tất. Numeric completeness không đồng nghĩa mọi kỳ đã hoàn tất. Index 0 không chứng minh zero searches, buyers hoặc demand.

Chính sách mặc định gắn `SPARSE_SIGNAL` khi dưới 20% tuần hoàn tất có index dương; chỉ diễn giải so sánh hai cửa sổ khi có đủ 8 tuần, chuỗi không sparse và mỗi cửa sổ có ít nhất 2 tuần dương. Đây là ngưỡng chẩn đoán công khai, không phải trọng số cơ hội thị trường. JSON vẫn giữ trung bình/delta quan sát được của các cửa sổ sparse cùng trạng thái không diễn giải; report không gọi đó là growth. Một chu kỳ năm không xác nhận seasonality, và cách viết query có thể không đại diện cho cả niche.

Output mới ở `outputs/comparisons/comparison-<timestamp>-<unique-id>/` gồm `comparison.json`, `comparison.md`, `dataset.json`, `source.csv` và `config.json`; không ghi đè batch/golden report. Report nối catalog/hypotheses bằng stable IDs, giữ URL/request/timestamp và checksum của batch artifacts. Product hypotheses cũ giữ nhãn chưa kiểm chứng. Raw observations nằm riêng trong audit dataset, không đổ vào executive report.

`observed_keyword_signal_order` chỉ sắp theo mean trên cùng joint-export scale; không dùng catalog counts, `numFound` hoặc edition count. Commercial gate kiểm tra phạm vi bằng chứng: các nguồn hiện có chưa cung cấp giá retail, sales, customer reviews hoặc cạnh tranh/availability Hoa Kỳ đã xác minh, nên trả `INSUFFICIENT_EVIDENCE`, `market_opportunity_rank=null` và tối đa ba `RESEARCH_PRIORITY`. Lượng search index/candidate lớn vẫn không mở gate. Chưa có scoring contract/source thương mại để hỗ trợ `RANKABLE`; cần bổ sung bằng chứng phù hợp trước khi triển khai nhánh ranking này. Không điền điểm trung tính hoặc tạo Top 3 thương mại. `analysis_status=DETERMINISTIC_COMPARISON`, inference `NOT_REQUESTED`, claim validation `NOT_RUN`.

## Persistent local jobs — Milestone 3.1

M3.1 bổ sung hạ tầng job SQLite và worker Python độc lập; M3.2 mở rộng bằng job `research` trong phần bên dưới. `smoke` vẫn kiểm tra hạ tầng offline, tạo `TEST_SMOKE` JSON với `market_validation=NOT_APPLICABLE`, không phải evidence hay research report. Job type ngoài allowlist bị từ chối; payload smoke không cho phép command, module, credential, đường dẫn hoặc text tùy ý. Không có thread, worker hay database được khởi tạo chỉ vì import module.

Database mặc định: `outputs/jobs/jobs.sqlite3`; artifact: `outputs/jobs/artifacts/<job-id>/smoke.json`. Cả hai nằm trong `outputs/` đã bị Git ignore, tách khỏi batch/comparison/golden cũ. Mỗi job có UUID 32 ký tự hex và thư mục mới, không dùng lại thư mục có sẵn. Có thể đổi `--db` và `--artifact-dir` trước subcommand; nếu không chỉ định artifact root thì dùng `<thư-mục-db>/artifacts`. Chỉ dùng SQLite trên ổ đĩa cục bộ. Artifact root được lưu cùng DB; mở cùng DB với root khác sẽ bị từ chối. Khi chọn vị trí tùy chỉnh, giữ runtime data ngoài Git.

Schema v1 gồm ba bảng: `jobs` chứa request JSON đã kiểm tra/không thể sửa, state, UTC timestamps tạo/bắt đầu/kết thúc/cancel-request, phase, số bước hoàn tất/tổng số bước, safe error code/message, relative artifact reference, attempt count, provenance và worker ID; `workers` lưu UUID chủ sở hữu, heartbeat, lease expiry và thời gian dừng; `settings` lưu schema version và artifact root. SQLite dùng WAL, transaction `BEGIN IMMEDIATE`, foreign keys và trigger bảo vệ request/state transition. Lock contention chờ tối đa 2 giây mỗi thao tác rồi báo `StoreBusy`; không tự chạy lại handler.

| Trạng thái hiện tại | Chuyển trạng thái được phép |
| --- | --- |
| `QUEUED` | Worker claim → `RUNNING`; yêu cầu hủy → `CANCELLED` |
| `RUNNING` | Handler thành công và có artifact an toàn → `COMPLETED`; lỗi → `FAILED`; xác nhận hủy → `CANCELLED`; dừng/mất lease → `INTERRUPTED` |
| `COMPLETED`, `FAILED`, `CANCELLED`, `INTERRUPTED` | Terminal; không trở lại queue |

`COMPLETED` chỉ nói handler thực thi xong và artifact tồn tại/không rỗng trong đúng thư mục job; không đồng nghĩa evidence hoặc market analysis `VALID`. Tiến độ là số bước handler thực sự hoàn tất, không ước lượng phần trăm. Exception text/traceback không được lưu hoặc in bởi runner; lỗi có code/message cố định cùng phase để chẩn đoán mà không lộ credential.

**PowerShell smoke test:** chạy từ repository, dùng môi trường Python hiện có. Các lệnh này tạo TEST/SMOKE artifacts, không gọi mạng hoặc model:

```powershell
$python = Join-Path $PWD '.venv\Scripts\python.exe'
$job = & $python -m market_research.jobs submit-smoke --steps 3 | ConvertFrom-Json
& $python -m market_research.jobs list
& $python -m market_research.jobs inspect $job.job_id
& $python -m market_research.jobs worker --once
& $python -m market_research.jobs inspect $job.job_id
```

Worker `--once` xử lý tối đa một job rồi thoát; queue rỗng không phải lỗi. Exit code: 0 khi thành công/queue rỗng, 1 nếu job vừa xử lý kết thúc khác `COMPLETED`, 2 nếu lệnh/input/database không hợp lệ hoặc đang bận. Xem cú pháp bằng `python -m market_research.jobs --help`. Để thử bằng DB riêng:

```powershell
& $python -m market_research.jobs --db .\.tmp\m31-smoke\jobs.sqlite3 submit-smoke
& $python -m market_research.jobs --db .\.tmp\m31-smoke\jobs.sqlite3 worker --once
& $python -m market_research.jobs --db .\.tmp\m31-smoke\jobs.sqlite3 list
```

Để worker tiếp tục chờ queue trong terminal riêng, dùng lệnh sau; Streamlit không cần mở:

```powershell
& $python -m market_research.jobs worker --poll-seconds 2
```

Worker xử lý tuần tự; polling chỉ nhận giá trị 0.1–60 giây và dùng event wait khi queue rỗng. UUID worker và lease được kiểm tra khi claim/progress/finish; mỗi job chỉ có một chủ sở hữu và mỗi worker chỉ có một job `RUNNING`. Heartbeat thread gia hạn lease 30 giây mỗi 10 giây, độc lập với việc handler báo tiến độ. Worker mới không đánh dấu job còn lease là stale. Ctrl+C/SIGTERM yêu cầu dừng hợp tác; handler phải gọi checkpoint giữa các phase, worker cũng kiểm tra trước/sau handler. Không cưỡng chế kill thread đang làm việc.

Hủy job chưa chạy hoặc yêu cầu hủy job đang chạy:

```powershell
$cancelJob = & $python -m market_research.jobs submit-smoke | ConvertFrom-Json
& $python -m market_research.jobs cancel $cancelJob.job_id
& $python -m market_research.jobs inspect $cancelJob.job_id
```

Hủy `QUEUED` kết thúc ngay và không claim được nữa. Hủy `RUNNING` chỉ lưu yêu cầu, giữ `RUNNING` tới checkpoint hoặc lúc handler trở về; artifact một phần vẫn có thể tồn tại và không được công bố như result. Yêu cầu hủy thắng completion nếu đã được lưu trước terminal transaction. Nếu handler ném lỗi thật, trạng thái là `FAILED`; terminal job không nhận hủy thêm.

Nếu worker bị kill/mất điện, job có thể còn `RUNNING` đến khi lease hết hạn và worker/lệnh recovery đối chiếu DB. Recovery chỉ chuyển các job có owner đã dừng hoặc hết lease sang `INTERRUPTED`, giữ attempt count và output một phần, không enqueue hay replay. Có thể chạy kiểm tra thủ công:

```powershell
& $python -m market_research.jobs recover
```

Lease hết hạn là tín hiệu mất quyền sở hữu, không chứng minh process đã chết: máy bị pause, đồng hồ đổi hoặc SQLite bị khóa lâu cũng có thể làm lease hết. Owner cũ không được gia hạn lease đã hết hoặc ghi trạng thái hoàn tất sau khi bị fence; handler cũ vẫn có thể còn thao tác cho tới checkpoint. Trước khi tự tạo job mới, người dùng phải xác nhận process cũ đã dừng và xem output một phần. M3.1 không hỗ trợ retry tự động hay requeue, kể cả đối với smoke.

M3.2 đã triển khai adapter cho research theo hợp đồng dưới đây. Smoke handler và các lệnh M3.1 giữ nguyên; job type/request được đăng ký tường minh, không import module hoặc chạy shell theo payload. CLI dùng JSON với Unicode escapes để giữ nguyên topic/filename khi PowerShell pipe dùng encoding cũ; các artifact text vẫn ghi UTF-8.

Kiểm thử job runner hoàn toàn offline, tạo DB/artifacts trong temp riêng:

```powershell
& $python -m pytest tests/test_jobs.py -q
```

## Research jobs — Milestone 3.2 (backend)

`research_job.research_handler` gọi đúng production entry point `orchestrator.run_research`, dùng lại `EvidenceStore`, `ExecutionLogger`, fact builders, `OllamaClient`, validator và JSON/Markdown/HTML report writer. Không có research engine thứ hai. Execution context tùy chọn bổ sung đường dẫn theo job, quyền gọi dịch vụ và callbacks. Legacy `main.py` không truyền context nên giữ nguyên đường dẫn snapshot/log/report và hành vi cũ. Chưa có job controls trong dashboard; comparison/batch và Open Library standalone không thay đổi. `live` của pipeline đơn này dùng Google Books hiện có, không tự chuyển sang Open Library.

**Request bất biến:** một topic, mode `csv_import`/`replay`/`live`, input path + SHA-256 đối với mode local, URL export thật đối với CSV, `allow_network`, `allow_inference`, model và timeout. Mặc định model `qwen3:4b-instruct`, region Hoa Kỳ, timeout nguồn 15 giây và inference 120 giây. Mode local không cho bật network; live bắt buộc `--allow-network` và không nhận local input. Inference chỉ được gọi khi có `--allow-inference`; biến `OLLAMA_MODEL` không thay model đã lưu trong request. Nếu được phép inference, worker dùng `OLLAMA_BASE_URL` hoặc `http://localhost:11434`, chỉ nhận endpoint HTTP loopback không chứa credential. Không đổi host binding hoặc tải model. Quyền inference bao gồm planning của live mode và bounded JSON/claim repair vốn có trong engine; runner không retry hoặc requeue job.

Input phải là CSV/JSON local có sẵn, tối đa 32 MiB, nằm trong input root cho phép. Mặc định root là repository; có thể đặt `--input-root` khi tạo DB riêng để dùng file thật ở vị trí khác. Root được persist trong DB nên worker process mới không cần khai báo lại. Reject traversal, UNC, symlink/junction, alternate data stream và đường dẫn mơ hồ. CSV phải có đúng một keyword series cho một niche Hoa Kỳ, kèm URL Google Trends thực có `geo=US` và một giá trị `q` khớp keyword trong header; chưa hỗ trợ joint export hoặc topic-ID URL trong job đơn này. Replay giữ nguyên evidence IDs, retrieval timestamps và nguồn cũ. SHA-256 được chốt khi submit, đối chiếu lại trước khi copy input vào job; file bị sửa/xóa trong queue làm job `FAILED` trước khi gọi dịch vụ. Không đưa key hoặc credential vào topic, source URL hoặc file input. API key chỉ lấy từ environment của worker, không lưu trong request/SQLite/log.

**Schema v2:** thêm loại `research`, provenance `RESEARCH_PIPELINE`, input root và `outcome_json` chứa bản tóm tắt từ `ResearchResult` chuẩn. Khi mở DB M3.1, migration transaction giữ nguyên smoke jobs, states và references. Phải dừng worker M3.1 và chờ lease hết trước khi nâng cấp; migration từ chối nếu còn lease cũ hoạt động. Không có downgrade tự động; không mở lại DB v2 bằng code M3.1.

**State và validation:** `COMPLETED` chỉ xác nhận workflow và bundle hoàn tất. `research_outcome.validation_status/valid/schema_valid/claims_valid/evidence_sufficient` lấy từ validator hiện có, không suy ra từ state job. Workflow offline không được phép inference dùng deterministic fallback; với evidence, validator thường trả `DEGRADED`, không gắn `VALID`. Model trả lời thành công nhưng claim sai có thể tạo job `COMPLETED` với outcome `INVALID` và report cách ly nội dung chưa đạt. Source operation lỗi hoặc analysis timeout/failure tạo job `FAILED`, có safe error code và outcome chẩn đoán nếu pipeline đã tạo được; reports chẩn đoán giữ ở `_partial`, không có result reference. Canonical facts, uncertainty và ID tham chiếu giữ nguyên. Analysis `VALID` không xác minh sales hoặc cơ hội thương mại; catalog và Trends vẫn có các giới hạn đã nêu ở trên.

**Output và publication:** mỗi job ghi input snapshot, evidence snapshot, log và ba reports vào `outputs/jobs/artifacts/<job-id>/_partial/`. Các reference bên trong là đường dẫn tương đối theo bundle. Sau khi kiểm tra manifest/checksums, worker giữ transaction và quyền sở hữu, đối chiếu cancellation rồi rename nguyên thư mục thành `result/` và ghi reference `<job-id>/result/manifest.json` cùng outcome. Không ghi đè thư mục có sẵn. Manifest liên kết job/request/hash input, research run, validation, model provenance và hash từng artifact. SQLite và filesystem không có transaction chung: nếu commit DB thất bại sau rename, folder có thể còn nhưng không có reference `COMPLETED`; đó là output chưa công bố để xem xét, không tự retry. M3.3 chỉ được hiển thị kết quả qua job `COMPLETED` có reference hợp lệ, không quét folder để tự công nhận kết quả.

Progress ghi các boundary thật: input validation, planning, collection/import, evidence preparation, model hoặc deterministic analysis, claim validation, reports, artifact verification và ready-to-publish. `PUBLICATION` chỉ ghi khi terminal transaction công bố bundle thành công. Không có phần trăm ước lượng. Heartbeat chạy trên thread riêng trong lúc HTTP/model blocking. Checkpoint trước/sau từng HTTP request ngăn retry/repair kế tiếp khi đã hủy. Request HTTP đang chạy không bị ngắt ngay; cancellation giữ `RUNNING` cho tới request trả về hoặc timeout. Sau cancellation/interruption, reference vẫn rỗng và không rename `_partial`. Policy lease/recovery M3.1 giữ nguyên: mất quyền sở hữu → `INTERRUPTED`, cần xem xét thủ công, không tự replay.

### Xác minh offline (không gọi mạng hoặc model)

Test sử dụng input **SYNTHETIC**, fake services/MockTransport và DB tạm; kiểm tra worker process mới có chặn network:

```powershell
$py = Join-Path $PWD '.venv\Scripts\python.exe'
& $py -m pytest tests/test_jobs.py tests/test_research_jobs.py -q
& $py -m pytest -q
```

Để chạy pipeline offline với CSV thật đã có sẵn, tự chọn đúng file export **một keyword** ở United States và URL nguồn của chính export đó. Không cần Ollama cho lệnh này. Lệnh tạo DB xác minh mới để không đổi input root của DB đang dùng:

```powershell
$csv = Get-Item -LiteralPath (Read-Host 'Đường dẫn CSV Google Trends thật đã có trên máy')
if ($csv.PSIsContainer -or $csv.Extension -ne '.csv') { throw 'Hãy chọn một file CSV thật.' }
$topic = Read-Host 'Một niche/chủ đề được phép nghiên cứu'
$sourceUrl = Read-Host 'URL Google Trends thực của export này (geo=US)'
$db = Join-Path $PWD ('outputs\jobs\m32-' + [guid]::NewGuid().ToString('N') + '.sqlite3')
$submitted = & $py -m market_research.jobs --db $db --input-root $csv.DirectoryName submit-research --topic $topic --mode csv_import --input $csv.FullName --source-url $sourceUrl
if ($LASTEXITCODE -ne 0) { throw 'Submit thất bại; đọc diagnostic JSON trước khi thử lại.' }
$job = $submitted | ConvertFrom-Json
& $py -m market_research.jobs --db $db worker --once
& $py -m market_research.jobs --db $db inspect $job.job_id
```

Kiểm tra state cùng `research_outcome`, không chỉ exit code. Với no-inference và có evidence, kết quả thông thường là `COMPLETED` + `DEGRADED` + `model_execution_status=not_requested`, không phải nghiên cứu AI đã kiểm định. Đọc manifest từ `result_path` dưới artifact root của DB. Có thể dùng mode `replay` với một evidence JSON thật do người dùng chọn, bỏ `--source-url`; job không đọc lại analysis cũ như một kết quả model mới.

### Tùy chọn: người dùng chủ động cho phép inference thật

Chỉ chạy nếu muốn gọi Ollama local. Dùng lại các biến `$csv`, `$topic`, `$sourceUrl`, `$db` từ quy trình trên:

```powershell
$aiJob = & $py -m market_research.jobs --db $db submit-research --topic $topic --mode csv_import --input $csv.FullName --source-url $sourceUrl --allow-inference --model 'qwen3:4b-instruct' | ConvertFrom-Json
& $py -m market_research.jobs --db $db worker --once
& $py -m market_research.jobs --db $db inspect $aiJob.job_id
```

Chỉ sau khi người dùng kiểm tra outcome, references và report mới xác nhận live inference end-to-end. Tests offline với model fake không chứng minh Ollama thực đã nhận evidence.

### Tùy chọn: người dùng chủ động cho phép collection mạng

Lệnh sau chỉ cho phép Google Books collection; không bật Ollama. Google Books có thể tiếp tục trả 429; job sẽ báo lỗi/diagnostic đúng và không thay bằng dữ liệu giả:

```powershell
$catalogJob = & $py -m market_research.jobs --db $db submit-research --topic $topic --mode live --allow-network | ConvertFrom-Json
& $py -m market_research.jobs --db $db worker --once
& $py -m market_research.jobs --db $db inspect $catalogJob.job_id
```

Chỉ thêm `--allow-inference` khi đồng thời muốn cho phép model. Worker liên tục, list, inspect, cancel và recover dùng như M3.1, thêm cùng `--db $db`. Không cần giữ Streamlit mở.

**Hợp đồng M3.3:** UI gọi `JobStore.submit('research', validated_payload)` với từng quyền được người dùng chọn rõ, poll `get/list_jobs`, đọc state và outcome riêng, kiểm tra manifest trước khi trình bày report và dùng `request_cancel` cho hủy. UI không thực thi handler, không tự tạo worker lúc import/rerun và không dùng `COMPLETED` thay cho validator `VALID`. Worker CLI tiếp tục là process độc lập. M3.2 chưa thêm dashboard controls, chưa xác minh live collection/inference trong lượt triển khai này.

## Dashboard Streamlit — Milestone 2.5

App duy nhất là [dashboard.py](dashboard.py), đọc artifacts có sẵn trong `outputs/`. Không tự chạy batch, collector, comparison generation hoặc Ollama; không ghi database hay sửa report. Giao diện dùng sidebar navy với navigation buttons, workspace sáng, KPI cards/status chips riêng và evidence flow phản ánh đúng execution. HTML từ artifact luôn escape; thiết kế nằm trong `dashboard.css`, màu sidebar dùng theme chính thức, không dùng JS hoặc selector class ngẫu nhiên. Cài dependency UI trên Windows (cần package index hoặc wheelhouse được phép), rồi khởi động từ repository:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dashboard,dev]"
.\.venv\Scripts\python.exe -m streamlit run .\dashboard.py
```

Mở `http://localhost:8501`. `.streamlit/config.toml` giới hạn server ở `127.0.0.1`, tắt usage telemetry và cấu hình giao diện sáng. `requirements.txt` cũng cài dashboard extra; việc chạy engine và unit tests không bắt buộc phải cài Streamlit. Không mở rộng Ollama binding.

Dashboard có năm trang:

- **Tổng quan:** bốn KPI thật, ma trận nguồn/thực thi bốn niche, pipeline, gate và preview hai giả thuyết AI lịch sử. Không lặp biểu đồ đường lớn. Context batch/comparison nằm trong expander, có ID đầy đủ để sao chép.
- **Phân tích từ khóa:** chart tương tác cùng joint-export scale, chọn keyword, tùy chọn hiện tuần chưa hoàn tất, mean/max/positive counts và sparsity. Metric vẫn chỉ dùng tuần hoàn tất; hai cửa sổ chỉ hiển thị diễn giải khi artifact hỗ trợ.
- **Khám phá niche:** hồ sơ từng niche, heatmap tuần hoàn tất, bảng quan sát thay thế, catalog và rà soát liên quan dựa trên tiêu đề. Mỗi ô heatmap giữ nguyên index từ joint export, xếp theo thứ tự tuần trong nhóm 13 quan sát; không phải lịch theo thứ trong tuần. Cozy có chi tiết AI lịch sử, ba niche khác không nhận phân tích AI thay thế.
- **Đánh giá cơ hội:** ma trận bằng chứng tìm kiếm/catalog/giá/doanh số/khách hàng/bán lẻ, gate thực tế, lý do abstain và ưu tiên nghiên cứu. Thứ tự keyword mô tả nằm riêng, không tạo leaderboard thương mại.
- **Bằng chứng & truy vết:** tóm tắt ngắn, ID/raw enums và kỹ thuật trong expander, tải xuống theo nhóm batch/comparison, selector lịch sử riêng. Không mở raw JSON mặc định.

`market_research.dashboard_data` là lớp đọc/presentation tái sử dụng, không phụ thuộc Streamlit. Nó chỉ khám phá các run directory trực tiếp trong `outputs/batches/` và `outputs/comparisons/`; không nhận đường dẫn tự do từ UI. Mặc định đọc outputs trong repository. Có thể đặt `MARKETLENS_ARTIFACTS_DIR` thành thư mục artifact khác có cấu trúc `outputs/` tương thích; CSS vẫn được đọc từ source app. Mặc định chọn comparison mới nhất theo metadata filesystem đã kiểm tra hợp lệ và batch được tham chiếu bằng ID. Khi chọn batch khác, selector chỉ đưa ra các comparison đúng batch đó. Absolute path lưu trong artifact cũ không được dùng để truy cập ngoài repository.

Loader đối chiếu checksum batch, dataset/context/CSV, niche IDs, nguồn snapshot và metric tính từ CSV gốc trước khi ghép. Comparison sai/missing/stale bị loại và có chẩn đoán trong audit; batch vẫn có thể hiển thị. Nếu catalog snapshot thiếu hoặc hỏng, counts vẫn được ghi rõ là metadata từ result, nhưng không dựng book records thay thế. Overview/Explorer đọc riêng golden để hiển thị AI Insights; các run lịch sử khác chỉ đọc khi chọn trong Audit. Bản ghi nguồn không tải ảnh hoặc gọi API ngoài.

Cache Streamlit tối đa 10 giây, keyed bằng paths/mtime/size của artifact files; màn hình ghi thời điểm loader đã đọc và có nút **Đọc lại artifacts**. Nút này xóa cache bộ nhớ, không tạo lại research. Giữ riêng source retrieval time, CSV observation window, as-of cutoff và loader/cache time.

`market_research.dashboard_charts` tạo DataFrame với ngày `datetime`, index `float64` và trạng thái kỳ `bool`; Altair dùng field ASCII nội bộ, bốn màu ổn định theo niche, tooltip và zoom theo trục thời gian. Chart chính giữ thang đo 0–100. Focus mode nằm trong expander, có nhãn trục Y phóng đại rõ ràng, không so độ cao với chart chung. Tuần chưa hoàn tất xuất hiện bằng đoạn nét đứt/điểm diamond; thao tác này không thay metric. Heatmap chỉ dùng tuần hoàn tất và cùng thang màu 0–100, không tự chuẩn hóa riêng từng niche. Hai ma trận trình bày trạng thái có chữ, không biến dữ liệu thiếu thành số 0 hoặc điểm cơ hội.

**AI Research Insights:** `dashboard_insights.py` đọc golden `a7c48c0031e4` bằng schema hiện có, yêu cầu provenance `ollama`, execution `success`, saved validation `VALID` và các cờ kiểm định đạt. Tham chiếu idea → fact → evidence phải tồn tại, đúng run, có URL HTTPS an toàn. UI hiển thị nguyên văn hai concept, giả thuyết khách hàng, khác biệt, bất định và timing gắn facts bởi Python; bảng fact/evidence truy ngược nguồn được mở trong expander. Đây là phân tích cozy một niche lịch sử từ CSV riêng, không phải Qwen phân tích batch hiện tại, không trộn numeric với joint comparison. Artifact thiếu/hỏng/không đạt sẽ có thông báo, không sinh nội dung thay thế. Guard trình bày này không chạy lại claim validator.

**Rà soát catalog:** nhãn hiển thị bảo thủ gồm khớp chủ đề rõ trong tiêu đề, ứng viên sơ bộ cần rà soát và chưa đủ bằng chứng liên quan. `Cozy Critter Caravan` / `Catholic Girl: Cute Cozy` chỉ khớp cozy nên cần rà soát; không xác nhận category hoặc đối thủ. Giữ nguyên nhãn và counts collector gốc trong bảng/audit. Chưa có mô tả/subjects để xác minh sâu; cải thiện collector thuộc mốc sau.

**Typography & layout:** theme gốc 16px; CSS tokens cho heading 32–38px, section 22–24px, card 18px, mô tả 14–16px. Padding đầu trang 5.5rem dành chỗ cho toolbar (trước đây 1.6rem, mobile 1.2rem); line-height và padding heading cho dấu tiếng Việt. Card không có fixed height hay overflow ẩn; grid reflow tại 1100/768/420px. Giữ kích thước sidebar do Streamlit quản lý và native navigation. Selector `data-testid` ổn định được ưu tiên; thay đổi DOM Streamlit vẫn có thể cần QA lại.

**Sidebar & màn hình hẹp:** `initial_sidebar_state=288` dùng chế độ `auto` gốc Streamlit với chiều rộng mở ban đầu 288px trên desktop; ở viewport nhỏ sidebar tự thu gọn, navigation vẫn nằm trong drawer. Nút thu gọn trong header và nút mở sidebar gốc cho phép đóng/mở. CSS không ép chiều rộng drawer. Streamlit lưu lựa chọn người dùng trong browser local storage; nếu drawer vẫn mở ở 390px sau khi đã mở thủ công, thu bằng nút mũi tên ở đầu sidebar. Khi thu lại, phần nội dung chính chiếm viewport; dùng nút menu/header để mở navigation lại. `st.code(..., wrap_lines=True)` và CSS cho code block cho phép đọc ID nhiều dòng; nút copy của Streamlit vẫn giữ toàn bộ ID.

**Catalog & Audit:** bảng chỉ hiển thị tiêu đề, trạng thái rà soát, tác giả, năm, số ấn bản và link nguồn. Chọn một bản ghi để mở chi tiết lý do đầy đủ, metadata, URL và nhãn collector gốc. Nút tải theo cặp trong hai cột; trên màn hình hẹp Streamlit xếp cột theo responsive layout. Filename và byte tải vẫn lấy từ artifact nguyên gốc.

**Chart trống trước đây:** raw Vega-Lite spec gắn một interval zoom vào nhiều layer, tạo lỗi frontend `Duplicate signal name: "zoom_Ng_y"` dù Arrow có đủ dữ liệu và AppTest không báo exception Python. Renderer mới scope selection vào duy nhất named view `completed_timeline`. Regression kiểm tra cả emitted Arrow/spec từ AppTest và render SVG bằng Vega-Lite; không chỉ kiểm tra số dòng loader.

Kiểm thử loader/presentation vẫn chạy độc lập với Streamlit. Để chạy thêm AppTest và compiler/render checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dashboard,dashboard-test,dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

`dashboard-test` là extra kiểm thử tùy chọn (`vl-convert-python`), không phải dependency production. Để chạy đủ test, cài cả `dashboard`, `dashboard-test`, `dev`. Dashboard/AppTest regressions dùng artifacts và CSV mang nhãn `SYNTHETIC TEST FIXTURE`, tạo trong temp directory riêng; không đọc hoặc ghi outputs/CSV người dùng. Trên Windows, AppTest cần thư mục `%TEMP%`/`%TMP%` có quyền ghi và loopback socketpair của asyncio. Sandbox có thể chặn thao tác `socket.accept` cần thiết cho Proactor self-pipe; khi đó chạy pytest trên Windows host có quyền loopback cục bộ. Không bật network exposure hoặc collector. AppTest và render SVG offline không thay thế xác nhận hover/layout/screenshot trong browser thực.

### QA trình duyệt Windows — bắt buộc trước final sign-off

Browser tool của phiên Codex bị chặn truy cập localhost bởi URL policy. AppTest và SVG compiler kiểm tra dữ liệu/render spec, không chứng minh hover, clipping hoặc responsive trong trình duyệt. Chưa ký duyệt giao diện bằng browser cho Milestone 2.5.

1. Dừng Streamlit cũ bằng `Ctrl+C` trong terminal đã chạy server. Khởi động lại để nhận theme:

   ```powershell
   $repo = Join-Path $HOME "ai-market-research-agent"
   Set-Location $repo
   .\.venv\Scripts\python.exe -m streamlit run .\dashboard.py
   ```

2. Mở `http://localhost:8501`, dùng `Ctrl+F5`. Trong Edge/Chrome nhấn `F12`, `Ctrl+Shift+M`, chọn Responsive và lần lượt đặt **1440×1000, 1024×900, 768×1024, 390×844**, zoom browser 100%; kiểm tra thêm 125%. Chụp đủ năm trang ở mỗi chiều rộng. Heading tiếng Việt phải đủ dấu và không nằm dưới toolbar; sidebar đóng/mở được, navigation và ID copy vẫn truy cập được; bảng có thể cuộn ngang có chủ đích, phần trang không tràn ngang.
   Dùng cửa sổ InPrivate mới để kiểm tra trạng thái `auto` ban đầu mà không có sidebar preference cũ: ở 390px drawer bắt đầu đóng. Mở bằng nút menu trên cùng, chuyển trang, đóng bằng mũi tên trong drawer; phần nội dung phải đọc và thao tác được. Mở lại để xác nhận navigation luôn truy cập được. Cửa sổ thường có thể nhớ trạng thái mở từ lần dùng trước.
3. Tổng quan: bốn KPI, ma trận, pipeline và AI lịch sử hiển thị; không có biểu đồ đường lớn. Mở expander AI: đủ hai concept, timing, bất định, fact/source IDs. Xác nhận batch chưa chạy mô hình/claim validation; các số AI standalone không được gọi là metric joint.
4. Từ khóa: bốn series/legend, hover đúng ngày/index/keyword, lọc từng keyword và bỏ hết để thấy thông báo. Bật kỳ chưa hoàn tất: diamond/nét đứt cho 18/10/2026 trong dataset hiện tại; metrics vẫn dùng 52 tuần hoàn tất. Mở focus để thấy trục phóng đại được ghi rõ.
5. Niche: chọn đủ bốn niche; heatmap và bảng quan sát chỉ thuộc keyword đã chọn. Hover từng ô, kiểm tra tuần/index. Rà soát tiêu đề dài và hai ví dụ cozy yếu; chỉ cozy có AI lịch sử liên kết.
6. Cơ hội: gate `INSUFFICIENT_EVIDENCE`, thiếu bằng chứng thương mại, không có Top 3 thương mại. Audit: raw status trong expander, chọn comparison cũ/mới vẫn đúng batch, sao chép ID và tải JSON/Markdown/CSV để đối chiếu checksum; thử mở golden từ selector riêng.
7. Kiểm tra mở/đóng mọi expander, tải xuống, legend và bảng dài trên màn hình 390px; ghi lỗi kèm ảnh/viewport/zoom trước khi xác nhận hoàn tất. Không dùng việc không có exception Python làm chứng cứ UI đã đẹp hoặc hết clipping.

## Kiểm thử

Fixture synthetic chỉ dùng trong unit test offline, không dùng trong live mode. Chạy:

```powershell
python -m pytest -q
```

Test bao gồm schema, Google Books/Google Trends CSV normalization, allowlist/budget, HTTP 429 + Retry-After, lỗi quota, redaction API key, Ollama response parse/diagnostic, evidence sufficiency, blocked status, UTF-8 report, và replay. Chạy bằng Python 3.12 thông thường; không cần `-X utf8`. Trong Codex sandbox nếu pytest temp directory hệ thống không cho ghi, dùng `python -m pytest -q --basetemp=.pytest-tmp`.

## Chi phí và giới hạn còn lại

Chưa có API trả phí nào được cấu hình; quota Google Books có thể giới hạn request. Ollama không có API charge; có chi phí tài nguyên máy. Google Books không đủ để kết luận market demand. Chưa có collector search trend/bestseller/sales volume hoặc customer discussion được xác thực; insight về khách hàng và idea cần giữ nhãn giả thuyết khi bằng chứng thiếu.
