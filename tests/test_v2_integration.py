import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot import (
    ApiUsageStore,
    BacktestResult,
    BotApplication,
    DeepSignalScanner,
    FundamentalSnapshot,
    MacroContext,
    PriceBar,
    Quote,
    SignalStore,
    TradePlan,
    WatchlistStore,
    backtest_similar_patterns,
    build_deep_signal,
)
from model_council import CouncilConfig, ModelCouncil
from signal_ledger import SignalLedger


class RecordingAdapter:
    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls = 0

    def analyze(self, evidence, *, allowed_evidence_ids):
        self.calls += 1
        return {
            "verdict": self.verdict,
            "confidence": 0.7,
            "summary": "Evidence was reviewed without changing the quant decision.",
            "evidence_ids": [allowed_evidence_ids[0]],
            "risks": ["Regime can change."],
            "missing_data": [],
        }


class RecordingGemini:
    def __init__(self):
        self.contexts = []

    def analyze(self, signal, *, council_context="", allow_burst=False):
        self.contexts.append(council_context)
        return "Phân tích Gemini trung lập."

    def status_text(self):
        return "test"


def signal_fixture(symbol: str = "FPT"):
    start = date(2024, 1, 1)
    bars = [
        PriceBar(
            close=100 + index * 0.2,
            open_price=99.9 + index * 0.2,
            high=101 + index * 0.2,
            low=99 + index * 0.2,
            volume=1_000_000,
            date=(start + timedelta(days=index)).isoformat(),
            source="test",
        )
        for index in range(260)
    ]
    quote = Quote(symbol, symbol, bars[-1].close, bars[-2].close, as_of=bars[-1].date)
    snapshot = FundamentalSnapshot(
        symbol,
        symbol,
        price=quote.price,
        trailing_pe=10,
        price_to_book=1.4,
        revenue_growth=12,
        net_income_growth=15,
        return_on_equity=18,
        debt_to_equity=0.4,
        current_ratio=1.5,
        fundamentals_as_of="2024-06-30",
        fundamentals_source="test",
    )
    return build_deep_signal(symbol, snapshot, quote, bars, MacroContext())


class V2IntegrationTests(unittest.TestCase):
    def test_council_is_shadow_context_and_reviews_are_written_to_ledger(self):
        glm = RecordingAdapter("support")
        deepseek = RecordingAdapter("reject")
        council = ModelCouncil(
            CouncilConfig(
                enabled=True,
                glm_api_key="glm-test-key",
                deepseek_api_key="deepseek-test-key",
            ),
            glm_adapter=glm,
            deepseek_adapter=deepseek,
        )
        gemini = RecordingGemini()
        signal = signal_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = ApiUsageStore(root / "usage.json", council_daily_budget=2)
            scanner = DeepSignalScanner(
                provider=Mock(),
                gemini=gemini,
                symbols=["FPT"],
                council=council,
                council_usage_store=usage,
            )
            rendered = scanner.render_signal(signal)
            self.assertIn("Ba góc nhìn AI", rendered)
            self.assertIn("GLM — cơ bản &amp; định giá", rendered)
            self.assertIn("DeepSeek — kỹ thuật &amp; phản biện rủi ro", rendered)
            self.assertIn("Gemini — dữ liệu hiện tại", rendered)
            self.assertIn('"reviews"', gemini.contexts[0])
            self.assertIn('"support"', gemini.contexts[0])
            self.assertEqual(glm.calls, 1)
            self.assertEqual(deepseek.calls, 1)
            self.assertEqual(len(scanner.reviews_for("FPT")), 2)

            ledger = SignalLedger(root / "signal_ledger.json")
            app = BotApplication(
                telegram=Mock(),
                provider=Mock(),
                store=WatchlistStore(root / "watchlists.json"),
                signal_store=SignalStore(root / "signal_state.json"),
                scanner=scanner,
                outcome_ledger=ledger,
            )
            app._record_live_signal(signal)
            records = ledger.list_signals()
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0]["model_reviews"]), 2)

    def test_council_daily_budget_falls_back_to_quant_only(self):
        glm = RecordingAdapter("neutral")
        deepseek = RecordingAdapter("neutral")
        council = ModelCouncil(
            CouncilConfig(
                enabled=True,
                glm_api_key="glm-test-key",
                deepseek_api_key="deepseek-test-key",
                cache_ttl_seconds=0,
            ),
            glm_adapter=glm,
            deepseek_adapter=deepseek,
        )
        with tempfile.TemporaryDirectory() as directory:
            usage = ApiUsageStore(Path(directory) / "usage.json", council_daily_budget=1)
            scanner = DeepSignalScanner(
                provider=Mock(),
                gemini=RecordingGemini(),
                symbols=["FPT"],
                council=council,
                council_usage_store=usage,
            )
            scanner.render_signal(signal_fixture("FPT"))
            second = scanner.render_signal(signal_fixture("VNM"))
            self.assertEqual((glm.calls, deepseek.calls), (1, 1))
            self.assertIn("ngân sách", second)
            self.assertEqual(scanner.reviews_for("VNM"), [])

    def test_scanner_production_gate_uses_lower_bound_and_expectancy(self):
        signal = signal_fixture()
        weak = replace(
            signal,
            backtest=BacktestResult(
                samples=10,
                wins=6,
                losses=2,
                unresolved=2,
                hit_rate=75.0,
                resolved=8,
                effective_samples=10,
                hit_rate_lower=40.0,
                expected_r=0.5,
                target_trials=10,
                target_hit_rate=60.0,
                target_hit_rate_lower=40.0,
            ),
        )
        strong = replace(
            weak,
            backtest=replace(
                weak.backtest,
                hit_rate_lower=50.0,
                expected_r=0.10,
                target_hit_rate_lower=50.0,
            ),
        )
        provider = Mock()
        provider.get_quote.return_value = signal.quote
        provider.get_history.return_value = signal.bars
        scanner = DeepSignalScanner(
            provider=provider,
            gemini=RecordingGemini(),
            symbols=["FPT"],
            min_score=0,
            require_backtest=True,
            min_backtest_resolved=8,
            min_backtest_win_lower=45.0,
            min_backtest_expectancy_r=0.05,
        )
        with patch("bot.build_deep_signal", return_value=weak):
            self.assertIsNone(scanner._evaluate("FPT", signal.snapshot, signal.macro))
        with patch("bot.build_deep_signal", return_value=strong):
            self.assertIs(scanner._evaluate("FPT", signal.snapshot, signal.macro), strong)
        provider.get_history.assert_called_with("FPT", range_value="5y", interval="1d")

    def test_target_trials_count_filled_timeouts_as_non_wins(self):
        start = date(2025, 1, 1)
        bars = [
            PriceBar(
                close=100.0,
                open_price=100.0,
                high=100.5,
                low=99.5,
                volume=1_000_000,
                date=(start + timedelta(days=index)).isoformat(),
            )
            for index in range(200)
        ]
        plan = TradePlan(
            entry_low=99.0,
            entry_high=101.0,
            stop=90.0,
            target_1=110.0,
            target_2=120.0,
            target_3=130.0,
            risk_pct=10.0,
        )
        with patch("bot._market_signature", return_value=(1, 1, 1, 1)), patch(
            "bot.build_trade_plan", return_value=plan
        ):
            result = backtest_similar_patterns(bars, plan, lookahead_sessions=20)

        self.assertGreaterEqual(result.samples, 5)
        self.assertEqual(result.resolved, 0)
        self.assertEqual(result.not_filled, 0)
        self.assertEqual(result.target_trials, result.samples)
        self.assertEqual(result.target_hit_rate, 0.0)
        self.assertEqual(result.target_hit_rate_lower, 0.0)

    def test_rank_uses_worst_horizon_and_symbol_tie_break(self):
        base = signal_fixture("AAA")

        def result(lower: float, expectancy: float) -> BacktestResult:
            return BacktestResult(
                samples=10,
                wins=6,
                losses=2,
                unresolved=2,
                hit_rate=75.0,
                resolved=8,
                hit_rate_lower=50.0,
                expected_r=expectancy,
                target_trials=10,
                target_hit_rate=60.0,
                target_hit_rate_lower=lower,
            )

        one_horizon_wonder = replace(
            base,
            symbol="ZZZ",
            backtest=result(70.0, 0.8),
            backtest_3m=result(20.0, 0.8),
        )
        balanced = replace(
            base,
            symbol="BBB",
            backtest=result(55.0, 0.3),
            backtest_3m=result(50.0, 0.3),
        )
        tied_aaa = replace(balanced, symbol="AAA")
        scanner = DeepSignalScanner(Mock(), RecordingGemini(), [], max_per_scan=99)

        ranked = sorted(
            [one_horizon_wonder, balanced, tied_aaa],
            key=scanner._rank_key,
        )
        self.assertEqual([item.symbol for item in ranked], ["AAA", "BBB", "ZZZ"])
        self.assertEqual(scanner.max_per_scan, 3)

    def test_find_candidates_returns_full_order_for_cooldown_backfill(self):
        symbols = ["AAA", "BBB", "CCC", "DDD"]
        base = signal_fixture()
        candidates = {
            symbol: replace(base, symbol=symbol, score=70 + index)
            for index, symbol in enumerate(symbols)
        }
        provider = Mock()
        provider.get_fundamentals_batch.return_value = {
            symbol: base.snapshot for symbol in symbols
        }
        scanner = DeepSignalScanner(
            provider,
            RecordingGemini(),
            symbols,
            min_score=0,
            max_per_scan=99,
            max_workers=2,
        )
        with patch.object(scanner, "_fundamental_ceiling", return_value=100), patch.object(
            scanner,
            "_evaluate",
            side_effect=lambda symbol, snapshot, macro: candidates[symbol],
        ):
            ranked = scanner.find_candidates()

        self.assertEqual(len(ranked), 4)
        self.assertEqual([item.symbol for item in ranked], ["DDD", "CCC", "BBB", "AAA"])
        self.assertEqual(scanner.max_per_scan, 3)

    def test_scanner_rejects_undated_or_stale_history(self):
        signal = signal_fixture()
        provider = Mock()
        provider.get_quote.return_value = signal.quote
        scanner = DeepSignalScanner(
            provider=provider,
            gemini=RecordingGemini(),
            symbols=["FPT"],
            min_score=0,
            require_dated_history=True,
            max_history_staleness_days=10,
        )
        provider.get_history.return_value = [replace(bar, date=None) for bar in signal.bars]
        self.assertIsNone(scanner._evaluate("FPT", signal.snapshot, signal.macro))
        provider.get_history.return_value = signal.bars
        self.assertIsNone(scanner._evaluate("FPT", signal.snapshot, signal.macro))
        fresh = list(signal.bars)
        fresh[-1] = replace(fresh[-1], date=date.today().isoformat())
        provider.get_history.return_value = fresh
        with patch("bot.build_deep_signal", return_value=signal):
            self.assertIs(scanner._evaluate("FPT", signal.snapshot, signal.macro), signal)

    def test_empty_performance_report_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = BotApplication(
                telegram=Mock(),
                provider=Mock(),
                store=WatchlistStore(root / "watchlists.json"),
                outcome_ledger=SignalLedger(root / "signal_ledger.json"),
            )
            result = app.handle_text("/performance", 1)
            self.assertIn("Hiệu suất tín hiệu live", result)
            self.assertIn("chưa đủ", result)

    def test_live_ledger_settles_on_raw_not_adjusted_yahoo_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = SignalLedger(root / "signal_ledger.json")
            record = ledger.record_signal(
                symbol="FPT",
                signal_at="2026-08-09T20:30:00+07:00",
                entry_plan={
                    "method": "next_open",
                    "reference_price": 100.0,
                    "entry_low": 99.0,
                    "entry_high": 101.0,
                    "max_entry_sessions": 3,
                },
                targets=[{"label": "T1", "price": 105.0}],
                stop=95.0,
                score_version="v2.0",
                features={"source": "test"},
            )
            provider = Mock()
            provider.get_history.return_value = [
                PriceBar(
                    close=52.5,
                    open_price=50.0,
                    high=53.0,
                    low=49.5,
                    date="2026-08-10",
                    source="Yahoo Finance",
                    adjusted=True,
                    raw_close=105.0,
                    raw_open=100.0,
                    raw_high=106.0,
                    raw_low=99.0,
                ),
                PriceBar(
                    close=53.0,
                    open_price=52.5,
                    high=53.5,
                    low=52.0,
                    date="2026-08-11",
                    source="Yahoo Finance",
                    adjusted=True,
                    raw_close=106.0,
                    raw_open=105.0,
                    raw_high=107.0,
                    raw_low=104.0,
                ),
            ]
            app = BotApplication(
                telegram=Mock(),
                provider=provider,
                store=WatchlistStore(root / "watchlists.json"),
                outcome_ledger=ledger,
            )
            app._refresh_live_outcomes()
            settled = ledger.get_signal(record["id"])
            self.assertEqual(settled["outcome"]["status"], "win")
            self.assertEqual(settled["outcome"]["entry_price"], 100.0)
            self.assertEqual(settled["outcome"]["exit_price"], 105.0)


if __name__ == "__main__":
    unittest.main()
