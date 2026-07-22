import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (  # noqa: E402
    BotApplication,
    BotError,
    DeepSignal,
    FundamentalSnapshot,
    GeminiAnalyzer,
    PriceBar,
    Quote,
    SignalStore,
    WatchlistStore,
    YahooQuoteProvider,
    score_candidate,
    format_chart,
    format_quote,
    format_report,
    format_ta,
    normalize_symbol,
    yahoo_symbol,
)


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return Quote(
            symbol=symbol,
            name=f"{symbol} Company",
            price=100.0,
            previous_close=95.0,
            open_price=98.0,
            day_high=101.0,
            day_low=97.0,
            volume=123456,
        )

    def get_history(self, symbol):
        self.calls.append(f"history:{symbol}")
        return [
            PriceBar(
                close=90.0 + index,
                high=91.0 + index,
                low=89.0 + index,
                open_price=89.5 + index,
                volume=1000 + index,
            )
            for index in range(30)
        ]


class BotTests(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol(" fpt.vn "), "FPT")
        with self.assertRaises(ValueError):
            normalize_symbol("FPT-ABC")

    def test_yahoo_symbol(self):
        self.assertEqual(yahoo_symbol("FPT"), "FPT.VN")
        self.assertEqual(yahoo_symbol("VNINDEX"), "^VNINDEX.VN")

    def test_format_quote_escapes_name(self):
        quote = Quote("FPT", "<unsafe>", 100, 95)
        rendered = format_quote(quote)
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertIn("+5.26%", rendered)

    def test_format_report_includes_session_data(self):
        quote = Quote("FPT", "FPT Company", 100, 95, open_price=98, day_high=101, day_low=97, volume=123456)
        rendered = format_report(quote)
        self.assertIn("Báo cáo nhanh", rendered)
        self.assertIn("123,456", rendered)

    def test_chart_and_ta_formatters(self):
        bars = [PriceBar(close=100.0 + index, high=101.0 + index, low=99.0 + index) for index in range(30)]
        self.assertIn("Chart nhanh", format_chart("FPT", bars))
        rendered_ta = format_ta("FPT", bars)
        self.assertIn("TA nhanh", rendered_ta)
        self.assertIn("RSI14", rendered_ta)

    def test_deep_signal_score_rewards_discount_and_low_valuation(self):
        quote = Quote("FPT", "FPT Company", 70.0, 72.0)
        snapshot = FundamentalSnapshot(
            symbol="FPT",
            name="FPT Company",
            price=70.0,
            trailing_pe=9.0,
            price_to_book=1.1,
            fifty_two_week_high=110.0,
        )
        bars = [PriceBar(close=90.0 - index, high=91.0 - index, low=89.0 - index) for index in range(30)]
        score, reasons = score_candidate(snapshot, quote, bars)
        self.assertGreaterEqual(score, 70)
        self.assertTrue(reasons)

    def test_deep_signal_score_rejects_negative_multiples(self):
        quote = Quote("BAD", "Bad Company", 70.0, 72.0)
        snapshot = FundamentalSnapshot(
            symbol="BAD",
            name="Bad Company",
            price=70.0,
            trailing_pe=-4.0,
            price_to_book=-1.0,
            fifty_two_week_high=80.0,
        )
        bars = [PriceBar(close=70.0 + index / 10) for index in range(30)]
        score, reasons = score_candidate(snapshot, quote, bars)
        self.assertLess(score, 70)
        self.assertFalse(any("P/E" in reason or "P/B" in reason for reason in reasons))

    def test_gemini_interactions_uses_grounding_and_high_thinking(self):
        captured = {}

        class FakeInteractions:
            def create(self, **kwargs):
                captured.update(kwargs)
                annotation = SimpleNamespace(
                    title="Công bố doanh nghiệp",
                    url="https://example.com/report",
                )
                block = SimpleNamespace(
                    text="1) Định giá: đang ở vùng cần theo dõi.",
                    annotations=[annotation],
                )
                return SimpleNamespace(
                    output_text=block.text,
                    steps=[SimpleNamespace(type="model_output", content=[block])],
                )

        fake_client = SimpleNamespace(interactions=FakeInteractions())
        analyzer = GeminiAnalyzer(
            "secret",
            model="models/gemini-3-flash-preview",
            thinking_level="high",
            use_google_search=True,
            min_interval=0,
            client_factory=lambda _: fake_client,
        )
        quote = Quote("FPT", "FPT Company", 70.0, 72.0)
        snapshot = FundamentalSnapshot(
            symbol="FPT",
            name="FPT Company",
            price=70.0,
            trailing_pe=9.0,
            price_to_book=1.1,
            fifty_two_week_high=110.0,
        )
        bars = [PriceBar(close=90.0 - index, high=91.0 - index, low=89.0 - index) for index in range(30)]
        rendered = analyzer.analyze(
            DeepSignal("FPT", 80, snapshot, quote, bars, ["P/E thấp"])
        )
        self.assertEqual(captured["model"], "gemini-3-flash-preview")
        self.assertEqual(captured["tools"], [{"type": "google_search"}])
        self.assertEqual(captured["generation_config"]["thinking_level"], "high")
        self.assertNotIn("temperature", captured["generation_config"])
        self.assertNotIn("top_p", captured["generation_config"])
        self.assertFalse(captured["store"])
        self.assertIn("https://example.com/report", rendered)

    def test_gemini_opens_circuit_without_retrying_on_quota(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("tools"):
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return SimpleNamespace(
                    output_text="Định giá: dựa trên số liệu đầu vào.",
                    steps=[],
                )

        analyzer = GeminiAnalyzer(
            "secret",
            use_google_search=True,
            min_interval=0,
            quota_cooldown=600,
            client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
        )
        quote = Quote("HPG", "Hoa Phat", 21_850.0, 22_000.0)
        snapshot = FundamentalSnapshot(
            "HPG",
            "Hoa Phat",
            price=21_850.0,
            trailing_pe=8.4,
            price_to_book=1.33,
        )
        rendered = analyzer.analyze(
            DeepSignal("HPG", 85, snapshot, quote, [], ["P/E thấp"])
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("tạm nghỉ do vượt giới hạn API", rendered)
        self.assertIn("tạm nghỉ do quota", analyzer.status_text())
        analyzer.analyze(DeepSignal("FPT", 80, snapshot, quote, [], []))
        self.assertEqual(len(calls), 1)

    def test_gemini_uses_one_fallback_and_caches_success(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("tools"):
                    raise RuntimeError("grounding unavailable")
                return SimpleNamespace(
                    output_text="Định giá: dựa trên số liệu định lượng.",
                    steps=[],
                )

        analyzer = GeminiAnalyzer(
            "secret",
            use_google_search=True,
            min_interval=0,
            cache_ttl=600,
            client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
        )
        quote = Quote("HPG", "Hoa Phat", 21_850.0, 22_000.0)
        snapshot = FundamentalSnapshot(
            "HPG",
            "Hoa Phat",
            price=21_850.0,
            trailing_pe=8.4,
            price_to_book=1.33,
        )
        signal = DeepSignal("HPG", 85, snapshot, quote, [], ["P/E thấp"])
        first = analyzer.analyze(signal)
        second = analyzer.analyze(signal)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["model"], analyzer.fallback_model)
        self.assertIsNone(calls[1]["tools"])
        self.assertIn("model dự phòng", first)
        self.assertIn("cache", second)

    def test_gemini_without_key_keeps_quantitative_result(self):
        analyzer = GeminiAnalyzer("")
        quote = Quote("FPT", "FPT Company", 70.0, 72.0)
        snapshot = FundamentalSnapshot("FPT", "FPT Company")
        signal = DeepSignal("FPT", 70, snapshot, quote, [], [])
        self.assertIn("chưa có API key", analyzer.analyze(signal))

    def test_fundamentals_batch_uses_one_tradingview_request(self):
        response = {
            "data": [
                {"s": "HOSE:FPT", "d": ["FPT", "FPT Corp", 67000, 11.8, 2.9, 1000]},
                {"s": "HOSE:HPG", "d": ["HPG", "Hoa Phat", 21850, 8.4, 1.3, 900]},
            ]
        }
        provider = YahooQuoteProvider()
        with patch("bot._json_post", return_value=response) as post:
            snapshots = provider.get_fundamentals_batch(["FPT", "HPG"])
        self.assertEqual(post.call_count, 1)
        self.assertAlmostEqual(snapshots["FPT"].trailing_pe, 11.8)
        self.assertAlmostEqual(snapshots["HPG"].price_to_book, 1.3)

    def test_watchlist_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlists.json"
            first = WatchlistStore(path)
            self.assertTrue(first.add("42", "FPT")[0])
            second = WatchlistStore(path)
            self.assertEqual(second.get("42"), ["FPT"])
            self.assertTrue(second.remove("42", "FPT")[0])
            self.assertEqual(second.get("42"), [])

    def test_command_router(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = Mock()
            provider = FakeProvider()
            app = BotApplication(
                telegram=telegram,
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                signal_store=SignalStore(Path(directory) / "signals.json"),
            )
            self.assertIn("VN Equity Bot", app.handle_text("/start", 1))
            self.assertIn("FPT", app.handle_text("/add FPT", 1))
            self.assertIn("FPT", app.handle_text("/watchlist", 1))
            self.assertIn("100.00", app.handle_text("/quote FPT", 1))
            self.assertIn("100.00", app.handle_text("FPT", 1))
            self.assertIn("123,456", app.handle_text("/report FPT", 1))
            self.assertIn("Chart nhanh", app.handle_text("/chart FPT", 1))
            self.assertIn("TA nhanh", app.handle_text("/ta FPT", 1))
            self.assertIn("Đã bật", app.handle_text("/signals_on", 1))
            self.assertIn("đang bật", app.handle_text("/signals_status", 1))
            self.assertEqual(provider.calls, ["FPT", "FPT", "FPT", "history:FPT", "history:FPT"])
            app.handle_update(
                {"update_id": 1, "message": {"chat": {"id": 1}, "text": "/ping"}}
            )
            telegram.send_message.assert_called_once_with(1, "pong ✅")

    def test_provider_errors_are_user_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = Mock()
            provider = Mock()
            provider.get_quote.side_effect = BotError("Không lấy được dữ liệu thị trường lúc này.")
            app = BotApplication(
                telegram=telegram,
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
            )
            self.assertIn("Không lấy được", app.handle_text("/quote VNM", 1))


if __name__ == "__main__":
    unittest.main()
