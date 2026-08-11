# VN Equity Bot

Bot Telegram để tra cứu, lọc định giá và theo dõi cổ phiếu Việt Nam. Bot chạy
trên Python 3.10+; phần định lượng hoạt động độc lập với API LLM. Gemini, GLM và
DeepSeek là các lớp nghiên cứu tùy chọn, không được quyền sửa điểm hoặc phát lệnh.

## Tính năng

- `/start`, `/help`, `/ping`
- `/quote FPT`: giá gần nhất và phần trăm thay đổi
- Gõ trực tiếp `FPT` hoặc `VNM` để tra giá nhanh không cần lệnh
- `/report FPT`: báo cáo nhanh gồm giá, mở cửa, cao/thấp phiên và khối lượng
- `/chart FPT`: biểu đồ chữ 30 phiên để xem nhanh trong Telegram
- `/ta FPT`: MA5, MA20, RSI14, hỗ trợ/kháng cự gần
- `/deep FPT`: nghiên cứu sâu tối đa 3 lượt/ngày; GLM kiểm tra chất lượng doanh
  nghiệp, DeepSeek phản biện rủi ro và Gemini tổng hợp/bổ sung dữ liệu hiện tại.
  Nếu một trợ lý hết quota hoặc lỗi, bot giữ các góc nhìn đã nhận và tự mở panel
  Telegram cho đúng mã để bạn chỉ bổ sung trợ lý còn thiếu bằng `/ai_add`.
  Ba lớp AI chỉ diễn giải evidence, không sửa điểm 100, target/stop hay backtest
- `/news FPT` hoặc `/news Thông tư 14/2026`: phân tích tiêu đề theo hai chiều,
  luôn gắn nguồn và nêu phần chưa thể xác nhận
- `/new FPT`: bí danh ngắn của `/news FPT`; khi đầu vào là một mã, bot lấy thêm
  tên doanh nghiệp và loại mọi tiêu đề không chứa mã/tên tương ứng
- `/macro`: trạng thái/tin vĩ mô trung lập từ
  [`vimo-VN`](https://github.com/maikhanhthieu-droid/vimo-VN)
- `/usage`: xem phần trăm ngân sách Gemini, VNStock, `/deep`, `/scan`, `/news`
  và sức khỏe từng nguồn
- `/performance`: cập nhật và xem win/loss/timeout, expectancy R và lợi nhuận
  ròng của chính các tín hiệu live đã được bot ghi sổ
- `/signals_on`, `/signals_off`, `/signals_status`: bật/tắt tín hiệu lọc sâu VN100
- `/scan`: shortlist VN100 thuần định lượng, không gọi Gemini/GLM/DeepSeek và chỉ
  trả tối đa 3 mã đạt toàn bộ gate trong ngày
- `/market`: VN-Index
- `/add FPT`, `/remove FPT`: quản lý danh sách theo dõi theo từng cuộc trò chuyện
- `/watchlist`, `/watch`: xem và lấy giá danh sách theo dõi
- Lưu token bằng biến môi trường, không nhận token trên command line và không ghi token
  vào log/file.

Bot phân vai nguồn dữ liệu: Yahoo cho giá nhanh, VNStock `VCI ↔ KBS` cho lịch sử/TA,
TradingView cho số liệu doanh nghiệp/định giá theo lô, `vimo-VN` cho trạng thái vĩ
mô, Google News RSS cho metadata tiêu đề. Lịch sử OHLCV giữ ngày, nguồn và cờ
điều chỉnh; khi VNStock hết ngân sách hoặc một nguồn lỗi, bot tự chuyển nguồn rồi
rơi về Yahoo. Dữ liệu có thể trễ, thiếu hoặc không khả dụng; bot không đưa ra
khuyến nghị đầu tư.

## Chạy cục bộ

1. Vào `@BotFather`, thu hồi token đã từng được đăng trong chat và tạo token mới.
2. Tạo môi trường riêng và cài thư viện:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Tạo biến môi trường (PowerShell):

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "<TOKEN_MỚI>"
   $env:TELEGRAM_ALLOWED_CHAT_IDS = "<CHAT_ID_CỦA_BẠN>"
   $env:GEMINI_API_KEY = "<KEY_GEMINI_THẬT>"
   $env:VNSTOCK_API_KEY = "<KEY_VNSTOCK_THẬT>"
   # Thêm một hoặc cả hai key khi muốn bật council tùy chọn:
   $env:GLM_API_KEY = "<KEY_GLM_THẬT>"
   $env:DEEPSEEK_API_KEY = "<KEY_DEEPSEEK_THẬT>"
   $env:MODEL_COUNCIL_ENABLED = "true"
   ```

   `TELEGRAM_ALLOWED_CHAT_IDS` nhận một hoặc nhiều chat ID nguyên, phân tách bằng
   dấu phẩy; nếu để trống, bot dùng `TELEGRAM_CHAT_ID` làm fallback tương thích.
   Người ngoài allowlist không được dùng lệnh để tiêu hao quota. Lưu ý:
   `os.environ.get("GEMINI_API_KEY")` chỉ là lệnh đọc biến môi trường, không phải
   API key. Key thật là chuỗi được tạo trong Google AI Studio.

4. Chạy bot:

   ```powershell
   .\.venv\Scripts\python.exe bot.py
   ```

   Bot dùng long polling, nên cửa sổ/process phải luôn chạy. Gửi `/start` cho bot
   trên Telegram rồi thử `FPT`, `/quote FPT`, `/report FPT`, `/chart FPT`
   hoặc `/ta FPT`.

   Hoặc nhập token bằng ô ẩn, không lưu token vào file:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
   ```

Python 3.12 được khuyến nghị và là phiên bản dùng trong Docker/GitHub Actions. Trên
Python 3.14, bot vẫn chạy bằng Yahoo dự phòng nhưng thư viện VNStock 4.0.4 chưa được
cài vì chuỗi phụ thuộc NumPy hiện chưa tương thích đầy đủ.

Các biến tùy chọn: `POLL_TIMEOUT`, `YAHOO_TIMEOUT`, `DATA_DIR`, `LOG_LEVEL`.

## Tín hiệu lọc sâu VN100

Bot có thể tự quét rổ VN100 mặc định 2 lần/tuần. Mỗi ngày scanner chỉ tạo một
shortlist thuần định lượng, tối đa 3 mã vượt đủ gate; `/scan` tuyệt đối không gọi
Gemini, GLM hay DeepSeek. TradingView được gọi theo lô, còn dữ liệu lịch sử được
lấy song song có giới hạn. Khung điểm 100 cố định:

- Chất lượng doanh nghiệp: `30` điểm — tăng trưởng doanh thu/lợi nhuận, ROE,
  đòn bẩy và thanh toán hiện hành
- Định giá: `25` điểm — P/E, P/B và tương quan định giá/tăng trưởng
- Kỹ thuật/mẫu hình: `25` điểm — MA20/50/200, RSI, mẫu hình và khối lượng
- Rủi ro: `10` điểm — biến động năm hóa và giá trị giao dịch bình quân
- Vĩ mô: `10` điểm — ánh xạ bảo thủ từ trạng thái trung lập của `vimo-VN`;
  không tải được nguồn thì giữ `5/10`

`/deep` và scanner dùng tối đa năm năm dữ liệu. Mẫu lịch sử phải giống trạng thái
xu hướng/RSI và ít nhất một chế độ biến động/khối lượng; hai mẫu liên tiếp cách
nhau tối thiểu bằng toàn bộ horizon nên cửa sổ kết quả không chồng lấn. Tín hiệu
được tạo sau close, chờ tối đa ba next-open nằm trong vùng entry, dùng ATR/hỗ trợ
chỉ từ dữ liệu có tại ngày lịch sử và trừ phí/trượt giá. Không có open hợp lệ thì
ghi không khớp lệnh; tuyệt đối không lấy close cùng ngày thay cho open bị thiếu.
Gap qua stop khớp ở open xấu hơn; cùng nến chạm cả target và stop được tính stop
trước.

Mỗi khung 20/60 phiên hiển thị hit-rate mô tả, cận dưới Wilson 95%, số mẫu hiệu
dụng, lợi nhuận ròng trung vị và expectancy theo R. Gate production mặc định yêu
cầu khung 20 phiên vượt đồng thời score, số lệnh đã khớp (kể cả timeout), cận dưới
Wilson và expectancy. Sau gate, thứ hạng dùng giá trị yếu hơn giữa cả khung 20 và
60 phiên để tránh chọn mã chỉ đẹp ở một horizon. Đây vẫn không phải xác suất thắng
tương lai hay kiểm định walk-forward toàn thị trường.

Lệnh Telegram:

```text
/signals_on
/signals_status
/scan
/deep FPT
/performance
/news FPT
/macro
```

Biến cấu hình:

- `GEMINI_API_KEY`: key Gemini thật; nếu thiếu, bot vẫn gửi điểm định lượng
- `GEMINI_MODEL`: mặc định `gemini-3.5-flash-lite`
- `GEMINI_FALLBACK_MODEL`: mặc định `gemini-3.1-flash-lite`
- `GEMINI_THINKING_LEVEL`: mặc định `minimal`
- `GEMINI_MAX_OUTPUT_TOKENS`: mặc định `1000`
- `GEMINI_GOOGLE_SEARCH`: mặc định `false`; chỉ bật khi project có đủ quota grounding
- `GEMINI_TIMEOUT`: mặc định `30` giây
- `GEMINI_MIN_INTERVAL`: tối thiểu `60` giây giữa hai yêu cầu Gemini; bot trả
  thông báo cooldown ngay thay vì đứng chờ
- `GEMINI_CACHE_TTL`: cache theo fingerprint facts/model/prompt/Search trong `1800` giây
- `GEMINI_QUOTA_COOLDOWN`: khi gặp 429, ngừng gọi Gemini trong `900` giây
- `RESEARCH_COMMAND_COOLDOWN`: `/deep`, `/scan` và `/news` cách nhau ít nhất `60` giây;
  `/ping`, `/quote` và các lệnh thường không bị ảnh hưởng
- `GEMINI_DAILY_BUDGET`: ngân sách an toàn nội bộ, mặc định `6` API call/ngày;
  đủ cho tối đa 3 lệnh `/deep` khi mỗi lệnh có nhiều nhất một fallback Gemini
- `VNSTOCK_API_KEY`: key VNStock; bot tự ánh xạ thêm sang `VNDATA_API_KEY`
- `VNSTOCK_SOURCES`: thứ tự nguồn cho phép, mặc định `VCI,KBS`; thứ tự thực tế được
  xoay theo mã để chia tải
- `VNSTOCK_DAILY_BUDGET`: tối đa `60` lần gọi/ngày; hết mức này tự dùng Yahoo
- `VNSTOCK_REQUESTS_PER_MINUTE`: trần khai báo cho từng nguồn, mặc định `12`
- `VNSTOCK_USAGE_RATIO`: chỉ dùng `70%` trần trên, tức `8.4` lần/phút/nguồn
- `VNSTOCK_ERROR_COOLDOWN`: nghỉ nguồn `300` giây khi gặp 429/quota
- `VNSTOCK_CACHE_TTL`: cache lịch sử mỗi mã `480` giây
- `DEEP_DAILY_LIMIT`: tối đa `3` lệnh `/deep` được nhận mỗi ngày
- `SCAN_DAILY_LIMIT`: tối đa `1` lượt shortlist `/scan` mỗi ngày; scanner không
  tiêu ngân sách của Gemini, GLM hoặc DeepSeek
- `NEWS_DAILY_LIMIT`: tối đa `8` lệnh `/news` hoặc `/macro` mỗi ngày
- `VIMO_LATEST_URL`: mặc định đọc `vimo-VN/output/latest.json`
- `VIMO_CACHE_TTL`: cache vĩ mô `900` giây
- `VIMO_MAX_AGE_HOURS`: quá `72` giờ thì vimo-VN bị coi là stale và lớp vĩ mô tự về trung tính
- `NEWS_CACHE_TTL`: cache tiêu đề theo chủ đề `900` giây
- `NEWS_MAX_ITEMS`: tối đa `5` tiêu đề có nguồn mỗi lần
- `SCAN_WEEKDAYS`: ngày quét, định dạng số thứ trong tuần của Python; mặc định `0,3` là thứ Hai và thứ Năm
- `SCAN_TIME`: giờ quét theo giờ máy, mặc định `20:30`
- `SCAN_WORKERS`: mặc định `2`, tối đa nội bộ `12`; giảm tải đồng thời lên nguồn giá
- `DAILY_SHORTLIST_LIMIT`: trần tổng tối đa `3` mã shortlist được phát mỗi ngày,
  dùng chung cho scan thủ công và scan theo lịch
- `MAX_SIGNALS_PER_SCAN`: mặc định `3`; kết hợp với một lượt scan/ngày để giữ
  shortlist không quá 3 mã/ngày
- `MIN_SIGNAL_SCORE`: mặc định `70`
- `VN100_SYMBOLS`: danh sách mã override, phân tách bằng dấu phẩy nếu rổ VN100 thay đổi
- `SIGNAL_REQUIRE_BACKTEST`: mặc định `true`; bật gate thống kê cho scanner tự động
- `SIGNAL_REQUIRE_DATED_HISTORY`: mặc định `true`; không phát signal tự động nếu
  OHLCV thiếu ngày phiên
- `MAX_HISTORY_STALENESS_DAYS`: tối đa `10` ngày lịch từ phiên OHLCV gần nhất
  (đủ bao phủ cuối tuần/kỳ nghỉ dài; có thể siết theo hạ tầng dữ liệu)
- `MIN_BACKTEST_RESOLVED`: tối thiểu `8` mẫu T1/stop đã ngã ngũ ở khung 20 phiên
- `MIN_BACKTEST_WIN_LOWER`: cận dưới Wilson 95% tối thiểu, mặc định `45` (%)
- `MIN_BACKTEST_EXPECTANCY_R`: expectancy ròng tối thiểu, mặc định `0.05R`
- `MIN_BACKTEST_FILL_RATE`: tối thiểu `40%` mẫu có next-open khớp vùng entry
- `BACKTEST_ROUND_TRIP_COST_PCT`: phí, thuế và slippage giả định khứ hồi,
  mặc định `0.45%`

Tín hiệu này là bộ lọc nghiên cứu tự động, không phải khuyến nghị mua/bán.

Gemini chỉ là lớp diễn giải. Khi tắt Search, số không có trong đầu vào deterministic
bị loại. Khi bật Search, dữ kiện hiện tại mới chỉ được phép xuất hiện nếu phản hồi
có nguồn grounding; các số thuộc score, target/stop và backtest vẫn phải khớp đầu
vào. Bot luôn tiếp tục với kết quả chấm điểm gốc. Cache Gemini được fingerprint
theo toàn bộ facts/model/prompt/Search thay vì chỉ theo mã. Fallback được phép bỏ
qua local interval đúng một lần nhưng vẫn chịu daily budget và circuit breaker.
Google Search chỉ bổ sung sự kiện/tin có nguồn; giá, OHLCV và số dùng để gate luôn
lấy từ provider có timestamp, không lấy từ snippet web.

## `/deep`: ba vai trò Gemini + GLM + DeepSeek

Mặc định các API key để trống nên clone chạy quant-only và không gọi provider.
`MODEL_COUNCIL_ENABLED=false` là kill switch; mỗi key bật độc lập provider tương
ứng. Khi có cả hai key, backend gửi cùng một evidence snapshot bất biến cho GLM
(chất lượng doanh nghiệp) và DeepSeek (phản biện rủi ro) song song qua API
OpenAI-compatible. Mỗi model chỉ được trả JSON hẹp gồm verdict, confidence nội bộ,
evidence ID, rủi ro và dữ liệu còn thiếu. Timeout, JSON sai hoặc thiếu key đều trở
thành `abstain`; không làm scanner dừng.

Chỉ `/deep` dùng ba API: GLM đánh giá dữ kiện cơ bản, DeepSeek tìm phản chứng/rủi
ro, sau đó Gemini tổng hợp hai phản biện cùng dữ liệu định lượng và có thể bổ sung
sự kiện hiện tại khi Google Search grounding được bật. `/scan` không đi qua đường
này, vì vậy lỗi hoặc hết số dư AI không ảnh hưởng shortlist định lượng.

Council hiện chạy **shadow-only**: kết quả được lưu cùng signal ledger và đưa cho
Gemini để tổng hợp, nhưng không thay đổi score, target/stop, gate backtest hoặc
thứ hạng. Chỉ nên cho council tác động lựa chọn sau khi `/performance` chứng minh
lift ngoài mẫu trên đủ dữ liệu live.

Biến cấu hình:

- `OPENROUTER_API_KEY`: tuyến miễn phí ưu tiên. Một key chạy hai vai trò độc lập
  qua `openrouter/free`; mỗi `/deep` dùng hai request OpenRouter và một Gemini
- `MODEL_COUNCIL_FREE_FIRST=true`: ưu tiên OpenRouter, rồi SiliconFlow nếu đã
  khai báo đủ hai model 0đ; đặt `false` để quay về API Z.AI/DeepSeek trực tiếp
- `SILICONFLOW_API_KEY`, `SILICONFLOW_GLM_MODEL`, `SILICONFLOW_DEEPSEEK_MODEL`:
  tuyến miễn phí tùy chọn; phải chọn model đang hiển thị giá 0đ trong tài khoản
- `GLM_API_KEY`, `DEEPSEEK_API_KEY`: để trống cho đến khi bạn điền key thật
- `GLM_BASE_URL=https://api.z.ai/api/paas/v4`, `GLM_MODEL=glm-5.2`
- `DEEPSEEK_BASE_URL=https://api.deepseek.com`, `DEEPSEEK_MODEL=deepseek-v4-flash`
- `MODEL_COUNCIL_REQUEST_TIMEOUT=20`, `MODEL_COUNCIL_OVERALL_TIMEOUT=22`
- `MODEL_COUNCIL_MAX_OUTPUT_TOKENS=800`, `MODEL_COUNCIL_CACHE_TTL=900`
- `MODEL_COUNCIL_DAILY_BUDGET=3`: tối đa ba lượt council/ngày; mỗi lượt gọi
  đồng thời một GLM và một DeepSeek
- `MODEL_COUNCIL_ERROR_COOLDOWN=900`: sau lỗi provider, tạm nghỉ provider đó
  `900` giây để tránh lặp lại lỗi quota/billing

Endpoint GLM trên là Z.AI quốc tế. Nếu API key được tạo trên BigModel Trung Quốc,
đổi `GLM_BASE_URL` thành `https://open.bigmodel.cn/api/paas/v4`.

Không cần cấp MCP cho Gemini trong đường production này. Backend điều phối trực
tiếp giúp giữ API key ngoài prompt, giới hạn đúng hai endpoint và tránh cho model
quyền gọi HTTP/tool tùy ý. MCP chỉ nên thêm sau nếu cần dùng chung các tool
read-only cho nhiều ứng dụng; nó không tự làm tăng win rate.

`data/signal_ledger.json` lưu snapshot bất biến, feature hash, score version,
review model và outcome next-open sau phí. File này được ignore khỏi Git và được
GitHub Actions cache cùng state bot. Không có API key/token nào được phép ghi vào
ledger. Khi Yahoo là nguồn dự phòng, bot giữ riêng OHLC raw để ledger so với
entry/target/stop raw; chuỗi OHLC đã điều chỉnh chỉ dùng cho TA/backtest.

Kiến trúc chia sẻ dữ liệu giữa các repository được mô tả tại
[`docs/ECOSYSTEM_DATA_CONTRACT.md`](docs/ECOSYSTEM_DATA_CONTRACT.md).

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
```

## Triển khai

Đặt `TELEGRAM_BOT_TOKEN` trong secret manager của VPS/Render/Railway/Docker rồi
chạy `python bot.py`. Không commit `.env` hoặc token vào GitHub. Nếu dùng GitHub
Actions, hãy chạy một worker dài hạn bên ngoài Actions; workflow miễn phí không
phù hợp để giữ long polling liên tục.

### Khởi động một phiên từ GitHub Mobile

Workflow `Start Telegram Bot Session` cho phép mở bot theo yêu cầu trong 15, 30
hoặc 60 phút. Trong thời gian phiên đang chạy, Telegram gửi lệnh đến runner bằng
long polling và runner trả kết quả về cuộc trò chuyện.

1. Mở repository trong GitHub Mobile hoặc trình duyệt.
2. Chọn **Actions → Start Telegram Bot Session → Run workflow**.
3. Chọn thời gian rồi bấm **Run workflow**.
4. Chờ job hiện màu vàng/xanh, sau đó gửi `/ping`, `/usage` hoặc `/deep FPT`.
5. Hết thời gian bot tự dừng. Có thể bấm **Cancel workflow** để dừng sớm.

Khi muốn bật council sau này, vào **Settings → Secrets and variables → Actions**,
thêm một hoặc cả hai repository secret `GLM_API_KEY` và `DEEPSEEK_API_KEY`. Workflow đã có
base URL/model/budget; không đưa key vào `.env.example`, source code hoặc log.
Giữ `TELEGRAM_CHAT_ID` hiện có để làm allowlist một người, hoặc thêm secret
`TELEGRAM_ALLOWED_CHAT_IDS` chứa nhiều chat ID phân tách bằng dấu phẩy. Workflow
ưu tiên allowlist mới và tự fallback về `TELEGRAM_CHAT_ID`; job sẽ dừng sớm nếu
không có allowlist hợp lệ.
Để Gemini bổ sung sự kiện hiện tại có grounding, tạo thêm repository variable
`GEMINI_GOOGLE_SEARCH=true`; nếu không, workflow giữ chế độ định lượng tiết kiệm quota.

Chỉ một phiên được phép chạy cùng lúc. Lệnh `/start` trong Telegram không thể tự
khởi động một runner đã tắt; phải mở phiên GitHub trước. Cách này dành cho phiên
thử nghiệm theo yêu cầu, không thay thế dịch vụ bot 24/7.

Mỗi lệnh chỉ gọi Gemini tối đa hai lần (model chính và một model dự phòng). Khi
gặp lỗi quota 429, bot mở circuit breaker và vẫn trả kết quả định lượng thay vì
tiếp tục retry. Các lệnh `/start`, `/ping`, `/quote` không phụ thuộc Gemini.

## Giới hạn cần hiểu đúng

TradingView và RSS có thể thiếu/chậm; tiêu đề không thay thế nội dung văn bản gốc.
Đối với ngân hàng/bảo hiểm, D/E và current ratio không được chấm như doanh nghiệp
thông thường; bot giữ điểm trung tính và nhắc so sánh theo ngành. Target/stop chỉ
là kịch bản theo ATR/hỗ trợ gần để chuẩn hóa rủi ro, không phải mức giá bảo đảm.

Score v2.1 sửa trường hợp P/E/P/B âm bị cộng điểm, trừ điểm CFO/FCF âm ở doanh
nghiệp phi tài chính và giảm độ tin cậy khi thiếu trường lõi. Score vẫn là heuristic
có thể audit, chưa phải xác suất đã calibrate. Cận Wilson và expectancy làm gate
bảo thủ hơn nhưng không loại được survivorship bias, dữ liệu tài chính không
point-in-time hoặc thay đổi chế độ thị trường; cần tiếp tục tích lũy ledger live.

Phần `/deep` hiển thị riêng kỳ dữ liệu tài chính và thời điểm lấy dữ liệu. Ngoài
ROE, D/E và current ratio, bot lấy thêm vốn chủ sở hữu, tổng tài sản, tỷ lệ
VCSH/TTS, nợ vay, tiền và đầu tư ngắn hạn, nợ ròng, CFO/FCF TTM, ROA, biên gộp,
biên hoạt động, biên ròng và BVPS từ TradingView Vietnam Scanner. Trường thiếu
được để trống, không nội suy.

Gemini phải đặt P/B cạnh hiệu quả vốn và dòng tiền: P/B thấp không tự động là
“hấp dẫn”, current ratio cao không đồng nghĩa lượng tiền mặt lớn và D/E thấp
không đủ để kết luận cấu trúc tài chính “lành mạnh”. Các nhãn chủ quan này bị
kiểm tra sau khi model trả lời; dữ liệu định lượng gốc vẫn được gửi nếu lớp AI
không đạt kiểm chứng.
