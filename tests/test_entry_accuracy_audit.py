"""15m 开仓准确率计划统计门的只读、scope 与防冒充测试。"""

import json
import os
import sqlite3
import tempfile
import unittest

import config
from storage import db
from tools.entry_accuracy_audit import audit_status


class EntryAccuracyAuditTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()
        db.init_db(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def _insert_candidates(self, n=300):
        samples, outcomes = [], []
        for idx in range(n):
            signal_id = f"sig-{idx:04d}"
            event_ts = 1_700_000_000 + idx * 900
            samples.append((
                signal_id, ("BTC", "ETH", "SOL")[idx % 3],
                "long" if idx % 2 else "short", event_ts,
                int(event_ts * 1000), config.SIGNAL_SAMPLE_TIMEFRAME,
                "swap", config.ENTRY_STRATEGY_VERSION, "cfg", "features-v1",
                100.0, 99.0, 102.0, 1.0,
                config.SIGNAL_OUTCOME_HORIZON_HOURS,
                .5, .5, .5, .5, .5, .5, "{}", event_ts, event_ts))
            label = idx % 3
            outcomes.append((
                signal_id, config.SIGNAL_OUTCOME_HORIZON_HOURS,
                int(label == 0), int(label == 1), int(label == 2), 0,
                2.0 if label == 0 else -1.0 if label == 1 else 0.0,
                2.1, -1.0, .02, -.01, event_ts + 4 * 3600, "1m", "path-v1"))
        with db.tx(db_path=self.path) as conn:
            conn.executemany(
                "INSERT INTO signal_samples (signal_id,symbol,direction,event_ts,"
                "kline_ts,timeframe,venue,strategy_version,config_hash,"
                "feature_schema_version,entry,stop,tp,atr,horizon_hours,wick,depth,"
                "trend,volume,funding,book,features,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", samples)
            conn.executemany(
                "INSERT INTO signal_outcomes (signal_id,horizon_hours,tp_first,"
                "sl_first,timeout,ambiguous,pnl_r,mfe_r,mae_r,high_ret_h,low_ret_h,"
                "settled_at,bar_resolution,label_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", outcomes)

    def test_empty_runtime_is_explicitly_incomplete(self):
        result = audit_status(self.path)
        self.assertFalse(result["statistically_complete"])
        self.assertEqual(result["counts"]["raw_candidate_snapshots"], 0)
        self.assertEqual(result["counts"]["duplicate_version_snapshots"], 0)
        self.assertEqual(result["counts"]["paper_closed"], 0)
        self.assertFalse(result["gates"]["candidate_training_sample"]["passed"])
        self.assertTrue(result["gates"]["budget_lock_safe"]["passed"])
        self.assertTrue(result["research_only_samples_do_not_count_as_paper"])

    def test_sealed_wal_backup_is_readable_without_sidecar_writes(self):
        backup_dir = tempfile.mkdtemp(prefix="entry_audit_backup_")
        backup_path = os.path.join(backup_dir, "snapshot.db")
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with open(backup_path, "rb") as handle:
            before = handle.read()
        result = audit_status(backup_path)
        with open(backup_path, "rb") as handle:
            after = handle.read()
        self.assertFalse(result["statistically_complete"])
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(backup_path + "-wal"))
        self.assertFalse(os.path.exists(backup_path + "-shm"))

    def test_historical_candidates_do_not_impersonate_paper_closes(self):
        self._insert_candidates()
        result = audit_status(self.path)
        self.assertTrue(result["gates"]["candidate_training_sample"]["passed"])
        self.assertTrue(result["gates"]["tp_class_sample"]["passed"])
        self.assertTrue(result["gates"]["sl_class_sample"]["passed"])
        self.assertEqual(result["counts"]["six_dim_outcomes"], 300)
        self.assertEqual(result["counts"]["paper_closed"], 0)
        self.assertFalse(result["gates"]["paper_closed"]["passed"])
        self.assertFalse(result["statistically_complete"])

    def test_breakout_shadow_does_not_inflate_pullback_training_gate(self):
        self._insert_candidates(299)
        event_ts = 1_800_000_000
        with db.tx(db_path=self.path) as conn:
            conn.execute(
                "INSERT INTO signal_samples (signal_id,symbol,direction,event_ts,"
                "kline_ts,timeframe,venue,strategy_id,strategy_version,config_hash,"
                "feature_schema_version,entry,stop,tp,atr,horizon_hours,features,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("breakout-only", "BTC", "long", event_ts,
                 int(event_ts * 1000), config.SIGNAL_SAMPLE_TIMEFRAME, "swap",
                 config.BREAKOUT_SIGNAL_STRATEGY_ID, "breakout-v1", "cfg-b",
                 config.SIGNAL_FEATURE_SCHEMA_VERSION, 100.0, 99.0, 102.0, 1.0,
                 config.SIGNAL_OUTCOME_HORIZON_HOURS, "{}", event_ts, event_ts))
            conn.execute(
                "INSERT INTO signal_outcomes (signal_id,horizon_hours,tp_first,"
                "sl_first,timeout,ambiguous,pnl_r,mfe_r,mae_r,high_ret_h,low_ret_h,"
                "settled_at,bar_resolution,label_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("breakout-only", config.SIGNAL_OUTCOME_HORIZON_HOURS,
                 1, 0, 0, 0, 2.0, 2.0, 0.2, .02, -.01,
                 event_ts + 4 * 3600, "1m", "path-v1"))
        result = audit_status(self.path)
        self.assertEqual(result["counts"]["candidates"], 299)
        self.assertEqual(result["counts"]["outcomes"], 299)
        self.assertFalse(result["gates"]["candidate_training_sample"]["passed"])

    def test_config_version_duplicate_does_not_inflate_training_gate(self):
        self._insert_candidates(299)
        # 与 sig-0000 是同一策略/币/方向/15m K，只是配置版本更新。
        # 原始审计轨迹保留两行，但统计与训练只能算一个自然机会。
        event_ts = 1_800_000_000
        with db.tx(db_path=self.path) as conn:
            conn.execute(
                "INSERT INTO signal_samples (signal_id,symbol,direction,event_ts,"
                "kline_ts,timeframe,venue,strategy_version,config_hash,"
                "feature_schema_version,entry,stop,tp,atr,horizon_hours,features,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sig-0000-new-config", "BTC", "short", event_ts,
                 1_700_000_000_000, config.SIGNAL_SAMPLE_TIMEFRAME, "swap",
                 "new-config-version", "new-config-hash",
                 config.SIGNAL_FEATURE_SCHEMA_VERSION, 100.0, 99.0, 102.0, 1.0,
                 config.SIGNAL_OUTCOME_HORIZON_HOURS, "{}", event_ts, event_ts))
            conn.execute(
                "INSERT INTO signal_outcomes (signal_id,horizon_hours,tp_first,"
                "sl_first,timeout,ambiguous,pnl_r,mfe_r,mae_r,high_ret_h,low_ret_h,"
                "settled_at,bar_resolution,label_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sig-0000-new-config",
                 config.SIGNAL_OUTCOME_HORIZON_HOURS, 1, 0, 0, 0,
                 2.0, 2.0, -.2, .02, -.01,
                 event_ts + 4 * 3600, "1m", "path-v1"))
        raw_n = db.q1("SELECT COUNT(*) n FROM signal_samples",
                      db_path=self.path)["n"]
        result = audit_status(self.path)
        self.assertEqual(raw_n, 300)
        self.assertEqual(result["counts"]["raw_candidate_snapshots"], 300)
        self.assertEqual(result["counts"]["duplicate_version_snapshots"], 1)
        self.assertEqual(result["counts"]["candidates"], 299)
        self.assertEqual(result["counts"]["outcomes"], 299)
        self.assertFalse(result["gates"]["candidate_training_sample"]["passed"])

    def test_breakout_evidence_does_not_complete_pullback_factor_or_model_gates(self):
        with db.tx(db_path=self.path) as conn:
            conn.execute(
                "INSERT INTO factor_trials (ts,name,strategy_id,status,timeframe,"
                "horizon_hours) VALUES (?,?,?,?,?,?)",
                (1, "b-only-factor", config.BREAKOUT_SIGNAL_STRATEGY_ID,
                 "validated", config.SIGNAL_SAMPLE_TIMEFRAME,
                 config.SIGNAL_OUTCOME_HORIZON_HOURS))
            conn.executemany(
                "INSERT INTO model_artifacts (model_id,model_type,strategy_id,"
                "version,state,created_at,feature_names,artifact,metrics) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [("b-entry", "entry_probability",
                  config.BREAKOUT_SIGNAL_STRATEGY_ID, "v1", "kept", 1,
                  "[]", "{}", "{}"),
                 ("b-extrema", "extrema",
                  config.BREAKOUT_SIGNAL_STRATEGY_ID, "v1", "accepted", 2,
                  "[]", "{}", "{}")])
        result = audit_status(self.path)
        self.assertEqual(result["counts"]["validated_factors"], 0)
        self.assertFalse(result["gates"]["validated_factor"]["passed"])
        self.assertFalse(result["gates"]["entry_model_observed"]["passed"])
        self.assertFalse(result["gates"]["extrema_model_observed"]["passed"])
        self.assertEqual(result["scope"]["strategy_id"],
                         config.ENTRY_SIGNAL_STRATEGY_ID)

    def test_trade_and_agent_evidence_are_isolated_by_strategy(self):
        complete_dims = json.dumps({name: .5 for name in config.SHADOW_DIMS})
        agent_metrics = json.dumps({
            "n": config.AGENT_EVAL_MIN_VALID,
            "reject_n": config.AGENT_EVAL_MIN_REJECT,
            "incremental_ev_lower_bound": .1,
            "max_segment_share": .5,
        })
        with db.tx(db_path=self.path) as conn:
            conn.executemany(
                "INSERT INTO trades (id,symbol,strategy_id,status,"
                "strategy_timeframe,max_hold_hours,shadow_dims) "
                "VALUES (?,?,?,?,?,?,?)",
                [(f"a-trade-{idx}", "BTC",
                  config.ENTRY_SIGNAL_STRATEGY_ID, "closed",
                  config.SIGNAL_SAMPLE_TIMEFRAME,
                  config.SIGNAL_OUTCOME_HORIZON_HOURS, complete_dims)
                 for idx in range(59)] +
                [("b-trade", "BTC", config.BREAKOUT_SIGNAL_STRATEGY_ID,
                  "closed", config.SIGNAL_SAMPLE_TIMEFRAME,
                  config.SIGNAL_OUTCOME_HORIZON_HOURS, complete_dims)])
            conn.execute(
                "INSERT INTO agent_versions "
                "(version,strategy_id,role,status,created_ts,metrics_json) "
                "VALUES (?,?,?,?,?,?)",
                ("b-agent", config.BREAKOUT_SIGNAL_STRATEGY_ID,
                 "challenger", "validated", 1, agent_metrics))

        pullback = audit_status(
            self.path, strategy_id=config.ENTRY_SIGNAL_STRATEGY_ID)
        breakout = audit_status(
            self.path, strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID)
        self.assertEqual(pullback["counts"]["paper_closed"], 59)
        self.assertEqual(breakout["counts"]["paper_closed"], 1)
        self.assertIsNone(pullback["agent_version"])
        self.assertEqual(breakout["agent_version"]["version"], "b-agent")
        self.assertIn(config.BREAKOUT_SIGNAL_STRATEGY_ID,
                      breakout["scope"]["strategy_version"])
        self.assertNotEqual(pullback["scope"]["strategy_version"],
                            breakout["scope"]["strategy_version"])
        self.assertFalse(pullback["gates"]["paper_closed"]["passed"])
        self.assertFalse(breakout["gates"]["paper_closed"]["passed"])

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            audit_status(self.path, strategy_id="unknown")

    def test_all_statistical_gates_can_be_proven_without_writes(self):
        self._insert_candidates()
        complete_dims = json.dumps({name: .5 for name in config.SHADOW_DIMS})
        with db.tx(db_path=self.path) as conn:
            conn.executemany(
                "INSERT INTO trades (id,symbol,status,strategy_timeframe,"
                "max_hold_hours,shadow_dims) VALUES (?,?,?,?,?,?)",
                [(f"trade-{idx}", "BTC", "closed",
                  config.SIGNAL_SAMPLE_TIMEFRAME,
                  config.SIGNAL_OUTCOME_HORIZON_HOURS,
                  complete_dims if idx < 30 else "{}") for idx in range(60)])
            conn.executemany(
                "INSERT INTO forecast_calibration (trade_id,signal_id,ts,p_hit_tp,"
                "p_hit_sl,hit_tp,hit_sl,pnl) VALUES (?,?,?,?,?,?,?,?)",
                [(f"cal-{idx}", f"sig-{idx:04d}", 1_800_000_000 + idx,
                  .6, .3, int(idx % 2 == 0), int(idx % 2 == 1), 0.0)
                 for idx in range(config.FORECAST_MIN_CALIBRATION)])
            conn.execute(
                "INSERT INTO factor_trials (ts,name,status,timeframe,horizon_hours) "
                "VALUES (?,?,?,?,?)", (1, "validated-edge", "validated",
                                        config.SIGNAL_SAMPLE_TIMEFRAME,
                                        config.SIGNAL_OUTCOME_HORIZON_HOURS))
            conn.executemany(
                "INSERT INTO model_artifacts (model_id,model_type,version,state,"
                "created_at,feature_names,artifact,metrics) VALUES (?,?,?,?,?,?,?,?)",
                [("entry-kept", "entry_probability", "v1", "kept", 1,
                  "[]", "{}", json.dumps({"brier_skill": .1})),
                 ("extrema-shadow-accepted", "extrema", "v1", "accepted", 2,
                  "[]", "{}", json.dumps({"pinball_improvement": .1}))])
            for idx in range(config.AGENT_EVAL_MIN_VALID):
                reject = idx < config.AGENT_EVAL_MIN_REJECT
                conn.execute(
                    "INSERT INTO agent_runs (run_id,signal_id,idempotency_key,"
                    "created_ts,runtime_status,final_action,model_verdict,run_role) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"run-{idx}", f"sig-{idx:04d}", f"idem-{idx}", idx,
                     "completed", "shadow_reject" if reject else "baseline_pass",
                     "reject" if reject else "approve", "champion"))
                conn.execute(
                    "INSERT INTO agent_evaluations (run_id,lifecycle_status,pnl_r) "
                    "VALUES (?,?,?)", (f"run-{idx}", "mature",
                                        -1.0 if reject else .2))
            conn.execute(
                "INSERT INTO agent_versions (version,role,status,created_ts,metrics_json) "
                "VALUES (?,?,?,?,?)",
                ("agent-v1", "challenger", "validated", 1,
                 json.dumps({"n": config.AGENT_EVAL_MIN_VALID,
                             "reject_n": config.AGENT_EVAL_MIN_REJECT,
                             "incremental_ev_lower_bound": .1,
                             "max_segment_share": .5})))

        result = audit_status(self.path)
        self.assertTrue(result["statistically_complete"], result["blockers"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["counts"]["paper_closed"], 60)
        self.assertEqual(result["counts"]["paper_six_dim_closed"], 30)
        self.assertEqual(result["counts"]["agent_valid_distinct_signals"], 100)
        self.assertEqual(result["counts"]["agent_reject_distinct_signals"], 30)
        self.assertTrue(result["gates"]["extrema_model_observed"]["passed"])


if __name__ == "__main__":
    unittest.main()
