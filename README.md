# VN Equity Bot

Bot Telegram để tra cứu, lọc định giá và theo dõi cổ phiếu Việt Nam. Bot chạy
trên Python 3.10+ và dùng Google Gen AI SDK cho phần nghiên cứu có kiểm chứng.

## Tính năng

- `/start`, `/help`, `/ping`
- `/quote FPT`: giá gần nhất và phần trăm thay đổi
- Gõ trực tiếp `FPT` hoặc `VNM` để tra giá nhanh không cần lệnh
- `/report FPT`: báo cáo nhanh gồm giá, mở cửa, cao/thấp phiên và khối lượng
- `/chart FPT`: biểu đồ chữ 30 phiên để xem nhanh trong Telegram
- `/ta FPT`: MA5, MA20, RSI14, hỗ trợ/kháng cự gần
- `/deep FPT`: cấu trúc doanh nghiệp, định giá, mẫu hình, điểm 100, target/stop
  theo kịch bản và backtest trạng thái tương tự
- `/news FPT` hoặc `/news Thông tư 14/2026`: phân tích tiêu đề theo hai chiều,
  luôn gắn nguồn và nêu phần chưa thể xác nhận
- `/new FPT`: bí danh ngắn của `/news FPT`; khi đầu vào là một mã, bot lấy thêm
  tên doanh nghiệp và loại mọi tiêu đề không chứa mã/tên tương ứng
- `/macro`: trạng thái/tin vĩ mô trung lập từ
  [`vimo-VN`](https://github.com/maikhanhthieu-droid/vimo-VN)
- `/usage`: xem phần trăm ngân sách Gemini, VNStock, `/deep`, `/scan`, `/news`
  và sức khỏe từng nguồn
- `/signals_on`, `/signals_off`, `/signals_status`: bật/tắt tín hiệu lọc sâu VN100
- `/scan`: quét VN100 ngay, mặc định chỉ gửi mã đạt ngưỡng đủ sâu
- `/market`: VN-Index
- `/add FPT`, `/remove FPT`: quản lý danh sách theo dõi theo từng cuộc trò chuyện
- `/watchlist`, `/watch`: xem và lấy giá danh sách theo dõi
- Lưu token bằng biến môi trường, không nhận token trên command line và không ghi token
  vào log/file.

Bot phân vai nguồn dữ liệu: Yahoo cho giá nhanh, VNStock `VCI ↔ KBS` cho lịch sử/TA,
TradingView cho số liệu doanh nghiệp/định giá theo lô, `vimo-VN` cho trạng thái vĩ
mô, Google News RSS cho metadata tiêu đề. Khi VNStock hết ngân sách hoặc một nguồn
lỗi, bot tự chuyển nguồn rồi rơi về Yahoo. Dữ liệu có thể trễ, thiếu hoặc không khả
dụng; bot không đưa ra khuyến nghị đầu tư.

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
   $env:GEMINI_API_KEY = "<KEY_GEMINI_THẬT>"
   $env:VNSTOCK_API_KEY = "<KEY_VNSTOCK_THẬT>"
   ```

   Lưu ý: `os.environ.get("GEMINI_API_KEY")` chỉ là lệnh đọc biến môi trường,
   không phải API key. Key thật là chuỗi được tạo trong Google AI Studio.

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

Bot có thể tự quét rổ VN100 mặc định 2 lần/tuần và chỉ gửi tối đa vài tín hiệu
mỗi tháng. TradingView được gọi theo lô, còn dữ liệu lịch sử được lấy song song
có giới hạn. Khung điểm 100 cố định, Gemini chỉ giải thích và không được sửa:

- Chất lượng doanh nghiệp: `30` điểm — tăng trưởng doanh thu/lợi nhuận, ROE,
  đòn bẩy và thanh toán hiện hành
- Định giá: `25` điểm — P/E, P/B và tương quan định giá/tăng trưởng
- Kỹ thuật/mẫu hình: `25` điểm — MA20/50/200, RSI, mẫu hình và khối lượng
- Rủi ro: `10` điểm — biến động năm hóa và giá trị giao dịch bình quân
- Vĩ mô: `10` điểm — ánh xạ bảo thủ từ trạng thái trung lập của `vimo-VN`;
  không tải được nguồn thì giữ `5/10`

`/deep` dùng tối đa hai năm dữ liệu để tìm các trạng thái xu hướng/RSI tương tự
ở hai khung: 1 tháng (20 phiên) và 3 tháng (60 phiên). Mỗi khung hiển thị hai
định nghĩa riêng: tỷ lệ chạm T1 trước stop, và tỷ lệ giá cuối kỳ cao hơn giá vào
kèm lợi nhuận trung vị. Hit-rate T1 chỉ hiện khi có ít nhất 5 mẫu đã ngã ngũ;
tỷ lệ cuối kỳ chỉ hiện khi có ít nhất 5 mẫu hoàn tất. Nếu cùng một nến ngày chạm
cả target và stop, bot tính là stop. Các con số không phải xác suất tương lai.

Lệnh Telegram:

```text
/signals_on
/signals_status
/scan
/deep FPT
/news FPT
/macro
```

Biến cấu hình:

- `GEMINI_API_KEY`: key Gemini thật; nếu thiếu, bot vẫn gửi điểm định lượng
- `GEMINI_MODEL`: mặc định `gemini-3.5-flash-lite`
- `GEMINI_FALLBACK_MODEL`: mặc định `gemini-2.5-flash-lite`
- `GEMINI_THINKING_LEVEL`: mặc định `minimal`
- `GEMINI_MAX_OUTPUT_TOKENS`: mặc định `1000`
- `GEMINI_GOOGLE_SEARCH`: mặc định `false`; chỉ bật khi project có đủ quota grounding
- `GEMINI_TIMEOUT`: mặc định `30` giây
- `GEMINI_MIN_INTERVAL`: tối thiểu `60` giây giữa hai yêu cầu Gemini; bot trả
  thông báo cooldown ngay thay vì đứng chờ
- `GEMINI_CACHE_TTL`: cache kết quả mỗi mã trong `1800` giây
- `GEMINI_QUOTA_COOLDOWN`: khi gặp 429, ngừng gọi Gemini trong `900` giây
- `RESEARCH_COMMAND_COOLDOWN`: `/deep`, `/scan` và `/news` cách nhau ít nhất `60` giây;
  `/ping`, `/quote` và các lệnh thường không bị ảnh hưởng
- `GEMINI_DAILY_BUDGET`: ngân sách an toàn nội bộ, mặc định `12` API call/ngày
- `VNSTOCK_API_KEY`: key VNStock; bot tự ánh xạ thêm sang `VNDATA_API_KEY`
- `VNSTOCK_SOURCES`: thứ tự nguồn cho phép, mặc định `VCI,KBS`; thứ tự thực tế được
  xoay theo mã để chia tải
- `VNSTOCK_DAILY_BUDGET`: tối đa `60` lần gọi/ngày; hết mức này tự dùng Yahoo
- `VNSTOCK_REQUESTS_PER_MINUTE`: trần khai báo cho từng nguồn, mặc định `12`
- `VNSTOCK_USAGE_RATIO`: chỉ dùng `70%` trần trên, tức `8.4` lần/phút/nguồn
- `VNSTOCK_ERROR_COOLDOWN`: nghỉ nguồn `300` giây khi gặp 429/quota
- `VNSTOCK_CACHE_TTL`: cache lịch sử mỗi mã `480` giây
- `DEEP_DAILY_LIMIT`: tối đa `10` lệnh `/deep` được nhận mỗi ngày
- `SCAN_DAILY_LIMIT`: tối đa `2` lệnh `/scan` thủ công mỗi ngày
- `NEWS_DAILY_LIMIT`: tối đa `8` lệnh `/news` hoặc `/macro` mỗi ngày
- `VIMO_LATEST_URL`: mặc định đọc `vimo-VN/output/latest.json`
- `VIMO_CACHE_TTL`: cache vĩ mô `900` giây
- `NEWS_CACHE_TTL`: cache tiêu đề theo chủ đề `900` giây
- `NEWS_MAX_ITEMS`: tối đa `5` tiêu đề có nguồn mỗi lần
- `SCAN_WEEKDAYS`: ngày quét, định dạng số thứ trong tuần của Python; mặc định `0,3` là thứ Hai và thứ Năm
- `SCAN_TIME`: giờ quét theo giờ máy, mặc định `20:30`
- `SCAN_WORKERS`: mặc định `2`, tối đa nội bộ `12`; giảm tải đồng thời lên nguồn giá
- `MONTHLY_SIGNAL_LIMIT`: mặc định `2`
- `MAX_SIGNALS_PER_SCAN`: mặc định `2`
- `MIN_SIGNAL_SCORE`: mặc định `70`
- `SIGNAL_COOLDOWN_DAYS`: mặc định `30`
- `VN100_SYMBOLS`: danh sách mã override, phân tách bằng dấu phẩy nếu rổ VN100 thay đổi

Tín hiệu này là bộ lọc nghiên cứu tự động, không phải khuyến nghị mua/bán.

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
