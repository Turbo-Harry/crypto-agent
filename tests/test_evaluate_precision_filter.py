#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CRYPTO_AGENT_MODE", "paper")

from tools.evaluate_precision_filter import (CUTOFF_TS, DEVELOPMENT_SYMBOLS,
                                             HOLDOUT_SYMBOLS, evaluate)


class PrecisionFilterEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="precision_filter_")
        self.db_path = os.path.join(self.tmp.name, "research.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
            CREATE TABLE signal_samples (
                signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT,
                event_ts REAL, entry REAL, stop REAL, tp REAL, atr REAL,
                horizon_hours INTEGER, features TEXT, strategy_id TEXT
            );
            CREATE TABLE signal_outcomes (
                signal_id TEXT PRIMARY KEY, tp_first INTEGER, sl_first INTEGER,
                timeout INTEGER, pnl_r REAL
            );
        """)
        conn.execute("INSERT INTO kv VALUES (?,?,?)", (
            "research.15m_replay.latest",
            json.dumps({"research_only": True, "version": "fixture"}), 0))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_winners(self, symbols, count, start_ts):
        conn = sqlite3.connect(self.db_path)
        features = json.dumps({"factor_features": {
            "adx": 0.30, "bb_width_percentile": 0.10,
            "funding_rate": 0.0,
        }})
        for index in range(count):
            symbol = symbols[index % len(symbols)]
            signal_id = f"{symbol}-{start_ts}-{index}"
            conn.execute(
                "INSERT INTO signal_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (signal_id, symbol, "short", start_ts + index * 900,
                 100.0, 101.0, 98.0, 1.0, 4, features, "A_pullback"))
            conn.execute(
                "INSERT INTO signal_outcomes VALUES (?,?,?,?,?)",
                (signal_id, 1, 0, 0, 2.0))
        conn.commit()
        conn.close()

    def test_failed_late_development_keeps_symbol_holdout_sealed(self):
        self._insert_winners(DEVELOPMENT_SYMBOLS, 48, CUTOFF_TS)
        self._insert_winners(HOLDOUT_SYMBOLS, 40, CUTOFF_TS + 100_000)

        result = evaluate(self.db_path)

        self.assertEqual(result["status"], "stop_no_promotion")
        self.assertFalse(result["late_development"]["passed"])
        self.assertEqual(result["late_development"]["n"], 48)
        self.assertEqual(result["holdout"], {"status": "sealed_not_opened"})
        self.assertFalse(result["execution_authority"])
        self.assertFalse(result["budget_expansion_allowed"])

    def test_all_frozen_gates_can_only_recommend_separate_shadow_review(self):
        self._insert_winners(DEVELOPMENT_SYMBOLS, 64, CUTOFF_TS)
        self._insert_winners(HOLDOUT_SYMBOLS, 40, CUTOFF_TS + 100_000)

        result = evaluate(self.db_path)

        self.assertTrue(result["late_development"]["passed"])
        self.assertEqual(result["holdout"]["status"], "opened_passed")
        self.assertTrue(
            result["combined_validation"]["wilson_exceeds_median_breakeven"])
        self.assertEqual(
            result["status"], "eligible_for_separate_paper_shadow_review")
        self.assertFalse(result["execution_authority"])
        self.assertFalse(result["budget_expansion_allowed"])


if __name__ == "__main__":
    unittest.main()
