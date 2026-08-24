"""T9 模型状态机、独立 shadow 样本与一键回滚。"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from decision.model_lifecycle import (advance, budget_expansion_allowed,
                                      rollback, snapshot)
from decision.signal_identity import config_identity

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def insert_model(db, model_id, state, parent=None, metrics=None):
    import storage.db as sdb
    strategy_version = config_identity(config.ENTRY_SIGNAL_STRATEGY_ID)[0]
    artifact = {"version": "v1", "feature_names": ["edge"],
                "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
                "strategy_version": strategy_version,
                "model": {"weights": [1], "means": [0], "scales": [1],
                          "bias": 0, "n_train": 300, "base_rate": 0.5}}
    sdb.x(
        "INSERT INTO model_artifacts (model_id,model_type,direction,version,state,"
        "created_at,training_cutoff,data_hash,feature_names,artifact,metrics,parent_id,"
        "strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [model_id, "entry_probability", "long", "v1", state, time.time(),
         1_700_000_000, "hash", '["edge"]', json.dumps(artifact),
         json.dumps(metrics or {}), parent, strategy_version], db_path=db)


def insert_extrema_model(db, model_id, state):
    import storage.db as sdb
    strategy_version = config_identity(config.ENTRY_SIGNAL_STRATEGY_ID)[0]
    artifact = {"version": "extrema-v1", "feature_names": ["trend"],
                "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
                "strategy_version": strategy_version}
    sdb.x(
        "INSERT INTO model_artifacts (model_id,model_type,direction,version,state,"
        "created_at,training_cutoff,data_hash,feature_names,artifact,metrics,"
        "strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [model_id, "extrema", "long", "extrema-v1", state, time.time(),
         1_700_000_000, "extrema-hash", '["trend"]', json.dumps(artifact), "{}",
         strategy_version],
        db_path=db)


def main():
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "models.db")
        import storage.db as sdb
        sdb.init_db(db)
        insert_model(db, "candidate", "validated")
        actions = advance("candidate", db)
        state = sdb.q1("SELECT state FROM model_artifacts WHERE model_id='candidate'",
                       db_path=db)["state"]
        check("validated 自动进入 shadow", state == "shadow", str(actions))
        candidate_view = next(model for model in snapshot(db)["models"]
                              if model["model_id"] == "candidate")
        check("状态跃迁历史进入只读快照",
              candidate_view["history"][-1]["to_state"] == "shadow",
              str(candidate_view["history"]))
        actions = advance("candidate", db)
        check("无独立新样本保持 shadow",
              actions[0].get("reason") == "insufficient_shadow", str(actions))
        from engines.signal_sampling import (merge_sample_features,
                                             record_signal_sample)
        from decision.signal_outcomes import persist_outcome
        for i in range(config.MODEL_SHADOW_MIN_CANDIDATES):
            win = i % 2 == 0
            sig = {"dir": "long", "entry": 100.0, "stop": 99.0, "tp": 102.0,
                   "atr": 1.0, "kline_ts": 1_700_100_000_000 + i * 3_600_000,
                   "shadow_dims": {name: 0.5 for name in config.SHADOW_DIMS}}
            sid, _ = record_signal_sample("BTC", sig, "swap", db_path=db,
                                          event_ts=1_700_100_000 + i * 3600)
            merge_sample_features(
                sid, {"entry_probability": {"model_id": "candidate",
                                             "p_tp": 0.9 if win else 0.1,
                                             "ev_r_lower": 1.0 if win else -1.0}}, db)
            persist_outcome(
                {"signal_id": sid, "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
                 "tp_first": 1 if win else 0, "sl_first": 0 if win else 1,
                 "timeout": 0, "ambiguous": 0, "pnl_r": 2.0 if win else -1.0,
                 "mfe_r": 2.0 if win else 0.2, "mae_r": 0.2 if win else 1.0,
                 "high_ret_h": 0.02, "low_ret_h": -0.01,
                 "time_to_tp_sec": 60 if win else None,
                 "time_to_sl_sec": None if win else 60,
                 "time_to_high_sec": 60, "time_to_low_sec": 120,
                 "settled_at": 1_700_300_000 + i, "bar_resolution": "1m",
                 "label_version": "test-v1"}, db_path=db)
        actions = advance("candidate", db)
        state = sdb.q1("SELECT state FROM model_artifacts WHERE model_id='candidate'",
                       db_path=db)["state"]
        check("60 个独立 shadow 候选优于基线后 accepted",
              state == "accepted", str(actions))
        metrics = json.loads(sdb.q1(
            "SELECT metrics FROM model_artifacts WHERE model_id='candidate'",
            db_path=db)["metrics"])
        check("shadow 晋升使用费用后收益且至少 30 个真实放行样本",
              metrics["selected_n"] == 30 and
              metrics["selected_ev_r"] > 0 and metrics["baseline_ev_r"] < .5,
              str(metrics))
        activated = advance("candidate", db)
        check("通过独立 shadow 门的开仓模型自动激活为 2:1 前置预测器",
              activated[0].get("to") == "active", str(activated))
        check("无长期正 EV 证据禁止扩大预算",
              budget_expansion_allowed(db) is False)

        insert_extrema_model(db, "extrema_candidate", "shadow")
        for i in range(config.MODEL_SHADOW_MIN_CANDIDATES):
            outside = i % 5 == 0
            sig = {"dir": "long", "entry": 100.0, "stop": 99.0, "tp": 102.0,
                   "atr": 1.0, "kline_ts": 1_800_100_000_000 + i * 3_600_000,
                   "shadow_dims": {name: 0.5 for name in config.SHADOW_DIMS}}
            sid, _ = record_signal_sample("ETH", sig, "swap", db_path=db,
                                          event_ts=1_800_100_000 + i * 3600)
            prediction = {
                "model_id": "extrema_candidate",
                "high_returns": {"q10": 0.01, "q50": 0.02, "q90": 0.03},
                "low_returns": {"q10": -0.03, "q50": -0.02, "q90": -0.01},
                "high_interval": {"lower": 0.01, "upper": 0.03},
                "low_interval": {"lower": -0.03, "upper": -0.01},
                "baseline_high_returns": {"q10": 0.10, "q50": 0.11, "q90": 0.12},
                "baseline_low_returns": {"q10": -0.12, "q50": -0.11, "q90": -0.10}}
            merge_sample_features(sid, {"extrema_prediction": prediction}, db)
            persist_outcome(
                {"signal_id": sid, "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
                 "tp_first": 1,
                 "sl_first": 0, "timeout": 0, "ambiguous": 0, "pnl_r": 2.0,
                 "mfe_r": 2.0, "mae_r": 0.2,
                 "high_ret_h": 0.05 if outside else 0.02,
                 "low_ret_h": -0.05 if outside else -0.02,
                 "time_to_tp_sec": 60, "time_to_sl_sec": None,
                 "time_to_high_sec": 60, "time_to_low_sec": 120,
                 "settled_at": 1_800_300_000 + i, "bar_resolution": "1m",
                 "label_version": "test-v1"}, db_path=db)
        extrema_actions = advance("extrema_candidate", db)
        extrema_state = sdb.q1(
            "SELECT state FROM model_artifacts WHERE model_id='extrema_candidate'",
            db_path=db)["state"]
        check("extrema 用 pinball+80% coverage 独立晋升",
              extrema_state == "accepted", str(extrema_actions))
        extrema_held = advance("extrema_candidate", db)
        check("extrema shadow_only 默认阻止自动激活",
              extrema_held[0].get("reason") == "shadow_only_enabled",
              str(extrema_held))

        insert_model(db, "parent", "kept",
                     metrics={"long_term_backtest_ev_r": 0.1})
        insert_model(db, "child", "active", parent="parent")
        ok, _ = rollback("child", db)
        states = {row["model_id"]: row["state"] for row in
                  sdb.q("SELECT model_id,state FROM model_artifacts", db_path=db)}
        check("一键回滚当前模型", ok and states["child"] == "rolled_back",
              str(states))
        check("回滚恢复 parent active", states["parent"] == "active", str(states))
        check("长期正 EV parent 才允许预算评估通过",
              budget_expansion_allowed(db) is True)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
