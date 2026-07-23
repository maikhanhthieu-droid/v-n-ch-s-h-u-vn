import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (  # noqa: E402
    ApiUsageStore,
    BotApplication,
    BotError,
    DeepSignal,
    FundamentalSnapshot,
    GeminiAnalyzer,
    MacroContext,
    MacroContextClient,
    NewsItem,
    NeutralNewsService,
    PriceBar,
    Quote,
    RoutedMarketDataProvider,
    SignalStore,
    WatchlistStore,
    YahooQuoteProvider,
    _vnstock_bars,
    backtest_similar_patterns,
    build_deep_signal,
    build_score_breakdown,
    build_trade_plan,
    format_news_report,
    score_candidate,
    split_telegram_html,
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

    def test_long_telegram_output_splits_at_paragraph_boundaries(self):
        first = "<b>Một</b>\n" + "a" * 2500
        second = "<b>Hai</b>\n" + "b" * 2500
        chunks = split_telegram_html(first + "\n\n" + second)
        self.assertEqual(chunks, [first, second])
        self.assertTrue(all(len(chunk) <= 4090 for chunk in chunks))

    def test_deep_signal_score_rewards_quality_valuation_and_trend(self):
        bars = [
            PriceBar(
                close=50_000 + index * 120 + ((index % 6) - 3) * 80,
                high=50_500 + index * 120 + ((index % 6) - 3) * 80,
                low=49_500 + index * 120 + ((index % 6) - 3) * 80,
                volume=1_000_000 + index * 1_000,
            )
            for index in range(250)
        ]
        quote = Quote("FPT", "FPT Company", bars[-1].close, bars[-2].close)
        snapshot = FundamentalSnapshot(
            symbol="FPT",
            name="FPT Company",
            price=bars[-1].close,
            trailing_pe=9.0,
            price_to_book=1.1,
            revenue_growth=18.0,
            net_income_growth=24.0,
            return_on_equity=23.0,
            debt_to_equity=0.35,
            current_ratio=1.8,
            earnings_growth=24.0,
        )
        score, reasons = score_candidate(snapshot, quote, bars)
        self.assertGreaterEqual(score, 70)
        self.assertTrue(reasons)

    def test_score_breakdown_is_auditable_and_totals_100_scale(self):
        bars = [
            PriceBar(close=100 + index / 2, volume=1_000_000)
            for index in range(220)
        ]
        quote = Quote("FPT", "FPT", bars[-1].close, bars[-2].close)
        snapshot = FundamentalSnapshot(
            "FPT",
            "FPT",
            price=quote.price,
            trailing_pe=10,
            price_to_book=1.4,
            revenue_growth=12,
            net_income_growth=15,
            return_on_equity=18,
            debt_to_equity=0.4,
            current_ratio=1.5,
        )
        macro = MacroContext(score=2, available=True)
        breakdown, _ = build_score_breakdown(snapshot, quote, bars, macro)
        self.assertEqual(
            breakdown.total,
            breakdown.business
            + breakdown.valuation
            + breakdown.technical
            + breakdown.risk
            + breakdown.macro,
        )
        self.assertLessEqual(breakdown.total, 100)
        self.assertEqual(breakdown.macro, 6)

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

    def test_gemini_min_interval_returns_immediately_without_second_call(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output_text="Phân tích định lượng.", steps=[])

        analyzer = GeminiAnalyzer(
            "secret",
            min_interval=60,
            cache_ttl=0,
            client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
        )
        quote = Quote("FPT", "FPT", 100.0, 99.0)
        snapshot = FundamentalSnapshot("FPT", "FPT")
        first = analyzer.analyze(DeepSignal("FPT", 70, snapshot, quote, [], []))
        second = analyzer.analyze(DeepSignal("VNM", 70, snapshot, quote, [], []))
        self.assertIn("Phân tích định lượng", first)
        self.assertIn("hãy thử lại sau", second)
        self.assertEqual(len(calls), 1)

    def test_gemini_daily_budget_stops_before_extra_api_call(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output_text="Phân tích định lượng.", steps=[])

        with tempfile.TemporaryDirectory() as directory:
            usage = ApiUsageStore(Path(directory) / "usage.json", gemini_daily_budget=1)
            analyzer = GeminiAnalyzer(
                "secret",
                min_interval=0,
                cache_ttl=0,
                usage_store=usage,
                client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
            )
            quote = Quote("FPT", "FPT", 100.0, 99.0)
            snapshot = FundamentalSnapshot("FPT", "FPT")
            first = analyzer.analyze(DeepSignal("FPT", 70, snapshot, quote, [], []))
            second = analyzer.analyze(DeepSignal("VNM", 70, snapshot, quote, [], []))
            self.assertIn("Phân tích định lượng", first)
            self.assertIn("100% ngân sách an toàn", second)
            self.assertEqual(len(calls), 1)

    def test_gemini_without_key_keeps_quantitative_result(self):
        analyzer = GeminiAnalyzer("")
        quote = Quote("FPT", "FPT Company", 70.0, 72.0)
        snapshot = FundamentalSnapshot("FPT", "FPT Company")
        signal = DeepSignal("FPT", 70, snapshot, quote, [], [])
        self.assertIn("chưa có API key", analyzer.analyze(signal))

    def test_fundamentals_batch_uses_one_tradingview_request(self):
        response = {
            "data": [
                {
                    "s": "HOSE:FPT",
                    "d": [
                        "FPT", "FPT Corp", 67000, 11.8, 2.9, 1000,
                        "Technology Services", "IT Services", 5000, 18.2,
                        900, 21.5, 24.0, 0.35, 1.7, 5400, 20.2,
                    ],
                },
                {
                    "s": "HOSE:HPG",
                    "d": [
                        "HPG", "Hoa Phat", 21850, 8.4, 1.3, 900,
                        "Non-Energy Minerals", "Steel", 6000, 9.0,
                        700, 12.0, 16.0, 0.55, 1.2, 2600, 11.0,
                    ],
                },
            ]
        }
        provider = YahooQuoteProvider()
        with patch("bot._json_post", return_value=response) as post:
            snapshots = provider.get_fundamentals_batch(["FPT", "HPG"])
        self.assertEqual(post.call_count, 1)
        self.assertAlmostEqual(snapshots["FPT"].trailing_pe, 11.8)
        self.assertAlmostEqual(snapshots["HPG"].price_to_book, 1.3)
        self.assertEqual(snapshots["FPT"].sector, "Technology Services")
        self.assertAlmostEqual(snapshots["FPT"].return_on_equity, 24.0)

    def test_macro_context_parses_vimo_sources_and_drivers(self):
        payload = {
            "generated_at_bkk": "2026-07-23 08:00",
            "macro_strategy": {
                "stance": "NẮM GIỮ / CHỜ XÁC NHẬN",
                "score": -2,
                "reason_short": "Tăng trưởng và lạm phát đang kéo theo hai hướng.",
                "positive_drivers": ["IIP tăng"],
                "risk_drivers": ["CPI cao"],
                "confidence": "LOW",
            },
            "cards": [
                {
                    "key": "cpi",
                    "source_primary": "NSO/GSO",
                    "source_url": "https://example.com/cpi",
                    "as_of": "2026-07",
                    "source_quality": "official",
                }
            ],
        }
        client = MacroContextClient(fetcher=lambda url, timeout: payload)
        context = client.latest()
        self.assertTrue(context.available)
        self.assertEqual(context.score, -2)
        self.assertEqual(context.risk_drivers, ("CPI cao",))
        self.assertEqual(context.sources[0].url, "https://example.com/cpi")

    def test_rss_parser_and_news_formatter_keep_sources(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel><item>
        <title>Doanh nghiệp công bố kế hoạch mới</title>
        <link>https://example.com/news</link>
        <source>Ví dụ</source><pubDate>Thu, 23 Jul 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""
        items = NeutralNewsService._parse_rss(rss, 5)
        self.assertEqual(items[0].source, "Ví dụ")
        rendered = format_news_report(
            "FPT",
            items,
            MacroContext(
                stance="TRUNG LẬP",
                score=0,
                summary="Chờ xác nhận.",
                available=True,
            ),
            "Tác động có cả cơ hội và rủi ro; cần mở công bố gốc.",
        )
        self.assertIn("https://example.com/news", rendered)
        self.assertIn("không phải khuyến nghị", rendered)

    def test_backtest_reports_samples_without_calling_it_future_probability(self):
        bars = [
            PriceBar(
                close=100 + index * 0.03 + (index % 20) * 0.4,
                high=100.8 + index * 0.03 + (index % 20) * 0.4,
                low=99.2 + index * 0.03 + (index % 20) * 0.4,
                volume=500_000,
            )
            for index in range(360)
        ]
        quote = Quote("FPT", "FPT", bars[-1].close, bars[-2].close)
        plan = build_trade_plan(quote, bars)
        result = backtest_similar_patterns(bars, plan)
        self.assertLessEqual(result.samples, 40)
        if result.hit_rate is not None:
            self.assertGreaterEqual(result.hit_rate, 0)
            self.assertLessEqual(result.hit_rate, 100)
        signal = build_deep_signal(
            "FPT",
            FundamentalSnapshot("FPT", "FPT"),
            quote,
            bars,
            MacroContext(),
        )
        self.assertEqual(signal.backtest.samples, result.samples)

    def test_vnstock_bars_normalize_thousand_vnd_units(self):
        bars = _vnstock_bars(
            [
                {"open": 99, "high": 102, "low": 98, "close": 100, "volume": 1000},
                {"open": 100, "high": 103, "low": 99, "close": 101, "volume": 1200},
            ],
            "FPT",
        )
        self.assertEqual(bars[0].close, 100_000)
        self.assertEqual(bars[1].high, 103_000)
        self.assertEqual(bars[1].volume, 1200)

    def test_vnstock_router_moves_to_next_source_after_quota_error(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = ApiUsageStore(
                Path(directory) / "usage.json",
                vnstock_daily_budget=5,
            )
            fallback = Mock()
            calls = []

            def fetch(source, symbol, range_value, interval):
                calls.append((source, symbol))
                if source == "VCI":
                    raise RuntimeError("429 quota exceeded")
                return [PriceBar(close=100_000), PriceBar(close=101_000)]

            provider = RoutedMarketDataProvider(
                fallback=fallback,
                usage_store=usage,
                api_key="configured",
                sources=["VCI", "KBS"],
                history_fetcher=fetch,
            )
            bars = provider.get_history("FPT")
            self.assertEqual(bars[-1].close, 101_000)
            self.assertEqual(calls, [("VCI", "FPT"), ("KBS", "FPT")])
            self.assertEqual(usage.snapshot()["vnstock_requests"]["used"], 2)
            self.assertIn("VCI: khỏe", provider.status_text())
            fallback.get_history.assert_not_called()

    def test_vnstock_router_uses_yahoo_after_daily_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = ApiUsageStore(
                Path(directory) / "usage.json",
                vnstock_daily_budget=1,
            )
            fallback = Mock()
            fallback.get_history.return_value = [
                PriceBar(close=99_000),
                PriceBar(close=100_000),
            ]
            calls = []

            def fetch(source, symbol, range_value, interval):
                calls.append(source)
                raise RuntimeError("temporary upstream error")

            provider = RoutedMarketDataProvider(
                fallback=fallback,
                usage_store=usage,
                api_key="configured",
                sources=["VCI", "KBS"],
                history_fetcher=fetch,
            )
            bars = provider.get_history("FPT")
            self.assertEqual(bars[-1].close, 100_000)
            self.assertEqual(len(calls), 1)
            fallback.get_history.assert_called_once_with("FPT", "6mo", "1d")
            self.assertEqual(usage.snapshot()["vnstock_requests"]["used"], 1)

    def test_watchlist_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlists.json"
            first = WatchlistStore(path)
            self.assertTrue(first.add("42", "FPT")[0])
            second = WatchlistStore(path)
            self.assertEqual(second.get("42"), ["FPT"])
            self.assertTrue(second.remove("42", "FPT")[0])
            self.assertEqual(second.get("42"), [])

    def test_api_usage_is_persisted_and_formats_percentages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            first = ApiUsageStore(path, gemini_daily_budget=2, deep_daily_limit=1, scan_daily_limit=1)
            self.assertTrue(first.claim("gemini_requests")[0])
            self.assertTrue(first.claim("deep_commands")[0])
            second = ApiUsageStore(path, gemini_daily_budget=2, deep_daily_limit=1, scan_daily_limit=1)
            status = second.format_status("đã bật")
            self.assertIn("Gemini: 1/2", status)
            self.assertIn("/deep: 1/1", status)
            self.assertIn("100%", status)

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

    def test_deep_and_scan_share_nonblocking_command_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Mock()
            provider.get_quote.return_value = Quote("FPT", "FPT", 100.0, 99.0)
            provider.get_history.return_value = []
            provider.get_fundamentals.return_value = FundamentalSnapshot("FPT", "FPT")
            scanner = Mock()
            scanner.render_signal.return_value = "deep result"
            app = BotApplication(
                telegram=Mock(),
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                signal_store=SignalStore(Path(directory) / "signals.json"),
                scanner=scanner,
                research_command_cooldown=60,
            )
            self.assertEqual(app.handle_text("/deep FPT", 1), "deep result")
            blocked = app.handle_text("/scan", 1)
            self.assertIn("chỉ chạy một lần mỗi phút", blocked)
            self.assertEqual(app.handle_text("/ping", 1), "pong ✅")
            provider.get_quote.assert_called_once()
            scanner.find_candidates.assert_not_called()

    def test_deep_and_scan_daily_limits_are_reported_by_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Mock()
            provider.get_quote.return_value = Quote("FPT", "FPT", 100.0, 99.0)
            provider.get_history.return_value = []
            provider.get_fundamentals.return_value = FundamentalSnapshot("FPT", "FPT")
            scanner = Mock()
            scanner.render_signal.return_value = "deep result"
            scanner.find_candidates.return_value = []
            scanner.gemini.status_text.return_value = "đã bật"
            usage = ApiUsageStore(
                Path(directory) / "usage.json",
                deep_daily_limit=1,
                scan_daily_limit=1,
            )
            app = BotApplication(
                telegram=Mock(),
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                signal_store=SignalStore(Path(directory) / "signals.json"),
                scanner=scanner,
                usage_store=usage,
                research_command_cooldown=0,
            )
            self.assertEqual(app.handle_text("/deep FPT", 1), "deep result")
            self.assertIn("100%", app.handle_text("/deep VNM", 1))
            self.assertIn("chưa có mã", app.handle_text("/scan", 1))
            self.assertIn("100%", app.handle_text("/scan", 1))
            usage_text = app.handle_text("/usage", 1)
            self.assertIn("/deep: 1/1", usage_text)
            self.assertIn("/scan: 1/1", usage_text)

    def test_news_command_uses_sources_macro_and_daily_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = Mock()
            scanner.gemini.analyze_news.return_value = (
                "Dữ kiện còn hạn chế; cần kiểm tra công bố gốc."
            )
            scanner.gemini.status_text.return_value = "đã bật"
            news_service = Mock()
            news_service.headlines.return_value = [
                NewsItem(
                    "FPT công bố thông tin mới",
                    "Nguồn thử nghiệm",
                    "https://example.com/fpt",
                    "2026-07-23",
                )
            ]
            macro_client = Mock()
            macro_client.latest.return_value = MacroContext(
                stance="TRUNG LẬP",
                score=0,
                summary="Chờ xác nhận.",
                available=True,
            )
            usage = ApiUsageStore(
                Path(directory) / "usage.json",
                news_daily_limit=1,
            )
            app = BotApplication(
                telegram=Mock(),
                provider=Mock(),
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                scanner=scanner,
                usage_store=usage,
                macro_client=macro_client,
                news_service=news_service,
                research_command_cooldown=0,
            )
            first = app.handle_text("/news FPT", 1)
            self.assertIn("https://example.com/fpt", first)
            self.assertIn("Đánh giá hai chiều", first)
            self.assertIn("100%", app.handle_text("/news VNM", 1))
            self.assertIn("/news: 1/1", app.handle_text("/usage", 1))

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
