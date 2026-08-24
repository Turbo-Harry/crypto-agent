"""T7 极值分位、交叉拒绝、pinball 与 conformal 纯函数测试。"""
import math
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from decision.signal_identity import config_identity
from decision.extrema_forecast import (conformal_radius, fit_linear_quantiles,
                                       interval_coverage, pinball_loss,
                                       predict_linear_quantiles, predict_signal,
                                       price_quantiles, quantile,
                                       validate_quantiles)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def main():
    values = list(range(1, 101))
    check("经验 q50 插值", math.isclose(quantile(values, 0.5), 50.5))
    good = {"q10": 0.01, "q50": 0.02, "q90": 0.04}
    bad = {"q10": 0.03, "q50": 0.02, "q90": 0.04}
    check("分位单调通过", validate_quantiles(good))
    check("分位交叉被拒绝", not validate_quantiles(bad))
    check("交叉时拒绝价格输出", price_quantiles(100, bad, good) is None)
    prices = price_quantiles(100, good,
                             {"q10": -0.04, "q50": -0.02, "q90": -0.01})
    check("价格分位按 exp 映射", prices and prices["high_q50"] > 100 and
          prices["low_q50"] < 100, str(prices))

    actual = [0.0, 1.0, 2.0, 3.0, 4.0]
    lower = [-0.5, 0.5, 1.5, 2.5, 3.5]
    upper = [0.5, 1.5, 2.5, 3.5, 4.5]
    check("80% 区间覆盖计算", interval_coverage(actual, lower, upper) == 1.0)
    check("全覆盖 conformal 半径为 0",
          conformal_radius(actual, lower, upper, alpha=0.2) == 0.0)
    check("准确中位 pinball 优于偏移预测",
          pinball_loss(actual, actual, 0.5) <
          pinball_loss(actual, [v + 1 for v in actual], 0.5))

    features = [[i / 79] for i in range(80)]
    targets = [0.01 + 0.03 * row[0] + 0.001 * math.sin(i)
               for i, row in enumerate(features)]
    fitted = fit_linear_quantiles(features, targets, min_samples=30, epochs=200)
    predicted = predict_linear_quantiles(fitted, [0.5])
    check("受约束线性分位模型可训练", fitted is not None)
    check("location-shift 模型结构上不交叉",
          predicted is not None and validate_quantiles(predicted), str(predicted))
    broken = dict(fitted)
    broken["models"] = {}
    check("损坏分位制品 fail-safe 返回 None",
          predict_linear_quantiles(broken, [0.5]) is None)

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "extrema.db")
        import storage.db as sdb
        sdb.init_db(db)
        high_model = fit_linear_quantiles(
            features, targets, min_samples=30, epochs=200)
        low_targets = [-0.04 + 0.02 * row[0] - 0.001 * math.sin(i)
                       for i, row in enumerate(features)]
        low_model = fit_linear_quantiles(
            features, low_targets, min_samples=30, epochs=200)
        artifact = {
            "version": "test-extrema-v1", "direction": "long",
            "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
            "strategy_version": config_identity(
                config.ENTRY_SIGNAL_STRATEGY_ID)[0],
            "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
            "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
            "feature_names": ["trend"], "high_model": high_model,
            "low_model": low_model,
            "baseline_high_returns": {"q10": 0.01, "q50": 0.02, "q90": 0.04},
            "baseline_low_returns": {"q10": -0.04, "q50": -0.02, "q90": -0.01},
            "high_conformal_radius": 0.001, "low_conformal_radius": 0.001}
        sdb.x(
            "INSERT INTO model_artifacts (model_id,model_type,direction,version,state,"
            "created_at,training_cutoff,data_hash,feature_names,artifact,metrics,"
            "strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ["extrema_shadow", "extrema", "long", "test-extrema-v1", "shadow",
             time.time(), 1_700_000_000, "hash", '["trend"]',
             json.dumps(artifact), "{}", artifact["strategy_version"]],
            db_path=db)
        shadow = predict_signal(
            {"dir": "long", "entry": 100.0, "shadow_dims": {"trend": 0.5}},
            db_path=db, allow_shadow=True)
        check("shadow 极值模型生成条件分位", shadow is not None, str(shadow))
        check("极值输出明确不参与交易决策",
              shadow and shadow["decision_effective"] is False)
        check("conformal 区间不写成保证点位",
              shadow and shadow["interval"] == "probabilistic_not_guaranteed")

        # 360 个去重候选，5 折 purge 后验证训练、评估和制品持久化完整闭环。
        sample_rows, outcome_rows = [], []
        strategy_version, config_hash = config_identity(
            config.ENTRY_SIGNAL_STRATEGY_ID)
        start = 1_700_000_000
        for i in range(360):
            x = (i % 37) / 36
            noise = 0.002 * math.sin(i * 1.7)
            event_ts = start + i * 172_800
            signal_id = f"train_{i:03d}"
            snapshot = json.dumps({"shadow_dims": {"trend": x},
                                   "factor_features": {}})
            sample_rows.append((
                signal_id, "BTC", "long", event_ts, int(event_ts * 1000),
                config.SIGNAL_SAMPLE_TIMEFRAME,
                "swap", strategy_version, config_hash,
                config.SIGNAL_FEATURE_SCHEMA_VERSION, 100.0, 99.0,
                102.0, 1.0, config.SIGNAL_OUTCOME_HORIZON_HOURS, x, snapshot,
                event_ts, event_ts))
            outcome_rows.append((
                signal_id, config.SIGNAL_OUTCOME_HORIZON_HOURS,
                1, 0, 0, 0, 2.0, 2.0, 0.2,
                0.01 + 0.03 * x + noise, -0.04 + 0.02 * x - noise,
                event_ts + 86_400, "1m", "test-v1"))
        # 旧 feature schema 即使已有完整标签，也不得补当前极值训练样本门。
        sample_rows.append((
            "train_old_schema", "ETH", "long", start + 999 * 172_800,
            int((start + 999 * 172_800) * 1000),
            config.SIGNAL_SAMPLE_TIMEFRAME, "swap", "old-strategy",
            "old-config", "signal-features-v4", 100.0, 99.0, 102.0, 1.0,
            config.SIGNAL_OUTCOME_HORIZON_HOURS, .5,
            json.dumps({"shadow_dims": {"trend": .5},
                        "factor_features": {}}),
            start + 999 * 172_800, start + 999 * 172_800))
        outcome_rows.append((
            "train_old_schema", config.SIGNAL_OUTCOME_HORIZON_HOURS,
            1, 0, 0, 0, 2.0, 2.0, .2, .03, -.03,
            start + 1000 * 172_800, "1m", "test-v1"))
        with sdb.tx(db_path=db) as conn:
            conn.executemany(
                "INSERT INTO signal_samples (signal_id,symbol,direction,event_ts,"
                "kline_ts,timeframe,venue,strategy_version,config_hash,"
                "feature_schema_version,entry,stop,tp,atr,horizon_hours,trend,"
                "features,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                sample_rows)
            conn.executemany(
                "INSERT INTO signal_outcomes (signal_id,horizon_hours,tp_first,"
                "sl_first,timeout,ambiguous,pnl_r,mfe_r,mae_r,high_ret_h,low_ret_h,"
                "settled_at,bar_resolution,label_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                outcome_rows)
        from factors.extrema_model_training import train_extrema_model
        old_epochs = config.EXTREMA_EPOCHS
        config.EXTREMA_EPOCHS = 200
        try:
            trained = train_extrema_model("long", db_path=db,
                                          feature_names=["trend"])
            check("360 候选通过极值模型 OOS 门进入 validated",
                  trained["status"] == "validated" and trained["n"] == 360,
                  str(trained))
            saved = sdb.q1(
                "SELECT model_type,state FROM model_artifacts WHERE model_id=?",
                [trained.get("model_id")], db_path=db)
            check("extrema 制品与状态已持久化",
                  saved and saved["model_type"] == "extrema" and
                  saved["state"] == "validated", str(saved))
            from decision.model_lifecycle import advance
            advance(trained["model_id"], db)
            reused = train_extrema_model("long", db_path=db,
                                         feature_names=["trend"])
        finally:
            config.EXTREMA_EPOCHS = old_epochs
        state_after_retrain = sdb.q1(
            "SELECT state FROM model_artifacts WHERE model_id=?",
            [trained.get("model_id")], db_path=db)["state"]
        check("相同制品重训直接复用且不重置 shadow",
              reused.get("reused") is True and state_after_retrain == "shadow",
              f"{reused}/{state_after_retrain}")
        eval_n = sdb.q1(
            "SELECT COUNT(*) n FROM model_evaluations WHERE model_id=?",
            [trained.get("model_id")], db_path=db)["n"]
        check("5 折 pinball/coverage 评估逐折落库", eval_n == 5, str(eval_n))
        history = sdb.q(
            "SELECT to_state FROM model_state_events WHERE model_id=? ORDER BY id",
            [trained.get("model_id")], db_path=db)
        check("candidate→validated→shadow 历史可审计",
              [row["to_state"] for row in history] ==
              ["candidate", "validated", "shadow"], str(history))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
