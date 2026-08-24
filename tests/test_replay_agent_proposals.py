#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CRYPTO_AGENT_MODE", "paper")

import tools.replay_agent_proposals as replay_tool
from decision.agent_proposals import MarketSnapshot, run_proposal_cycle
from decision.signal_outcomes import persist_outcome
from engines.signal_sampling import record_agent_proposal_sample
from tools.evaluate_agent_proposal_replay import evaluate_phase


def _model_for_prompt(prompt):
    payload = json.loads(prompt)
    snapshot = payload["snapshots"][0]
    direction = "long" if int(snapshot["kline_ts"] / 900_000) % 2 else "short"
    return json.dumps({"proposals": [{
        "base": snapshot["base"], "direction": direction,
        "confidence": 0.8, "thesis": "frozen causal fixture",
        "evidence_ids": [snapshot["evidence_ids"][0]],
    }]})


_model_for_prompt.model_version = "fixture-model"


class AgentProposalReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="proposal_replay_")
        self.market_db = os.path.join(self.tmp.name, "market.db")
        self.output_db = os.path.join(self.tmp.name, "research.db")
        self._create_market()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_market(self):
        event_ms = int(replay_tool.TRAIN_START_TS * 1000)
        conn = sqlite3.connect(self.market_db)
        conn.execute(
            "CREATE TABLE klines (inst_id TEXT,bar TEXT,open_time INTEGER,"
            "open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL,"
            "PRIMARY KEY(inst_id,bar,open_time))")
        for base in replay_tool.SYMBOLS:
            inst_id = f"{base}-USDT-SWAP"
            for bar, width in (("15m", 900_000), ("1H", 3_600_000),
                               ("4H", 14_400_000)):
                for index in range(60):
                    ts = event_ms - (60 - index) * width
                    conn.execute(
                        "INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                        (inst_id, bar, ts, 100.0, 101.0, 99.0, 100.0,
                         1000.0 + index, 100_000.0))
            for index in range(240):
                high = 105.0 if index == 0 and base == "BTC" else 101.0
                conn.execute(
                    "INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                    (inst_id, "1m", event_ms + index * 60_000,
                     100.0, high, 99.0, 100.0, 100.0, 10_000.0))
        conn.commit()
        conn.close()

    def test_one_event_replays_idempotently_and_settles_without_authority(self):
        calls = []
        active_prompt_version = replay_tool.config.AGENT_PROPOSAL_PROMPT_VERSION
        active_implementation_version = (
            replay_tool.config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION)
        active_schema_version = replay_tool.config.AGENT_PROPOSAL_SCHEMA_VERSION

        def model(prompt):
            calls.append(prompt)
            payload = json.loads(prompt)
            snapshot = payload["snapshots"][0]
            return json.dumps({"proposals": [{
                "base": snapshot["base"], "direction": "long",
                "confidence": 0.8, "thesis": "fixture trend",
                "evidence_ids": [snapshot["evidence_ids"][0]],
            }]})

        model.model_version = "fixture-model"
        with mock.patch.object(
                replay_tool, "TRAIN_END_TS",
                replay_tool.TRAIN_START_TS + replay_tool.EVENT_STRIDE_SECONDS):
            first = replay_tool.replay(
                self.market_db, self.output_db, "training", model_call=model)
            second = replay_tool.replay(
                self.market_db, self.output_db, "training", model_call=model)

        self.assertEqual(first["runs_created"], 1)
        self.assertEqual(first["settled"], 1)
        self.assertEqual(second["runs_deduplicated"], 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("microstructure",
                         json.loads(calls[0])["snapshots"][0])
        self.assertNotIn("aligned_direction",
                         json.loads(calls[0])["snapshots"][0])
        self.assertNotIn("eligible_candidates", json.loads(calls[0]))
        self.assertEqual(replay_tool.config.AGENT_PROPOSAL_PROMPT_VERSION,
                         active_prompt_version)
        self.assertEqual(
            replay_tool.config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION,
            active_implementation_version)
        self.assertEqual(replay_tool.config.AGENT_PROPOSAL_SCHEMA_VERSION,
                         active_schema_version)
        conn = sqlite3.connect(self.output_db)
        conn.row_factory = sqlite3.Row
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM agent_proposal_runs").fetchone()[0], 1)
        proposal = conn.execute(
            "SELECT execution_authority,prediction_passed FROM agent_proposals"
        ).fetchone()
        outcome = conn.execute(
            "SELECT tp_first,sl_first FROM signal_outcomes").fetchone()
        run = conn.execute(
            "SELECT prompt_version,schema_version FROM agent_proposal_runs"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(proposal), (0, 0))
        self.assertEqual(tuple(outcome), (1, 0))
        self.assertEqual(tuple(run), (replay_tool.FROZEN_PROMPT_VERSION,
                                      replay_tool.FROZEN_SCHEMA_VERSION))
        self.assertEqual(evaluate_phase(
            self.output_db, "training")["status"], "stop_no_promotion")

    def test_validation_cannot_run_before_training_passes(self):
        replay_tool._init_output(Path(self.output_db), self.market_db)
        with self.assertRaisesRegex(ValueError, "validation 保持封存"):
            replay_tool.replay(
                self.market_db, self.output_db, "validation",
                model_call=_model_for_prompt)

    def test_full_synthetic_training_gate_still_has_no_execution_authority(self):
        replay_tool._init_output(Path(self.output_db), self.market_db)
        with replay_tool._frozen_v1_protocol():
            for index in range(100):
                event = replay_tool.TRAIN_START_TS + index * 900
                base = replay_tool.SYMBOLS[index % len(replay_tool.SYMBOLS)]
                kline_ts = int(event * 1000) - 900_000
                evidence = (f"market:{base}:{kline_ts}:15m",)
                snapshot = MarketSnapshot(
                    base=base, kline_ts=kline_ts, reference_entry=100.0,
                    atr=1.0, ema20_15m=101.0, ema50_15m=100.0,
                    momentum_1h=0.01, momentum_4h=0.02, volume_ratio=1.5,
                    evidence_ids=evidence)
                direction = "long" if index % 2 else "short"

                def model(_prompt, *, b=base, d=direction, ev=evidence):
                    return json.dumps({"proposals": [{
                        "base": b, "direction": d, "confidence": 0.8,
                        "thesis": "fixture", "evidence_ids": [ev[0]],
                    }]})

                model.model_version = "fixture-model"
                result = run_proposal_cycle(
                    [snapshot], model_call=model,
                    sample_recorder=lambda **kwargs: record_agent_proposal_sample(
                        **kwargs, db_path=self.output_db),
                    db_path=self.output_db, event_ts=event)
                run = result["run"]
                proposal = result["proposals"][0]
                replay_tool._record_cost(
                    self.output_db, run["run_id"], "training", 1000, 1000,
                    200, 0.0002)
                persist_outcome({
                    "signal_id": proposal["signal_id"], "horizon_hours": 4,
                    "tp_first": 1, "sl_first": 0, "timeout": 0,
                    "ambiguous": 0, "pnl_r": 2.0, "mfe_r": 2.1,
                    "mae_r": 0.1, "high_ret_h": 0.02, "low_ret_h": -0.001,
                    "time_to_tp_sec": 60.0, "time_to_sl_sec": None,
                    "time_to_high_sec": 60.0, "time_to_low_sec": 0.0,
                    "settled_at": event + 4 * 3600, "bar_resolution": "1m",
                    "label_version": "fixture-v1",
                }, db_path=self.output_db)

        result = evaluate_phase(self.output_db, "training")
        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(result["gates"].values()))
        self.assertFalse(result["execution_authority"])
        self.assertFalse(result["budget_expansion_allowed"])


if __name__ == "__main__":
    unittest.main()
