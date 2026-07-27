# Hợp đồng dữ liệu hệ sinh thái maikhanhthieu-droid

## Nguyên tắc vận hành

1. Mỗi repository phải chạy được khi mọi repository và API AI khác đều lỗi.
2. Python/dữ liệu nguồn sở hữu `facts`; AI chỉ tạo `analysis` và không được
   ghi đè `facts`.
3. Consumer phải kiểm tra `schema_version`, `status`, `as_of` và độ mới trước
   khi dùng một feed.
4. Thiếu nguồn, thiếu ngày hoặc quá hạn phải trả `null`/`missing`/`stale`;
   không nội suy số thay thế.
5. Score, target và backtest phải ghi đúng bản chất mô hình/kịch bản. Không gọi
   score là xác suất thắng và không hứa mức tăng 20–30% trong một tháng.

## Các luồng độc lập

| Producer | Feed dùng chung | Vai trò |
|---|---|---|
| `laixuat_tienVN` | `data/rates_feed_latest.json` | Facts lãi suất, LSTP và tỷ giá đã qua verifier |
| `vimo-VN` | `docs/api/facts.json` | Facts vĩ mô; Gemini nằm ở endpoint riêng |
| `THIUCUBU` | `data/filter_feed_latest.json` | Bộ lọc thô theo mã, kèm nguồn/ngày/cache status |
| `quet-mau-hinh` | `data/pattern_feed_latest.json` | Ứng viên mẫu hình causal; AI review nằm riêng |
| `v-n-ch-s-h-u-vn` | không làm nguồn facts | Consumer/orchestrator; tự quét được khi feed khác lỗi |

## Trường tối thiểu của feed

```json
{
  "schema_version": "producer.domain.v1",
  "producer": "owner/repository",
  "generated_at": "ISO-8601 datetime",
  "as_of": "ISO-8601 market/data date",
  "status": "ok | degraded | missing",
  "facts": [],
  "quality": {
    "facts_only": true,
    "ai_output_included": false
  }
}
```

`generated_at` là lúc pipeline chạy; `as_of` là ngày của dữ liệu. Hai trường
không được dùng thay thế cho nhau.

## Quy tắc kết hợp

- `quet-mau-hinh` có thể đọc THIUCUBU như advisory. Mặc định THIUCUBU không
  được xóa ứng viên; chỉ hard-gate khi người vận hành bật rõ ràng.
- Bot cổ phiếu đọc vimo-VN; feed quá 72 giờ hoặc thiếu `generated_at` phải về
  macro trung tính.
- Snapshot doanh nghiệp phải giữ riêng `fundamentals_as_of` (kỳ BCTC),
  `fundamentals_fetched_at` (lúc lấy) và nguồn. Không gọi dữ liệu quý là dữ liệu
  thời gian thực; giá thị trường và kỳ báo cáo tài chính không dùng chung ngày.
- GPT và GLM đánh giá độc lập trên facts mẫu hình. Đồng thuận AI không được
  nâng hạng một mã chưa qua scanner deterministic.
- Gemini giải thích trạng thái hiện tại. Phản hồi Search không có grounding
  source hoặc có con số mới không nằm trong input phải bị loại.
- Mọi lỗi mạng, quota hoặc API AI phải fail-open đối với việc quét và
  fail-closed đối với nội dung AI: scanner vẫn chạy, phần AI không được xuất bản.

## Lọc mục tiêu tăng 20–30%

Mức tăng 20–30% chỉ được dùng như một điều kiện sàng lọc kịch bản sau khi đã có:

- giá hiện tại và target deterministic cùng ngày dữ liệu;
- công thức target được công bố;
- thanh khoản/rủi ro đạt gate;
- kết quả backtest kèm cỡ mẫu và tỷ lệ unresolved.

Đầu ra phải ghi `scenario_candidate`, không ghi `will_return`, `guaranteed` hay
“mã chắc chắn tăng”.
