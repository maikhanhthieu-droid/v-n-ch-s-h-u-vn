import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from bot import (
    DeepSignal,
    FundamentalSnapshot,
    GeminiAnalyzer,
    PriceBar,
    Quote,
    YahooQuoteProvider,
    _ledger_price_bars,
    _vnstock_bars,
    backtest_similar_patterns,
    build_score_breakdown,
    build_trade_plan,
)


def trending_bars(count: int = 520) -> list[PriceBar]:
    return [
        PriceBar(
            close=100 + index * 0.04 + (index % 20) * 0.35,
            open_price=100 + index * 0.04 + (index % 20) * 0.35 - 0.1,
            high=100.8 + index * 0.04 + (index % 20) * 0.35,
            low=99.2 + index * 0.04 + (index % 20) * 0.35,
            volume=500_000 + (index % 5) * 10_000,
            date=f"2024-{index // 28 + 1:02d}-{index % 28 + 1:02d}"
            if index < 336
            else None,
        )
        for index in range(count)
    ]


class QuantRegressionTests(unittest.TestCase):
    def test_negative_multiples_receive_no_valuation_credit(self):
        quote = Quote("BAD", "BAD", 100.0, 99.0)
        snapshot = FundamentalSnapshot(
            "BAD",
            "BAD",
            trailing_pe=-4.0,
            price_to_book=-1.0,
        )
        breakdown, reasons = build_score_breakdown(snapshot, quote, [])
        # Two points are only the explicit neutral allowance for missing growth.
        self.assertEqual(breakdown.valuation, 2)
        self.assertTrue(any("P/E" in item for item in reasons))
        self.assertTrue(any("P/B" in item for item in reasons))

    def test_negative_cash_flow_reduces_nonfinancial_business_score(self):
        quote = Quote("AAA", "AAA", 100.0, 99.0)
        common = dict(
            symbol="AAA",
            name="AAA",
            revenue_growth=12.0,
            net_income_growth=15.0,
            return_on_equity=18.0,
            debt_to_equity=0.4,
            current_ratio=1.5,
            trailing_pe=10.0,
            price_to_book=1.4,
        )
        healthy, _ = build_score_breakdown(
            FundamentalSnapshot(**common, operating_cash_flow=10.0, free_cash_flow=5.0),
            quote,
            [],
        )
        weak, _ = build_score_breakdown(
            FundamentalSnapshot(**common, operating_cash_flow=-10.0, free_cash_flow=-5.0),
            quote,
            [],
        )
        self.assertEqual(healthy.business - weak.business, 3)

    def test_backtest_samples_do_not_overlap_and_ignore_current_risk_pct(self):
        bars = trending_bars()
        # Undated fixtures intentionally preserve input order.
        quote = Quote("FPT", "FPT", bars[-1].close, bars[-2].close)
        plan = build_trade_plan(quote, bars)
        first = backtest_similar_patterns(bars, replace(plan, risk_pct=3.0), 20)
        second = backtest_similar_patterns(bars, replace(plan, risk_pct=10.0), 20)
        self.assertGreater(first.samples, 1)
        self.assertTrue(
            all(
                right - left >= 20
                for left, right in zip(first.sample_indices, first.sample_indices[1:])
            )
        )
        self.assertEqual(first.sample_indices, second.sample_indices)
        self.assertEqual((first.wins, first.losses, first.unresolved),
                         (second.wins, second.losses, second.unresolved))
        self.assertEqual(first.expected_r, second.expected_r)

    def test_backtest_reports_cost_and_wilson_lower_bound(self):
        bars = trending_bars()
        plan = build_trade_plan(Quote("FPT", "FPT", bars[-1].close, bars[-2].close), bars)
        free = backtest_similar_patterns(bars, plan, round_trip_cost_pct=0.0)
        costly = backtest_similar_patterns(bars, plan, round_trip_cost_pct=1.0)
        self.assertEqual(costly.cost_pct, 1.0)
        self.assertEqual(costly.effective_samples, costly.samples)
        if costly.hit_rate is not None:
            self.assertIsNotNone(costly.hit_rate_lower)
            self.assertLessEqual(costly.hit_rate_lower, costly.hit_rate)
        if free.median_net_return is not None and costly.median_net_return is not None:
            self.assertAlmostEqual(free.median_net_return - costly.median_net_return, 1.0)

    def test_backtest_never_substitutes_same_day_close_for_missing_next_open(self):
        bars = [replace(bar, open_price=None) for bar in trending_bars()]
        plan = build_trade_plan(
            Quote("FPT", "FPT", bars[-1].close, bars[-2].close),
            bars,
        )
        result = backtest_similar_patterns(bars, plan, lookahead_sessions=20)
        self.assertGreater(result.samples, 0)
        self.assertEqual(result.not_filled, result.samples)
        self.assertEqual((result.wins, result.losses), (0, 0))

    def test_gemini_fallback_bypasses_only_local_interval(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise RuntimeError("grounding unavailable")
                return SimpleNamespace(output_text="Phân tích định lượng trung lập.", steps=[])

        analyzer = GeminiAnalyzer(
            "secret",
            use_google_search=True,
            min_interval=60,
            cache_ttl=0,
            client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
        )
        signal = DeepSignal(
            "FPT",
            80,
            FundamentalSnapshot("FPT", "FPT"),
            Quote("FPT", "FPT", 100.0, 99.0),
            [],
            [],
        )
        rendered = analyzer.analyze(signal)
        self.assertEqual(len(calls), 2)
        self.assertIn("model dự phòng", rendered)

    def test_gemini_cache_fingerprints_facts_not_just_symbol(self):
        calls = []

        class FakeInteractions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output_text="Phân tích trung lập.", steps=[])

        analyzer = GeminiAnalyzer(
            "secret",
            min_interval=0,
            cache_ttl=600,
            client_factory=lambda _: SimpleNamespace(interactions=FakeInteractions()),
        )
        snapshot = FundamentalSnapshot("FPT", "FPT")
        analyzer.analyze(DeepSignal("FPT", 70, snapshot, Quote("FPT", "FPT", 100, 99), [], []))
        analyzer.analyze(DeepSignal("FPT", 70, snapshot, Quote("FPT", "FPT", 101, 100), [], []))
        self.assertEqual(len(calls), 2)

    def test_yahoo_history_keeps_dates_source_and_adjusted_ohlc(self):
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [1_700_000_000, 1_700_086_400],
                    "indicators": {
                        "quote": [{
                            "open": [100.0, 102.0],
                            "high": [110.0, 112.0],
                            "low": [90.0, 92.0],
                            "close": [100.0, 102.0],
                            "volume": [10, 20],
                        }],
                        "adjclose": [{"adjclose": [50.0, 51.0]}],
                    },
                }],
            },
        }
        with patch("bot._json_request", return_value=payload):
            bars = YahooQuoteProvider(cache_ttl=0).get_history("FPT")
        self.assertEqual([bar.close for bar in bars], [50.0, 51.0])
        self.assertEqual(bars[0].high, 55.0)
        self.assertTrue(all(bar.date for bar in bars))
        self.assertTrue(all(bar.source == "Yahoo Finance" for bar in bars))
        self.assertTrue(all(bar.adjusted for bar in bars))
        self.assertEqual(bars[0].raw_open, 100.0)
        self.assertEqual(bars[0].raw_high, 110.0)
        self.assertEqual(bars[0].raw_low, 90.0)
        self.assertEqual(bars[0].raw_close, 100.0)

        ledger_bars = _ledger_price_bars(bars)
        self.assertEqual(ledger_bars[0]["open"], 100.0)
        self.assertEqual(ledger_bars[0]["high"], 110.0)
        self.assertEqual(ledger_bars[0]["low"], 90.0)
        self.assertEqual(ledger_bars[0]["close"], 100.0)

    def test_ledger_fails_closed_when_adjusted_bar_lacks_raw_basis(self):
        projected = _ledger_price_bars(
            [
                PriceBar(
                    close=50.0,
                    open_price=49.0,
                    high=51.0,
                    low=48.0,
                    date="2026-08-10",
                    adjusted=True,
                )
            ]
        )
        self.assertIsNone(projected[0]["open"])
        self.assertIsNone(projected[0]["close"])

    def test_vnstock_history_sorts_dates_and_preserves_source(self):
        bars = _vnstock_bars(
            [
                {"time": "2026-08-10", "open": 101, "high": 103, "low": 100, "close": 102},
                {"time": "2026-08-08", "open": 99, "high": 101, "low": 98, "close": 100},
            ],
            "FPT",
            "VCI",
        )
        self.assertEqual([bar.date for bar in bars], ["2026-08-08", "2026-08-10"])
        self.assertTrue(all(bar.source == "VNStock/VCI" for bar in bars))


if __name__ == "__main__":
    unittest.main()
