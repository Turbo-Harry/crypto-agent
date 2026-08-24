# -*- coding: utf-8 -*-
"""方向正确、路径语义分离的 15m 概率预测。

terminal distribution 始终模拟完整 H 根 15m K；first passage 在同批路径上
另行判定首次障碍，触碰后不会截断 terminal 样本。收益用波动 regime 匹配的
移动区块 bootstrap，避免 iid 抽样抹掉短期自相关与波动聚集。
"""
import json
import math
import random
import time
from typing import Iterable, List

import config
from decision.signal_identity import research_scope_version


def _returns(closes):
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def _quantile(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    pos = max(0.0, min(1.0, float(q))) * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    weight = pos - lo
    return s[lo] * (1 - weight) + s[hi] * weight


def _stdev(values: Iterable[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - 1))


def moving_block_returns(bar_returns: List[float], horizon: int, paths: int,
                         block_size: int, seed=None,
                         regime_lookback=None) -> List[List[float]]:
    """按当前波动 regime 选历史连续区块，输出固定长度收益路径。"""
    if not bar_returns or horizon <= 0 or paths <= 0:
        return []
    rng = random.Random(seed)
    values = [float(x) for x in bar_returns]
    size = max(1, min(int(block_size), len(values)))
    lookback = int(regime_lookback or config.FORECAST_REGIME_LOOKBACK_BARS)
    recent = values[-min(lookback, len(values)):]
    current_vol = _stdev(recent)
    starts = list(range(max(1, len(values) - size + 1)))
    if current_vol > 0 and size > 1:
        matched = []
        for start in starts:
            block_vol = _stdev(values[start:start + size])
            ratio = block_vol / current_vol if current_vol else 1.0
            if 0.5 <= ratio <= 2.0:
                matched.append(start)
        if matched:
            starts = matched
    out = []
    for _ in range(paths):
        path = []
        while len(path) < horizon:
            start = starts[rng.randrange(len(starts))]
            path.extend(values[start:start + size])
        out.append(path[:horizon])
    return out


def moving_block_profiles(profiles: List[dict], horizon: int, paths: int,
                          block_size: int, seed=None,
                          regime_lookback=None) -> List[List[dict]]:
    """OHLC excursion 连续区块；regime 匹配仍以 close-to-close 波动为准。"""
    if not profiles or horizon <= 0 or paths <= 0:
        return []
    rng = random.Random(seed)
    size = max(1, min(int(block_size), len(profiles)))
    returns = [float(row["close_ret"]) for row in profiles]
    lookback = int(regime_lookback or config.FORECAST_REGIME_LOOKBACK_BARS)
    current_vol = _stdev(returns[-min(lookback, len(returns)):])
    starts = list(range(max(1, len(profiles) - size + 1)))
    if current_vol > 0 and size > 1:
        matched = [start for start in starts
                   if 0.5 <= (_stdev(returns[start:start + size]) /
                              current_vol) <= 2.0]
        if matched:
            starts = matched
    out = []
    for _ in range(paths):
        path = []
        while len(path) < horizon:
            start = starts[rng.randrange(len(starts))]
            path.extend(dict(row) for row in profiles[start:start + size])
        out.append(path[:horizon])
    return out


def simulate_price_paths(entry: float, return_paths: Iterable[Iterable[float]]):
    paths = []
    for returns in return_paths:
        px = float(entry)
        prices = []
        for value in returns:
            px *= math.exp(float(value))
            prices.append(px)
        paths.append(prices)
    return paths


def simulate_bar_paths(entry: float, profile_paths: Iterable[Iterable[dict]]):
    """把相对前收的 OHLC excursion 还原成价格 bar，完整走满 horizon。"""
    paths = []
    for profiles in profile_paths:
        px = float(entry)
        bars = []
        for profile in profiles:
            high = px * math.exp(float(profile["high_ret"]))
            low = px * math.exp(float(profile["low_ret"]))
            close = px * math.exp(float(profile["close_ret"]))
            bars.append({"high": max(high, close), "low": min(low, close),
                         "close": close})
            px = close
        paths.append(bars)
    return paths


def terminal_distribution(price_paths):
    """只消费完整路径终点；障碍是否触达不会改变这里的样本。"""
    finals = [(path[-1]["close"] if isinstance(path[-1], dict) else path[-1])
              for path in price_paths if path]
    if not finals:
        return None
    return {"median": _quantile(finals, 0.5), "q05": _quantile(finals, 0.05),
            "q95": _quantile(finals, 0.95)}


def first_passage_probabilities(price_paths, direction, stop, tp):
    counts = {"tp": 0, "sl": 0, "timeout": 0}
    for path in price_paths:
        result = "timeout"
        for bar in path:
            high = bar["high"] if isinstance(bar, dict) else bar
            low = bar["low"] if isinstance(bar, dict) else bar
            tp_hit = high >= tp if direction == "long" else low <= tp
            sl_hit = low <= stop if direction == "long" else high >= stop
            if sl_hit:
                result = "sl"
                break
            if tp_hit:
                result = "tp"
                break
        counts[result] += 1
    n = max(1, sum(counts.values()))
    return {name: counts[name] / n for name in counts}


def _blend_probabilities(sim, emp_p_tp=None, emp_p_sl=None, emp_n=0,
                         prior_strength=None, explicit_weight=None):
    if emp_p_tp is None or emp_p_sl is None:
        return dict(sim), 0.0
    emp = {"tp": max(0.0, float(emp_p_tp)),
           "sl": max(0.0, float(emp_p_sl))}
    emp["timeout"] = max(0.0, 1.0 - emp["tp"] - emp["sl"])
    total = sum(emp.values()) or 1.0
    emp = {key: value / total for key, value in emp.items()}
    if explicit_weight is not None:
        weight = max(0.0, min(1.0, float(explicit_weight)))
    else:
        strength = float(prior_strength or config.FORECAST_EMP_PRIOR_STRENGTH)
        weight = max(0.0, float(emp_n)) / (max(0.0, float(emp_n)) + strength)
    mixed = {key: (1 - weight) * sim[key] + weight * emp[key]
             for key in ("tp", "sl", "timeout")}
    norm = sum(mixed.values()) or 1.0
    return {key: mixed[key] / norm for key in mixed}, weight


def forecast(entry, atr, direction, stop, tp, hourly_returns,
             horizon=None, paths=500, emp_p_tp=None, emp_p_sl=None,
             emp_n=0, blend=None, block_size=None, seed=None,
             calibration_status="uncalibrated", bar_profiles=None,
             bar_minutes=None, regime_lookback=None):
    """完整终值分布 + 独立首触概率；无效方向/障碍/样本明确返回 None。"""
    horizon = int(horizon or config.FORECAST_HORIZON_BARS)
    bar_minutes = int(bar_minutes or config.FORECAST_BAR_MINUTES)
    if (not hourly_returns or atr is None or atr <= 0 or entry <= 0 or
            direction not in ("long", "short")):
        return None
    valid = ((direction == "long" and stop < entry < tp) or
             (direction == "short" and tp < entry < stop))
    if not valid:
        return None
    if bar_profiles:
        profile_paths = moving_block_profiles(
            list(bar_profiles), int(horizon), int(paths),
            block_size or config.FORECAST_BLOCK_SIZE, seed=seed,
            regime_lookback=regime_lookback)
        price_paths = simulate_bar_paths(float(entry), profile_paths)
        passage_resolution = "intrabar_ohlc"
    else:
        return_paths = moving_block_returns(
            list(hourly_returns), int(horizon), int(paths),
            block_size or config.FORECAST_BLOCK_SIZE, seed=seed,
            regime_lookback=regime_lookback)
        price_paths = simulate_price_paths(float(entry), return_paths)
        passage_resolution = "close_only_fallback"
    terminal = terminal_distribution(price_paths)
    if terminal is None:
        return None
    passage = first_passage_probabilities(price_paths, direction, float(stop),
                                          float(tp))
    mixed, empirical_weight = _blend_probabilities(
        passage, emp_p_tp=emp_p_tp, emp_p_sl=emp_p_sl, emp_n=emp_n,
        explicit_weight=blend)
    p_hit_tp = round(mixed["tp"], 4)
    p_hit_sl = round(mixed["sl"], 4)
    p_timeout = round(mixed["timeout"], 4)
    # Timeout has no first-touch class direction.  Assigning it a neutral 50%
    # loss weight yields a predeclared, non-constant prior without pretending
    # the bootstrap knows the eventual timeout sign.  Harness may only adjust
    # this frozen prior when it cites current conflicting market evidence.
    p_loss_prior = round(min(1.0, p_hit_sl + 0.5 * p_timeout), 4)
    return {
        "median": round(terminal["median"], 6),
        "q05": round(terminal["q05"], 6),
        "q95": round(terminal["q95"], 6),
        # 这是候选既有、受风控约束的 2R 止盈障碍，不是模型根据终值
        # 分布临时生成的新目标；显式携带可避免 AI 只看到命中概率却
        # 不知道该概率对应哪个价位。
        "expected_take_profit": round(float(tp), 6),
        "p_hit_tp": p_hit_tp,
        "p_hit_sl": p_hit_sl,
        "p_timeout": p_timeout,
        "p_loss_prior": p_loss_prior,
        "loss_prior_method": "sl_plus_half_timeout_v1",
        "empirical_weight": round(empirical_weight, 4),
        "horizon_bars": int(horizon), "bar_minutes": bar_minutes,
        "horizon_minutes": int(horizon) * bar_minutes,
        "horizon_hours": round(int(horizon) * bar_minutes / 60, 4),
        "bootstrap": "regime_moving_block",
        "first_passage_resolution": passage_resolution,
        "calibration_status": calibration_status,
    }


def empirical_first_passage(db_path=None, direction=None, as_of_ts=None,
                            strategy_id=None):
    """只读取预测时点已经完成标签窗的经验结果，历史重放禁止偷看未来。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    conditions = ["s.strategy_id=?", "s.timeframe=?", "s.horizon_hours=?"]
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    params = [strategy_id,
              config.SIGNAL_SAMPLE_TIMEFRAME,
              config.SIGNAL_OUTCOME_HORIZON_HOURS]
    scope_version = research_scope_version(strategy_id)
    if scope_version:
        conditions.append("s.strategy_version=?")
        params.append(scope_version)
    if direction:
        conditions.append("s.direction=?")
        params.append(direction)
    cutoff = float(as_of_ts if as_of_ts is not None else time.time())
    conditions.append("s.event_ts+s.horizon_hours*3600<=?")
    params.append(cutoff)
    rows = sdb.q(
        "SELECT o.tp_first,o.sl_first,o.timeout FROM signal_outcomes o "
        "JOIN signal_samples s ON s.signal_id=o.signal_id WHERE " +
        " AND ".join(conditions), params, db_path=db_path)
    n = len(rows)
    if not n:
        return {"n": 0, "p_tp": None, "p_sl": None, "p_timeout": None}
    return {"n": n,
            "p_tp": sum(int(row["tp_first"]) for row in rows) / n,
            "p_sl": sum(int(row["sl_first"]) for row in rows) / n,
            "p_timeout": sum(int(row["timeout"]) for row in rows) / n}


def forecast_for_trade(sig, base, klines, db_path=None, *,
                       empirical_enabled=True, as_of_ts=None, seed=None):
    if not getattr(config, "FORECAST_ENABLED", False):
        return None
    try:
        closes = [k.get("close") for k in (klines or []) if k.get("close")]
        if len(closes) < 60:
            return None
        rets = _returns(closes[-config.FORECAST_LOOKBACK_BARS:])
        if len(rets) < config.FORECAST_MIN_RETURN_BARS:
            return None
        empirical = (empirical_first_passage(
            db_path, sig.get("dir"), as_of_ts=as_of_ts,
            strategy_id=sig.get("strategy_id"))
            if empirical_enabled else
            {"n": 0, "p_tp": None, "p_sl": None, "p_timeout": None})
        use_emp = empirical_enabled and empirical["n"] >= config.FORECAST_MIN_EMP_N
        cal = (calibration(
            db_path, min_n=config.FORECAST_MIN_CALIBRATION,
            as_of_ts=as_of_ts,
            strategy_id=sig.get("strategy_id")) if empirical_enabled else
            {"status": "uncalibrated"})
        recent_bars = list(klines or [])[-config.FORECAST_LOOKBACK_BARS:]
        profiles = []
        for idx in range(1, len(recent_bars)):
            previous = float(recent_bars[idx - 1].get("close") or 0)
            current = recent_bars[idx]
            if previous <= 0 or min(float(current.get(key) or 0)
                                    for key in ("high", "low", "close")) <= 0:
                continue
            profiles.append({
                "close_ret": math.log(float(current["close"]) / previous),
                "high_ret": math.log(float(current["high"]) / previous),
                "low_ret": math.log(float(current["low"]) / previous)})
        result = forecast(
            entry=float(sig.get("entry")), atr=float(sig.get("atr") or 0),
            direction=sig.get("dir"), stop=float(sig.get("stop") or 0),
            tp=float(sig.get("tp") or 0), hourly_returns=rets,
            horizon=config.FORECAST_HORIZON_BARS, paths=config.FORECAST_PATHS,
            emp_p_tp=empirical["p_tp"] if use_emp else None,
            emp_p_sl=empirical["p_sl"] if use_emp else None,
            emp_n=empirical["n"] if use_emp else 0,
            block_size=config.FORECAST_BLOCK_SIZE,
            calibration_status=cal["status"], bar_profiles=profiles,
            bar_minutes=config.FORECAST_BAR_MINUTES,
            regime_lookback=config.FORECAST_REGIME_LOOKBACK_BARS, seed=seed)
        if result:
            try:
                from decision.extrema_forecast import empirical_extrema_forecast
                extrema = empirical_extrema_forecast(
                    float(sig.get("entry")), sig.get("dir"), sig.get("regime"),
                    db_path=db_path,
                    strategy_id=sig.get("strategy_id")) if empirical_enabled else None
                if extrema:
                    result["extrema"] = extrema
            except Exception:
                pass
        return result
    except Exception:
        return None


def _directional_orderflow_score(features, direction):
    """把冻结订单流压到 [-1,1]；数据不足返回 None，不伪造中性流。"""
    if not isinstance(features, dict) or direction not in ("long", "short"):
        return None
    values = []
    for name in ("ofi_dynamic", "ofi_event_multilevel",
                 "ofi_event_cancel_imbalance", "depth_imbalance"):
        value = features.get(name)
        if value is not None:
            try:
                values.append(max(-1.0, min(1.0, float(value))))
            except (TypeError, ValueError):
                pass
    microprice = features.get("microprice_bps")
    if microprice is not None:
        try:
            values.append(max(-1.0, min(1.0, float(microprice) / 10.0)))
        except (TypeError, ValueError):
            pass
    if len(values) < config.DYNAMIC_TP_MIN_ORDERFLOW_FIELDS:
        return None
    score = sum(values) / len(values)
    return score if direction == "long" else -score


def _adjust_passage_for_orderflow(fc, score):
    """订单流仅作有界 odds 修正，timeout 权重保持不变后重新归一。"""
    scale = float(config.DYNAMIC_TP_ORDERFLOW_LOGIT_SCALE)
    tp = float(fc["p_hit_tp"]) * math.exp(scale * score)
    sl = float(fc["p_hit_sl"]) * math.exp(-scale * score)
    timeout = float(fc["p_timeout"])
    total = tp + sl + timeout
    if total <= 0:
        return None
    return {"p_hit_tp": tp / total, "p_hit_sl": sl / total,
            "p_timeout": timeout / total}


def optimize_take_profit(sig, base, klines, factor_features, db_path=None):
    """模拟盘动态 TP：市场结构候选 × 路径首触概率 × 订单流 × 成本后 EV。"""
    try:
        entry = float(sig["entry"])
        stop = float(sig["stop"])
        direction = str(sig["dir"])
        risk = abs(entry - stop)
        if entry <= 0 or risk <= 0 or direction not in ("long", "short"):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return {"passed": False, "reason": "invalid_geometry",
                "version": config.DYNAMIC_TP_VERSION}
    flow_score = _directional_orderflow_score(factor_features, direction)
    if flow_score is None:
        return {"passed": False, "reason": "insufficient_orderflow",
                "version": config.DYNAMIC_TP_VERSION}

    candidates = {round(float(rr), 8) for rr in
                  config.DYNAMIC_TP_REWARD_RISK_GRID if float(rr) > 0}
    for bar in list(klines or [])[-config.DYNAMIC_TP_STRUCTURE_LOOKBACK_BARS:]:
        try:
            level = float(bar["high"] if direction == "long" else bar["low"])
            reward = level - entry if direction == "long" else entry - level
            rr = reward / risk
            if min(config.DYNAMIC_TP_REWARD_RISK_GRID) <= rr <= max(
                    config.DYNAMIC_TP_REWARD_RISK_GRID):
                candidates.add(round(rr, 8))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue

    from decision.entry_probability import execution_cost_r
    ranked = []
    for rr in sorted(candidates):
        tp = entry + rr * risk if direction == "long" else entry - rr * risk
        candidate = dict(sig, tp=tp)
        fc = forecast_for_trade(
            candidate, base, klines, db_path=db_path, empirical_enabled=False,
            seed=config.DYNAMIC_TP_FORECAST_SEED)
        if not fc:
            continue
        adjusted = _adjust_passage_for_orderflow(fc, flow_score)
        cost_r = execution_cost_r(candidate)
        if adjusted is None or cost_r is None:
            continue
        terminal_r = ((float(fc["median"]) - entry) / risk
                      if direction == "long" else
                      (entry - float(fc["median"])) / risk)
        timeout_r = max(-1.0, min(rr, terminal_r))
        ev_r = (rr * adjusted["p_hit_tp"] - adjusted["p_hit_sl"] +
                timeout_r * adjusted["p_timeout"] - cost_r)
        ranked.append({
            "tp": round(tp, 8), "reward_risk": round(rr, 6),
            "ev_r": round(ev_r, 6), "cost_r": round(cost_r, 6),
            "timeout_r": round(timeout_r, 6),
            **{name: round(value, 6) for name, value in adjusted.items()},
            "forecast": dict(fc, expected_take_profit=round(tp, 6)),
        })
    if not ranked:
        return {"passed": False, "reason": "no_evaluable_target",
                "orderflow_score": round(flow_score, 6),
                "version": config.DYNAMIC_TP_VERSION}
    best = max(ranked, key=lambda item: (item["ev_r"], item["p_hit_tp"]))
    return {"passed": best["ev_r"] > config.DYNAMIC_TP_MIN_EV_R,
            "reason": ("positive_cost_adjusted_ev" if
                       best["ev_r"] > config.DYNAMIC_TP_MIN_EV_R else
                       "non_positive_cost_adjusted_ev"),
            "orderflow_score": round(flow_score, 6),
            "selected": best, "candidate_count": len(ranked),
            "version": config.DYNAMIC_TP_VERSION}


def describe(fc):
    if not fc:
        return "预测数据不足"
    status = "未校准" if fc.get("calibration_status") != "calibrated" else "已校准"
    minutes = fc.get("horizon_minutes")
    if minutes is None:
        minutes = int(float(fc.get("horizon_hours") or 0) * 60)
    target = fc.get("expected_take_profit")
    parts = [f"{minutes}min: 中位 {fc['median']}",
             f"5-95% [{fc['q05']}, {fc['q95']}]"]
    if target is not None:
        parts.append(f"预计止盈位 {target}")
    parts.extend([f"P(触止盈)={fc['p_hit_tp']*100:.0f}%",
                  f"P(触止损)={fc['p_hit_sl']*100:.0f}%", status])
    return " · ".join(parts)


def sync_calibration_for_signal(signal_id, db_path=None):
    """只从真实 signal_outcomes 写校准标签；没有完整路径时不落表。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    row = sdb.q1(
        "SELECT s.trade_id,s.features,o.* FROM signal_samples s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.signal_id=?", [signal_id], db_path=db_path)
    if not row:
        return False
    try:
        fc = json.loads(row.get("features") or "{}").get("forecast")
    except Exception:
        fc = None
    if not fc or fc.get("p_hit_tp") is None or fc.get("p_hit_sl") is None:
        return False
    p_timeout = fc.get("p_timeout")
    if p_timeout is None:
        p_timeout = max(0.0, 1 - float(fc["p_hit_tp"]) - float(fc["p_hit_sl"]))
    identity = row.get("trade_id") or signal_id
    sdb.x(
        "INSERT INTO forecast_calibration (trade_id,ts,p_hit_tp,p_hit_sl,"
        "hit_tp,hit_sl,pnl,signal_id,p_timeout,timeout,label_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
        "ts=excluded.ts,p_hit_tp=excluded.p_hit_tp,p_hit_sl=excluded.p_hit_sl,"
        "hit_tp=excluded.hit_tp,hit_sl=excluded.hit_sl,pnl=excluded.pnl,"
        "signal_id=excluded.signal_id,p_timeout=excluded.p_timeout,"
        "timeout=excluded.timeout,label_version=excluded.label_version",
        [identity, time.time(), fc["p_hit_tp"], fc["p_hit_sl"],
         row["tp_first"], row["sl_first"], row["pnl_r"], signal_id,
         p_timeout, row["timeout"], row["label_version"]], db_path=db_path)
    return True


def record_outcome(trade_id, forecast_json=None, closed=None, db_path=None):
    """兼容复盘调用：只有关联候选已有完整路径时才同步，不按 PnL 猜标签。"""
    import storage.db as sdb
    try:
        sdb.init_db(db_path)
        row = sdb.q1("SELECT signal_id FROM signal_samples WHERE trade_id=?",
                     [trade_id], db_path=db_path)
        return bool(row and sync_calibration_for_signal(row["signal_id"], db_path))
    except Exception:
        return False


def calibration(db_path=None, min_n=10, as_of_ts=None, strategy_id=None):
    """路径标签 Brier 报告；样本不足明确返回 uncalibrated。"""
    import storage.db as sdb
    try:
        sdb.init_db(db_path)
        conditions = ["c.signal_id IS NOT NULL", "c.p_hit_tp IS NOT NULL",
                      "c.p_hit_sl IS NOT NULL", "s.timeframe=?",
                      "s.horizon_hours=?", "s.strategy_id=?"]
        params = [config.SIGNAL_SAMPLE_TIMEFRAME,
                  config.SIGNAL_OUTCOME_HORIZON_HOURS]
        strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
        params.append(strategy_id)
        scope_version = research_scope_version(strategy_id)
        if scope_version:
            conditions.append("s.strategy_version=?")
            params.append(scope_version)
        if as_of_ts is not None:
            conditions.append("s.event_ts+s.horizon_hours*3600<=?")
            params.append(float(as_of_ts))
        rows = sdb.q(
            "SELECT c.p_hit_tp,c.p_hit_sl,c.p_timeout,c.hit_tp,c.hit_sl,c.timeout "
            "FROM forecast_calibration c JOIN signal_samples s "
            "ON s.signal_id=c.signal_id WHERE " + " AND ".join(conditions),
            params, db_path=db_path)
        n = len(rows)
        if n < min_n:
            return {"n": n, "status": "uncalibrated", "brier_tp": None,
                    "brier_sl": None, "brier_multiclass": None, "buckets": {}}
        b_tp = sum((row["p_hit_tp"] - row["hit_tp"]) ** 2 for row in rows) / n
        b_sl = sum((row["p_hit_sl"] - row["hit_sl"]) ** 2 for row in rows) / n
        b_multi = 0.0
        buckets = {}
        for row in rows:
            p_timeout = row["p_timeout"]
            if p_timeout is None:
                p_timeout = max(0.0, 1 - row["p_hit_tp"] - row["p_hit_sl"])
            b_multi += ((row["p_hit_tp"] - row["hit_tp"]) ** 2 +
                        (row["p_hit_sl"] - row["hit_sl"]) ** 2 +
                        (p_timeout - row["timeout"]) ** 2)
            for key, probability, hit in (
                    ("tp", row["p_hit_tp"], row["hit_tp"]),
                    ("sl", row["p_hit_sl"], row["hit_sl"]),
                    ("timeout", p_timeout, row["timeout"])):
                bucket = min(4, int(float(probability) * 5))
                item = buckets.setdefault(f"{key}_{bucket}",
                                          {"n": 0, "p_sum": 0.0, "hit": 0})
                item["n"] += 1
                item["p_sum"] += probability
                item["hit"] += hit
        out = {key: {"n": item["n"],
                     "avg_p": round(item["p_sum"] / item["n"], 4),
                     "hit_rate": round(item["hit"] / item["n"], 4)}
               for key, item in buckets.items()}
        return {"n": n, "status": "calibrated", "brier_tp": round(b_tp, 6),
                "brier_sl": round(b_sl, 6),
                "brier_multiclass": round(b_multi / n, 6), "buckets": out}
    except Exception:
        return {"n": 0, "status": "uncalibrated", "brier_tp": None,
                "brier_sl": None, "brier_multiclass": None, "buckets": {}}
