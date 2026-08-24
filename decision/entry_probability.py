"""开仓 TP-first 概率消费端；模型缺失/损坏时 fail-safe 回现役规则。"""
import json
import math
from typing import Dict, List, Optional

import config
from decision.feature_transforms import materialize_derived_features
from decision.signal_identity import research_scope_version


def sigmoid(value):
    value = max(-35.0, min(35.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def expected_value_r(p_tp, p_sl, p_timeout, timeout_r=0.0, cost_r=0.0):
    """权威 EV 口径：2R×TP - 1R×SL + timeout×E[R] - 成本。"""
    return (2 * float(p_tp) - float(p_sl) +
            float(p_timeout) * float(timeout_r) - float(cost_r))


def fit_logistic(features: List[List[float]], labels: List[int],
                 l2=None, learning_rate=None, epochs=None) -> Optional[dict]:
    if not features or len(features) != len(labels) or not features[0]:
        return None
    width = len(features[0])
    if any(len(row) != width for row in features):
        return None
    means = [sum(row[j] for row in features) / len(features) for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in features) / len(features)
        scales.append(math.sqrt(variance) or 1.0)
    x_rows = [[(row[j] - means[j]) / scales[j] for j in range(width)]
              for row in features]
    base = min(1 - 1e-6, max(1e-6, sum(labels) / len(labels)))
    bias = math.log(base / (1 - base))
    weights = [0.0] * width
    rate = float(learning_rate if learning_rate is not None
                 else config.ENTRY_MODEL_LEARNING_RATE)
    penalty = float(l2 if l2 is not None else config.ENTRY_MODEL_L2)
    epochs = int(epochs if epochs is not None else config.ENTRY_MODEL_EPOCHS)
    for _ in range(epochs):
        grad_b = 0.0
        grad_w = [0.0] * width
        for row, label in zip(x_rows, labels):
            pred = sigmoid(bias + sum(weight * value
                                      for weight, value in zip(weights, row)))
            error = pred - int(label)
            grad_b += error
            for j, value in enumerate(row):
                grad_w[j] += error * value
        n = len(x_rows)
        bias -= rate * grad_b / n
        for j in range(width):
            weights[j] -= rate * (grad_w[j] / n + penalty * weights[j])
    return {"means": means, "scales": scales, "weights": weights,
            "bias": bias, "n_train": len(features), "base_rate": base}


def raw_probability(model: dict, values: List[float]) -> Optional[float]:
    try:
        if len(values) != len(model["weights"]):
            return None
        normalized = [(float(value) - mean) / scale for value, mean, scale in
                      zip(values, model["means"], model["scales"])]
        return sigmoid(float(model["bias"]) + sum(
            float(weight) * value for weight, value in zip(model["weights"], normalized)))
    except Exception:
        return None


def raw_class_probabilities(models: dict, values: List[float]) -> Optional[dict]:
    """one-vs-rest 三分类分数归一化；任一模型损坏则整组 fail-safe。"""
    raw = {name: raw_probability(models.get(name), values)
           for name in ("tp", "sl", "timeout")}
    if any(value is None for value in raw.values()):
        return None
    total = sum(raw.values())
    if total <= 0:
        return None
    return {name: value / total for name, value in raw.items()}


def signal_feature_values(sig: Dict, feature_names: List[str]):
    dims = dict(sig.get("shadow_dims") or {})
    raw = materialize_derived_features(
        dict(sig.get("factor_features") or {}), dims)
    values = []
    for name in feature_names:
        value = dims.get(name, raw.get(name))
        if value is None:
            return None
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    return values


def _funding_rate(sig: Dict) -> Optional[float]:
    """Read the signal-time funding rate from flat or frozen feature inputs."""
    value = sig.get("funding_rate")
    factor_features = sig.get("factor_features")
    if value is None and isinstance(factor_features, dict):
        value = factor_features.get("funding_rate")
    if value is None:
        try:
            snapshot = json.loads(sig.get("features") or "{}")
            value = (snapshot.get("factor_features") or {}).get("funding_rate")
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cost_breakdown_r(sig: Dict) -> Optional[dict]:
    """Return conservative signal-time roundtrip and expected funding costs.

    Longs pay positive funding and shorts pay negative funding.  Because a
    four-hour trade may or may not cross the next settlement, the current rate
    is prorated by holding horizon / configured funding interval.  Potential
    funding income is deliberately floored at zero: uncertain carry must not
    make an otherwise negative-EV entry pass the strict 2:1 gate.
    """
    try:
        entry = float(sig.get("entry") or 0)
        stop = float(sig.get("stop") or 0)
        risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
        if risk_pct <= 0:
            return None
        trading_pct = 2 * (config.FEE_RATE_TAKER + config.SLIPPAGE)
        funding_rate = _funding_rate(sig)
        direction = str(sig.get("dir") or sig.get("direction") or "")
        direction_sign = 1.0 if direction == "long" else -1.0
        horizon = float(sig.get("horizon_hours") or
                        config.SIGNAL_OUTCOME_HORIZON_HOURS)
        interval = float(config.FUNDING_EXPECTED_INTERVAL_HOURS)
        aligned_rate = (direction_sign * funding_rate
                        if funding_rate is not None and
                        direction in ("long", "short") else 0.0)
        funding_pct = max(0.0, aligned_rate) * max(0.0, horizon) / interval
        trading_r = trading_pct / risk_pct
        funding_r = funding_pct / risk_pct
        return {"trading_cost_r": trading_r,
                "funding_cost_r": funding_r,
                "total_cost_r": trading_r + funding_r,
                "funding_rate": funding_rate,
                "cost_model_version": config.ENTRY_COST_MODEL_VERSION}
    except (TypeError, ValueError):
        return None


def execution_cost_r(sig: Dict) -> Optional[float]:
    """Compatibility accessor for total conservative candidate cost in R."""
    breakdown = cost_breakdown_r(sig)
    return breakdown["total_cost_r"] if breakdown is not None else None


def predict_from_artifact(artifact: dict, feature_values: Dict[str, float],
                          cost_r_override: Optional[float] = None):
    try:
        names = list(artifact["feature_names"])
        values = [float(feature_values[name]) for name in names]
        n = float(artifact["model"].get("n_train") or 0)
        prior = float(artifact.get("prior_strength") or
                      config.ENTRY_MODEL_PRIOR_STRENGTH)
        weight = n / (n + prior) if n + prior > 0 else 0.0
        if artifact.get("class_models"):
            raw_classes = raw_class_probabilities(artifact["class_models"], values)
            if raw_classes is None:
                return None
            priors = artifact.get("class_priors") or {}
            shrunk = {name: weight * raw_classes[name] +
                      (1 - weight) * float(priors.get(name, 1 / 3))
                      for name in raw_classes}
            total = sum(shrunk.values())
            p_tp, p_sl, p_timeout = (shrunk["tp"] / total,
                                     shrunk["sl"] / total,
                                     shrunk["timeout"] / total)
            probability_method = "ovr_multiclass_beta_shrink"
        else:
            # 兼容已有二分类制品；新训练器一律写 class_models。
            raw = raw_probability(artifact["model"], values)
            if raw is None:
                return None
            base = float(artifact["model"].get("base_rate") or 0.5)
            p_tp = weight * raw + (1 - weight) * base
            sl_given_not_tp = float(artifact.get("sl_given_not_tp", 0.5))
            p_sl = (1 - p_tp) * sl_given_not_tp
            p_timeout = max(0.0, 1 - p_tp - p_sl)
            probability_method = "binary_beta_shrink_legacy"
        timeout_r = float(artifact.get("mean_timeout_r", 0.0))
        cost_r = float(artifact.get("cost_r", 0.0) if cost_r_override is None
                       else cost_r_override)
        ev = expected_value_r(p_tp, p_sl, p_timeout, timeout_r, cost_r)
        payoff_mean = ev + cost_r
        payoff_var = (p_tp * (2 - payoff_mean) ** 2 +
                      p_sl * (-1 - payoff_mean) ** 2 +
                      p_timeout * (timeout_r - payoff_mean) ** 2)
        se = math.sqrt(max(0.0, payoff_var) / max(1.0, n))
        baseline_p_tp = float((artifact.get("class_priors") or {}).get(
            "tp", artifact["model"].get("base_rate", 0.5)))
        return {"p_tp": round(p_tp, 6), "p_sl": round(p_sl, 6),
                "p_timeout": round(p_timeout, 6), "ev_r": round(ev, 6),
                "ev_r_lower": round(ev - config.ENTRY_MODEL_EV_Z * se, 6),
                "cost_r": round(cost_r, 6),
                "binary_breakeven_win_rate": round((1 + cost_r) / 3, 6),
                "baseline_p_tp": round(baseline_p_tp, 6),
                "confidence": round(max(0.0, 1 - se), 6),
                "calibration": "beta_shrink",
                "probability_method": probability_method}
    except Exception:
        return None


def load_artifact(direction, db_path=None, allow_shadow=False, strategy_id=None):
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    states = ("active", "observing", "kept") if not allow_shadow else (
        "active", "observing", "kept", "shadow", "validated")
    placeholders = ",".join("?" for _ in states)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND strategy_version=?" if scope_version else ""
    row = sdb.q1(
        f"SELECT * FROM model_artifacts WHERE model_type='entry_probability' "
        f"AND strategy_id=? AND direction=? AND state IN ({placeholders}) "
        + scope_sql + " ORDER BY created_at DESC LIMIT 1",
        [strategy_id, direction, *states,
         *([scope_version] if scope_version else [])], db_path=db_path)
    if not row:
        return None
    try:
        artifact = json.loads(row["artifact"])
        if (artifact.get("strategy_id") != strategy_id or
                (scope_version and
                 artifact.get("strategy_version") != scope_version) or
                artifact.get("timeframe") != config.SIGNAL_SAMPLE_TIMEFRAME or
                int(artifact.get("horizon_hours") or 0) !=
                config.SIGNAL_OUTCOME_HORIZON_HOURS or
                artifact.get("cost_model_version") !=
                config.ENTRY_COST_MODEL_VERSION):
            return None
        artifact["model_id"] = row["model_id"]
        artifact["state"] = row["state"]
        return artifact
    except Exception:
        return None


def predict_signal(sig, db_path=None, allow_shadow=True):
    strategy_id = str(sig.get("strategy_id") or
                      config.ENTRY_SIGNAL_STRATEGY_ID)
    artifact = load_artifact(sig.get("dir"), db_path,
                             allow_shadow=allow_shadow,
                             strategy_id=strategy_id)
    if not artifact:
        return None
    names = list(artifact.get("feature_names") or [])
    values = signal_feature_values(sig, names)
    if values is None:
        return None
    candidate_cost_r = execution_cost_r(sig)
    if candidate_cost_r is None:
        return None
    prediction = predict_from_artifact(
        artifact, dict(zip(names, values)), cost_r_override=candidate_cost_r)
    if prediction is None:
        return None
    prediction.update({"model_version": artifact.get("version"),
                       "model_id": artifact.get("model_id"),
                       "state": artifact.get("state"),
                       "decision_effective": artifact.get("state") in
                       ("active", "observing", "kept")})
    breakdown = cost_breakdown_r(sig) or {}
    prediction.update({name: round(float(breakdown[name]), 6)
                       for name in ("trading_cost_r", "funding_cost_r")
                       if breakdown.get(name) is not None})
    prediction["cost_model_version"] = config.ENTRY_COST_MODEL_VERSION
    return prediction


def entry_gate_decision(sig, db_path=None):
    """只有 active 模型可否决；无模型/损坏/缺特征一律保持现役行为。"""
    prediction = predict_signal(sig, db_path=db_path, allow_shadow=False)
    if not prediction:
        return True, None
    return prediction["ev_r_lower"] > 0, prediction


def preopen_2to1_decision(sig, prediction=None, db_path=None):
    """模拟盘严格开仓闸门：固定 2:1、已验证模型、成本后 EV 下界均须通过。

    这里与兼容用 ``entry_gate_decision`` 的 fail-safe 语义刻意分开：后者不改变
    旧调用方；本函数用于用户明确要求的“先预测 2:1 再开仓”，模型缺失时
    fail-closed。候选留样发生在本闸门之前，拒绝不会造成监督标签断流。
    """
    entry = float(sig.get("entry") or 0)
    stop = float(sig.get("stop") or 0)
    tp = float(sig.get("tp") or 0)
    risk = abs(entry - stop)
    actual_rr = abs(tp - entry) / risk if entry > 0 and risk > 0 else None
    required_rr = float(config.ENTRY_REQUIRED_REWARD_RISK)
    result = {
        "required_reward_risk": required_rr,
        "actual_reward_risk": round(actual_rr, 6) if actual_rr is not None else None,
        "passed": False,
        "reason": "invalid_trade_geometry",
        "prediction_source": "validated_entry_probability",
    }
    if actual_rr is None or not math.isclose(actual_rr, required_rr,
                                              rel_tol=1e-6, abs_tol=1e-6):
        return result
    candidate_cost_r = execution_cost_r(sig)
    if candidate_cost_r is None:
        result["reason"] = "invalid_cost_geometry"
        return result
    result.update({
        "candidate_cost_r": round(candidate_cost_r, 6),
        "binary_breakeven_win_rate": round((1 + candidate_cost_r) / 3, 6),
    })
    breakdown = cost_breakdown_r(sig) or {}
    result.update({name: round(float(breakdown[name]), 6)
                   for name in ("trading_cost_r", "funding_cost_r")
                   if breakdown.get(name) is not None})
    result["cost_model_version"] = config.ENTRY_COST_MODEL_VERSION
    if prediction is None:
        prediction = predict_signal(sig, db_path=db_path, allow_shadow=False)
    if not prediction:
        result["reason"] = "no_validated_active_model"
        return result
    try:
        prediction_cost = float(prediction["cost_r"])
    except (KeyError, TypeError, ValueError):
        result["reason"] = "prediction_cost_missing"
        return result
    if not math.isclose(prediction_cost, candidate_cost_r,
                        rel_tol=1e-6, abs_tol=1e-6):
        result["reason"] = "prediction_cost_mismatch"
        return result
    result.update({
        "model_id": prediction.get("model_id"),
        "model_version": prediction.get("model_version"),
        "model_state": prediction.get("state"),
        "predicted_win_rate": prediction.get("p_tp"),
        "predicted_stop_rate": prediction.get("p_sl"),
        "predicted_timeout_rate": prediction.get("p_timeout"),
        "cost_adjusted_ev_r": prediction.get("ev_r"),
        "cost_adjusted_ev_r_lower": prediction.get("ev_r_lower"),
    })
    if not prediction.get("decision_effective"):
        result["reason"] = "model_not_active"
        return result
    if float(prediction.get("ev_r_lower") or 0) <= 0:
        result["reason"] = "non_positive_conservative_ev"
        return result
    result["passed"] = True
    result["reason"] = "validated_2to1_positive_ev"
    return result
