"""Causal confirmation, path, cost, and sealed-stage tests for B research."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from tools.evaluate_strategy_b_confirmation import (
    BAR_15M_MS, MINUTE_MS, _candidate_rows, confirm_candidate,
    confirm_failed_breakout, evaluate, resolve_trade, summarize)


class StrategyBConfirmationTest(unittest.TestCase):
    def test_confirmation_requires_bull_body_and_close_above_breakout(self):
        event_ms = 1_700_000_000_000
        candidate = {"event_ts": event_ms / 1000, "entry": 100.5, "atr": 1.0}
        accepted = confirm_candidate(
            candidate, (event_ms, 100.0, 101.2, 99.8, 101.0, 10.0))
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["entry_event_ms"], event_ms + BAR_15M_MS)
        self.assertIsNone(confirm_candidate(
            candidate, (event_ms, 101.0, 101.2, 99.8, 100.8, 10.0)))
        self.assertIsNone(confirm_candidate(
            candidate, (event_ms, 100.0, 100.5, 99.8, 100.4, 10.0)))
        self.assertIsNone(confirm_candidate(
            candidate, (event_ms + BAR_15M_MS, 100, 102, 99, 101, 10)))

    def test_entry_is_after_confirmation_and_same_minute_tie_is_stop_first(self):
        event_ms = 1_700_000_000_000
        delayed = {
            "symbol": "BTC", "event_ts": event_ms / 1000,
            "entry": 100.5, "atr": 1.0,
            "entry_event_ms": event_ms + BAR_15M_MS,
        }
        bars = []
        for index in range(240):
            open_time = delayed["entry_event_ms"] + index * MINUTE_MS
            if index == 0:
                bars.append((open_time, 101.0, 103.2, 99.8, 101.0, 1.0))
            else:
                bars.append((open_time, 101.0, 101.1, 100.9, 101.0, 1.0))
        row = resolve_trade(delayed, bars, funding_rate=0.0001)
        self.assertIsNotNone(row)
        self.assertEqual(row["entry"], 101.0)
        self.assertEqual(row["outcome"], "sl")
        self.assertAlmostEqual(row["gross_r"], -1.0)
        self.assertGreater(row["cost_r"], 0)
        self.assertLess(row["net_r"], -1.0)

    def test_failed_breakout_reverses_both_source_directions(self):
        event_ms = 1_700_000_000_000
        failed_long = confirm_failed_breakout({
            "event_ts": event_ms / 1000, "entry": 100.5, "atr": 1.0,
            "direction": "long",
        }, (event_ms, 101.0, 101.2, 99.8, 100.0, 10.0))
        self.assertEqual(failed_long["trade_direction"], "short")
        failed_short = confirm_failed_breakout({
            "event_ts": event_ms / 1000, "entry": 99.5, "atr": 1.0,
            "direction": "short",
        }, (event_ms, 99.0, 100.2, 98.8, 100.0, 10.0))
        self.assertEqual(failed_short["trade_direction"], "long")
        self.assertIsNone(confirm_failed_breakout({
            "event_ts": event_ms / 1000, "entry": 100.5, "atr": 1.0,
            "direction": "long",
        }, (event_ms, 100.0, 101.2, 99.8, 101.0, 10.0)))

    def test_failed_long_breakout_short_trade_uses_mirrored_geometry(self):
        event_ms = 1_700_000_000_000
        delayed = {
            "entry_event_ms": event_ms, "atr": 1.0,
            "trade_direction": "short",
        }
        bars = []
        for index in range(240):
            open_time = event_ms + index * MINUTE_MS
            if index == 0:
                bars.append((open_time, 100.0, 101.2, 97.8, 100.0, 1.0))
            else:
                bars.append((open_time, 100.0, 100.1, 99.9, 100.0, 1.0))
        row = resolve_trade(delayed, bars, funding_rate=0.0001)
        self.assertEqual(row["direction"], "short")
        self.assertEqual(row["stop"], 101.0)
        self.assertEqual(row["tp"], 98.0)
        self.assertEqual(row["outcome"], "sl")
        self.assertAlmostEqual(row["gross_r"], -1.0)

    def test_candidate_query_preserves_source_direction_for_reversal(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE signal_samples (signal_id TEXT,symbol TEXT,"
            "direction TEXT,event_ts REAL,entry REAL,atr REAL,"
            "strategy_id TEXT,timeframe TEXT,horizon_hours INTEGER)")
        conn.executemany(
            "INSERT INTO signal_samples VALUES (?,?,?,?,?,?,?,?,?)", [
                ("long-1", "BTC", "long", 1, 100, 1, "B_breakout", "15m", 4),
                ("short-1", "BTC", "short", 2, 100, 1, "B_breakout", "15m", 4),
            ])
        rows = _candidate_rows(
            conn, ("BTC",), directions=("long", "short"))
        conn.close()
        self.assertEqual([row["direction"] for row in rows], ["long", "short"])

    def test_incomplete_path_is_rejected(self):
        event_ms = 1_700_000_000_000
        delayed = {"entry_event_ms": event_ms, "atr": 1.0}
        bars = [(event_ms + index * MINUTE_MS, 100, 101, 99, 100, 1)
                for index in range(239)]
        self.assertIsNone(resolve_trade(delayed, bars))

    def test_summary_requires_lower_bound_folds_and_concentration(self):
        rows = []
        for index in range(100):
            rows.append({
                "entry_event_ms": 1_700_000_000_000 + index * BAR_15M_MS,
                "symbol": "BTC" if index % 2 == 0 else "ETH",
                "outcome": "tp", "gross_r": 2.0, "net_r": 1.8,
            })
        result = summarize(
            rows, stats={}, folds=5, min_n=100, min_positive_folds=4,
            max_symbol_concentration=0.5)
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["positive_folds"], 5)
        self.assertGreater(result["net_ev_lower_95"], 0)

    def test_development_failure_keeps_later_stages_sealed(self):
        with tempfile.TemporaryDirectory() as folder:
            market_path = os.path.join(folder, "market-research.db")
            replay_path = os.path.join(folder, "replay-research.db")
            market = sqlite3.connect(market_path)
            market.execute(
                "CREATE TABLE klines (inst_id TEXT,bar TEXT,open_time INTEGER,"
                "open REAL,high REAL,low REAL,close REAL,volume REAL,"
                "quote_volume REAL,PRIMARY KEY(inst_id,bar,open_time))")
            market.execute(
                "CREATE TABLE funding_rates (inst_id TEXT,funding_time INTEGER,"
                "funding_rate REAL,realized_rate REAL)")
            market.commit()
            market.close()
            replay = sqlite3.connect(replay_path)
            replay.execute("CREATE TABLE kv (key TEXT PRIMARY KEY,value TEXT)")
            replay.execute(
                "CREATE TABLE signal_samples (signal_id TEXT,symbol TEXT,"
                "direction TEXT,event_ts REAL,entry REAL,atr REAL,"
                "strategy_id TEXT,timeframe TEXT,horizon_hours INTEGER)")
            replay.execute(
                "INSERT INTO kv VALUES (?,?)",
                ("research.15m_replay.latest", json.dumps({
                    "research_only": True, "market_db": market_path})))
            replay.commit()
            replay.close()
            result = evaluate(replay_path, market_path)
            self.assertEqual(result["verdict"], "stop_no_promotion")
            self.assertEqual(result["late_validation"]["status"],
                             "sealed_not_opened")
            self.assertEqual(result["holdout"]["status"],
                             "sealed_not_opened")
            self.assertFalse(result["execution_authority"])
            self.assertFalse(result["budget_expansion_allowed"])

    def test_market_must_match_replay_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            replay_path = os.path.join(folder, "replay-research.db")
            market_path = os.path.join(folder, "market-research.db")
            other_path = os.path.join(folder, "other-market.db")
            sqlite3.connect(market_path).close()
            sqlite3.connect(other_path).close()
            replay = sqlite3.connect(replay_path)
            replay.execute("CREATE TABLE kv (key TEXT PRIMARY KEY,value TEXT)")
            replay.execute(
                "INSERT INTO kv VALUES (?,?)",
                ("research.15m_replay.latest", json.dumps({
                    "research_only": True, "market_db": other_path})))
            replay.commit()
            replay.close()
            with self.assertRaisesRegex(ValueError, "does not match"):
                evaluate(replay_path, market_path)


if __name__ == "__main__":
    unittest.main()
