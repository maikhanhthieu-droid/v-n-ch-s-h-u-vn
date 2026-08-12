# Setup bot Telegram đa AI qua GitHub

## 1. Secrets bắt buộc

Trong GitHub vào `Settings -> Secrets and variables -> Actions`, tạo:

- `TELEGRAM_BOT_TOKEN`: token lấy từ `@BotFather`.
- `TELEGRAM_ALLOWED_CHAT_IDS`: chat ID được phép dùng bot; có thể nhập nhiều ID,
  phân tách bằng dấu phẩy.

Không đưa token vào code, commit hoặc tin nhắn Telegram. Nếu token từng bị lộ,
hãy thu hồi và tạo token mới trước khi chạy.

## 2. Gemini tùy chọn

Thêm secret:

- `GEMINI_API_KEY`: key từ Google AI Studio.

Luồng mặc định giữ `GEMINI_BRIDGE_SEARCH=false`: bot lấy tiêu đề hiện tại từ
Google News RSS miễn phí rồi đưa chúng cho Gemini phân tích. Đây là chế độ phù
hợp với mục tiêu không gia hạn.

Chỉ tạo repository variable `GEMINI_BRIDGE_SEARCH=true` nếu bạn chấp nhận quota
và điều kiện billing của Google Search grounding. Không cần bật biến này để dùng
Gemini trong `/ask`.

## 3. Khởi động phiên

1. Vào tab `Actions` của repository.
2. Chọn `Start Telegram Bot Session`.
3. Bấm `Run workflow` và chọn 15, 30 hoặc 60 phút.
4. Trong thời gian phiên còn chạy, gửi lệnh cho bot trên Telegram.

GitHub runner là phiên tạm thời; khi hết thời lượng, bot dừng. Lần sau chỉ cần
chạy workflow lại, không cần gia hạn dịch vụ lưu trữ.

## 4. Cách hỏi nhiều AI

### Câu hỏi tự do

```text
/ask phân tích cấu trúc cũ của kế hoạch này và chỉ ra phần còn thiếu
```

Bot sẽ tạo một panel với các link Poe, Duck.ai, GPT, Coze và Gemini. Gemini có
thể được điền tự động; các web AI còn lại dùng quy trình bán tự động:

1. Mở link trong panel.
2. Xác thực trên web nếu được yêu cầu.
3. Sao chép prompt từ `/ai_prompt poe` hoặc `/ai_prompt gpt`.
4. Dán câu trả lời về Telegram:

```text
/ai_add poe nội dung trả lời của Poe
/ai_add duckai nội dung trả lời của Duck.ai
/ai_add gpt nội dung trả lời của ChatGPT
/ai_add coze nội dung trả lời của Coze
```

Cuối cùng:

```text
/ai_summary
/ai_status
```

### Phân tích cổ phiếu

```text
/panel FPT
```

Lệnh này giữ thêm dữ liệu quant, giá, vùng và backtest trong prompt. Phần AI chỉ
được yêu cầu diễn giải evidence, không được tự sửa các giá trị quant.

## 5. Giới hạn an toàn mặc định

- Gemini có bộ đếm nội bộ theo ngày để tránh đốt quota.
- RSS được cache để không gọi lại nguồn cho cùng một câu hỏi trong thời gian ngắn.
- Bot không lưu mật khẩu, cookie, mã xác thực hoặc session web.
- Không tự vượt CAPTCHA và không scrape phiên đăng nhập.
- Chỉ một phiên GitHub Actions được phép chạy cùng lúc cho bot này.
