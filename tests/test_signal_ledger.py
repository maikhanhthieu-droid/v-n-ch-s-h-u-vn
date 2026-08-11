from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from signal_ledger import (
    SCHEMA_VERSION,
    SignalLedger,
    SignalLedgerError,
    evaluate_outcome,
    features_hash,
)


@dataclass(frozen=True)
class ObjectBar:
    date: str
    open_price: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class UpperObjectBar:
    Date: str
    Open: float
    High: float
    Low: float
    Close: float


class SignalLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "signal_ledger.json"
        self.ledger = SignalLedger(self.path)

    def record(
        self,
        symbol: str = "FPT",
        *,
        signal_at: str = "2026-08-03T15:05:00+07:00",
        score_version: str = "score.v1",
        target: float = 110.0,
        stop: float = 90.0,
        score: float = 75.0,
        entry_plan: dict | None = None,
    ) -> dict:
        return self.ledger.record_signal(
            symbol=symbol,
            signal_at=signal_at,
            entry_plan=entry_plan or {"method": "next_open", "reference_price": 100.0},
            targets=[{"label": "T1", "price": target}, 120.0],
            stop=stop,
            score_version=score_version,
            score=score,
            features={"technical": {"rsi": 54.2}, "score": score},
            model_reviews=[
                {
                    "provider": "gemini",
                    "model": "flash",
                    "verdict": "watch",
                    "summary": "Structured review only.",
                }
            ],
        )

    def test_record_is_versioned_atomic_safe_and_idempotent(self):
        first = self.record()
        revision = self.ledger.revision
        second = self.record()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.ledger.revision, revision)
        self.assertEqual(first["features_hash"], features_hash(first["features"]))
        self.assertEqual(first["entry_plan"]["method"], "next_open")
        self.assertEqual(first["targets"][0], {"label": "T1", "price": 110.0})
        self.assertEqual(first["model_reviews"][0]["provider"], "gemini")

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["ledger_version"], 1)
        self.assertIn(first["id"], payload["signals"])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_rejects_secrets_from_features_reviews_and_entry_plan(self):
        with self.assertRaisesRegex(ValueError, "secret-like"):
            self.ledger.record_signal(
                symbol="FPT",
                signal_at="2026-08-03",
                entry_plan={"reference_price": 100},
                targets=[110],
                stop=90,
                score_version="v1",
                features={"api_key": "do-not-store"},
            )
        with self.assertRaisesRegex(ValueError, "secret-like"):
            self.ledger.record_signal(
                symbol="FPT",
                signal_at="2026-08-03",
                entry_plan={"reference_price": 100},
                targets=[110],
                stop=90,
                score_version="v1",
                features={"rsi": 50},
                model_reviews=[{"provider": "glm", "token": "do-not-store"}],
            )

    def test_same_candle_is_stop_first_and_uses_next_open(self):
        signal = self.record()
        bars = [
            # The signal-day candle must never become the fill.
            {"date": "2026-08-03", "open": 50, "high": 200, "low": 20, "close": 100},
            ObjectBar("2026-08-04", 100, 112, 89, 105),
        ]
        updated = self.ledger.update_outcome(
            signal["id"],
            bars,
            round_trip_cost_bps=20,
        )
        outcome = updated["outcome"]

        self.assertEqual(outcome["status"], "loss")
        self.assertEqual(outcome["entry_at"], "2026-08-04")
        self.assertEqual(outcome["entry_price"], 100.0)
        self.assertEqual(outcome["exit_price"], 90.0)
        self.assertEqual(outcome["exit_reason"], "stop")
        self.assertEqual(outcome["r_multiple"], -1.02)

    def test_gap_stop_gets_worse_open_and_target_gap_gets_no_favorable_slippage(self):
        loss_signal = self.record("FPT")
        win_signal = self.record("VNM")
        entry_day = {"date": "2026-08-04", "open": 100, "high": 105, "low": 95, "close": 102}

        loss = self.ledger.update_outcome(
            loss_signal["id"],
            [
                entry_day,
                {"date": "2026-08-05", "open": 85, "high": 92, "low": 82, "close": 88},
            ],
            round_trip_cost_bps=0,
        )["outcome"]
        win = self.ledger.update_outcome(
            win_signal["id"],
            [
                entry_day,
                {"date": "2026-08-05", "open": 115, "high": 118, "low": 112, "close": 116},
            ],
            round_trip_cost_bps=0,
        )["outcome"]

        self.assertEqual((loss["status"], loss["exit_reason"], loss["exit_price"]), ("loss", "gap_stop", 85.0))
        self.assertEqual((win["status"], win["exit_reason"], win["exit_price"]), ("win", "target", 110.0))

    def test_entry_open_at_target_is_cancelled_without_negative_win(self):
        signal = self.record()
        outcome = self.ledger.update_outcome(
            signal["id"],
            [ObjectBar("2026-08-04", 115, 118, 112, 116)],
            round_trip_cost_bps=30,
        )["outcome"]

        self.assertEqual(outcome["status"], "timeout")
        self.assertEqual(outcome["exit_reason"], "entry_not_filled")
        self.assertEqual(outcome["entry_sessions_observed"], 1)
        self.assertIsNone(outcome["entry_at"])
        self.assertIsNone(outcome["entry_price"])
        self.assertIsNone(outcome["net_return_pct"])
        self.assertIsNone(outcome["r_multiple"])
        summary = self.ledger.summary()
        self.assertEqual((summary["resolved"], summary["timeout"]), (1, 1))
        self.assertIsNone(summary["expectancy_r"])
        self.assertIsNone(summary["average_net_return_pct"])

    def test_entry_range_can_wait_for_a_valid_later_open(self):
        signal = self.record(
            entry_plan={
                "method": "next_open",
                "reference_price": 100,
                "entry_low": 98,
                "entry_high": 102,
                "max_entry_sessions": 2,
            }
        )
        outcome = self.ledger.update_outcome(
            signal["id"],
            [
                ObjectBar("2026-08-04", 105, 109, 103, 106),
                ObjectBar("2026-08-05", 101, 111, 99, 109),
            ],
            round_trip_cost_bps=0,
        )["outcome"]

        self.assertEqual(outcome["status"], "win")
        self.assertEqual(outcome["entry_sessions_observed"], 2)
        self.assertEqual(outcome["entry_at"], "2026-08-05")
        self.assertEqual(outcome["entry_price"], 101.0)
        self.assertEqual(outcome["exit_price"], 110.0)
        self.assertGreater(outcome["net_return_pct"], 0)

    def test_timeout_uses_nth_close_and_deducts_round_trip_costs(self):
        signal = self.record(target=120, stop=80)
        result = self.ledger.update_outcome(
            signal["id"],
            [
                ObjectBar("2026-08-04", 100, 105, 95, 102),
                ObjectBar("2026-08-05", 103, 106, 94, 104),
                ObjectBar("2026-08-06", 104, 130, 70, 120),
            ],
            timeout_sessions=2,
            round_trip_cost_bps=50,
        )["outcome"]

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["exit_at"], "2026-08-05")
        self.assertEqual(result["exit_price"], 104.0)
        self.assertEqual(result["gross_return_pct"], 4.0)
        self.assertEqual(result["net_return_pct"], 3.5)
        self.assertEqual(result["r_multiple"], 0.175)

    def test_open_can_resolve_later_and_repeated_update_is_noop(self):
        signal = self.record()
        first = self.ledger.update_outcome(
            signal["id"],
            [ObjectBar("2026-08-04", 100, 105, 95, 102)],
            timeout_sessions=5,
            round_trip_cost_bps=0,
        )
        self.assertEqual(first["outcome"]["status"], "open")

        resolved = self.ledger.update_outcome(
            signal["id"],
            [
                ObjectBar("2026-08-04", 100, 105, 95, 102),
                ObjectBar("2026-08-05", 102, 111, 98, 110),
            ],
            timeout_sessions=5,
            round_trip_cost_bps=0,
        )
        self.assertEqual(resolved["outcome"]["status"], "win")
        revision = self.ledger.revision

        repeated = self.ledger.update_outcome(
            signal["id"],
            [
                ObjectBar("2026-08-04", 100, 105, 95, 102),
                ObjectBar("2026-08-05", 102, 111, 98, 110),
                ObjectBar("2026-08-06", 50, 50, 40, 45),
            ],
            timeout_sessions=5,
            round_trip_cost_bps=0,
        )
        self.assertEqual(repeated["outcome"], resolved["outcome"])
        self.assertEqual(self.ledger.revision, revision)

        retried_signal = self.record()
        self.assertEqual(retried_signal["outcome"], resolved["outcome"])
        self.assertEqual(self.ledger.revision, revision)

    def test_signal_date_keeps_source_offset_for_next_session(self):
        signal = self.record(signal_at="2026-08-04T01:00:00+07:00")
        outcome = self.ledger.update_outcome(
            signal["id"],
            [
                UpperObjectBar("2026-08-04", 100, 150, 50, 100),
                UpperObjectBar("2026-08-05T09:00:00+07:00", 100, 111, 95, 109),
            ],
            round_trip_cost_bps=0,
        )["outcome"]

        self.assertEqual(signal["signal_at"], "2026-08-04T01:00:00+07:00")
        self.assertEqual(outcome["entry_at"], "2026-08-05")
        self.assertEqual(outcome["status"], "win")

    def test_summary_reports_live_counts_expectancy_and_score_version_groups(self):
        winner = self.record("FPT", score_version="score.v1")
        loser = self.record("VNM", score_version="score.v1")
        timeout = self.record("HPG", score_version="score.v2", target=120, stop=80)
        self.record("VCB", score_version="score.v2")

        self.ledger.update_outcome(
            winner["id"],
            [ObjectBar("2026-08-04", 100, 111, 95, 110)],
            round_trip_cost_bps=0,
        )
        self.ledger.update_outcome(
            loser["id"],
            [ObjectBar("2026-08-04", 100, 105, 89, 90)],
            round_trip_cost_bps=0,
        )
        self.ledger.update_outcome(
            timeout["id"],
            [ObjectBar("2026-08-04", 100, 105, 95, 102)],
            timeout_sessions=1,
            round_trip_cost_bps=0,
        )

        summary = self.ledger.summary()
        self.assertEqual(
            {key: summary[key] for key in ("resolved", "open", "win", "loss", "timeout")},
            {"resolved": 3, "open": 1, "win": 1, "loss": 1, "timeout": 1},
        )
        self.assertEqual(summary["hit_rate_pct"], 50.0)
        self.assertAlmostEqual(summary["expectancy_r"], (1 - 1 + 0.1) / 3, places=6)
        self.assertAlmostEqual(summary["average_net_return_pct"], (10 - 10 + 2) / 3, places=6)
        self.assertEqual(summary["by_score_version"]["score.v1"]["hit_rate_pct"], 50.0)
        self.assertEqual(summary["by_score_version"]["score.v2"]["timeout"], 1)
        self.assertEqual(summary["by_score_version"]["score.v2"]["open"], 1)

    def test_batch_update_and_add_review_are_idempotent(self):
        signal = self.record()
        review = {"provider": "glm", "model": "glm-test", "verdict": "pass"}
        self.ledger.add_model_review(signal["id"], review)
        revision = self.ledger.revision
        self.ledger.add_model_review(signal["id"], review)
        self.assertEqual(self.ledger.revision, revision)

        records = self.ledger.update_outcomes(
            {
                "fpt": [
                    {"trading_date": "2026-08-04", "o": 100, "h": 111, "l": 95, "c": 110}
                ]
            },
            round_trip_cost_bps=0,
        )
        self.assertEqual(records[0]["outcome"]["status"], "win")
        self.assertEqual(len(records[0]["model_reviews"]), 2)

    def test_threaded_records_leave_valid_json_without_lost_updates(self):
        def write(index: int) -> str:
            record = self.ledger.record_signal(
                symbol=f"S{index:02d}",
                signal_at=f"2026-08-{(index % 9) + 1:02d}T15:{index:02d}:00+07:00",
                entry_plan={"reference_price": 100 + index},
                targets=[120 + index],
                stop=90 + index,
                score_version="thread.v1",
                features={"index": index},
            )
            return record["id"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(write, range(20)))

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len(payload["signals"]), 20)
        self.assertEqual(self.ledger.summary()["total"], 20)

    def test_schema_mismatch_is_rejected(self):
        bad_path = Path(self.directory.name) / "bad.json"
        bad_path.write_text(
            json.dumps({"schema_version": "old", "ledger_version": 0, "signals": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(SignalLedgerError):
            SignalLedger(bad_path)

    def test_evaluate_outcome_public_function_accepts_generic_mapping(self):
        signal = self.record()
        outcome = evaluate_outcome(
            signal,
            [
                {
                    "timestamp": "2026-08-04T09:00:00+07:00",
                    "open": 100,
                    "high": 111,
                    "low": 95,
                    "close": 109,
                }
            ],
            round_trip_cost_bps=0,
        )
        self.assertEqual(outcome["status"], "win")


if __name__ == "__main__":
    unittest.main()
