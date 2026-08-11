# Bàn trợ lý AI trong Telegram

Chức năng này dùng Telegram làm nơi giữ dữ liệu quant và các góc nhìn thu thập
từ những tài khoản web AI. Bot không lưu mật khẩu, cookie hay tự điều khiển trang
web đã đăng nhập.

## Cách dùng

1. Chạy workflow `Start Telegram Bot Session` như hiện tại.
2. Gửi `/panel FPT`. Bot tạo dữ liệu nền, prompt chung và link mở DeepSeek, GLM,
   ChatGPT, Gemini.
3. Dán prompt vào trợ lý muốn hỏi. Sau đó đưa câu trả lời về Telegram bằng
   `/ai_add deepseek nội_dung` (đổi tên thành `glm`, `gpt` hoặc `gemini`).
4. Gửi `/ai_prompt glm` để nhận prompt kế tiếp có cả những ý kiến đã thu thập.
5. Gửi `/ai_summary` để Gemini tổng hợp trung lập; `/ai_status` để xem lại.

Mỗi phản hồi được giữ tối đa 30 từ. Phần tổng hợp không được sửa score, vùng vào,
target, stop hoặc gọi hit-rate quá khứ là xác suất thắng tương lai. Các phiên được
lưu ở `data/assistant_conversations.json` và được workflow khôi phục qua cache.

## Ngân sách GitHub mặc định

- `/deep`: một mã mỗi ngày.
- Gemini: tối đa hai lượt mỗi ngày để dành chỗ cho một phân tích và một tổng hợp.
- `/scan`: không gọi AI; shortlist tối đa hai mã.
- Lịch scan tự động: thứ Hai (`SCAN_WEEKDAYS=0`).
- GLM/DeepSeek API mặc định tắt; bật lại `MODEL_COUNCIL_ENABLED=true` chỉ khi tài
  khoản API có quota. Link web trong `/panel` vẫn dùng được khi council API tắt.

