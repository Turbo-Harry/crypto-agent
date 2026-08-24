"""模型 candidate→validated→shadow→accepted→active→observing→kept/rollback。"""
import json
import time

import config
from decision.signal_identity import research_scope_version


def _shadow_rows(model_id, db_path=None, after_ts=None):
    import storage.db as sdb
    model = sdb.q1(
        "SELECT strategy_id,strategy_version FROM model_artifacts WHERE model_id=?",
        [model_id], db_path=db_path)
    strategy_id = (model.get("strategy_id") if model else None) or \
        config.ENTRY_SIGNAL_STRATEGY_ID
    sql = ("SELECT s.event_ts,s.trade_id,s.entry,s.stop,s.direction,"
           "s.horizon_hours,s.features,"
           "o.tp_first,o.sl_first,o.timeout,o.pnl_r "
           "FROM signal_samples s "
           "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
           "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=? ")
    params = [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
              config.SIGNAL_OUTCOME_HORIZON_HOURS]
    if model and model.get("strategy_version"):
        sql += "AND s.strategy_version=? "
        params.append(model["strategy_version"])
    if after_ts is not None:
        sql += "AND s.event_ts>? "
        params.append(after_ts)
    sql += "ORDER BY s.event_ts"
    rows = []
    for row in sdb.q(sql, params, db_path=db_path):
        try:
            prediction = json.loads(row["features"] or "{}").get("entry_probability")
        except Exception:
            prediction = None
        if prediction and prediction.get("model_id") == model_id:
            item = dict(row)
            item["prediction"] = prediction
            rows.append(item)
    return rows


def _entry_shadow_metrics(model_id, db_path=None, after_ts=None):
    import storage.db as sdb
    from decision.entry_probability import execution_cost_r
    rows = _shadow_rows(model_id, db_path, after_ts)
    if not rows:
        return {"n": 0, "closed_n": 0, "brier": None, "baseline_brier": None,
                "brier_skill": None, "policy_ev_r": None,
                "selected_ev_r": None, "baseline_ev_r": None,
                "selected_n": 0, "coverage": 0.0, "max_drawdown_r": None}
    actual = [int(row["tp_first"]) for row in rows]
    probabilities = [float(row["prediction"]["p_tp"]) for row in rows]
    model = sdb.q1("SELECT artifact FROM model_artifacts WHERE model_id=?",
                   [model_id], db_path=db_path)
    try:
        artifact = json.loads(model["artifact"] or "{}") if model else {}
        base_rate = float((artifact.get("class_priors") or {}).get(
            "tp", (artifact.get("model") or {}).get("base_rate", 0.5)))
    except (TypeError, ValueError, json.JSONDecodeError):
        base_rate = 0.5
    baseline_probabilities = [float(row["prediction"].get(
        "baseline_p_tp", base_rate)) for row in rows]
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, actual)) / len(rows)
    baseline_brier = sum((p - y) ** 2 for p, y in
                         zip(baseline_probabilities, actual)) / len(rows)
    net_returns = [float(row["pnl_r"]) - float(execution_cost_r(row) or 0)
                   for row in rows]
    selected_mask = [float(row["prediction"].get("ev_r_lower", -1)) > 0
                     for row in rows]
    selected_returns = [value for value, selected in
                        zip(net_returns, selected_mask) if selected]
    policy_returns = [value if selected else 0.0 for value, selected in
                      zip(net_returns, selected_mask)]
    baseline_ev = sum(net_returns) / len(rows)
    policy_ev = sum(policy_returns) / len(rows)
    selected_ev = (sum(selected_returns) / len(selected_returns)
                   if selected_returns else None)
    trade_ids = [row["trade_id"] for row in rows if row.get("trade_id")]
    closed_n = 0
    if trade_ids:
        placeholders = ",".join("?" for _ in trade_ids)
        closed_n = int(sdb.q1(
            f"SELECT COUNT(*) n FROM trades WHERE status='closed' "
            f"AND id IN ({placeholders})", trade_ids, db_path=db_path)["n"])
    peak = equity = drawdown = 0.0
    for value in policy_returns:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    skill = 1 - brier / baseline_brier if baseline_brier > 0 else 0.0
    return {"n": len(rows), "closed_n": closed_n, "brier": brier,
            "baseline_brier": baseline_brier, "brier_skill": skill,
            "policy_ev_r": policy_ev, "selected_ev_r": selected_ev,
            "baseline_ev_r": baseline_ev,
            "selected_n": len(selected_returns),
            "coverage": len(selected_returns) / len(rows),
            "max_drawdown_r": drawdown}


def _extrema_shadow_metrics(model_id, db_path=None, after_ts=None):
    """用模型训练截止点之后的真实极值标签评价影子分位，不复用训练指标。"""
    import storage.db as sdb
    from decision.extrema_forecast import interval_coverage, pinball_loss, \
        validate_quantiles
    model = sdb.q1(
        "SELECT strategy_id,strategy_version FROM model_artifacts WHERE model_id=?",
        [model_id], db_path=db_path)
    strategy_id = (model.get("strategy_id") if model else None) or \
        config.ENTRY_SIGNAL_STRATEGY_ID
    sql = ("SELECT s.features,o.high_ret_h,o.low_ret_h "
           "FROM signal_samples s "
           "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
           "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=? ")
    params = [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
              config.SIGNAL_OUTCOME_HORIZON_HOURS]
    if model and model.get("strategy_version"):
        sql += "AND s.strategy_version=? "
        params.append(model["strategy_version"])
    if after_ts is not None:
        sql += "AND s.event_ts>? "
        params.append(after_ts)
    sql += "ORDER BY s.event_ts"
    rows = []
    crossings = 0
    for row in sdb.q(sql, params, db_path=db_path):
        try:
            prediction = json.loads(row.get("features") or "{}").get(
                "extrema_prediction")
            if not prediction or prediction.get("model_id") != model_id:
                continue
            high = prediction["high_returns"]
            low = prediction["low_returns"]
            if not validate_quantiles(high) or not validate_quantiles(low):
                crossings += 1
                continue
            rows.append({**dict(row), "prediction": prediction})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not rows:
        return {"n": 0, "closed_n": 0, "pinball_loss": None,
                "baseline_pinball_loss": None, "pinball_improvement": None,
                "high_coverage": None, "low_coverage": None,
                "quantile_crossings": crossings}
    quantiles = (("q10", 0.1), ("q50", 0.5), ("q90", 0.9))
    model_losses, baseline_losses = [], []
    for target in ("high", "low"):
        actual = [float(row[f"{target}_ret_h"]) for row in rows]
        for key, tau in quantiles:
            predicted = [float(row["prediction"][f"{target}_returns"][key])
                         for row in rows]
            baseline = [float(row["prediction"][f"baseline_{target}_returns"][key])
                        for row in rows]
            model_losses.append(pinball_loss(actual, predicted, tau))
            baseline_losses.append(pinball_loss(actual, baseline, tau))
    model_loss = sum(model_losses) / len(model_losses)
    baseline_loss = sum(baseline_losses) / len(baseline_losses)
    improvement = (1 - model_loss / baseline_loss if baseline_loss > 0 else 0.0)
    high_actual = [float(row["high_ret_h"]) for row in rows]
    low_actual = [float(row["low_ret_h"]) for row in rows]
    high_coverage = interval_coverage(
        high_actual,
        [row["prediction"]["high_interval"]["lower"] for row in rows],
        [row["prediction"]["high_interval"]["upper"] for row in rows])
    low_coverage = interval_coverage(
        low_actual,
        [row["prediction"]["low_interval"]["lower"] for row in rows],
        [row["prediction"]["low_interval"]["upper"] for row in rows])
    return {"n": len(rows), "closed_n": 0, "pinball_loss": model_loss,
            "baseline_pinball_loss": baseline_loss,
            "pinball_improvement": improvement,
            "high_coverage": high_coverage, "low_coverage": low_coverage,
            "quantile_crossings": crossings}


def shadow_metrics(model_id, db_path=None, after_ts=None):
    """按模型类型选择独立影子指标，禁止用 entry 的 Brier 评价 extrema。"""
    import storage.db as sdb
    model = sdb.q1("SELECT model_type FROM model_artifacts WHERE model_id=?",
                   [model_id], db_path=db_path)
    if model and model["model_type"] == "extrema":
        return _extrema_shadow_metrics(model_id, db_path, after_ts)
    return _entry_shadow_metrics(model_id, db_path, after_ts)


def _update(model_id, state, metrics=None, db_path=None, reason=None, **fields):
    import storage.db as sdb
    previous = sdb.q1("SELECT state FROM model_artifacts WHERE model_id=?",
                      [model_id], db_path=db_path)
    assignments, params = ["state=?"], [state]
    if metrics is not None:
        assignments.append("metrics=?")
        params.append(json.dumps(metrics, ensure_ascii=False))
    for name, value in fields.items():
        assignments.append(f"{name}=?")
        params.append(value)
    params.append(model_id)
    sdb.x(f"UPDATE model_artifacts SET {','.join(assignments)} WHERE model_id=?",
          params, db_path=db_path)
    if previous and previous["state"] != state:
        sdb.x(
            "INSERT INTO model_state_events (model_id,ts,from_state,to_state,reason,metrics) "
            "VALUES (?,?,?,?,?,?)",
            [model_id, time.time(), previous["state"], state, reason,
             json.dumps(metrics or {}, ensure_ascii=False)], db_path=db_path)


def advance(model_id=None, db_path=None):
    """推进一个或全部模型；每一步都以独立新样本证据为前提。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    if model_id:
        models = sdb.q("SELECT * FROM model_artifacts WHERE model_id=?",
                       [model_id], db_path=db_path)
    else:
        models = sdb.q(
            "SELECT * FROM model_artifacts WHERE state IN "
            "('validated','shadow','accepted','active','observing') "
            "ORDER BY created_at", db_path=db_path)
    actions = []
    for model in models:
        state, mid = model["state"], model["model_id"]
        current_scope = research_scope_version(model.get("strategy_id"))
        if current_scope and model.get("strategy_version") != current_scope:
            actions.append({"model_id": mid, "from": state, "to": state,
                            "reason": "stale_strategy_version"})
            continue
        if state == "validated":
            _update(mid, "shadow", db_path=db_path, reason="oos_validated")
            actions.append({"model_id": mid, "from": state, "to": "shadow"})
            continue
        if state == "shadow":
            metrics = shadow_metrics(mid, db_path,
                                     after_ts=model.get("training_cutoff"))
            if ((metrics["n"] < config.MODEL_SHADOW_MIN_CANDIDATES and
                    metrics["closed_n"] < config.MODEL_OBSERVE_MIN_CLOSED) or
                    (model["model_type"] == "entry_probability" and
                     metrics["selected_n"] <
                     config.MODEL_MIN_SELECTED_EVALUATIONS)):
                actions.append({"model_id": mid, "from": state, "to": state,
                                "reason": "insufficient_shadow", "metrics": metrics})
                continue
            if model["model_type"] == "extrema":
                passed = (
                    metrics["pinball_improvement"] is not None and
                    metrics["pinball_improvement"] >=
                    config.EXTREMA_PINBALL_IMPROVEMENT and
                    config.EXTREMA_COVERAGE_LOW <= metrics["high_coverage"] <=
                    config.EXTREMA_COVERAGE_HIGH and
                    config.EXTREMA_COVERAGE_LOW <= metrics["low_coverage"] <=
                    config.EXTREMA_COVERAGE_HIGH and
                    metrics["quantile_crossings"] == 0)
            else:
                passed = (metrics["brier_skill"] is not None and
                          metrics["brier_skill"] > config.ENTRY_MODEL_MIN_BRIER_SKILL and
                          metrics["selected_ev_r"] is not None and
                          metrics["selected_ev_r"] > 0 and
                          metrics["policy_ev_r"] >= metrics["baseline_ev_r"] and
                          metrics["max_drawdown_r"] <= config.MODEL_MAX_DRAWDOWN_R)
            target = "accepted" if passed else "rejected"
            _update(mid, target, metrics=metrics, db_path=db_path,
                    reason="shadow_gate_pass" if passed else "shadow_gate_fail")
            actions.append({"model_id": mid, "from": state, "to": target,
                            "metrics": metrics})
            continue
        if state == "accepted":
            shadow_only = (config.EXTREMA_MODEL_SHADOW_ONLY
                           if model["model_type"] == "extrema" else
                           config.ENTRY_MODEL_SHADOW_ONLY)
            if shadow_only:
                actions.append({"model_id": mid, "from": state, "to": state,
                                "reason": "shadow_only_enabled"})
                continue
            scope_sql = (" AND strategy_version=?"
                         if model.get("strategy_version") else "")
            previous = sdb.q1(
                "SELECT model_id FROM model_artifacts WHERE model_type=? "
                "AND strategy_id=? AND direction=? "
                "AND state IN ('active','observing','kept') "
                + scope_sql + " ORDER BY activated_at DESC LIMIT 1",
                [model["model_type"], model.get("strategy_id") or
                 config.ENTRY_SIGNAL_STRATEGY_ID, model.get("direction"),
                 *([model.get("strategy_version")]
                   if model.get("strategy_version") else [])],
                db_path=db_path)
            parent = previous["model_id"] if previous else None
            if parent:
                _update(parent, "kept", db_path=db_path,
                        reason="superseded", retired_at=time.time())
            now = time.time()
            _update(mid, "active", db_path=db_path,
                    reason="shadow_accepted", activated_at=now,
                    parent_id=parent)
            actions.append({"model_id": mid, "from": state, "to": "active"})
            continue
        if state == "active":
            _update(mid, "observing", db_path=db_path,
                    reason="activation_observation")
            actions.append({"model_id": mid, "from": state, "to": "observing"})
            continue
        if state == "observing":
            current = shadow_metrics(mid, db_path, after_ts=model.get("activated_at"))
            if (current["n"] < config.MODEL_OBSERVE_MIN_CANDIDATES and
                    current["closed_n"] < config.MODEL_OBSERVE_MIN_CLOSED) or (
                    model["model_type"] == "entry_probability" and
                    current["selected_n"] <
                    config.MODEL_MIN_SELECTED_EVALUATIONS):
                actions.append({"model_id": mid, "from": state, "to": state,
                                "reason": "insufficient_observation",
                                "metrics": current})
                continue
            try:
                accepted = json.loads(model.get("metrics") or "{}")
            except Exception:
                accepted = {}
            if model["model_type"] == "extrema":
                degraded = (
                    current["pinball_improvement"] is None or
                    current["pinball_improvement"] <
                    config.EXTREMA_PINBALL_IMPROVEMENT or
                    current["high_coverage"] is None or
                    not config.EXTREMA_COVERAGE_LOW <=
                    current["high_coverage"] <= config.EXTREMA_COVERAGE_HIGH or
                    current["low_coverage"] is None or
                    not config.EXTREMA_COVERAGE_LOW <=
                    current["low_coverage"] <= config.EXTREMA_COVERAGE_HIGH or
                    current["quantile_crossings"] > 0)
            else:
                degraded = (
                    accepted.get("brier") is not None and
                    current["brier"] is not None and
                    current["brier"] - accepted["brier"] >
                    config.MODEL_MAX_BRIER_DEGRADE) or (
                    accepted.get("policy_ev_r") is not None and
                    current["policy_ev_r"] < accepted["policy_ev_r"] -
                    config.MODEL_MAX_EV_DEGRADE_R) or (
                    current["max_drawdown_r"] > config.MODEL_MAX_DRAWDOWN_R)
            if degraded:
                rollback(mid, db_path=db_path)
                target = "rolled_back"
            else:
                _update(mid, "kept", metrics=current, db_path=db_path,
                        reason="observation_pass")
                target = "kept"
            actions.append({"model_id": mid, "from": state, "to": target,
                            "metrics": current})
    return actions


def rollback(model_id=None, db_path=None, strategy_id=None):
    """一键回滚指定或当前 entry 模型，并恢复 parent。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    if model_id:
        model = sdb.q1("SELECT * FROM model_artifacts WHERE model_id=?",
                       [model_id], db_path=db_path)
    else:
        model = sdb.q1(
            "SELECT * FROM model_artifacts WHERE model_type='entry_probability' "
            "AND strategy_id=? "
            "AND state IN ('active','observing','kept','accepted') "
            + ("AND strategy_version=? " if scope_version else "") +
            "ORDER BY COALESCE(activated_at,created_at) DESC LIMIT 1",
            [strategy_id, *([scope_version] if scope_version else [])],
            db_path=db_path)
    if not model:
        return False, "没有可回滚的模型"
    now = time.time()
    _update(model["model_id"], "rolled_back", db_path=db_path,
            reason="manual_or_degradation_rollback", retired_at=now)
    if model.get("parent_id"):
        _update(model["parent_id"], "active", db_path=db_path,
                reason="parent_restored",
                activated_at=now, retired_at=None)
    return True, f"已回滚 {model['model_id']}"


def budget_expansion_allowed(db_path=None, strategy_id=None):
    """长期样本外 EV 未证明为正时，短期盈利也不得扩大预算。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    row = sdb.q1(
        "SELECT metrics FROM model_artifacts WHERE model_type='entry_probability' "
        "AND strategy_id=? AND state IN ('active','observing','kept') "
        + ("AND strategy_version=? " if scope_version else "") +
        "ORDER BY created_at DESC LIMIT 1",
        [strategy_id, *([scope_version] if scope_version else [])], db_path=db_path)
    if not row:
        return False
    try:
        metrics = json.loads(row["metrics"] or "{}")
        return float(metrics.get("long_term_backtest_ev_r")) > \
            config.MODEL_BUDGET_EXPANSION_MIN_LONG_TERM_EV_R
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def snapshot(db_path=None, strategy_id=None):
    import storage.db as sdb
    sdb.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    scope_version = research_scope_version(strategy_id)
    rows = sdb.q(
        "SELECT model_id,model_type,strategy_id,direction,version,state,created_at,"
        "strategy_version,training_cutoff,data_hash,feature_names,metrics,"
        "parent_id,activated_at "
        "FROM model_artifacts WHERE model_type IN ('entry_probability','extrema') "
        "AND strategy_id=?" +
        (" AND strategy_version=?" if scope_version else "") +
        " ORDER BY created_at DESC",
        [strategy_id, *([scope_version] if scope_version else [])], db_path=db_path)
    for row in rows:
        for key in ("feature_names", "metrics"):
            try:
                row[key] = json.loads(row.get(key) or ("[]" if key == "feature_names" else "{}"))
            except Exception:
                row[key] = [] if key == "feature_names" else {}
        history = sdb.q(
            "SELECT ts,from_state,to_state,reason,metrics FROM model_state_events "
            "WHERE model_id=? ORDER BY id", [row["model_id"]], db_path=db_path)
        for event in history:
            try:
                event["metrics"] = json.loads(event.get("metrics") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                event["metrics"] = {}
        row["history"] = history
    return {"models": rows, "budget_expansion_allowed":
            budget_expansion_allowed(db_path, strategy_id)}
