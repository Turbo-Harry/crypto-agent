"""预注册止损 ATR 尺度 × reward:risk 的 research-only 时间验证。"""
import math
import sqlite3

import config
from decision.signal_outcomes import settle_barrier_grid
from factors.intraday_factor_gate import purged_walk_forward_splits


def _cluster_lower(rows, variant_name):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["kline_ts"], []).append(
            row["barriers"][variant_name]["net_pnl_r"])
    values = [sum(group) / len(group) for group in grouped.values()]
    if not values:
        return None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return mean - config.ENTRY_MODEL_EV_Z * math.sqrt(variance / len(values))


def evaluate_barrier_rows(rows):
    """所有预注册方案逐个裁决，不从结果中挑“最佳”方案。"""
    splits = purged_walk_forward_splits(rows)
    output = []
    for name, stop_atr_mult, reward_risk in config.EXIT_BARRIER_RESEARCH_GRID:
        folds = []
        oos = []
        for fold, (_, test_idx) in enumerate(splits):
            test = [rows[idx] for idx in test_idx]
            returns = [row["barriers"][name]["net_pnl_r"] for row in test]
            oos.extend(test)
            folds.append({"fold": fold, "n": len(test),
                          "net_ev": sum(returns) / len(returns),
                          "cluster_lower": _cluster_lower(test, name)})
        net = [row["barriers"][name]["net_pnl_r"] for row in oos]
        positive_folds = sum(item["net_ev"] > 0 and
                             item["cluster_lower"] is not None and
                             item["cluster_lower"] > 0 for item in folds)
        lower = _cluster_lower(oos, name)
        eligible = (len(folds) >= config.FACTOR_WALK_FORWARD_FOLDS and
                    len(net) >= config.ENTRY_MODEL_MIN_SAMPLES and
                    positive_folds >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                    lower is not None and lower > 0)
        output.append({
            "name": name, "stop_atr_mult": stop_atr_mult,
            "reward_risk": reward_risk,
            "target_atr_mult": stop_atr_mult * reward_risk,
            "n": len(net), "folds": folds,
            "positive_folds": positive_folds,
            "oos_net_ev": sum(net) / len(net) if net else None,
            "cluster_lower": lower,
            "status": ("eligible_for_model_challenge" if eligible
                       else "stop_no_promotion"),
            "research_only": True,
        })
    return output


def load_and_settle_rows(sample_db, market_db, strategy_id,
                         strategy_version, direction):
    """从冻结候选和确认 1m 行情重建多障碍标签，不写任一数据库。"""
    samples = sqlite3.connect(sample_db)
    samples.row_factory = sqlite3.Row
    market = sqlite3.connect(market_db)
    market.row_factory = sqlite3.Row
    try:
        source = samples.execute(
            "SELECT * FROM signal_samples WHERE strategy_id=? "
            "AND strategy_version=? AND direction=? ORDER BY event_ts",
            (strategy_id, strategy_version, direction)).fetchall()
        rows = []
        for raw in source:
            sample = dict(raw)
            start_ms = int(float(sample["event_ts"]) * 1000)
            end_ms = start_ms + int(sample["horizon_hours"]) * 3_600_000
            bars = market.execute(
                "SELECT open_time ts,open,high,low,close FROM klines "
                "WHERE inst_id=? AND bar='1m' AND open_time>=? AND open_time<? "
                "ORDER BY open_time",
                (f"{sample['symbol']}-USDT-SWAP", start_ms, end_ms)).fetchall()
            barriers = settle_barrier_grid(sample, bars)
            if barriers is None:
                continue
            rows.append({"signal_id": sample["signal_id"],
                         "event_ts": sample["event_ts"],
                         "kline_ts": sample["kline_ts"],
                         "label_end_ts": sample["event_ts"] +
                         sample["horizon_hours"] * 3600,
                         "barriers": barriers})
        return rows
    finally:
        samples.close()
        market.close()
