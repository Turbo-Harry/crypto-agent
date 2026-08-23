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

from tools.evaluate_alt_panel import PANEL, VALIDATION_CUTOFF_TS, evaluate


class AltPanelEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alt_panel_")
        self.db_path = os.path.join(self.tmp.name, "alt_research.db")
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
        features = json.dumps({"factor_features": {"funding_rate": 0.0}})
        for strategy, won in (("A_pullback", True), ("B_breakout", False)):
            for index in range(300):
                symbol = PANEL[index % len(PANEL)]
                signal_id = f"{strategy}-{index}"
                conn.execute(
                    "INSERT INTO signal_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (signal_id, symbol, "long",
                     VALIDATION_CUTOFF_TS + index * 900,
                     100.0, 99.0, 102.0, 1.0, 4, features, strategy))
                conn.execute(
                    "INSERT INTO signal_outcomes VALUES (?,?,?,?,?)",
                    (signal_id, 1 if won else 0, 0 if won else 1, 0,
                     2.0 if won else -1.0))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_strategy_passing_all_validation_gates_is_eligible(self):
        result = evaluate(self.db_path)

        self.assertEqual(result["eligible_strategies"], ["A_pullback"])
        self.assertEqual(result["status"], "eligible_for_model_research")
        self.assertTrue(
            result["strategies"]["A_pullback"]["validation"]["passed"])
        self.assertFalse(
            result["strategies"]["B_breakout"]["validation"]["passed"])
        self.assertFalse(result["execution_authority"])
        self.assertFalse(result["budget_expansion_allowed"])

    def test_runtime_database_name_is_rejected(self):
        runtime_path = os.path.join(self.tmp.name, "crypto_agent.db")
        Path(runtime_path).touch()
        with self.assertRaisesRegex(ValueError, "独立 alt research DB"):
            evaluate(runtime_path)


if __name__ == "__main__":
    unittest.main()
