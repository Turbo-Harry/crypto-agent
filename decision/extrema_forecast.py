"""未来最高/最低的条件分位与 adaptive conformal 校准。

不预测保证点位。首版以方向+regime 滚动经验分位作基线；样本达到门槛后
可训练带 L2 的线性分位模型。任何分位交叉都拒绝输出，不静默排序美化。
"""
import json
import math
from typing import Dict, Iterable, List, Optional

import config
from decision.signal_identity import research_scope_version


def quantile(values: Iterable[float], tau: float) -> Optional[float]:
    values = sorted(float(value) for value in values)
    if not values:
        return None
    position = max(0.0, min(1.0, tau)) * (len(values) - 1)
    lo, hi = int(math.floor(position)), int(math.ceil(position))
    if lo == hi:
        return values[lo]
    weight = position - lo
    return values[lo] * (1 - weight) + values[hi] * weight


def pinball_loss(actual, predicted, tau):
    pairs = list(zip(actual, predicted))
    if not pairs:
        return None
    total = 0.0
    for truth, pred in pairs:
        error = float(truth) - float(pred)
        total += tau * error if error >= 0 else (tau - 1) * error
    return total / len(pairs)


def interval_coverage(actual, lower, upper):
    triples = list(zip(actual, lower, upper))
    if not triples:
        return None
    return sum(1 for truth, lo, hi in triples if lo <= truth <= hi) / len(triples)


def conformal_radius(actual, lower, upper, alpha=0.2, window=None):
    triples = list(zip(actual, lower, upper))
    if window:
        triples = triples[-int(window):]
    scores = [max(float(lo) - float(truth), float(truth) - float(hi), 0.0)
              for truth, lo, hi in triples]
    return quantile(scores, 1 - alpha) or 0.0


def validate_quantiles(prediction: Dict[str, float]) -> bool:
    return (prediction["q10"] <= prediction["q50"] <= prediction["q90"])


def fit_linear_quantiles(features: List[List[float]], targets: List[float],
                         quantiles=None, l2=None, learning_rate=None,
                         epochs=None, min_samples=None) -> Optional[dict]:
    """小型 L2 线性分位回归；训练不足时返回 None，不强行拟合。"""
    threshold = (config.EXTREMA_MIN_MODEL_SAMPLES if min_samples is None
                 else int(min_samples))
    if (len(features) < threshold or not features or
            len(features) != len(targets)):
        return None
    width = len(features[0])
    if width == 0 or any(len(row) != width for row in features):
        return None
    quantiles = tuple(quantiles or config.EXTREMA_QUANTILES)
    l2 = float(l2 if l2 is not None else config.EXTREMA_L2)
    rate = float(learning_rate if learning_rate is not None
                 else config.EXTREMA_LEARNING_RATE)
    epochs = int(epochs if epochs is not None else config.EXTREMA_EPOCHS)
    means = [sum(row[j] for row in features) / len(features) for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in features) / len(features)
        scales.append(math.sqrt(variance) or 1.0)
    x_rows = [[(row[j] - means[j]) / scales[j] for j in range(width)]
              for row in features]
    # 首版使用 location-shift 约束：共享一条 L2 正则化中位斜率，q10/q90
    # 由训练残差分位平移。三条超平面因此全域平行、结构上不交叉；这不是
    # 展示层排序，若制品损坏/未来换成自由斜率后发生交叉，消费端仍拒绝。
    weights = [0.0] * width
    bias = quantile(targets, 0.5) or 0.0
    for _ in range(epochs):
        grad_w = [0.0] * width
        grad_b = 0.0
        for row, truth in zip(x_rows, targets):
            pred = bias + sum(weight * value
                              for weight, value in zip(weights, row))
            derivative = -0.5 if truth > pred else 0.5
            grad_b += derivative
            for j, value in enumerate(row):
                grad_w[j] += derivative * value
        n = len(x_rows)
        bias -= rate * grad_b / n
        for j in range(width):
            weights[j] -= rate * (grad_w[j] / n + l2 * weights[j])
    residuals = [truth - (bias + sum(weight * value
                                     for weight, value in zip(weights, row)))
                 for row, truth in zip(x_rows, targets)]
    models = {str(tau): {
        "bias": bias + (quantile(residuals, tau) or 0.0),
        "weights": list(weights)} for tau in quantiles}
    return {"means": means, "scales": scales, "models": models,
            "quantiles": list(quantiles), "n_samples": len(features),
            "constraint": "parallel_location_shift"}


def predict_linear_quantiles(artifact: dict, features: List[float]) -> Optional[dict]:
    try:
        if not artifact or len(features) != len(artifact.get("means", [])):
            return None
        normalized = [(float(value) - mean) / scale for value, mean, scale in
                      zip(features, artifact["means"], artifact["scales"])]
        values = {}
        for tau in artifact["quantiles"]:
            model = artifact["models"][str(tau)]
            values[f"q{int(round(tau * 100)):02d}"] = (
                model["bias"] + sum(weight * value for weight, value in
                                    zip(model["weights"], normalized)))
        required = {"q10": values.get("q10"), "q50": values.get("q50"),
                    "q90": values.get("q90")}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if (any(value is None or not math.isfinite(value)
            for value in required.values()) or not validate_quantiles(required)):
        return None
    return required


def price_quantiles(entry: float, high_returns: dict,
                    low_returns: dict) -> Optional[dict]:
    if not validate_quantiles(high_returns) or not validate_quantiles(low_returns):
        return None
    return {
        "high_q10": entry * math.exp(high_returns["q10"]),
        "high_q50": entry * math.exp(high_returns["q50"]),
        "high_q90": entry * math.exp(high_returns["q90"]),
        "low_q10": entry * math.exp(low_returns["q10"]),
        "low_q50": entry * math.exp(low_returns["q50"]),
        "low_q90": entry * math.exp(low_returns["q90"]),
    }


def empirical_extrema_forecast(entry: float, direction: str, regime=None,
                               db_path=None, strategy_id=None) -> Optional[dict]:
    """方向+regime 的滚动经验分位；样本不足诚实返回 None。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND s.strategy_version=?" if scope_version else ""
    rows = sdb.q(
        "SELECT s.features,o.high_ret_h,o.low_ret_h FROM signal_outcomes o "
        "JOIN signal_samples s ON s.signal_id=o.signal_id "
        "WHERE s.direction=? AND s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=?" + scope_sql +
        " ORDER BY s.event_ts DESC",
        [direction, strategy_id,
         config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS,
         *([scope_version] if scope_version else [])], db_path=db_path)
    if regime:
        tag = regime.get("tag") if isinstance(regime, dict) else str(regime)
        filtered = []
        for row in rows:
            try:
                current = json.loads(row["features"] or "{}").get("regime")
                current_tag = current.get("tag") if isinstance(current, dict) else current
                if current_tag == tag:
                    filtered.append(row)
            except Exception:
                continue
        if len(filtered) >= config.EXTREMA_MIN_BASELINE_SAMPLES:
            rows = filtered
    if len(rows) < config.EXTREMA_MIN_BASELINE_SAMPLES:
        return None
    high = [float(row["high_ret_h"]) for row in rows]
    low = [float(row["low_ret_h"]) for row in rows]
    high_q = {f"q{int(tau*100):02d}": quantile(high, tau)
              for tau in config.EXTREMA_QUANTILES}
    low_q = {f"q{int(tau*100):02d}": quantile(low, tau)
             for tau in config.EXTREMA_QUANTILES}
    prices = price_quantiles(float(entry), high_q, low_q)
    if prices is None:
        return None
    return {**{key: round(value, 8) for key, value in prices.items()},
            "n": len(rows), "method": "rolling_empirical",
            "interval": "probabilistic_not_guaranteed"}


def load_artifact(direction: str, db_path=None, allow_shadow=False,
                  strategy_id=None) -> Optional[dict]:
    """读取最新极值制品；缺失或损坏时返回 None，不影响现役规则。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    states = (("active", "observing", "kept") if not allow_shadow else
              ("active", "observing", "kept", "shadow", "validated"))
    placeholders = ",".join("?" for _ in states)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND strategy_version=?" if scope_version else ""
    row = sdb.q1(
        f"SELECT * FROM model_artifacts WHERE model_type='extrema' "
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
                config.SIGNAL_OUTCOME_HORIZON_HOURS):
            return None
        artifact.update({"model_id": row["model_id"], "state": row["state"],
                         "training_cutoff": row.get("training_cutoff")})
        return artifact
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _online_conformal(model_id: str, target: str, db_path=None,
                      strategy_id=None) -> Optional[float]:
    """只用该模型随后已结算的影子预测更新半径，严格限制最近窗口。"""
    import storage.db as sdb
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    scope_sql = " AND s.strategy_version=?" if scope_version else ""
    rows = sdb.q(
        "SELECT s.features,o.high_ret_h,o.low_ret_h "
        "FROM signal_samples s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=?" +
        scope_sql + " ORDER BY s.event_ts DESC LIMIT ?",
        [strategy_id,
         config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS,
         *([scope_version] if scope_version else []),
         config.EXTREMA_CONFORMAL_WINDOW], db_path=db_path)
    actual, lower, upper = [], [], []
    for row in reversed(rows):
        try:
            prediction = json.loads(row.get("features") or "{}").get(
                "extrema_prediction")
            returns = prediction.get(f"{target}_returns") if prediction else None
            if prediction.get("model_id") != model_id or not returns:
                continue
            truth = row[f"{target}_ret_h"]
            actual.append(float(truth))
            lower.append(float(returns["q10"]))
            upper.append(float(returns["q90"]))
        except (AttributeError, KeyError, TypeError, ValueError,
                json.JSONDecodeError):
            continue
    if len(actual) < config.EXTREMA_MIN_CONFORMAL_SAMPLES:
        return None
    return conformal_radius(actual, lower, upper, alpha=0.2,
                            window=config.EXTREMA_CONFORMAL_WINDOW)


def predict_signal(sig: dict, db_path=None, allow_shadow=True) -> Optional[dict]:
    """生成条件极值分位与自适应 80% 区间；仅展示，绝不产生交易方向。"""
    strategy_id = str(sig.get("strategy_id") or
                      config.ENTRY_SIGNAL_STRATEGY_ID)
    artifact = load_artifact(str(sig.get("dir") or ""), db_path,
                             allow_shadow=allow_shadow,
                             strategy_id=strategy_id)
    if not artifact:
        return None
    try:
        from decision.entry_probability import signal_feature_values
        names = list(artifact["feature_names"])
        values = signal_feature_values(sig, names)
        if values is None:
            return None
        high = predict_linear_quantiles(artifact["high_model"], values)
        low = predict_linear_quantiles(artifact["low_model"], values)
        entry = float(sig["entry"])
        if not high or not low:
            return None
        high_radius = _online_conformal(
            artifact["model_id"], "high", db_path, strategy_id)
        low_radius = _online_conformal(
            artifact["model_id"], "low", db_path, strategy_id)
        if high_radius is None:
            high_radius = float(artifact.get("high_conformal_radius") or 0.0)
        if low_radius is None:
            low_radius = float(artifact.get("low_conformal_radius") or 0.0)
        high_interval = {"lower": high["q10"] - high_radius,
                         "upper": high["q90"] + high_radius}
        low_interval = {"lower": low["q10"] - low_radius,
                        "upper": low["q90"] + low_radius}
        prices = price_quantiles(entry, high, low)
        if prices is None or high_interval["lower"] > high_interval["upper"] or \
                low_interval["lower"] > low_interval["upper"]:
            return None
        return {
            **{key: round(value, 8) for key, value in prices.items()},
            "high_returns": {key: round(value, 10) for key, value in high.items()},
            "low_returns": {key: round(value, 10) for key, value in low.items()},
            "high_interval": {"lower": round(high_interval["lower"], 10),
                              "upper": round(high_interval["upper"], 10)},
            "low_interval": {"lower": round(low_interval["lower"], 10),
                             "upper": round(low_interval["upper"], 10)},
            "high_interval_price": {
                "lower": round(entry * math.exp(high_interval["lower"]), 8),
                "upper": round(entry * math.exp(high_interval["upper"]), 8)},
            "low_interval_price": {
                "lower": round(entry * math.exp(low_interval["lower"]), 8),
                "upper": round(entry * math.exp(low_interval["upper"]), 8)},
            "baseline_high_returns": artifact.get("baseline_high_returns"),
            "baseline_low_returns": artifact.get("baseline_low_returns"),
            "model_id": artifact["model_id"], "model_version": artifact["version"],
            "state": artifact["state"], "method": "linear_quantile_conformal",
            "interval": "probabilistic_not_guaranteed",
            "decision_effective": False,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
