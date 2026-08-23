#!/usr/bin/env python3
"""Evaluate the frozen A-short contraction filter on sealed replay slices.

The rule and split are intentionally constants, not CLI-tunable parameters:
``A_pullback`` short, ADX >= 0.24 and Bollinger-width percentile <= 0.21.
The late development slice is evaluated first.  BNB/LTC are not queried unless
that slice passes every predeclared gate, so a failed development result keeps
the symbol holdout sealed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.entry_probability import execution_cost_r


STRATEGY_ID = "A_pullback"
DIRECTION = "short"
ADX_MIN = 0.24
BB_WIDTH_PERCENTILE_MAX = 0.21
CUTOFF_TS = 1_784_966_400.0  # 2026-07-25 08:00:00 UTC
DEVELOPMENT_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX")
HOLDOUT_SYMBOLS = ("BNB", "LTC")
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}
VALIDATION_FOLDS = 4
MIN_DEVELOPMENT_N = 50
MIN_HOLDOUT_N = 30
MIN_TP_ACCURACY = 0.45
MAX_SYMBOL_CONCENTRATION = 0.50
Z_ONE_SIDED_95 = 1.645


def _open_research(path: str) -> tuple[Path, sqlite3.Connection, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    if resolved.name in RUNTIME_DB_NAMES:
        raise ValueError("拒绝在运行数据库执行精度过滤研究")
    if not resolved.is_file():
        raise FileNotFoundError(f"研究数据库不存在: {resolved}")
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'"
        ).fetchone()
        metadata = json.loads(row[0]) if row else {}
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        conn.close()
        raise ValueError("数据库没有有效 research-only 重放证明") from exc
    if not isinstance(metadata, dict) or metadata.get("research_only") is not True:
        conn.close()
        raise ValueError("数据库没有 research-only 重放证明")
    return resolved, conn, metadata


def _query_filtered(conn: sqlite3.Connection, symbols: Iterable[str], *,
                    start_ts: float | None = None,
                    end_ts: float | None = None) -> list[dict[str, Any]]:
    symbols = tuple(symbols)
    placeholders = ",".join("?" for _ in symbols)
    clauses = [
        "s.strategy_id=?", "s.direction=?",
        f"s.symbol IN ({placeholders})",
        "CAST(json_extract(s.features,'$.factor_features.adx') AS REAL)>=?",
        "CAST(json_extract(s.features,'$.factor_features.bb_width_percentile') "
        "AS REAL)<=?",
    ]
    args: list[Any] = [STRATEGY_ID, DIRECTION, *symbols, ADX_MIN,
                       BB_WIDTH_PERCENTILE_MAX]
    if start_ts is not None:
        clauses.append("s.event_ts>=?")
        args.append(float(start_ts))
    if end_ts is not None:
        clauses.append("s.event_ts<?")
        args.append(float(end_ts))
    rows = conn.execute(
        "SELECT s.signal_id,s.symbol,s.direction,s.event_ts,s.entry,s.stop,s.tp,"
        "s.atr,s.horizon_hours,s.features,o.tp_first,o.sl_first,o.timeout,o.pnl_r "
        "FROM signal_samples s JOIN signal_outcomes o USING(signal_id) WHERE " +
        " AND ".join(clauses) + " ORDER BY s.event_ts,s.symbol,s.signal_id",
        args).fetchall()
    material = []
    for raw in rows:
        row = dict(raw)
        try:
            features = json.loads(row.get("features") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        row["funding_rate"] = (features.get("factor_features") or {}).get(
            "funding_rate")
        cost = execution_cost_r(row)
        if cost is None or row.get("pnl_r") is None:
            continue
        row["cost_r"] = float(cost)
        row["net_r"] = float(row["pnl_r"]) - float(cost)
        material.append(row)
    return material


def _wilson_lower(successes: int, n: int) -> float | None:
    if n <= 0:
        return None
    probability = successes / n
    z2 = Z_ONE_SIDED_95 ** 2
    centre = probability + z2 / (2 * n)
    radius = Z_ONE_SIDED_95 * math.sqrt(
        probability * (1 - probability) / n + z2 / (4 * n * n))
    return (centre - radius) / (1 + z2 / n)


def _summary(rows: list[dict[str, Any]], *, folds: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "n": len(rows), "tp_first": 0, "sl_first": 0, "timeout": 0,
        "tp_accuracy": None, "tp_accuracy_wilson_lower_95": None,
        "mean_cost_r": None, "median_breakeven_win_rate": None,
        "net_ev_r": None, "clustered_event_net_ev_r": None,
        "net_ev_lower_95": None, "symbol_concentration": None,
        "positive_folds": 0, "folds": [], "symbols": {},
    }
    if not rows:
        return result
    result["tp_first"] = sum(int(row["tp_first"]) for row in rows)
    result["sl_first"] = sum(int(row["sl_first"]) for row in rows)
    result["timeout"] = sum(int(row["timeout"]) for row in rows)
    result["tp_accuracy"] = round(result["tp_first"] / len(rows), 6)
    result["tp_accuracy_wilson_lower_95"] = round(
        float(_wilson_lower(result["tp_first"], len(rows))), 6)
    costs = [float(row["cost_r"]) for row in rows]
    result["mean_cost_r"] = round(statistics.fmean(costs), 6)
    result["median_breakeven_win_rate"] = round(
        statistics.median((1 + cost) / 3 for cost in costs), 6)
    result["net_ev_r"] = round(statistics.fmean(
        float(row["net_r"]) for row in rows), 6)

    event_values: dict[float, list[float]] = defaultdict(list)
    symbol_values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        event_values[float(row["event_ts"])].append(float(row["net_r"]))
        symbol_values[str(row["symbol"])].append(float(row["net_r"]))
    clusters = [statistics.fmean(values)
                for _, values in sorted(event_values.items())]
    cluster_mean = statistics.fmean(clusters)
    variance = statistics.variance(clusters) if len(clusters) > 1 else 0.0
    lower = cluster_mean - Z_ONE_SIDED_95 * math.sqrt(
        variance / len(clusters))
    result["clustered_event_net_ev_r"] = round(cluster_mean, 6)
    result["net_ev_lower_95"] = round(lower, 6)

    totals = {symbol: sum(values) for symbol, values in symbol_values.items()}
    positive = [value for value in totals.values() if value > 0]
    concentration = max(positive) / sum(positive) if positive else 1.0
    result["symbol_concentration"] = round(concentration, 6)
    result["symbols"] = {
        symbol: {"n": len(values),
                 "net_ev_r": round(statistics.fmean(values), 6)}
        for symbol, values in sorted(symbol_values.items())}

    block = len(clusters) // folds
    if block:
        ordered_events = sorted(event_values.items())
        for index in range(folds):
            lo = index * block
            hi = ((index + 1) * block if index < folds - 1
                  else len(ordered_events))
            values = [value for _, event in ordered_events[lo:hi]
                      for value in event]
            if values:
                result["folds"].append({
                    "fold": index, "n": len(values),
                    "net_ev_r": round(statistics.fmean(values), 6),
                })
    result["positive_folds"] = sum(
        fold["net_ev_r"] > 0 for fold in result["folds"])
    return result


def _development_passed(summary: dict[str, Any]) -> bool:
    return bool(
        summary["n"] >= MIN_DEVELOPMENT_N and
        (summary["tp_accuracy"] or 0) >= MIN_TP_ACCURACY and
        (summary["net_ev_r"] or 0) > 0 and
        (summary["net_ev_lower_95"] or 0) > 0 and
        len(summary["folds"]) == VALIDATION_FOLDS and
        summary["positive_folds"] >= 3 and
        (summary["symbol_concentration"] or 1) <= MAX_SYMBOL_CONCENTRATION)


def evaluate(db_path: str) -> dict[str, Any]:
    path, conn, metadata = _open_research(db_path)
    try:
        training = _summary(_query_filtered(
            conn, DEVELOPMENT_SYMBOLS, end_ts=CUTOFF_TS),
            folds=VALIDATION_FOLDS)
        development = _summary(_query_filtered(
            conn, DEVELOPMENT_SYMBOLS, start_ts=CUTOFF_TS),
            folds=VALIDATION_FOLDS)
        development["passed"] = _development_passed(development)
        result: dict[str, Any] = {
            "policy": "a_short_adx_bb_contraction_v1",
            "research_only": True,
            "execution_authority": False,
            "budget_expansion_allowed": False,
            "db_path": str(path),
            "db_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "replay": metadata,
            "rule": {"strategy_id": STRATEGY_ID, "direction": DIRECTION,
                     "adx_min": ADX_MIN,
                     "bb_width_percentile_max": BB_WIDTH_PERCENTILE_MAX},
            "split": {"cutoff_ts": CUTOFF_TS,
                      "development_symbols": list(DEVELOPMENT_SYMBOLS),
                      "holdout_symbols": list(HOLDOUT_SYMBOLS)},
            "training_reference_only": training,
            "late_development": development,
            "holdout": {"status": "sealed_not_opened"},
            "combined_validation": None,
            "status": "stop_no_promotion",
        }
        if not development["passed"]:
            return result

        holdout_rows = _query_filtered(conn, HOLDOUT_SYMBOLS)
        holdout = _summary(holdout_rows, folds=VALIDATION_FOLDS)
        each_symbol_positive = (
            set(holdout["symbols"]) == set(HOLDOUT_SYMBOLS) and
            all(item["net_ev_r"] > 0
                for item in holdout["symbols"].values()))
        holdout["passed"] = bool(
            holdout["n"] >= MIN_HOLDOUT_N and
            (holdout["net_ev_lower_95"] or 0) > 0 and
            each_symbol_positive)
        holdout["status"] = ("opened_passed" if holdout["passed"]
                             else "opened_failed")
        result["holdout"] = holdout
        if not holdout["passed"]:
            return result

        combined = _summary(
            _query_filtered(conn, DEVELOPMENT_SYMBOLS, start_ts=CUTOFF_TS) +
            holdout_rows, folds=VALIDATION_FOLDS)
        combined["wilson_exceeds_median_breakeven"] = bool(
            (combined["tp_accuracy_wilson_lower_95"] or 0) >
            (combined["median_breakeven_win_rate"] or 1))
        result["combined_validation"] = combined
        if combined["wilson_exceeds_median_breakeven"]:
            result["status"] = "eligible_for_separate_paper_shadow_review"
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True,
                        help="independent research-only replay database")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2,
                     sort_keys=True))


if __name__ == "__main__":
    main()
