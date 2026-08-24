"""T7 条件极值模型的 purged walk-forward 训练、评估与制品落库。"""
import hashlib
import json
import time
from typing import List

import config
from decision.signal_identity import research_scope_version
from decision.extrema_forecast import (conformal_radius, fit_linear_quantiles,
                                       interval_coverage, pinball_loss,
                                       predict_linear_quantiles, quantile)
from factors.feature_registry import extract_features
from factors.intraday_factor_gate import purged_walk_forward_splits


def _validated_features(db_path=None, strategy_id=None):
    import storage.db as sdb
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND strategy_version=?" if scope_version else ""
    rows = sdb.q(
        "SELECT f.name,f.ic_tstat FROM factor_trials f JOIN "
        "(SELECT name,MAX(id) id FROM factor_trials WHERE strategy_id=? "
        "AND timeframe=? AND horizon_hours=?" + scope_sql +
        " GROUP BY name) x ON x.id=f.id "
        "WHERE f.status='validated' ORDER BY f.ic_tstat DESC",
        [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS,
         *([scope_version] if scope_version else [])],
        db_path=db_path)
    return [row["name"] for row in rows[:config.ENTRY_MODEL_MAX_FEATURES]]


def _load_rows(direction, feature_names, db_path=None, strategy_id=None):
    import storage.db as sdb
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND s.strategy_version=?" if scope_version else ""
    samples = sdb.q(
        "SELECT s.*,o.high_ret_h,o.low_ret_h FROM signal_samples s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.direction=? AND s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=?" + scope_sql +
        " ORDER BY s.event_ts", [direction, strategy_id,
                                 config.SIGNAL_SAMPLE_TIMEFRAME,
                                 config.SIGNAL_OUTCOME_HORIZON_HOURS,
                                 *([scope_version] if scope_version else [])],
        db_path=db_path)
    return [{"signal_id": sample["signal_id"], "event_ts": sample["event_ts"],
             "label_end_ts": sample["event_ts"] +
             sample["horizon_hours"] * 3600,
             "features": extract_features(sample),
             "high_ret_h": float(sample["high_ret_h"]),
             "low_ret_h": float(sample["low_ret_h"])}
            for sample in samples]


def _impute(train_rows, test_rows, feature_names):
    medians = {}
    for name in feature_names:
        values = sorted(float(row["features"][name]) for row in train_rows
                        if row["features"].get(name) is not None)
        if not values:
            return None, None
        medians[name] = values[len(values) // 2]

    def matrix(rows):
        return [[float(row["features"].get(name))
                 if row["features"].get(name) is not None else medians[name]
                 for name in feature_names] for row in rows]
    return matrix(train_rows), matrix(test_rows)


def _target_quantiles(rows, target):
    values = [float(row[target]) for row in rows]
    return {f"q{int(tau * 100):02d}": quantile(values, tau)
            for tau in config.EXTREMA_QUANTILES}


def _mean_pinball(actual, predictions):
    losses = []
    for tau in config.EXTREMA_QUANTILES:
        key = f"q{int(tau * 100):02d}"
        losses.append(pinball_loss(actual, [row[key] for row in predictions], tau))
    return sum(losses) / len(losses)


def evaluate_rows(rows: List[dict], feature_names: List[str]):
    """只用时间在前的训练窗预测后续窗；返回最高/最低共同晋升证据。"""
    folds = []
    high_actual, low_actual = [], []
    high_pred, low_pred = [], []
    high_base, low_base = [], []
    high_lower, high_upper, low_lower, low_upper = [], [], [], []
    crossings = 0
    for fold, (train_idx, test_idx) in enumerate(purged_walk_forward_splits(rows)):
        train = [rows[idx] for idx in train_idx]
        test = [rows[idx] for idx in test_idx]
        train_x, test_x = _impute(train, test, feature_names)
        if train_x is None:
            continue
        high_model = fit_linear_quantiles(
            train_x, [row["high_ret_h"] for row in train],
            min_samples=config.EXTREMA_MIN_FOLD_TRAIN_SAMPLES)
        low_model = fit_linear_quantiles(
            train_x, [row["low_ret_h"] for row in train],
            min_samples=config.EXTREMA_MIN_FOLD_TRAIN_SAMPLES)
        if not high_model or not low_model:
            continue
        fold_high, fold_low = [], []
        for values in test_x:
            high = predict_linear_quantiles(high_model, values)
            low = predict_linear_quantiles(low_model, values)
            if not high or not low:
                crossings += 1
                continue
            fold_high.append(high)
            fold_low.append(low)
        if len(fold_high) != len(test):
            continue
        high_truth = [row["high_ret_h"] for row in test]
        low_truth = [row["low_ret_h"] for row in test]
        baseline_high = _target_quantiles(train, "high_ret_h")
        baseline_low = _target_quantiles(train, "low_ret_h")
        fold_high_base = [baseline_high] * len(test)
        fold_low_base = [baseline_low] * len(test)
        model_loss = (_mean_pinball(high_truth, fold_high) +
                      _mean_pinball(low_truth, fold_low)) / 2
        baseline_loss = (_mean_pinball(high_truth, fold_high_base) +
                         _mean_pinball(low_truth, fold_low_base)) / 2
        improvement = (1 - model_loss / baseline_loss
                       if baseline_loss and baseline_loss > 0 else 0.0)
        # 每折只用时间更早的 OOS 残差校准；首折尚无历史 OOS 时以当前
        # 训练窗残差冷启动。pinball 仍评价原始分位，不用扩宽区间美化。
        if high_actual:
            high_radius = conformal_radius(
                high_actual, [row["q10"] for row in high_pred],
                [row["q90"] for row in high_pred],
                window=config.EXTREMA_CONFORMAL_WINDOW)
            low_radius = conformal_radius(
                low_actual, [row["q10"] for row in low_pred],
                [row["q90"] for row in low_pred],
                window=config.EXTREMA_CONFORMAL_WINDOW)
        else:
            train_high = [predict_linear_quantiles(high_model, values)
                          for values in train_x]
            train_low = [predict_linear_quantiles(low_model, values)
                         for values in train_x]
            high_radius = conformal_radius(
                [row["high_ret_h"] for row in train],
                [row["q10"] for row in train_high],
                [row["q90"] for row in train_high],
                window=config.EXTREMA_CONFORMAL_WINDOW)
            low_radius = conformal_radius(
                [row["low_ret_h"] for row in train],
                [row["q10"] for row in train_low],
                [row["q90"] for row in train_low],
                window=config.EXTREMA_CONFORMAL_WINDOW)
        fold_high_lower = [row["q10"] - high_radius for row in fold_high]
        fold_high_upper = [row["q90"] + high_radius for row in fold_high]
        fold_low_lower = [row["q10"] - low_radius for row in fold_low]
        fold_low_upper = [row["q90"] + low_radius for row in fold_low]
        folds.append({
            "fold": fold, "train_n": len(train), "test_n": len(test),
            "pinball_loss": model_loss, "baseline_pinball_loss": baseline_loss,
            "pinball_improvement": improvement,
            "high_coverage": interval_coverage(high_truth, fold_high_lower,
                                                fold_high_upper),
            "low_coverage": interval_coverage(low_truth, fold_low_lower,
                                               fold_low_upper),
            "high_conformal_radius": high_radius,
            "low_conformal_radius": low_radius,
        })
        high_actual.extend(high_truth)
        low_actual.extend(low_truth)
        high_pred.extend(fold_high)
        low_pred.extend(fold_low)
        high_base.extend(fold_high_base)
        low_base.extend(fold_low_base)
        high_lower.extend(fold_high_lower)
        high_upper.extend(fold_high_upper)
        low_lower.extend(fold_low_lower)
        low_upper.extend(fold_low_upper)

    if not high_actual:
        return {"folds": folds, "pinball_loss": None,
                "baseline_pinball_loss": None, "pinball_improvement": None,
                "good_folds": 0, "high_coverage": None, "low_coverage": None,
                "quantile_crossings": crossings, "eligible_for_shadow": False,
                "high_conformal_radius": 0.0, "low_conformal_radius": 0.0}
    model_loss = (_mean_pinball(high_actual, high_pred) +
                  _mean_pinball(low_actual, low_pred)) / 2
    baseline_loss = (_mean_pinball(high_actual, high_base) +
                     _mean_pinball(low_actual, low_base)) / 2
    improvement = (1 - model_loss / baseline_loss
                   if baseline_loss and baseline_loss > 0 else 0.0)
    high_coverage = interval_coverage(high_actual, high_lower, high_upper)
    low_coverage = interval_coverage(low_actual, low_lower, low_upper)
    good_folds = sum(fold["pinball_loss"] <= fold["baseline_pinball_loss"]
                     for fold in folds)
    eligible = (
        len(folds) >= config.FACTOR_WALK_FORWARD_FOLDS and
        improvement >= config.EXTREMA_PINBALL_IMPROVEMENT and
        good_folds >= config.EXTREMA_MIN_GOOD_FOLDS and crossings == 0 and
        config.EXTREMA_COVERAGE_LOW <= high_coverage <= config.EXTREMA_COVERAGE_HIGH and
        config.EXTREMA_COVERAGE_LOW <= low_coverage <= config.EXTREMA_COVERAGE_HIGH)
    return {
        "folds": folds, "pinball_loss": model_loss,
        "baseline_pinball_loss": baseline_loss,
        "pinball_improvement": improvement, "good_folds": good_folds,
        "high_coverage": high_coverage, "low_coverage": low_coverage,
        "quantile_crossings": crossings, "eligible_for_shadow": eligible,
        "high_conformal_radius": conformal_radius(
            high_actual, [row["q10"] for row in high_pred],
            [row["q90"] for row in high_pred], window=config.EXTREMA_CONFORMAL_WINDOW),
        "low_conformal_radius": conformal_radius(
            low_actual, [row["q10"] for row in low_pred],
            [row["q90"] for row in low_pred], window=config.EXTREMA_CONFORMAL_WINDOW),
    }


def train_extrema_model(direction, db_path=None, feature_names=None,
                        strategy_id=None):
    """训练并持久化极值候选；样本或已验证因子不足时不写可用制品。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    strategy_version = research_scope_version(strategy_id)
    names = list(feature_names or _validated_features(db_path, strategy_id))
    names = names[:config.ENTRY_MODEL_MAX_FEATURES]
    # 无 validated 特征时仍报告真实路径样本数，避免把特征门失败误写成无数据。
    rows = _load_rows(direction, names, db_path, strategy_id)
    if len(rows) < config.EXTREMA_MIN_MODEL_SAMPLES or not names:
        return {"status": "insufficient_data", "strategy_id": strategy_id,
                "n": len(rows), "features": names}
    version = "extrema-linear-quantile-v1"
    data_digest = hashlib.sha256(
        "|".join(row["signal_id"] for row in rows).encode("utf-8")).hexdigest()
    signature_data = {
        "data_hash": data_digest, "version": version, "features": names,
        "strategy_id": strategy_id,
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        "l2": config.EXTREMA_L2, "epochs": config.EXTREMA_EPOCHS,
        "constraint": "parallel_location_shift"}
    if strategy_version:
        signature_data["strategy_version"] = strategy_version
    signature = hashlib.sha256(json.dumps(
        signature_data, sort_keys=True).encode("utf-8")).hexdigest()
    prefix = ("extrema" if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID
              else f"extrema_{strategy_id}")
    model_id = f"{prefix}_{direction}_{signature[:16]}"
    existing = sdb.q1("SELECT state FROM model_artifacts WHERE model_id=?",
                      [model_id], db_path=db_path)
    if existing:
        return {"status": existing["state"], "model_id": model_id,
                "strategy_id": strategy_id,
                "n": len(rows), "features": names, "reused": True}
    evaluation = evaluate_rows(rows, names)
    train_x, _ = _impute(rows, [], names)
    high_model = fit_linear_quantiles(
        train_x, [row["high_ret_h"] for row in rows])
    low_model = fit_linear_quantiles(
        train_x, [row["low_ret_h"] for row in rows])
    if not high_model or not low_model:
        return {"status": "rejected", "strategy_id": strategy_id,
                "n": len(rows), "features": names,
                "reason": "quantile_fit_failed", "evaluation": evaluation}
    artifact = {
        "version": version, "direction": direction,
        "strategy_id": strategy_id,
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        "feature_names": names, "high_model": high_model, "low_model": low_model,
        "baseline_high_returns": _target_quantiles(rows, "high_ret_h"),
        "baseline_low_returns": _target_quantiles(rows, "low_ret_h"),
        "high_conformal_radius": evaluation["high_conformal_radius"],
        "low_conformal_radius": evaluation["low_conformal_radius"],
    }
    if strategy_version:
        artifact["strategy_version"] = strategy_version
    state = "validated" if evaluation["eligible_for_shadow"] else "rejected"
    now = time.time()
    with sdb.tx(db_path=db_path) as conn:
        conn.execute(
            "INSERT INTO model_artifacts (model_id,model_type,strategy_id,"
            "strategy_version,direction,version,"
            "state,created_at,training_cutoff,data_hash,feature_names,artifact,metrics) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [model_id, "extrema", strategy_id, strategy_version, direction,
             artifact["version"], "candidate", now,
             max(row["event_ts"] for row in rows), data_digest, json.dumps(names),
             json.dumps(artifact), json.dumps(evaluation)])
        conn.execute(
            "INSERT INTO model_state_events (model_id,ts,from_state,to_state,reason,metrics) "
            "VALUES (?,?,?,?,?,?)",
            [model_id, now, None, "candidate", "training_complete", "{}"])
        conn.execute("UPDATE model_artifacts SET state=? WHERE model_id=?",
                     [state, model_id])
        conn.execute(
            "INSERT INTO model_state_events (model_id,ts,from_state,to_state,reason,metrics) "
            "VALUES (?,?,?,?,?,?)",
            [model_id, now, "candidate", state,
             "oos_gate_pass" if state == "validated" else "oos_gate_fail",
             json.dumps(evaluation)])
    for fold in evaluation["folds"]:
        sdb.x(
            "INSERT INTO model_evaluations (model_id,fold,ts,n_samples,coverage,"
            "pinball_loss,details) VALUES (?,?,?,?,?,?,?)",
            [model_id, fold["fold"], now, fold["test_n"],
             (fold["high_coverage"] + fold["low_coverage"]) / 2,
             fold["pinball_loss"], json.dumps({
                 "baseline_pinball_loss": fold["baseline_pinball_loss"],
                 "pinball_improvement": fold["pinball_improvement"],
                 "high_coverage": fold["high_coverage"],
                 "low_coverage": fold["low_coverage"]})], db_path=db_path)
    return {"status": state, "model_id": model_id,
            "strategy_id": strategy_id, "n": len(rows),
            "features": names, "evaluation": evaluation}
