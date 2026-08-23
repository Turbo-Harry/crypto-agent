"""T6 L2 Logistic、Beta 收缩、样本门与损坏制品 fail-safe。"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from decision.entry_probability import (entry_gate_decision, fit_logistic,
                                        cost_breakdown_r, execution_cost_r,
                                        expected_value_r,
                                        preopen_2to1_decision,
                                        predict_signal as predict_entry_signal,
                                        predict_from_artifact, raw_probability,
                                        signal_feature_values)
from factors.entry_model_training import evaluate_rows, train_entry_model

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def synthetic_rows(n=720):
    start = 1_700_000_000.0
    out = []
    for i in range(n):
        value = ((i * 37) % 101 - 50) / 50
        tp = 1 if value >= 0 else 0
        event_ts = start + i * 900
        out.append({"signal_id": f"m{i}", "event_ts": event_ts,
                    "label_end_ts": event_ts + 4 * 3600,
                    "features": {"edge": value,
                                 "edge_aux": value * 0.5 + (i % 7) * 0.001},
                    "tp_first": tp,
                    "sl_first": 1 - tp, "timeout": 0,
                    "pnl_r": 2.0 if tp else -1.0, "cost_r": 0.1})
    return out


def main():
    check("EV 权威口径固定",
          abs(expected_value_r(0.4, 0.3, 0.3, 0.1, 0.05) - 0.48) < 1e-9)
    x = [[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]] * 60
    y = [0, 0, 0, 1, 1, 1] * 60
    model = fit_logistic(x, y, epochs=500)
    low, high = raw_probability(model, [-1.5]), raw_probability(model, [1.5])
    check("L2 Logistic 学到单调概率", low < 0.5 < high, f"{low}/{high}")

    artifact = {"version": "test", "feature_names": ["edge"], "model": model,
                "prior_strength": 30, "sl_given_not_tp": 1.0,
                "mean_timeout_r": 0.0, "cost_r": 0.1}
    pred = predict_from_artifact(artifact, {"edge": 1.5})
    check("输出三类概率与 EV",
          pred and abs(pred["p_tp"] + pred["p_sl"] + pred["p_timeout"] - 1) < 1e-6,
          str(pred))
    check("强正特征 EV 下界为正", pred["ev_r_lower"] > 0, str(pred))
    low_cost = predict_from_artifact(artifact, {"edge": 1.5},
                                     cost_r_override=.1)
    high_cost = predict_from_artifact(artifact, {"edge": 1.5},
                                      cost_r_override=1.7)
    check("同一胜率预测按候选自身成本 R 决定是否具备 2:1 正期望",
          low_cost["ev_r_lower"] > 0 > high_cost["ev_r_lower"] and
          low_cost["cost_r"] == .1 and high_cost["cost_r"] == 1.7,
          f"low={low_cost} high={high_cost}")
    check("双边成本按本候选止损距离换算",
          abs(execution_cost_r({"entry": 100, "stop": 99}) - .2) < 1e-9)
    funding_long = {"dir": "long", "entry": 100, "stop": 99,
                    "horizon_hours": 4,
                    "factor_features": {"funding_rate": .001}}
    funding_short = {"direction": "short", "entry": 100, "stop": 101,
                     "horizon_hours": 4,
                     "features": json.dumps({
                         "factor_features": {"funding_rate": -.001}})}
    long_cost = cost_breakdown_r(funding_long)
    short_cost = cost_breakdown_r(funding_short)
    income_floor = cost_breakdown_r(dict(
        funding_long, factor_features={"funding_rate": -.001}))
    check("不利资金费按方向与 4h/8h 持有比例折算为 R",
          abs(long_cost["funding_cost_r"] - .05) < 1e-9 and
          abs(short_cost["funding_cost_r"] - .05) < 1e-9 and
          abs(long_cost["total_cost_r"] - .25) < 1e-9,
          f"long={long_cost} short={short_cost}")
    check("不确定的资金费收入不用于降低严格开仓成本",
          income_floor["funding_cost_r"] == 0 and
          abs(income_floor["total_cost_r"] - .2) < 1e-9,
          str(income_floor))
    derived_values = signal_feature_values({
        "shadow_dims": {"trend": .8},
        "factor_features": {"volume_ratio": 1.5}},
        ["trend_volume_confirmation"])
    check("模型消费端可复算历史快照中的派生因子",
          derived_values is not None and
          abs(derived_values[0] - 1.2) < 1e-9, str(derived_values))
    check("旧二分类制品保持兼容", pred["probability_method"] ==
          "binary_beta_shrink_legacy")
    fixed_sig = {"dir": "long", "entry": 100.0, "stop": 99.0, "tp": 102.0}
    active_pred = dict(pred, cost_r=.2, model_id="m1", model_version="v1",
                       state="active", decision_effective=True)
    strict = preopen_2to1_decision(fixed_sig, prediction=active_pred)
    check("严格闸门放行已验证且成本后 EV 下界为正的 2:1 预测",
          strict["passed"] and strict["actual_reward_risk"] == 2.0,
          str(strict))
    funded_sig = dict(fixed_sig, factor_features={"funding_rate": .001})
    mismatch = preopen_2to1_decision(funded_sig, prediction=active_pred)
    check("旧成本预测不得绕过新增资金费成本",
          mismatch["reason"] == "prediction_cost_mismatch", str(mismatch))
    funded_strict = preopen_2to1_decision(
        funded_sig, prediction=dict(active_pred, cost_r=.25))
    check("2:1 前置审计输出交易/资金费成本拆分",
          funded_strict["trading_cost_r"] == .2 and
          funded_strict["funding_cost_r"] == .05 and
          funded_strict["candidate_cost_r"] == .25,
          str(funded_strict))
    check("非 2:1 几何失败关闭",
          preopen_2to1_decision(
              dict(fixed_sig, tp=101.5), prediction=active_pred)["reason"] ==
          "invalid_trade_geometry")
    check("没有已验证模型时失败关闭",
          preopen_2to1_decision(fixed_sig, prediction={})["reason"] ==
          "no_validated_active_model" and
          preopen_2to1_decision(fixed_sig, prediction={})["candidate_cost_r"] == .2)
    weak_pred = dict(active_pred, ev_r_lower=-0.01)
    check("成本后 EV 保守下界非正时失败关闭",
          preopen_2to1_decision(fixed_sig, prediction=weak_pred)["reason"] ==
          "non_positive_conservative_ev")

    evaluation = evaluate_rows(synthetic_rows(), ["edge"])
    check("5 折模型优于经验频率基线",
          evaluation["eligible_for_shadow"], str(evaluation))
    check("Brier Skill >5%", evaluation["brier_skill"] > 0.05,
          str(evaluation))
    check("三分类 Brier 同样优于经验基线",
          evaluation["multiclass_brier_skill"] > 0.05 and
          evaluation["good_multiclass_folds"] >= 4, str(evaluation))
    check("同等覆盖率下 precision 提升且至少 4/5 折稳定",
          evaluation["precision_lift"] > 0 and
          evaluation["good_precision_folds"] >= 4, str(evaluation))
    combo_evaluation = evaluate_rows(
        synthetic_rows(), ["edge", "edge_aux"])
    check("多个已验证因子会作为同一特征向量联合做 5 折样本外回测",
          combo_evaluation["eligible_for_shadow"] and
          combo_evaluation["feature_names"] == ["edge", "edge_aux"] and
          len(combo_evaluation["folds"]) == config.FACTOR_WALK_FORWARD_FOLDS,
          str(combo_evaluation))
    costly_rows = [dict(row, cost_r=3.0) for row in synthetic_rows()]
    costly_evaluation = evaluate_rows(costly_rows, ["edge"])
    check("样本外验证与部署同样按逐候选 EV 下界拒绝高成本信号",
          not costly_evaluation["eligible_for_shadow"] and
          costly_evaluation["good_ev_folds"] == 0,
          str(costly_evaluation))

    multi_artifact = dict(artifact, class_models={
        "tp": model,
        "sl": fit_logistic(x, [1 - value for value in y], epochs=500),
        "timeout": fit_logistic(x, [0 for _ in y], epochs=500)},
        class_priors={"tp": 0.5, "sl": 0.5, "timeout": 0.0})
    multi = predict_from_artifact(multi_artifact, {"edge": 1.5})
    check("三分类 OVR 概率归一且采用 Beta 收缩",
          multi and abs(multi["p_tp"] + multi["p_sl"] +
                        multi["p_timeout"] - 1) < 1e-6 and
          multi["probability_method"] == "ovr_multiclass_beta_shrink",
          str(multi))

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "entry.db")
        insufficient = train_entry_model("long", db_path=db,
                                         feature_names=["wick"])
        check("不足 300 候选不训练", insufficient["status"] == "insufficient_data",
              str(insufficient))
        import storage.db as sdb
        old_scope = dict(artifact, timeframe="1H", horizon_hours=24)
        sdb.x("INSERT INTO model_artifacts (model_id,model_type,direction,version,"
              "state,created_at,feature_names,artifact,metrics) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              ["old_scope", "entry_probability", "long", "v", "active",
               time.time() - 1, '["edge"]', json.dumps(old_scope), "{}"],
              db_path=db)
        allowed, old_prediction = entry_gate_decision(
            {"dir": "long", "shadow_dims": {}, "factor_features": {"edge": 1.0}},
            db)
        check("旧 1H/24H 模型不得在 15m/4h 策略下加载",
              allowed and old_prediction is None)
        old_cost = dict(
            artifact, timeframe=config.SIGNAL_SAMPLE_TIMEFRAME,
            horizon_hours=config.SIGNAL_OUTCOME_HORIZON_HOURS)
        sdb.x("INSERT INTO model_artifacts (model_id,model_type,direction,version,"
              "state,created_at,feature_names,artifact,metrics) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              ["old_cost", "entry_probability", "long", "v", "active",
               time.time(), '["edge"]', json.dumps(old_cost), "{}"],
              db_path=db)
        allowed, old_cost_prediction = entry_gate_decision(
            {"dir": "long", "entry": 100, "stop": 99,
             "shadow_dims": {}, "factor_features": {"edge": 1.0}}, db)
        check("旧成本口径模型不得获得 2:1 开仓权限",
              allowed and old_cost_prediction is None)
        sdb.x("INSERT INTO model_artifacts (model_id,model_type,direction,version,"
              "state,created_at,feature_names,artifact,metrics) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              ["broken", "entry_probability", "long", "v", "active",
               time.time() + 1, "[]", "{broken", "{}"], db_path=db)
        allowed, broken_pred = entry_gate_decision(
            {"dir": "long", "shadow_dims": {}, "factor_features": {}}, db)
        check("损坏 active 制品 fail-safe 回现役规则",
              allowed and broken_pred is None)

    with tempfile.TemporaryDirectory() as td:
        scoped_db = os.path.join(td, "strategy-models.db")
        import storage.db as sdb
        sdb.init_db(scoped_db)
        scoped_a = dict(
            multi_artifact, strategy_id=config.ENTRY_SIGNAL_STRATEGY_ID,
            timeframe=config.SIGNAL_SAMPLE_TIMEFRAME,
            horizon_hours=config.SIGNAL_OUTCOME_HORIZON_HOURS,
            cost_model_version=config.ENTRY_COST_MODEL_VERSION)
        scoped_b = dict(
            scoped_a, strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID)
        sdb.x(
            "INSERT INTO model_artifacts (model_id,model_type,strategy_id,direction,"
            "version,state,created_at,feature_names,artifact,metrics) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ["a-active", "entry_probability", config.ENTRY_SIGNAL_STRATEGY_ID,
             "long", "v", "active", 1, '["edge"]',
             json.dumps(scoped_a), "{}"], db_path=scoped_db)
        sdb.x(
            "INSERT INTO model_artifacts (model_id,model_type,strategy_id,direction,"
            "version,state,created_at,feature_names,artifact,metrics) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ["b-active", "entry_probability",
             config.BREAKOUT_SIGNAL_STRATEGY_ID, "long", "v", "active", 2,
             '["edge"]', json.dumps(scoped_b), "{}"], db_path=scoped_db)
        common = {"dir": "long", "entry": 100, "stop": 99,
                  "factor_features": {"edge": 1.0}, "shadow_dims": {}}
        a_prediction = predict_entry_signal(
            dict(common, strategy_id=config.ENTRY_SIGNAL_STRATEGY_ID),
            scoped_db, allow_shadow=False)
        b_prediction = predict_entry_signal(
            dict(common, strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID),
            scoped_db, allow_shadow=False)
        check("A/B active 入场模型按策略选择且互不遮挡",
              a_prediction and b_prediction and
              a_prediction["model_id"] == "a-active" and
              b_prediction["model_id"] == "b-active",
              f"A={a_prediction} B={b_prediction}")

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
