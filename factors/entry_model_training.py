"""开仓概率模型 Champion–Challenger 与 purged walk-forward 评估。"""
import base64
import hashlib
import json
import math
import os
import tempfile
import time
from typing import List

import config
from decision.signal_identity import research_scope_version
from decision.entry_probability import (execution_cost_r, fit_logistic,
                                        fit_temperature,
                                        predict_from_artifact)
from factors.feature_registry import extract_features
from factors.intraday_factor_gate import purged_walk_forward_splits


def brier(actual, predicted):
    return sum((float(p) - int(y)) ** 2 for y, p in zip(actual, predicted)) / len(actual)


def log_loss(actual, predicted):
    eps = 1e-9
    return -sum(int(y) * math.log(max(eps, min(1 - eps, p))) +
                (1 - int(y)) * math.log(max(eps, min(1 - eps, 1 - p)))
                for y, p in zip(actual, predicted)) / len(actual)


def multiclass_brier(actual, predicted):
    names = ("tp", "sl", "timeout")
    return sum(sum((float(prob[name]) - (1 if truth == name else 0)) ** 2
                   for name in names) for truth, prob in zip(actual, predicted)) / len(actual)


def multiclass_log_loss(actual, predicted):
    eps = 1e-9
    return -sum(math.log(max(eps, min(1.0, prob[truth])))
                for truth, prob in zip(actual, predicted)) / len(actual)


def _impute(train_rows, test_rows, feature_names):
    medians = {}
    for name in feature_names:
        values = sorted(row["features"].get(name) for row in train_rows
                        if row["features"].get(name) is not None)
        if not values:
            return None, None
        medians[name] = values[len(values) // 2]
    train_x = [[row["features"].get(name, medians[name])
                if row["features"].get(name) is not None else medians[name]
                for name in feature_names] for row in train_rows]
    test_x = [[row["features"].get(name, medians[name])
               if row["features"].get(name) is not None else medians[name]
               for name in feature_names] for row in test_rows]
    return train_x, test_x


def _independent_calibration_split(rows):
    """按时间留出尾段校准，并清除标签窗口与校准段重叠的训练样本。"""
    calibration_n = max(
        config.ENTRY_CALIBRATION_MIN_SAMPLES,
        int(math.ceil(len(rows) * config.ENTRY_CALIBRATION_FRACTION)),
    )
    if calibration_n >= len(rows):
        return list(rows), []
    calibration = list(rows[-calibration_n:])
    calibration_start = min(row["event_ts"] for row in calibration)
    fit_rows = [row for row in rows[:-calibration_n]
                if row["label_end_ts"] < calibration_start]
    return fit_rows, calibration


def _class_name(row):
    return "tp" if row["tp_first"] else "sl" if row["sl_first"] else "timeout"


def fit_catboost_artifact(train_x, train_rows, feature_names):
    """训练浅层三分类 Challenger 并序列化；CatBoost 只在训练路径导入。"""
    if not config.ENTRY_CATBOOST_ENABLED:
        return None
    labels = [(0 if row["tp_first"] else 1 if row["sl_first"] else 2)
              for row in train_rows]
    if len(set(labels)) < 2:
        return None
    try:
        from catboost import CatBoostClassifier
        model = CatBoostClassifier(
            loss_function="MultiClass",
            depth=config.ENTRY_CATBOOST_DEPTH,
            iterations=config.ENTRY_CATBOOST_ITERATIONS,
            learning_rate=config.ENTRY_CATBOOST_LEARNING_RATE,
            l2_leaf_reg=config.ENTRY_CATBOOST_L2_LEAF_REG,
            random_seed=config.ENTRY_CATBOOST_RANDOM_SEED,
            allow_writing_files=False,
            verbose=False,
            thread_count=1,
        )
        model.fit(train_x, labels, verbose=False)
        handle = tempfile.NamedTemporaryFile(suffix=".cbm", delete=False)
        handle.close()
        try:
            model.save_model(handle.name)
            with open(handle.name, "rb") as saved:
                encoded = base64.b64encode(saved.read()).decode("ascii")
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        priors = {
            "tp": sum(row["tp_first"] for row in train_rows) / len(train_rows),
            "sl": sum(row["sl_first"] for row in train_rows) / len(train_rows),
            "timeout": sum(row["timeout"] for row in train_rows) / len(train_rows),
        }
        timeouts = [row["pnl_r"] for row in train_rows if row["timeout"]]
        return {
            "model_family": "catboost_multiclass",
            "feature_names": list(feature_names),
            "catboost_model_b64": encoded,
            "catboost_classes": [int(value) for value in model.classes_],
            "n_train": len(train_rows),
            "class_priors": priors,
            "prior_strength": config.ENTRY_MODEL_PRIOR_STRENGTH,
            "mean_timeout_r": (sum(timeouts) / len(timeouts)
                               if timeouts else 0.0),
        }
    except Exception:
        return None


def select_model_family(champion: dict, challenger: dict):
    """CatBoost 只有自身过门且相对 Brier 增益足够时才能击败 Champion。"""
    champion_brier = champion.get("multiclass_brier")
    challenger_brier = challenger.get("multiclass_brier")
    relative_gain = (
        (champion_brier - challenger_brier) / champion_brier
        if champion_brier is not None and champion_brier > 0 and
        challenger_brier is not None else None)
    selected = "logistic_ovr"
    if (challenger.get("eligible_for_shadow") and
            relative_gain is not None and
            relative_gain >= config.ENTRY_CATBOOST_MIN_RELATIVE_BRIER_GAIN):
        selected = "catboost_multiclass"
    return selected, relative_gain


def evaluate_rows(rows: List[dict], feature_names: List[str],
                  model_family: str = "logistic_ovr"):
    splits = purged_walk_forward_splits(rows)
    folds = []
    all_y, all_p, all_base = [], [], []
    selected_truth = []
    selected_net_returns = []
    baseline_selected_truth = []
    baseline_selected_net_returns = []
    all_classes, all_class_p, all_class_base = [], [], []
    for fold, (train_idx, test_idx) in enumerate(splits):
        train = [rows[idx] for idx in train_idx]
        test = [rows[idx] for idx in test_idx]
        fit_rows, calibration_rows = _independent_calibration_split(train)
        train_x, holdout_x = _impute(
            fit_rows, calibration_rows + test, feature_names)
        if train_x is None:
            continue
        calibration_x = holdout_x[:len(calibration_rows)]
        test_x = holdout_x[len(calibration_rows):]
        labels = [row["tp_first"] for row in fit_rows]
        class_priors = {name: sum(row[f"{name}_first"] for row in fit_rows)
                        / len(fit_rows) for name in ("tp", "sl")}
        class_priors["timeout"] = (sum(row["timeout"] for row in fit_rows) /
                                    len(fit_rows))
        if model_family == "catboost_multiclass":
            fold_artifact = fit_catboost_artifact(
                train_x, fit_rows, feature_names)
            if fold_artifact is None:
                continue
        else:
            model = fit_logistic(train_x, labels)
            if model is None:
                continue
            class_models = {
                "tp": model,
                "sl": fit_logistic(
                    train_x, [row["sl_first"] for row in fit_rows]),
                "timeout": fit_logistic(
                    train_x, [row["timeout"] for row in fit_rows])}
            if any(value is None for value in class_models.values()):
                continue
            timeouts = [row["pnl_r"] for row in fit_rows if row["timeout"]]
            fold_artifact = {
                "model_family": "logistic_ovr",
                "feature_names": feature_names, "model": model,
                "class_models": class_models, "class_priors": class_priors,
                "prior_strength": config.ENTRY_MODEL_PRIOR_STRENGTH,
                "mean_timeout_r": (sum(timeouts) / len(timeouts)
                                   if timeouts else 0.0),
            }
        calibration_predictions = [predict_from_artifact(
            fold_artifact, dict(zip(feature_names, values)),
            cost_r_override=row["cost_r"])
            for row, values in zip(calibration_rows, calibration_x)]
        if (not calibration_predictions or
                any(value is None for value in calibration_predictions)):
            continue
        calibration = fit_temperature(
            [{name: prediction[f"p_{name}"]
              for name in ("tp", "sl", "timeout")}
             for prediction in calibration_predictions],
            [_class_name(row) for row in calibration_rows])
        if not calibration["passed"]:
            continue
        fold_artifact["temperature"] = calibration["temperature"]
        predictions = [predict_from_artifact(
            fold_artifact, dict(zip(feature_names, values)),
            cost_r_override=row["cost_r"])
            for row, values in zip(test, test_x)]
        if any(value is None for value in predictions):
            continue
        probs = [prediction["p_tp"] for prediction in predictions]
        class_probs = [{name: prediction[f"p_{name}"]
                        for name in ("tp", "sl", "timeout")}
                       for prediction in predictions]
        truth = [row["tp_first"] for row in test]
        truth_classes = [("tp" if row["tp_first"] else
                          "sl" if row["sl_first"] else "timeout") for row in test]
        base_rate = sum(labels) / len(labels)
        baseline = [base_rate] * len(test)
        class_baseline = [class_priors] * len(test)
        model_brier, base_brier = brier(truth, probs), brier(truth, baseline)
        model_multi = multiclass_brier(truth_classes, class_probs)
        base_multi = multiclass_brier(truth_classes, class_baseline)
        selected = [row for row, prediction in zip(test, predictions)
                    if float(prediction["ev_r_lower"]) > 0]
        selected_y = [row["tp_first"] for row in selected]
        selected_net = [row["pnl_r"] - row["cost_r"] for row in selected]
        # 与现役连续信号分在完全相同的覆盖率下比较。直接拿模型子集对
        # 全候选总体胜率，会把覆盖率变化误报成 precision 提升。
        baseline_ranked = sorted(
            test,
            key=lambda row: (
                float(row.get("baseline_score"))
                if row.get("baseline_score") is not None else float("-inf"),
                str(row.get("signal_id") or ""),
            ),
            reverse=True,
        )
        baseline_selected = baseline_ranked[:len(selected)]
        baseline_selected_y = [row["tp_first"] for row in baseline_selected]
        baseline_selected_net = [row["pnl_r"] - row["cost_r"]
                                 for row in baseline_selected]
        precision = (sum(selected_y) / len(selected_y)
                     if selected_y else 0.0)
        baseline_precision = (sum(baseline_selected_y) /
                              len(baseline_selected_y)
                              if baseline_selected_y else 0.0)
        actual_ev = (sum(row["pnl_r"] - row["cost_r"] for row in selected) /
                     len(selected)) if selected else -99.0
        baseline_ev = (sum(baseline_selected_net) / len(baseline_selected_net)
                       if baseline_selected_net else -99.0)
        population_ev = sum(row["pnl_r"] - row["cost_r"] for row in test) / len(test)
        folds.append({"fold": fold, "n": len(test), "brier": model_brier,
                      "baseline_brier": base_brier,
                      "log_loss": log_loss(truth, probs),
                      "multiclass_brier": model_multi,
                      "baseline_multiclass_brier": base_multi,
                      "multiclass_log_loss": multiclass_log_loss(
                          truth_classes, class_probs),
                      "net_ev": actual_ev, "baseline_ev": baseline_ev,
                      "population_ev": population_ev,
                      "coverage": len(selected) / len(test),
                      "selected_n": len(selected),
                      "baseline_selected_n": len(baseline_selected),
                      "precision": precision,
                      "baseline_precision": baseline_precision,
                      "population_precision": sum(truth) / len(truth),
                      "precision_lift": precision - baseline_precision,
                      "calibration_n": calibration["n"],
                      "temperature": calibration["temperature"],
                      "calibration_raw_log_loss": calibration["raw_log_loss"],
                      "calibration_log_loss": calibration["calibrated_log_loss"],
                      "calibration_passed": calibration["passed"]})
        all_y.extend(truth)
        all_p.extend(probs)
        all_base.extend(baseline)
        all_classes.extend(truth_classes)
        all_class_p.extend(class_probs)
        all_class_base.extend(class_baseline)
        selected_truth.extend(selected_y)
        selected_net_returns.extend(selected_net)
        baseline_selected_truth.extend(baseline_selected_y)
        baseline_selected_net_returns.extend(baseline_selected_net)
    if not all_y:
        return {"feature_names": list(feature_names), "model_family": model_family,
                "folds": folds,
                "brier_skill": None, "good_brier_folds": 0,
                "multiclass_brier_skill": None, "good_multiclass_folds": 0,
                "good_calibration_folds": 0,
                "good_ev_folds": 0, "good_precision_folds": 0,
                "precision": None, "baseline_precision": None,
                "population_precision": None, "precision_lift": None,
                "selected_n": 0, "baseline_selected_n": 0,
                "baseline_oos_net_ev": None,
                "oos_net_ev": None, "oos_net_ev_lower_bound": None,
                "eligible_for_shadow": False}
    base_score = brier(all_y, all_base)
    skill = 1 - brier(all_y, all_p) / base_score if base_score > 0 else 0.0
    good_brier = sum(fold["brier"] <= fold["baseline_brier"] for fold in folds)
    multi_base = multiclass_brier(all_classes, all_class_base)
    multi_skill = (1 - multiclass_brier(all_classes, all_class_p) / multi_base
                   if multi_base > 0 else 0.0)
    good_multi = sum(fold["multiclass_brier"] <=
                     fold["baseline_multiclass_brier"] for fold in folds)
    good_ev = sum(fold["net_ev"] > 0 and
                  fold["net_ev"] >= fold["baseline_ev"] for fold in folds)
    good_precision = sum(fold["precision"] > fold["baseline_precision"]
                         for fold in folds)
    good_calibration = sum(fold["calibration_passed"] for fold in folds)
    precision = (sum(selected_truth) / len(selected_truth)
                 if selected_truth else 0.0)
    baseline_precision = (sum(baseline_selected_truth) /
                          len(baseline_selected_truth)
                          if baseline_selected_truth else 0.0)
    oos_net_ev = (sum(selected_net_returns) / len(selected_net_returns)
                  if selected_net_returns else None)
    if selected_net_returns:
        variance = (sum((value - oos_net_ev) ** 2
                        for value in selected_net_returns) /
                    (len(selected_net_returns) - 1)
                    if len(selected_net_returns) > 1 else 0.0)
        oos_lower = (oos_net_ev - config.ENTRY_MODEL_EV_Z *
                     math.sqrt(variance / len(selected_net_returns)))
    else:
        oos_lower = None
    eligible = (len(folds) >= config.FACTOR_WALK_FORWARD_FOLDS and
                skill > config.ENTRY_MODEL_MIN_BRIER_SKILL and
                good_brier >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                good_multi >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                good_ev >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                good_precision >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                good_calibration >= config.ENTRY_CALIBRATION_MIN_GOOD_FOLDS and
                len(selected_net_returns) >=
                config.MODEL_MIN_SELECTED_EVALUATIONS and
                precision > baseline_precision and
                oos_lower is not None and oos_lower > 0)
    return {"feature_names": list(feature_names), "model_family": model_family,
            "folds": folds,
            "brier": brier(all_y, all_p), "baseline_brier": base_score,
            "multiclass_brier": multiclass_brier(all_classes, all_class_p),
            "baseline_multiclass_brier": multi_base,
            "brier_skill": skill,
            "multiclass_brier_skill": multi_skill,
            "good_brier_folds": good_brier,
            "good_multiclass_folds": good_multi, "good_ev_folds": good_ev,
            "good_precision_folds": good_precision,
            "good_calibration_folds": good_calibration,
            "precision": precision, "baseline_precision": baseline_precision,
            "population_precision": sum(all_y) / len(all_y),
            "precision_lift": precision - baseline_precision,
            "selected_n": len(selected_net_returns),
            "baseline_selected_n": len(baseline_selected_net_returns),
            "baseline_oos_net_ev": (
                sum(baseline_selected_net_returns) /
                len(baseline_selected_net_returns)
                if baseline_selected_net_returns else None),
            "oos_net_ev": oos_net_ev,
            "oos_net_ev_lower_bound": oos_lower,
            "eligible_for_shadow": eligible}


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
        "SELECT s.*,o.pnl_r,o.tp_first,o.sl_first,o.timeout "
        "FROM signal_samples s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id WHERE s.direction=? "
        "AND s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=?" +
        scope_sql + " ORDER BY s.event_ts",
        [direction, strategy_id,
         config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS,
         *([scope_version] if scope_version else [])], db_path=db_path)
    rows = []
    for sample in samples:
        cost_r = float(execution_cost_r(sample) or 0.0)
        try:
            frozen = json.loads(sample.get("features") or "{}")
            baseline_score = (frozen.get("shadow_score")
                              if isinstance(frozen, dict) else None)
        except (TypeError, ValueError, json.JSONDecodeError):
            baseline_score = None
        rows.append({"signal_id": sample["signal_id"],
                     "event_ts": sample["event_ts"],
                     "kline_ts": sample["kline_ts"],
                     "label_end_ts": sample["event_ts"] +
                     sample["horizon_hours"] * 3600,
                     "features": extract_features(sample),
                     "tp_first": int(sample["tp_first"]),
                     "sl_first": int(sample["sl_first"]),
                     "timeout": int(sample["timeout"]),
                     "pnl_r": float(sample["pnl_r"]), "cost_r": cost_r,
                     "baseline_score": baseline_score})
    return rows


def train_entry_model(direction, db_path=None, feature_names=None,
                      strategy_id=None):
    """训练候选制品；不足门槛返回 insufficient，不写可用模型。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    strategy_version = research_scope_version(strategy_id)
    names = list(feature_names or _validated_features(db_path, strategy_id))
    names = names[:config.ENTRY_MODEL_MAX_FEATURES]
    # 即使尚无 validated 特征也要读取标签样本，报告真实 n/tp_n/sl_n；
    # “不能训练”和“没有数据”是两个不同阻塞原因，不能都伪装成 n=0。
    rows = _load_rows(direction, names, db_path, strategy_id)
    tp_n = sum(row["tp_first"] for row in rows)
    sl_n = sum(row["sl_first"] for row in rows)
    if (len(rows) < config.ENTRY_MODEL_MIN_SAMPLES or
            tp_n < config.ENTRY_MODEL_MIN_TP or sl_n < config.ENTRY_MODEL_MIN_SL or
            not names):
        return {"status": "insufficient_data", "strategy_id": strategy_id,
                "n": len(rows),
                "tp_n": tp_n, "sl_n": sl_n, "features": names}
    champion_evaluation = evaluate_rows(rows, names, "logistic_ovr")
    challenger_evaluation = evaluate_rows(
        rows, names, "catboost_multiclass")
    selected_family, relative_gain = select_model_family(
        champion_evaluation, challenger_evaluation)
    evaluation = dict(challenger_evaluation if
                      selected_family == "catboost_multiclass" else
                      champion_evaluation)
    evaluation["champion_challenger"] = {
        "champion_family": "logistic_ovr",
        "challenger_family": "catboost_multiclass",
        "selected_family": selected_family,
        "relative_multiclass_brier_gain": relative_gain,
        "minimum_relative_gain":
            config.ENTRY_CATBOOST_MIN_RELATIVE_BRIER_GAIN,
        "champion": champion_evaluation,
        "challenger": challenger_evaluation,
    }
    version = ("entry-catboost-multiclass-v2-purged-tempcal-ci"
               if selected_family == "catboost_multiclass" else
               "entry-logit-ovr-v6-champion-tempcal-ci")
    # 不能只哈希 signal_id：标签回填、成本口径或特征修正后，即使候选身份
    # 不变，也必须生成新的制品身份，避免错误复用旧模型。
    data_evidence = [{
        "signal_id": row["signal_id"],
        "event_ts": row["event_ts"],
        "kline_ts": row.get("kline_ts"),
        "label_end_ts": row["label_end_ts"],
        "features": {name: row["features"].get(name) for name in names},
        "tp_first": row["tp_first"],
        "sl_first": row["sl_first"],
        "timeout": row["timeout"],
        "pnl_r": row["pnl_r"],
        "cost_r": row["cost_r"],
    } for row in rows]
    data_digest = hashlib.sha256(json.dumps(
        data_evidence, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    signature_data = {
        "data_hash": data_digest, "version": version, "features": names,
        "strategy_id": strategy_id,
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        "cost_model_version": config.ENTRY_COST_MODEL_VERSION,
        "l2": config.ENTRY_MODEL_L2, "epochs": config.ENTRY_MODEL_EPOCHS,
        "prior": config.ENTRY_MODEL_PRIOR_STRENGTH,
        "selected_family": selected_family,
        "catboost": {
            "depth": config.ENTRY_CATBOOST_DEPTH,
            "iterations": config.ENTRY_CATBOOST_ITERATIONS,
            "learning_rate": config.ENTRY_CATBOOST_LEARNING_RATE,
            "l2_leaf_reg": config.ENTRY_CATBOOST_L2_LEAF_REG,
            "random_seed": config.ENTRY_CATBOOST_RANDOM_SEED,
            "minimum_relative_brier_gain":
                config.ENTRY_CATBOOST_MIN_RELATIVE_BRIER_GAIN,
        },
        "calibration": {
            "fraction": config.ENTRY_CALIBRATION_FRACTION,
            "minimum_samples": config.ENTRY_CALIBRATION_MIN_SAMPLES,
            "temperature_min": config.ENTRY_TEMPERATURE_MIN,
            "temperature_max": config.ENTRY_TEMPERATURE_MAX,
            "grid_size": config.ENTRY_TEMPERATURE_GRID_SIZE,
        }}
    if strategy_version:
        signature_data["strategy_version"] = strategy_version
    signature = hashlib.sha256(json.dumps(
        signature_data, sort_keys=True).encode("utf-8")).hexdigest()
    prefix = ("entry" if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID
              else f"entry_{strategy_id}")
    model_id = f"{prefix}_{direction}_{signature[:16]}"
    existing = sdb.q1("SELECT state FROM model_artifacts WHERE model_id=?",
                      [model_id], db_path=db_path)
    if existing:
        return {"status": existing["state"], "model_id": model_id,
                "strategy_id": strategy_id,
                "n": len(rows), "tp_n": tp_n, "sl_n": sl_n,
                "features": names, "model_family": selected_family,
                "reused": True}
    fit_rows, calibration_rows = _independent_calibration_split(rows)
    train_x, calibration_x = _impute(fit_rows, calibration_rows, names)
    if (train_x is None or
            len(calibration_rows) < config.ENTRY_CALIBRATION_MIN_SAMPLES):
        return {"status": "insufficient_calibration",
                "strategy_id": strategy_id, "n": len(rows),
                "calibration_n": len(calibration_rows), "features": names}
    fit_tp_n = sum(row["tp_first"] for row in fit_rows)
    fit_sl_n = sum(row["sl_first"] for row in fit_rows)
    non_tp = [row for row in fit_rows if not row["tp_first"]]
    if not non_tp or fit_tp_n == 0 or fit_sl_n == 0:
        return {"status": "insufficient_calibration_fit",
                "strategy_id": strategy_id, "n": len(rows),
                "fit_n": len(fit_rows), "calibration_n": len(calibration_rows),
                "features": names}
    sl_given = sum(row["sl_first"] for row in non_tp) / len(non_tp)
    timeouts = [row["pnl_r"] for row in fit_rows if row["timeout"]]
    common_artifact = {"version": version, "direction": direction,
                "strategy_id": strategy_id,
                "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
                "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
                "feature_names": names,
                "class_priors": {
                    "tp": fit_tp_n / len(fit_rows),
                    "sl": fit_sl_n / len(fit_rows),
                    "timeout": sum(row["timeout"] for row in fit_rows) /
                    len(fit_rows)},
                "prior_strength": config.ENTRY_MODEL_PRIOR_STRENGTH,
                "cost_model_version": config.ENTRY_COST_MODEL_VERSION,
                "sl_given_not_tp": sl_given,
                "mean_timeout_r": sum(timeouts) / len(timeouts) if timeouts else 0.0,
                "cost_r": (sum(row["cost_r"] for row in fit_rows) /
                           len(fit_rows))}
    if selected_family == "catboost_multiclass":
        artifact = fit_catboost_artifact(train_x, fit_rows, names)
        if artifact is None:
            selected_family = "logistic_ovr"
            evaluation = champion_evaluation
        else:
            artifact.update(common_artifact)
    if selected_family == "logistic_ovr":
        labels = [row["tp_first"] for row in fit_rows]
        model = fit_logistic(train_x, labels)
        artifact = dict(common_artifact, model_family="logistic_ovr",
                        model=model, class_models={
                            "tp": model,
                            "sl": fit_logistic(
                                train_x, [row["sl_first"] for row in fit_rows]),
                            "timeout": fit_logistic(
                                train_x, [row["timeout"] for row in fit_rows]),
                        })
    artifact["model_family"] = selected_family
    artifact["n_train"] = len(fit_rows)
    raw_calibration = [predict_from_artifact(
        artifact, dict(zip(names, values)), cost_r_override=row["cost_r"])
        for row, values in zip(calibration_rows, calibration_x)]
    if any(prediction is None for prediction in raw_calibration):
        return {"status": "calibration_failed", "strategy_id": strategy_id,
                "n": len(rows), "calibration_n": len(calibration_rows),
                "features": names}
    calibration = fit_temperature(
        [{name: prediction[f"p_{name}"] for name in ("tp", "sl", "timeout")}
         for prediction in raw_calibration],
        [_class_name(row) for row in calibration_rows])
    if not calibration["passed"]:
        return {"status": "calibration_failed", "strategy_id": strategy_id,
                "n": len(rows), "calibration_n": len(calibration_rows),
                "features": names, "calibration": calibration}
    artifact["temperature"] = calibration["temperature"]
    artifact["calibration_metrics"] = calibration
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
            [model_id, "entry_probability", strategy_id, strategy_version,
             direction, artifact["version"],
             "candidate", now, max(row["event_ts"] for row in rows), data_digest,
             json.dumps(names), json.dumps(artifact), json.dumps(evaluation)])
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
            "INSERT INTO model_evaluations (model_id,fold,ts,n_samples,brier,"
            "baseline_brier,log_loss,net_ev,baseline_ev,coverage,details) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [model_id, fold["fold"], now, fold["n"], fold["brier"],
             fold["baseline_brier"], fold["log_loss"], fold["net_ev"],
             fold["baseline_ev"], fold["coverage"], json.dumps({
                 "multiclass_brier": fold["multiclass_brier"],
                 "baseline_multiclass_brier": fold["baseline_multiclass_brier"],
                 "multiclass_log_loss": fold["multiclass_log_loss"],
                 "precision": fold["precision"],
                 "baseline_precision": fold["baseline_precision"],
                 "precision_lift": fold["precision_lift"]})],
            db_path=db_path)
    return {"status": state, "model_id": model_id,
            "strategy_id": strategy_id, "n": len(rows),
            "tp_n": tp_n, "sl_n": sl_n, "features": names,
            "model_family": selected_family,
            "evaluation": evaluation}
