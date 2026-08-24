#!/usr/bin/env python3
"""对独立 15m/4h research-only 重放库执行完整、可复现的停止/晋升裁决。

该工具会写因子试验和候选模型制品，因此只接受带
``research.15m_replay.latest.research_only=true`` 证明的独立研究库；运行库、
普通临时库和缺少 provenance 的数据库一律拒绝。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.signal_identity import research_scope_version

RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}
_MINUTE_MS = 60_000
PASSIVE_ENTRY_EVALUATION_VERSION = "passive-entry-v2-confirmed-klines"


def _research_metadata(db_path: str) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    if path.name in RUNTIME_DB_NAMES:
        raise ValueError("拒绝在运行数据库执行历史研究裁决")
    if not path.is_file():
        raise FileNotFoundError(f"研究数据库不存在: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("数据库没有 research-only 重放证明") from exc
    finally:
        conn.close()
    try:
        metadata = json.loads(row[0]) if row else {}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("research-only 重放证明损坏") from exc
    if not isinstance(metadata, dict) or metadata.get("research_only") is not True:
        raise ValueError("数据库没有 research-only 重放证明")
    return metadata


def _cost_r(row: dict[str, Any]) -> float:
    from decision.entry_probability import execution_cost_r
    return float(execution_cost_r(row) or 0.0)


def _outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(material: list[dict[str, Any]]) -> dict[str, Any]:
        if not material:
            return {"n": 0, "tp_first": 0, "sl_first": 0, "timeout": 0,
                    "gross_ev_r": None, "net_ev_r": None,
                    "net_profitable_rate": None}
        gross = [float(row["pnl_r"]) for row in material]
        net = [value - _cost_r(row)
               for value, row in zip(gross, material)]
        return {
            "n": len(material),
            "tp_first": sum(int(row["tp_first"]) for row in material),
            "sl_first": sum(int(row["sl_first"]) for row in material),
            "timeout": sum(int(row["timeout"]) for row in material),
            "gross_ev_r": round(sum(gross) / len(gross), 6),
            "net_ev_r": round(sum(net) / len(net), 6),
            "net_profitable_rate": round(
                sum(value > 0 for value in net) / len(net), 6),
        }

    return {
        "all": summarize(rows),
        "long": summarize([row for row in rows if row["direction"] == "long"]),
        "short": summarize([row for row in rows if row["direction"] == "short"]),
    }


def _calibration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "model": None, "constant_baseline": None,
                "brier_skill": None}
    n = len(rows)
    rates = {
        "tp": sum(int(row["hit_tp"]) for row in rows) / n,
        "sl": sum(int(row["hit_sl"]) for row in rows) / n,
        "timeout": sum(int(row["timeout"]) for row in rows) / n,
    }
    model_tp = sum((float(row["p_hit_tp"]) - int(row["hit_tp"])) ** 2
                   for row in rows) / n
    model_sl = sum((float(row["p_hit_sl"]) - int(row["hit_sl"])) ** 2
                   for row in rows) / n
    model_multi = 0.0
    base_tp = sum((rates["tp"] - int(row["hit_tp"])) ** 2 for row in rows) / n
    base_sl = sum((rates["sl"] - int(row["hit_sl"])) ** 2 for row in rows) / n
    base_multi = 0.0
    for row in rows:
        p_timeout = row.get("p_timeout")
        if p_timeout is None:
            p_timeout = max(0.0, 1 - float(row["p_hit_tp"]) -
                            float(row["p_hit_sl"]))
        model_multi += (
            (float(row["p_hit_tp"]) - int(row["hit_tp"])) ** 2 +
            (float(row["p_hit_sl"]) - int(row["hit_sl"])) ** 2 +
            (float(p_timeout) - int(row["timeout"])) ** 2)
        base_multi += (
            (rates["tp"] - int(row["hit_tp"])) ** 2 +
            (rates["sl"] - int(row["hit_sl"])) ** 2 +
            (rates["timeout"] - int(row["timeout"])) ** 2)
    model_multi /= n
    base_multi /= n

    def skill(model: float, baseline: float) -> float | None:
        return round(1 - model / baseline, 6) if baseline > 0 else None

    return {
        "n": n,
        "model": {"brier_tp": round(model_tp, 6),
                  "brier_sl": round(model_sl, 6),
                  "brier_multiclass": round(model_multi, 6)},
        "constant_baseline": {"rates": rates,
                              "brier_tp": round(base_tp, 6),
                              "brier_sl": round(base_sl, 6),
                              "brier_multiclass": round(base_multi, 6)},
        "brier_skill": {"tp": skill(model_tp, base_tp),
                        "sl": skill(model_sl, base_sl),
                        "multiclass": skill(model_multi, base_multi)},
    }


def _strategy_segments(rows: list[dict[str, Any]],
                       strategy_id: str) -> dict[str, Any]:
    """按信号时点行情与 shadow 路由分层；只汇总，不授予策略权限。"""
    market_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    route_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    match_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            snapshot = json.loads(row.get("features") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot = {}
        market = snapshot.get("market_regime") or {}
        route = snapshot.get("strategy_route") or {}
        state = (market.get("state") if isinstance(market, dict)
                 else None) or "unknown"
        selected = (route.get("selected_strategy")
                    if isinstance(route, dict) else None)
        route_key = str(selected or
                        ("abstain" if isinstance(route, dict) and
                         route.get("abstain") else "unknown"))
        match_key = "route_match" if selected == strategy_id else "route_mismatch"
        market_groups[state].append(row)
        route_groups[route_key].append(row)
        match_groups[match_key].append(row)

    def summarize(groups):
        return {name: _outcome_summary(material)["all"]
                for name, material in sorted(groups.items())}

    return {"market_regime": summarize(market_groups),
            "selected_strategy": summarize(route_groups),
            "route_alignment": summarize(match_groups)}


def _passive_entry_summary(rows: list[dict[str, Any]],
                           market_db: str | None,
                           entry_offset_pct: float = 0.0) -> dict[str, Any]:
    """Conservatively replay one-bar passive entry without granting authority.

    A limit rests at the frozen signal entry for one 15m signal bar.  Entry
    slippage is removed, but entry still pays the configured taker fee; the
    exit keeps taker fee plus slippage.  This intentionally understates a
    maker advantage.  A favorable TP inside the fill minute is ignored because
    OHLC cannot prove that it happened after the fill; a same-minute stop is
    counted because price must cross a long/short limit before reaching the
    adverse barrier (gaps are also conservatively stopped).
    """
    result: dict[str, Any] = {
        "evaluation_version": PASSIVE_ENTRY_EVALUATION_VERSION,
        "policy": ("roundtrip_cost_recovery_limit_one_15m_bar"
                   if entry_offset_pct > 0 else
                   "signal_entry_limit_one_15m_bar"),
        "entry_offset_pct": float(entry_offset_pct),
        "cost_assumption": "entry_taker_fee_no_slippage_exit_taker_plus_slippage",
        "candidates": len(rows), "fills": 0, "complete": 0,
        "unfilled": 0, "missing_path": 0, "fill_rate": None,
        "tp_first": 0, "sl_first": 0, "timeout": 0,
        "gross_ev_r": None, "net_ev_r_per_fill": None,
        "net_ev_r_per_candidate": None, "net_ev_lower_95": None,
        "clustered_event_net_ev_r": None, "symbol_concentration": None,
        "positive_folds": 0, "folds": [], "months": {}, "symbols": {},
        "market_table": None,
        "status": "unavailable",
        "budget_expansion_allowed": False,
    }
    if not rows or not market_db:
        result["reason"] = "missing_replay_market_db"
        return result
    timeframe = str(config.SIGNAL_SAMPLE_TIMEFRAME)
    if not timeframe.endswith("m") or not timeframe[:-1].isdigit():
        result["reason"] = "unsupported_signal_timeframe"
        return result
    ttl_ms = int(timeframe[:-1]) * _MINUTE_MS
    path = Path(market_db).expanduser().resolve()
    if path.name in RUNTIME_DB_NAMES or not path.is_file():
        result["reason"] = "invalid_replay_market_db"
        return result

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row["symbol"])].append(row)
    series: dict[str, tuple[list[int], list[tuple[Any, ...]]]] = {}
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        has_confirmed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='klines_v2'").fetchone()
        kline_table = "klines_v2" if has_confirmed else "klines"
        result["market_table"] = kline_table
        for symbol, material in by_symbol.items():
            lo = int(min(float(row["event_ts"]) for row in material) * 1000)
            hi = int(max(float(row["event_ts"]) +
                         float(row.get("horizon_hours") or
                               config.SIGNAL_OUTCOME_HORIZON_HOURS) * 3600 +
                         ttl_ms / 1000 for row in material) * 1000)
            bars = conn.execute(
                f"SELECT open_time,open,high,low,close FROM {kline_table} "
                "WHERE inst_id=? AND bar='1m' AND open_time>=? "
                "AND open_time<? ORDER BY open_time",
                [f"{symbol}-USDT-SWAP", lo, hi]).fetchall()
            series[symbol] = ([int(bar[0]) for bar in bars], bars)
    except sqlite3.Error:
        result["reason"] = "market_db_missing_klines"
        return result
    finally:
        conn.close()

    completed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (
            float(item["event_ts"]), str(item.get("signal_id") or ""))):
        times, bars = series.get(str(row["symbol"]), ([], []))
        event_ms = int(float(row["event_ts"]) * 1000)
        fill_end = event_ms + ttl_ms
        fill_lo = bisect.bisect_left(times, event_ms)
        fill_hi = bisect.bisect_left(times, fill_end)
        fill_window = bars[fill_lo:fill_hi]
        if (len(fill_window) < ttl_ms // _MINUTE_MS - 1 or
                (fill_window and int(fill_window[0][0]) > event_ms + _MINUTE_MS) or
                any(int(right[0]) - int(left[0]) > _MINUTE_MS
                    for left, right in zip(fill_window, fill_window[1:]))):
            result["missing_path"] += 1
            continue
        direction = str(row["direction"])
        original_entry = float(row["entry"])
        original_stop = float(row["stop"])
        risk = (original_entry - original_stop if direction == "long" else
                original_stop - original_entry)
        if direction not in ("long", "short") or risk <= 0 or original_entry <= 0:
            result["missing_path"] += 1
            continue
        entry = original_entry * (
            1 - entry_offset_pct if direction == "long" else
            1 + entry_offset_pct)
        stop = entry - risk if direction == "long" else entry + risk
        tp = entry + 2 * risk if direction == "long" else entry - 2 * risk
        fill_bar = next((bar for bar in fill_window
                         if (float(bar[3]) <= entry if direction == "long"
                             else float(bar[2]) >= entry)), None)
        if fill_bar is None:
            result["unfilled"] += 1
            continue
        result["fills"] += 1
        fill_ts = int(fill_bar[0])
        horizon_hours = int(row.get("horizon_hours") or
                            config.SIGNAL_OUTCOME_HORIZON_HOURS)
        end_ms = fill_ts + horizon_hours * 3_600_000
        path_lo = bisect.bisect_left(times, fill_ts)
        path_hi = bisect.bisect_left(times, end_ms)
        path_bars = bars[path_lo:path_hi]
        expected = horizon_hours * 60
        if (len(path_bars) < expected - 2 or not path_bars or
                int(path_bars[-1][0]) + _MINUTE_MS < end_ms or
                any(int(right[0]) - int(left[0]) > _MINUTE_MS
                    for left, right in zip(path_bars, path_bars[1:]))):
            result["missing_path"] += 1
            continue
        sl_first = tp_first = 0
        fill_stop = (float(fill_bar[3]) <= stop if direction == "long"
                     else float(fill_bar[2]) >= stop)
        if fill_stop:
            sl_first = 1
            exit_price = stop
        else:
            exit_price = None
            # Skip favorable same-fill-minute highs/lows: their order relative
            # to the limit fill is not observable from OHLC.
            for bar in path_bars[1:]:
                tp_hit = (float(bar[2]) >= tp if direction == "long"
                          else float(bar[3]) <= tp)
                sl_hit = (float(bar[3]) <= stop if direction == "long"
                          else float(bar[2]) >= stop)
                if sl_hit:
                    sl_first, exit_price = 1, stop
                    break
                if tp_hit:
                    tp_first, exit_price = 1, tp
                    break
        timeout = int(exit_price is None)
        if exit_price is None:
            exit_price = float(path_bars[-1][4])
        gross = ((exit_price - entry) / risk if direction == "long"
                 else (entry - exit_price) / risk)
        risk_pct = risk / entry
        immediate_trading = 2 * (config.FEE_RATE_TAKER + config.SLIPPAGE)
        immediate_cost = _cost_r(row)
        original_risk_pct = risk / original_entry
        funding_r = max(
            0.0, immediate_cost - immediate_trading / original_risk_pct)
        funding_r *= entry / original_entry
        passive_trading = (2 * config.FEE_RATE_TAKER + config.SLIPPAGE)
        cost_r = passive_trading / risk_pct + funding_r
        completed.append({
            "event_ts": float(row["event_ts"]), "symbol": row["symbol"],
            "direction": direction, "tp_first": tp_first,
            "sl_first": sl_first, "timeout": timeout,
            "gross_r": gross, "net_r": gross - cost_r,
        })

    result["complete"] = len(completed)
    result["fill_rate"] = (round(result["fills"] / len(rows), 6)
                           if rows else None)
    if not completed:
        result["reason"] = "no_complete_fills"
        return result
    result["tp_first"] = sum(row["tp_first"] for row in completed)
    result["sl_first"] = sum(row["sl_first"] for row in completed)
    result["timeout"] = sum(row["timeout"] for row in completed)
    gross = [row["gross_r"] for row in completed]
    net = [row["net_r"] for row in completed]
    mean_net = sum(net) / len(net)
    result["gross_ev_r"] = round(sum(gross) / len(gross), 6)
    result["net_ev_r_per_fill"] = round(mean_net, 6)
    result["net_ev_r_per_candidate"] = round(sum(net) / len(rows), 6)

    event_groups: list[list[dict[str, Any]]] = []
    for row in completed:
        if not event_groups or event_groups[-1][0]["event_ts"] != row["event_ts"]:
            event_groups.append([])
        event_groups[-1].append(row)
    # Cross-symbol candidates from one 15m close are a correlated market
    # event, not independent observations.  Use event-cluster means for the
    # uncertainty bound so a broad market move cannot manufacture confidence.
    cluster_net = [sum(item["net_r"] for item in group) / len(group)
                   for group in event_groups]
    cluster_mean = sum(cluster_net) / len(cluster_net)
    cluster_variance = (
        sum((value - cluster_mean) ** 2 for value in cluster_net) /
        (len(cluster_net) - 1) if len(cluster_net) > 1 else 0.0)
    lower = cluster_mean - config.ENTRY_MODEL_EV_Z * math.sqrt(
        cluster_variance / len(cluster_net))
    result["clustered_event_net_ev_r"] = round(cluster_mean, 6)
    result["net_ev_lower_95"] = round(lower, 6)

    symbol_net: dict[str, float] = defaultdict(float)
    month_rows: dict[str, list[float]] = defaultdict(list)
    for item in completed:
        symbol_net[str(item["symbol"])] += item["net_r"]
        month_rows[time.strftime(
            "%Y-%m", time.gmtime(item["event_ts"]))].append(item["net_r"])
    positive_symbol_net = [value for value in symbol_net.values() if value > 0]
    concentration = (max(positive_symbol_net) / sum(positive_symbol_net)
                     if positive_symbol_net else 1.0)
    result["symbol_concentration"] = round(concentration, 6)
    result["symbols"] = {
        symbol: {
            "n": sum(item["symbol"] == symbol for item in completed),
            "net_ev_r": round(
                sum(item["net_r"] for item in completed
                    if item["symbol"] == symbol) /
                sum(item["symbol"] == symbol for item in completed), 6),
        }
        for symbol in sorted(symbol_net)}
    result["months"] = {
        month: {"n": len(values),
                "net_ev_r": round(sum(values) / len(values), 6)}
        for month, values in sorted(month_rows.items())}

    fold_n = max(1, int(config.FACTOR_WALK_FORWARD_FOLDS))
    block = max(1, len(event_groups) // fold_n)
    folds = []
    for fold in range(fold_n):
        lo = fold * block
        hi = (fold + 1) * block if fold < fold_n - 1 else len(event_groups)
        material = [item for group in event_groups[lo:hi] for item in group]
        if not material:
            continue
        value = sum(item["net_r"] for item in material) / len(material)
        folds.append({"fold": fold, "n": len(material),
                      "net_ev_r": round(value, 6)})
    result["folds"] = folds
    result["positive_folds"] = sum(fold["net_ev_r"] > 0 for fold in folds)
    eligible = (
        len(completed) >= config.MODEL_MIN_SELECTED_EVALUATIONS and
        len(folds) >= config.FACTOR_WALK_FORWARD_FOLDS and
        result["positive_folds"] >= config.FACTOR_MIN_CONSISTENT_FOLDS and
        concentration <= config.FACTOR_MAX_SYMBOL_CONCENTRATION and lower > 0)
    result["status"] = ("eligible_for_separate_paper_shadow_review"
                        if eligible else "stop_no_promotion")
    return result


def _forecast_risk_prior_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the frozen first-passage SL probability as a veto prior.

    This is deliberately a fixed-threshold, research-only policy.  Rejected
    candidates contribute zero policy return; accepted candidates retain their
    full conservative net return.  No labels are used to change the threshold.
    """
    threshold = float(config.AGENT_HARNESS_REJECT_MIN_RISK)
    result: dict[str, Any] = {
        "policy": "frozen_first_passage_sl_risk_prior",
        "threshold": threshold, "candidates": len(rows), "usable": 0,
        "missing_forecast": 0, "reject_n": 0, "accepted_n": 0,
        "reject_coverage": None, "blocked_loss_precision": None,
        "rejected_net_ev_r": None, "accepted_net_ev_r": None,
        "baseline_net_ev_r": None, "policy_net_ev_r_per_candidate": None,
        "incremental_ev_r_per_candidate": None,
        "accepted_clustered_event_net_ev_r": None,
        "accepted_net_ev_lower_95": None, "brier": None,
        "baseline_brier": None, "brier_skill": None,
        "positive_folds": 0, "folds": [], "symbols": {},
        "symbol_concentration": None, "status": "unavailable",
        "budget_expansion_allowed": False,
    }
    usable = []
    for row in rows:
        try:
            snapshot = json.loads(row.get("features") or "{}")
            forecast = snapshot.get("forecast") or {}
            probability = float(forecast["p_hit_sl"])
            if not 0 <= probability <= 1:
                raise ValueError("probability outside [0,1]")
            net_r = float(row["pnl_r"]) - _cost_r(row)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            result["missing_forecast"] += 1
            continue
        usable.append({
            "event_ts": float(row["event_ts"]),
            "symbol": str(row["symbol"]),
            "probability": probability,
            "sl_first": bool(row.get("sl_first")),
            "net_r": net_r,
            "rejected": probability >= threshold,
        })
    result["usable"] = len(usable)
    if not usable:
        result["reason"] = "no_frozen_forecast"
        return result
    rejected = [row for row in usable if row["rejected"]]
    accepted = [row for row in usable if not row["rejected"]]
    result["reject_n"] = len(rejected)
    result["accepted_n"] = len(accepted)
    result["reject_coverage"] = round(len(rejected) / len(usable), 6)
    if rejected:
        result["blocked_loss_precision"] = round(
            sum(row["net_r"] < 0 for row in rejected) / len(rejected), 6)
        result["rejected_net_ev_r"] = round(
            sum(row["net_r"] for row in rejected) / len(rejected), 6)
    if accepted:
        result["accepted_net_ev_r"] = round(
            sum(row["net_r"] for row in accepted) / len(accepted), 6)
    baseline_ev = sum(row["net_r"] for row in usable) / len(usable)
    policy_ev = sum(row["net_r"] for row in accepted) / len(usable)
    result["baseline_net_ev_r"] = round(baseline_ev, 6)
    result["policy_net_ev_r_per_candidate"] = round(policy_ev, 6)
    result["incremental_ev_r_per_candidate"] = round(
        policy_ev - baseline_ev, 6)

    actual_rate = sum(row["sl_first"] for row in usable) / len(usable)
    model_brier = sum(
        (row["probability"] - float(row["sl_first"])) ** 2
        for row in usable) / len(usable)
    base_brier = sum(
        (actual_rate - float(row["sl_first"])) ** 2
        for row in usable) / len(usable)
    result["brier"] = round(model_brier, 6)
    result["baseline_brier"] = round(base_brier, 6)
    result["brier_skill"] = round(
        1 - model_brier / base_brier if base_brier > 0 else 0.0, 6)

    if accepted:
        event_values: dict[float, list[float]] = defaultdict(list)
        for row in accepted:
            event_values[row["event_ts"]].append(row["net_r"])
        clusters = [sum(values) / len(values)
                    for _, values in sorted(event_values.items())]
        cluster_mean = sum(clusters) / len(clusters)
        variance = (sum((value - cluster_mean) ** 2 for value in clusters) /
                    (len(clusters) - 1) if len(clusters) > 1 else 0.0)
        lower = cluster_mean - config.AGENT_EVAL_EV_Z * math.sqrt(
            variance / len(clusters))
        result["accepted_clustered_event_net_ev_r"] = round(cluster_mean, 6)
        result["accepted_net_ev_lower_95"] = round(lower, 6)
    else:
        lower = float("-inf")

    symbol_rows: dict[str, list[float]] = defaultdict(list)
    for row in accepted:
        symbol_rows[row["symbol"]].append(row["net_r"])
    result["symbols"] = {
        symbol: {"n": len(values),
                 "net_ev_r": round(sum(values) / len(values), 6)}
        for symbol, values in sorted(symbol_rows.items())}
    positive = [sum(values) for values in symbol_rows.values()
                if sum(values) > 0]
    concentration = max(positive) / sum(positive) if positive else 1.0
    result["symbol_concentration"] = round(concentration, 6)

    event_groups: list[list[dict[str, Any]]] = []
    for row in sorted(usable, key=lambda item: (
            item["event_ts"], item["symbol"])):
        if (not event_groups or
                event_groups[-1][0]["event_ts"] != row["event_ts"]):
            event_groups.append([])
        event_groups[-1].append(row)
    fold_n = int(config.FACTOR_WALK_FORWARD_FOLDS)
    block = len(event_groups) // fold_n
    if block:
        for fold in range(fold_n):
            lo = fold * block
            hi = ((fold + 1) * block if fold < fold_n - 1
                  else len(event_groups))
            material = [row for group in event_groups[lo:hi] for row in group]
            fold_accepted = [row for row in material if not row["rejected"]]
            fold_rejected = [row for row in material if row["rejected"]]
            fold_baseline = sum(row["net_r"] for row in material) / len(material)
            fold_policy = sum(row["net_r"] for row in fold_accepted) / len(material)
            accepted_ev = (sum(row["net_r"] for row in fold_accepted) /
                           len(fold_accepted) if fold_accepted else -99.0)
            result["folds"].append({
                "fold": fold, "n": len(material),
                "reject_n": len(fold_rejected),
                "accepted_n": len(fold_accepted),
                "accepted_net_ev_r": round(accepted_ev, 6),
                "incremental_ev_r_per_candidate": round(
                    fold_policy - fold_baseline, 6),
                "blocked_loss_precision": (round(
                    sum(row["net_r"] < 0 for row in fold_rejected) /
                    len(fold_rejected), 6) if fold_rejected else None),
            })
    result["positive_folds"] = sum(
        fold["incremental_ev_r_per_candidate"] > 0 and
        fold["accepted_net_ev_r"] > 0 for fold in result["folds"])
    eligible = (
        len(usable) >= config.FACTOR_MIN_SAMPLES and
        len(rejected) >= config.AGENT_EVAL_MIN_REJECT and
        len(accepted) >= config.AGENT_EVAL_MIN_VALID and
        result["brier_skill"] > 0 and
        len(result["folds"]) == fold_n and
        result["positive_folds"] >= config.FACTOR_MIN_CONSISTENT_FOLDS and
        result["blocked_loss_precision"] is not None and
        result["blocked_loss_precision"] >= threshold and
        lower > 0 and
        concentration <= config.FACTOR_MAX_SYMBOL_CONCENTRATION)
    result["status"] = ("eligible_for_harness_shadow_context_review"
                        if eligible else "stop_no_promotion")
    return result


def evaluate_research(db_path: str, strategy_id: str | None = None) -> dict[str, Any]:
    """执行因子挖掘与官方模型门，并输出不会授权预算扩大的研究裁决。"""
    replay = _research_metadata(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    from storage import db as sdb
    from factors.intraday_factor_mining import run_mining
    from factors.entry_model_training import train_entry_model
    from factors.extrema_model_training import train_extrema_model

    sdb.init_db(db_path)
    strategy_version = research_scope_version(strategy_id)
    scope = [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
             config.SIGNAL_OUTCOME_HORIZON_HOURS, strategy_version]
    rows = sdb.q(
        "SELECT s.signal_id,s.event_ts,s.symbol,s.direction,s.entry,s.stop,s.tp,"
        "s.horizon_hours,s.features,o.pnl_r,o.tp_first,o.sl_first,o.timeout "
        "FROM signal_samples s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=? "
        "AND s.strategy_version=? "
        "ORDER BY s.event_ts",
        scope, db_path=db_path)
    calibration_rows = sdb.q(
        "SELECT c.p_hit_tp,c.p_hit_sl,c.p_timeout,c.hit_tp,c.hit_sl,c.timeout "
        "FROM forecast_calibration c JOIN signal_samples s "
        "ON s.signal_id=c.signal_id WHERE s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=? AND s.strategy_version=?",
        scope, db_path=db_path)
    candidates = int(sdb.q1(
        "SELECT COUNT(*) n FROM signal_samples WHERE strategy_id=? "
        "AND timeframe=? AND horizon_hours=? AND strategy_version=?",
        scope, db_path=db_path)["n"])
    months = Counter(time.strftime("%Y-%m", time.gmtime(float(row["event_ts"])))
                     for row in rows)
    regimes = Counter()
    for row in rows:
        try:
            regime = json.loads(row.get("features") or "{}").get("regime") or {}
            tag = regime.get("tag") if isinstance(regime, dict) else str(regime)
        except (TypeError, ValueError, json.JSONDecodeError):
            tag = None
        regimes[tag or "unknown"] += 1

    factor_results = run_mining(db_path=db_path, strategy_id=strategy_id)
    factor_status = Counter(result["status"] for result in factor_results)
    entry = {direction: train_entry_model(
                direction, db_path=db_path, strategy_id=strategy_id)
             for direction in ("long", "short")}
    extrema = {direction: train_extrema_model(
                  direction, db_path=db_path, strategy_id=strategy_id)
               for direction in ("long", "short")}
    outcomes = _outcome_summary(rows)
    segments = _strategy_segments(rows, strategy_id)
    market_db = replay.get("market_db")
    passive_entry = {
        "at_signal": _passive_entry_summary(rows, market_db),
        "cost_recovery": _passive_entry_summary(
            rows, market_db,
            entry_offset_pct=2 * (config.FEE_RATE_TAKER + config.SLIPPAGE)),
    }
    forecast_risk_prior = _forecast_risk_prior_summary(rows)
    calibration = _calibration_summary(calibration_rows)
    validated = [result["name"] for result in factor_results
                 if result["status"] == "validated"]
    positive_cost_ev = bool(outcomes["all"]["net_ev_r"] is not None and
                            outcomes["all"]["net_ev_r"] > 0)
    calibration_pass = bool(
        calibration["n"] >= config.FORECAST_MIN_CALIBRATION and
        calibration.get("brier_skill") and
        calibration["brier_skill"].get("multiclass") is not None and
        calibration["brier_skill"]["multiclass"] >
        config.ENTRY_MODEL_MIN_BRIER_SKILL)
    result = {
        "generated_ts": time.time(), "research_only": True,
        "db_path": str(Path(db_path).expanduser().resolve()),
        "replay": replay,
        "scope": {"strategy_id": strategy_id, "timeframe": scope[1],
                  "horizon_hours": scope[2],
                  "strategy_version": strategy_version},
        "coverage": {"candidates": candidates, "outcomes": len(rows),
                     "symbols": len({row["symbol"] for row in rows}),
                     "months": dict(sorted(months.items())),
                     "regimes": dict(sorted(regimes.items()))},
        "outcomes": outcomes,
        "segments": segments,
        "passive_entry": passive_entry,
        "forecast_risk_prior": forecast_risk_prior,
        "factors": {"tested": len(factor_results),
                    "status_counts": dict(sorted(factor_status.items())),
                    "validated": validated},
        "calibration": calibration,
        "models": {"entry": entry, "extrema": extrema},
        "decision": {
            "positive_cost_ev": positive_cost_ev,
            "validated_factor": bool(validated),
            "calibration_pass": calibration_pass,
            # 历史 research-only 结果永远不能直接扩大运行预算。
            "budget_expansion_allowed": False,
            "status": ("eligible_for_paper_shadow_review"
                       if positive_cost_ev and validated and calibration_pass
                       else "stop_no_promotion"),
        },
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    sdb.x("INSERT OR REPLACE INTO kv (key,value,updated_at) VALUES (?,?,?)",
          [f"research.15m_evaluation.{strategy_id}.latest", payload,
           time.time()], db_path=db_path)
    if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID:
        sdb.x("INSERT OR REPLACE INTO kv (key,value,updated_at) VALUES (?,?,?)",
              ["research.15m_evaluation.latest", payload, time.time()],
              db_path=db_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True,
                        help="带 research-only provenance 的独立重放库")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--strategy-id", default=config.ENTRY_SIGNAL_STRATEGY_ID,
                        help="独立评价的策略身份（默认 A_pullback）")
    args = parser.parse_args()
    result = evaluate_research(args.db, strategy_id=args.strategy_id)
    print(json.dumps(result, ensure_ascii=False,
                     indent=None if args.compact else 2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
