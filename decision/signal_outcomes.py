"""候选信号 4h/1m 路径标签（对应 15m 主周期）。

同一根分钟 K 同时覆盖 TP/SL 时采取保守约定：SL first，同时标 ambiguous。
数据覆盖不足返回 None，调用方保持 pending；绝不以当前价替代缺失路径。
"""
import math
import time
from typing import Any, Dict, Iterable, List, Optional

import config


_BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
           "1H": 3_600_000}


def _bar(row: Any) -> Dict[str, float]:
    if hasattr(row, "ts"):
        return {"ts": int(row.ts), "open": float(row.open),
                "high": float(row.high), "low": float(row.low),
                "close": float(row.close)}
    if isinstance(row, dict):
        ts = row.get("ts", row.get("open_time"))
        return {"ts": int(ts), "open": float(row["open"]),
                "high": float(row["high"]), "low": float(row["low"]),
                "close": float(row["close"])}
    return {"ts": int(row[0]), "open": float(row[1]),
            "high": float(row[2]), "low": float(row[3]),
            "close": float(row[4])}


def settle_path(sample: Dict[str, Any], bars: Iterable[Any],
                bar_resolution: str = None,
                label_version: str = None) -> Optional[Dict[str, Any]]:
    """纯函数：完整路径才返回标签；覆盖缺口或无效障碍返回 None。"""
    resolution = bar_resolution or config.SIGNAL_OUTCOME_BAR
    bar_ms = _BAR_MS.get(resolution)
    if not bar_ms:
        raise ValueError(f"unsupported bar resolution: {resolution}")
    event_ms = int(float(sample["event_ts"]) * 1000)
    horizon_hours = int(sample.get("horizon_hours") or
                        config.SIGNAL_OUTCOME_HORIZON_HOURS)
    if label_version is None:
        if (sample.get("timeframe") == config.SIGNAL_SAMPLE_TIMEFRAME and
                horizon_hours == config.SIGNAL_OUTCOME_HORIZON_HOURS):
            label_version = config.SIGNAL_OUTCOME_LABEL_VERSION
        else:
            label_version = (f"first-passage-{sample.get('timeframe') or 'unknown'}-"
                             f"{horizon_hours}h-v1")
    end_ms = event_ms + horizon_hours * 3_600_000
    normalized = {}
    for raw in bars:
        parsed = _bar(raw)
        normalized[parsed["ts"]] = parsed
    path: List[Dict[str, float]] = [normalized[key] for key in sorted(normalized)
                                    if event_ms <= key < end_ms]
    expected = horizon_hours * 3_600_000 // bar_ms
    # 事件通常不在整分钟；首尾各容许一个不完整 bar，但不容许中间大片缺失。
    if (not path or path[0]["ts"] > event_ms + bar_ms or
            path[-1]["ts"] + bar_ms < end_ms or len(path) < expected - 2):
        return None
    if any(right["ts"] - left["ts"] > bar_ms
           for left, right in zip(path, path[1:])):
        return None

    direction = str(sample["direction"])
    entry, stop, tp = (float(sample[name]) for name in ("entry", "stop", "tp"))
    risk = entry - stop if direction == "long" else stop - entry
    if direction not in ("long", "short") or min(entry, stop, tp, risk) <= 0:
        return None
    if (direction == "long" and not (stop < entry < tp)) or \
            (direction == "short" and not (tp < entry < stop)):
        return None

    tp_first = sl_first = ambiguous = 0
    t_tp = t_sl = None
    exit_price = None
    for row in path:
        tp_hit = row["high"] >= tp if direction == "long" else row["low"] <= tp
        sl_hit = row["low"] <= stop if direction == "long" else row["high"] >= stop
        elapsed = max(0.0, (row["ts"] - event_ms) / 1000)
        if tp_hit and t_tp is None:
            t_tp = elapsed
        if sl_hit and t_sl is None:
            t_sl = elapsed
        if exit_price is not None:
            continue
        if tp_hit and sl_hit:
            sl_first, ambiguous, exit_price = 1, 1, stop
        elif sl_hit:
            sl_first, exit_price = 1, stop
        elif tp_hit:
            tp_first, exit_price = 1, tp

    timeout = 1 if exit_price is None else 0
    if timeout:
        exit_price = path[-1]["close"]
    highs = [row["high"] for row in path]
    lows = [row["low"] for row in path]
    high_value, low_value = max(highs), min(lows)
    high_row = next(row for row in path if row["high"] == high_value)
    low_row = next(row for row in path if row["low"] == low_value)
    if direction == "long":
        pnl_r = (exit_price - entry) / risk
        mfe_r = max(0.0, (high_value - entry) / risk)
        mae_r = max(0.0, (entry - low_value) / risk)
    else:
        pnl_r = (entry - exit_price) / risk
        mfe_r = max(0.0, (entry - low_value) / risk)
        mae_r = max(0.0, (high_value - entry) / risk)
    return {
        "signal_id": sample["signal_id"], "horizon_hours": horizon_hours,
        "tp_first": tp_first, "sl_first": sl_first, "timeout": timeout,
        "ambiguous": ambiguous, "pnl_r": round(pnl_r, 8),
        "mfe_r": round(mfe_r, 8), "mae_r": round(mae_r, 8),
        "high_ret_h": round(math.log(high_value / entry), 10),
        "low_ret_h": round(math.log(low_value / entry), 10),
        "time_to_tp_sec": t_tp, "time_to_sl_sec": t_sl,
        "time_to_high_sec": max(0.0, (high_row["ts"] - event_ms) / 1000),
        "time_to_low_sec": max(0.0, (low_row["ts"] - event_ms) / 1000),
        "settled_at": time.time(), "bar_resolution": resolution,
        "label_version": label_version,
    }


def persist_outcome(outcome: Dict[str, Any], db_path: Optional[str] = None) -> None:
    """同一 signal_id 幂等覆盖；label_version 升级时允许确定性重算。"""
    import storage.db as sdb
    columns = list(outcome)
    updates = ",".join(f"{name}=excluded.{name}" for name in columns[1:])
    sdb.x(
        f"INSERT INTO signal_outcomes ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT(signal_id) DO UPDATE SET {updates}",
        [outcome[name] for name in columns], db_path=db_path)
    try:
        from decision.forecast import sync_calibration_for_signal
        sync_calibration_for_signal(outcome["signal_id"], db_path=db_path)
    except Exception:
        pass
    # 所有消费方必须复用同一条路径事实：旧 AI 判断与新 Harness 评价不能
    # 各自用终点价格或另一套标签。辅助回填失败不影响权威 outcome 落库，
    # worker 的周期 sweep 会再次幂等补齐。
    try:
        from decision.agent_judge import sweep_outcomes
        sweep_outcomes(db_path=db_path)
    except Exception:
        pass
    try:
        from storage.agent_harness import mature_pending_evaluations
        mature_pending_evaluations(signal_id=outcome["signal_id"],
                                   db_path=db_path)
    except Exception:
        pass


def settle_pending(exchange, db_path: Optional[str] = None,
                   now: Optional[float] = None) -> Dict[str, int]:
    """结算全部到期未标注候选；返回扫描/成功/缺失/错误计数。"""
    import storage.db as sdb
    now = float(now if now is not None else time.time())
    sdb.init_db(db_path)
    rows = sdb.q(
        "SELECT s.* FROM signal_samples s LEFT JOIN signal_outcomes o "
        "ON o.signal_id=s.signal_id WHERE o.signal_id IS NULL "
        "AND s.event_ts + s.horizon_hours*3600 <= ? ORDER BY s.event_ts",
        [now], db_path=db_path)
    stats = {"scanned": len(rows), "settled": 0, "missing": 0, "errors": 0}
    for sample in rows:
        try:
            inst_id = (f"{sample['symbol']}-USDT-SWAP"
                       if sample.get("venue") != "spot"
                       else f"{sample['symbol']}-USDT")
            since_ms = int(float(sample["event_ts"]) * 1000)
            until_ms = since_ms + int(sample["horizon_hours"]) * 3_600_000
            bar_ms = _BAR_MS[config.SIGNAL_OUTCOME_BAR]
            required_bars = int(sample["horizon_hours"]) * 3_600_000 // bar_ms + 2
            bars = exchange.fetch_candles_range(
                inst_id, config.SIGNAL_OUTCOME_BAR, since_ms, until_ms,
                max_bars=max(config.SIGNAL_OUTCOME_MAX_FETCH_BARS,
                             required_bars))
            outcome = settle_path(sample, bars)
            if outcome is None:
                stats["missing"] += 1
                continue
            persist_outcome(outcome, db_path=db_path)
            stats["settled"] += 1
        except Exception:
            stats["errors"] += 1
    return stats
