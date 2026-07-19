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
- `/deep FPT`: phân tích P/E, P/B, chiết khấu 52 tuần; Gemini có thể kiểm tra tin mới bằng Google Search
- `/signals_on`, `/signals_off`, `/signals_status`: bật/tắt tín hiệu lọc sâu VN100
- `/scan`: quét VN100 ngay, mặc định chỉ gửi mã đạt ngưỡng đủ sâu
- `/market`: VN-Index
- `/add FPT`, `/remove FPT`: quản lý danh sách theo dõi theo từng cuộc trò chuyện
- `/watchlist`, `/watch`: xem và lấy giá danh sách theo dõi
- Lưu token bằng biến môi trường, không nhận token trên command line và không ghi token
  vào log/file.

Nguồn giá là Yahoo Finance chart endpoint công khai. Dữ liệu có thể trễ, thiếu hoặc
không khả dụng; bot không đưa ra khuyến nghị đầu tư.

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

Các biến tùy chọn: `POLL_TIMEOUT`, `YAHOO_TIMEOUT`, `DATA_DIR`, `LOG_LEVEL`.

## Tín hiệu lọc sâu VN100

Bot có thể tự quét rổ VN100 mặc định 2 lần/tuần và chỉ gửi tối đa vài tín hiệu
định giá sâu mỗi tháng. TradingView được gọi theo lô, còn dữ liệu lịch sử được
lấy song song có giới hạn để lần quét hoàn thành nhanh hơn. Bộ lọc ưu tiên cổ
phiếu có:

- Chiết khấu mạnh so với đỉnh 52 tuần
- P/E và P/B ở vùng thấp/hợp lý
- RSI đã hạ nhiệt
- Điểm tổng hợp đạt `MIN_SIGNAL_SCORE`

Lệnh Telegram:

```text
/signals_on
/signals_status
/scan
/deep FPT
```

Biến cấu hình:

- `GEMINI_API_KEY`: key Gemini thật; nếu thiếu, bot vẫn gửi điểm định lượng
- `GEMINI_MODEL`: mặc định `gemini-3-flash-preview`
- `GEMINI_FALLBACK_MODEL`: mặc định `gemini-2.5-flash`
- `GEMINI_THINKING_LEVEL`: mặc định `high`
- `GEMINI_MAX_OUTPUT_TOKENS`: mặc định `3000`; không cần `65536` cho Telegram
- `GEMINI_GOOGLE_SEARCH`: mặc định `true`, gắn tối đa 3 nguồn kiểm chứng; nếu
  project bị giới hạn grounding/quota, bot tự chuyển sang phân tích không web
  trong 1 giờ để không làm hỏng tín hiệu
- `GEMINI_TIMEOUT`: mặc định `45` giây
- `SCAN_WEEKDAYS`: ngày quét, định dạng số thứ trong tuần của Python; mặc định `0,3` là thứ Hai và thứ Năm
- `SCAN_TIME`: giờ quét theo giờ máy, mặc định `20:30`
- `SCAN_WORKERS`: mặc định `6`, tối đa nội bộ `12`
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

## Phạm vi hiện tại

Đây là nền tảng MVP. Có thể bổ sung dữ liệu tài chính, định giá, tin tức và báo cáo
định kỳ sau khi thống nhất nguồn dữ liệu và các lệnh cần thiết.
