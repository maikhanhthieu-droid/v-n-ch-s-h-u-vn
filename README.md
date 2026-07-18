# VN Equity Bot

Bot Telegram tối giản để tra cứu nhanh dữ liệu tham khảo của cổ phiếu Việt Nam.
Repo gốc hiện chỉ có README; bản này là một MVP có thể chạy độc lập bằng Python
3.10+ và không cần thư viện bên ngoài.

## Tính năng

- `/start`, `/help`, `/ping`
- `/quote FPT`: giá gần nhất và phần trăm thay đổi
- Gõ trực tiếp `FPT` hoặc `VNM` để tra giá nhanh không cần lệnh
- `/report FPT`: báo cáo nhanh gồm giá, mở cửa, cao/thấp phiên và khối lượng
- `/chart FPT`: biểu đồ chữ 30 phiên để xem nhanh trong Telegram
- `/ta FPT`: MA5, MA20, RSI14, hỗ trợ/kháng cự gần
- `/market`: VN-Index
- `/add FPT`, `/remove FPT`: quản lý danh sách theo dõi theo từng cuộc trò chuyện
- `/watchlist`, `/watch`: xem và lấy giá danh sách theo dõi
- Lưu token bằng biến môi trường, không nhận token trên command line và không ghi token
  vào log/file.

Nguồn giá là Yahoo Finance chart endpoint công khai. Dữ liệu có thể trễ, thiếu hoặc
không khả dụng; bot không đưa ra khuyến nghị đầu tư.

## Chạy cục bộ

1. Vào `@BotFather`, thu hồi token đã từng được đăng trong chat và tạo token mới.
2. Tạo biến môi trường (PowerShell):

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "<TOKEN_MỚI>"
   ```

3. Chạy bot:

   ```powershell
   python bot.py
   ```

   Bot dùng long polling, nên cửa sổ/process phải luôn chạy. Gửi `/start` cho bot
   trên Telegram rồi thử `FPT`, `/quote FPT`, `/report FPT`, `/chart FPT`
   hoặc `/ta FPT`.

   Hoặc nhập token bằng ô ẩn, không lưu token vào file:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
   ```

Các biến tùy chọn: `POLL_TIMEOUT`, `YAHOO_TIMEOUT`, `DATA_DIR`, `LOG_LEVEL`.

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
