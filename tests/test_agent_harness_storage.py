import tempfile
import unittest
import sqlite3

from decision.agent_contracts import (
    AgentInput,
    AgentStep,
    FinalAction,
    HarnessRun,
    LifecycleStatus,
    RuntimeStatus,
    StepStatus,
    StepType,
    Verdict,
)
from storage import agent_harness


class AgentHarnessStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.input = AgentInput(
            run_id="run-1", signal_id="signal-1", event_ts="1", kline_ts="1",
            strategy_version="strategy-v1", prompt_version="judge-v1",
            model_version="model-v1", context_version="context-v1",
            schema_version="schema-v1", retrieval_version="retrieval-v1",
            tool_policy_version="tools-v1", pricing_version="price-v1",
            signal={"entry": 100, "stop": 99},
            account={"equity_usdt": 1000, "risk_per_trade": .01,
                     "max_notional_per_trade_usdt": 150},
        )

    def tearDown(self):
        import os
        os.unlink(self.tmp.name)

    def test_run_is_idempotent_and_steps_are_traceable(self):
        run = HarnessRun(
            run_id="run-1", signal_id="signal-1",
            runtime_status=RuntimeStatus.TIMEOUT,
            final_action=FinalAction.BASELINE_PASS,
            error_type="timeout",
        )
        first = agent_harness.record_run(run, self.input, db_path=self.tmp.name)
        second = agent_harness.record_run(run, self.input, db_path=self.tmp.name)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertIn('"equity_usdt":1000', first["input_snapshot"])
        self.assertEqual(first["evidence_hash"], self.input.evidence_hash)
        self.assertEqual(len(agent_harness.list_runs(db_path=self.tmp.name)), 1)
        agent_harness.record_step(AgentStep(
            run_id="run-1", step_no=1, step_type=StepType.MODEL,
            status=StepStatus.FAILED, started_at="1", finished_at="2",
            error_type="timeout", fallback_action="baseline_pass",
        ), db_path=self.tmp.name)
        self.assertEqual(agent_harness.get_run("run-1", db_path=self.tmp.name)["signal_id"], "signal-1")

    def test_evaluation_can_start_pending(self):
        agent_harness.record_evaluation("run-1", lifecycle_status=LifecycleStatus.PENDING,
                                        db_path=self.tmp.name)
        from storage import db
        row = db.q1("SELECT lifecycle_status FROM agent_evaluations WHERE run_id=?",
                    ["run-1"], db_path=self.tmp.name)
        self.assertEqual(row["lifecycle_status"], "pending")

    def test_signal_outcome_matures_evaluation_and_retry_cannot_reset_it(self):
        from decision.signal_outcomes import persist_outcome
        from engines.signal_sampling import record_signal_sample
        from storage import db

        signal_id, _ = record_signal_sample(
            "BTC", {"dir": "long", "kline_ts": 1_700_000_000_000,
                    "entry": 100, "stop": 99, "tp": 102, "atr": 1,
                    "shadow_dims": {}}, "swap", db_path=self.tmp.name,
            event_ts=1_700_000_900)
        agent_input = AgentInput(
            run_id="run-path", signal_id=signal_id, event_ts="1700000900",
            kline_ts="1700000000000", strategy_version="strategy-v1",
            prompt_version="judge-v1", model_version="model-v1",
            context_version="context-v1", schema_version="schema-v1",
            retrieval_version="retrieval-v1",
        )
        run = HarnessRun(
            run_id="run-path", signal_id=signal_id,
            runtime_status=RuntimeStatus.COMPLETED,
            final_action=FinalAction.SHADOW_REJECT,
            model_verdict=Verdict.REJECT,
            input_tokens=120, output_tokens=20,
            prompt_cache_hit_tokens=20, prompt_cache_miss_tokens=100,
            pricing_version="price-v1", estimated_cost=.0001,
            risk_probability=.8, confidence=.9,
            reason_codes=("liquidity_failure",),
            evidence_ids=("market:1",),
            missing_information=("orderflow",),
            decision_reason="thin book",
        )
        agent_harness.record_run(run, agent_input, created_ts=1,
                                 db_path=self.tmp.name)
        stored_run = db.q1("SELECT * FROM agent_runs WHERE run_id=?",
                           [run.run_id], db_path=self.tmp.name)
        self.assertEqual(stored_run["evidence_ids"], '["market:1"]')
        self.assertEqual(stored_run["input_tokens"], 120)
        self.assertEqual(stored_run["pricing_version"], "price-v1")
        agent_harness.record_evaluation(
            run.run_id, lifecycle_status=LifecycleStatus.PENDING,
            db_path=self.tmp.name)
        persist_outcome({
            "signal_id": signal_id, "horizon_hours": 4,
            "tp_first": 0, "sl_first": 1, "timeout": 0, "ambiguous": 0,
            "pnl_r": -1.0, "mfe_r": 0.2, "mae_r": 1.0,
            "high_ret_h": 0.002, "low_ret_h": -0.01,
            "time_to_tp_sec": None, "time_to_sl_sec": 60,
            "time_to_high_sec": 30, "time_to_low_sec": 60,
            "settled_at": 2, "bar_resolution": "1m",
            "label_version": "first-passage-15m-4h-v1",
        }, db_path=self.tmp.name)
        row = db.q1("SELECT * FROM agent_evaluations WHERE run_id=?",
                    [run.run_id], db_path=self.tmp.name)
        self.assertEqual(row["lifecycle_status"], "mature")
        self.assertEqual(row["label"], "sl_first")
        self.assertEqual(row["saved_loss"], 1.0)
        self.assertEqual(row["missed_profit"], 0.0)
        self.assertEqual(row["incremental_ev"], 1.0)

        agent_harness.record_evaluation(
            run.run_id, lifecycle_status=LifecycleStatus.PENDING,
            db_path=self.tmp.name)
        self.assertEqual(db.q1(
            "SELECT lifecycle_status FROM agent_evaluations WHERE run_id=?",
            [run.run_id], db_path=self.tmp.name)["lifecycle_status"], "mature")

    def test_divergent_v14_database_is_reconciled(self):
        """曾占用相同版本号的两条分支都必须能升级到完整 schema。"""
        from storage import db

        db.init_db(self.tmp.name)
        with sqlite3.connect(self.tmp.name) as conn:
            conn.execute("DROP TABLE factor_trials")
            conn.execute(
                "CREATE TABLE factor_trials ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
            )
            conn.execute("DROP TABLE forecast_calibration")
            conn.execute(
                "CREATE TABLE forecast_calibration ("
                "trade_id TEXT PRIMARY KEY, ts REAL, p_hit_tp REAL)"
            )
            # 模拟旧 Harness 分支已把 user_version 推到 14，但没有研究分支
            # 同号迁移增加的因子证据列和预测标签列。
            conn.execute("PRAGMA user_version=14")

        db.init_db(self.tmp.name)
        factor_columns = {
            row["name"] for row in db.q(
                "PRAGMA table_info(factor_trials)", db_path=self.tmp.name)
        }
        forecast_columns = {
            row["name"] for row in db.q(
                "PRAGMA table_info(forecast_calibration)", db_path=self.tmp.name)
        }
        tables = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='table'",
                db_path=self.tmp.name,
            )
        }
        version = db.q1("PRAGMA user_version", db_path=self.tmp.name)[
            "user_version"
        ]
        memory_columns = {
            row["name"] for row in db.q(
                "PRAGMA table_info(agent_memories)", db_path=self.tmp.name)
        }

        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertTrue(
            {"dsr", "pbo", "details", "timeframe", "horizon_hours",
             "trial_key", "data_hash", "evaluation_version"}
            <= factor_columns
        )
        self.assertTrue(
            {"signal_id", "p_timeout", "timeout", "label_version"}
            <= forecast_columns
        )
        self.assertTrue(
            {"signal_samples", "model_artifacts", "agent_runs"} <= tables
        )
        self.assertIn("outcome_r", memory_columns)
        run_columns = {
            row["name"] for row in db.q(
                "PRAGMA table_info(agent_runs)", db_path=self.tmp.name)
        }
        self.assertTrue({"risk_probability", "reason_codes", "input_snapshot",
                         "tool_policy_version", "pricing_version", "confidence",
                         "evidence_ids", "missing_information",
                         "evidence_hash",
                         "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                        <= run_columns)


if __name__ == "__main__":
    unittest.main()
