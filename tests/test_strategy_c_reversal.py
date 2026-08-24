"""Strategy C research rule, path ordering, cost, and sealed holdout tests."""

import os
import sqlite3
import tempfile
import unittest

from tools.evaluate_strategy_c_reversal import (
    MINUTE_MS, candidate_direction, evaluate, resolve_candidate, summarize)


class StrategyCReversalTest(unittest.TestCase):
    def test_candidate_rule_requires_extreme_band_wick_and_low_adx(self):
        long_bar = (0, 100.0, 100.2, 95.5, 98.0)
        self.assertEqual(candidate_direction(
            long_bar, rsi=24, atr=1.5, adx=19,
            bb_lower=98.5, bb_upper=102), "long")
        self.assertIsNone(candidate_direction(
            long_bar, rsi=26, atr=1.5, adx=19,
            bb_lower=98.5, bb_upper=102))
        self.assertIsNone(candidate_direction(
            long_bar, rsi=24, atr=1.5, adx=21,
            bb_lower=98.5, bb_upper=102))
        short_bar = (0, 100.0, 104.5, 99.8, 102.0)
        self.assertEqual(candidate_direction(
            short_bar, rsi=76, atr=1.5, adx=20,
            bb_lower=98, bb_upper=101.5), "short")

    def test_same_minute_tie_is_stop_first_and_cost_is_deducted(self):
        start = 1_700_000_000_000
        bars = []
        for index in range(240):
            open_time = start + index * MINUTE_MS
            if index == 0:
                bars.append((open_time, 100.0, 102.2, 98.8, 100.0))
            else:
                bars.append((open_time, 100.0, 100.1, 99.9, 100.0))
        row = resolve_candidate({
            "symbol": "BTC", "inst_id": "BTC-USDT-SWAP",
            "event_ms": start, "direction": "long", "atr": 1.0,
            "rsi": 24.0, "adx": 19.0, "funding_rate": 0.0001,
        }, bars)
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "sl")
        self.assertAlmostEqual(row["gross_r"], -1.0)
        self.assertGreater(row["cost_r"], 0)
        self.assertLess(row["net_r"], -1.0)

    def test_summary_gate_uses_folds_lower_bound_and_concentration(self):
        start = 1_700_000_000_000
        rows = []
        for index in range(100):
            rows.append({
                "event_ms": start + index * 15 * MINUTE_MS,
                "symbol": "BTC" if index % 2 == 0 else "ETH",
                "outcome": "tp", "gross_r": 2.0, "net_r": 1.8,
            })
        result = summarize(rows, candidates=100, missing_path=0)
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["positive_folds"], 5)
        self.assertGreater(result["net_ev_lower_95"], 0)
        self.assertEqual(result["symbol_concentration"], 0.5)

    def test_development_failure_keeps_holdout_sealed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "research.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE klines (inst_id TEXT,bar TEXT,open_time INTEGER,"
                "open REAL,high REAL,low REAL,close REAL,volume REAL,"
                "quote_volume REAL,PRIMARY KEY(inst_id,bar,open_time))")
            conn.execute(
                "CREATE TABLE funding_rates (inst_id TEXT,funding_time INTEGER,"
                "funding_rate REAL,realized_rate REAL,"
                "PRIMARY KEY(inst_id,funding_time))")
            conn.execute(
                "CREATE TABLE klines_v2 (source TEXT,venue TEXT,time_zone TEXT,"
                "inst_id TEXT,bar TEXT,open_time INTEGER,close_time INTEGER,"
                "open REAL,high REAL,low REAL,close REAL,volume REAL,"
                "quote_volume REAL,confirmed INTEGER,ingested_at REAL,"
                "as_of_ms INTEGER,raw_hash TEXT)")
            conn.commit()
            conn.close()
            result = evaluate(path)
            self.assertEqual(
                result["market_input_version"],
                "confirmed-klines-v2-preferred")
            self.assertEqual(result["market_table"], "klines_v2")
            self.assertEqual(result["verdict"], "stop_no_promotion")
            self.assertEqual(result["holdout"]["status"], "sealed_not_opened")
            self.assertFalse(result["execution_authority"])
            self.assertFalse(result["budget_expansion_allowed"])


if __name__ == "__main__":
    unittest.main()
