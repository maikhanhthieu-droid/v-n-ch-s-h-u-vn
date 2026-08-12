# Bàn trợ lý AI trong Telegram

Chức năng này dùng Telegram làm nơi giữ dữ liệu quant và các góc nhìn thu thập
từ những tài khoản web AI. Bot không lưu mật khẩu, cookie hay tự điều khiển trang
web đã đăng nhập.

## Cách dùng

1. Chạy workflow `Start Telegram Bot Session` như hiện tại.
2. Với mã cổ phiếu, gửi `/panel FPT`. Với câu hỏi tự do, gửi `/ask câu hỏi`.
   `/bridge` và `/multi` là bí danh của `/ask`.
3. Bot thu thập thêm tiêu đề hiện tại từ Google News RSS miễn phí và, nếu có
   `GEMINI_API_KEY`, tạo sẵn góc nhìn Gemini. `GEMINI_BRIDGE_SEARCH=true` chỉ
   bật khi bạn chấp nhận quota/billing của Google Search grounding.
4. Mở link Poe, Duck.ai, ChatGPT, Coze hoặc các AI khác trong panel. Xác thực
   và gửi prompt thủ công khi web yêu cầu, sau đó đưa câu trả lời về Telegram:
   `/ai_add poe nội_dung`, `/ai_add duckai nội_dung`, `/ai_add gpt nội_dung`,
   `/ai_add coze nội_dung`.
5. Gửi `/ai_prompt poe` hoặc `/ai_prompt gpt` để nhận prompt tiếp theo có cả
   những ý kiến đã thu thập. Gửi `/ai_summary` để Gemini tổng hợp trung lập;
   `/ai_status` để xem lại.

Các link web là cầu nối bán tự động: bot không lưu mật khẩu, cookie, mã xác thực,
không vượt CAPTCHA và không scrape phiên đăng nhập. Cách này giữ được quyền
kiểm soát của bạn và không phụ thuộc vào một API web không công khai.

Mỗi phản hồi được giữ trong khoảng tối đa 80 từ và phải nêu trạng thái, vùng mua,
vùng bán/chốt lời, stop cùng một cảnh báo hoặc xúc tác. Các mốc giá chỉ được chọn
từ vùng vào và T1/T2/T3 do quant cung cấp; nếu dữ liệu chưa đủ thì phải ghi
`CHƯA MUA`. Phần tổng hợp không được sửa score, vùng vào, target, stop hoặc gọi
hit-rate quá khứ là xác suất thắng tương lai. Các phiên được
lưu ở `data/assistant_conversations.json` và được workflow khôi phục qua cache.

## Ngân sách GitHub mặc định

- `/deep`: một mã mỗi ngày.
- Gemini: tối đa hai lượt mỗi ngày để dành chỗ cho `/ask`/phân tích và một tổng hợp.
- `/scan`: không gọi AI; shortlist tối đa hai mã.
- Lịch scan tự động: thứ Hai (`SCAN_WEEKDAYS=0`).
- GLM/DeepSeek API mặc định tắt; bật lại `MODEL_COUNCIL_ENABLED=true` chỉ khi tài
  khoản API có quota. Link web trong `/panel` vẫn dùng được khi council API tắt.
