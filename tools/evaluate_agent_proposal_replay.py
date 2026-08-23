#!/usr/bin/env python3
"""Evaluate a research-only causal replay of the frozen proposal Agent."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.entry_probability import execution_cost_r


TRAIN_START_TS = 1_779_840_000.0
TRAIN_END_TS = 1_784_937_600.0
VALIDATION_END_EXCLUSIVE_TS = 1_787_529_600.0
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}
Z_ONE_SIDED_95 = 1.645
MIN_ACCURACY = 0.45
MIN_SCHEMA_SUCCESS = 0.95
MAX_DIRECTION_SHARE = 0.90
MAX_SYMBOL_CONCENTRATION = 0.50
FOLDS = 5
# Charge model calls as if each proposal used only 10 USDT notional.  This is
# 15x more conservative than the paper nominal cap and prevents tiny provider
# costs from being silently rounded away in R units.
MODEL_COST_ASSUMED_NOTIONAL_USD = 10.0


def _bounds(phase: str) -> tuple[float, float, int, int]:
    if phase == "training":
        return TRAIN_START_TS, TRAIN_END_TS, 100, 100
    if phase == "validation":
        return TRAIN_END_TS, VALIDATION_END_EXCLUSIVE_TS, 50, 50
    raise ValueError(f"unsupported phase: {phase}")


def _open(path: str) -> tuple[sqlite3.Connection, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    if resolved.name in RUNTIME_DB_NAMES or not resolved.is_file():
        raise ValueError("必须使用独立 Agent proposal research DB")
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key='research.agent_proposal_replay.latest'"
        ).fetchone()
        proof = json.loads(row[0]) if row else {}
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        conn.close()
        raise ValueError("数据库没有有效 Agent proposal 回放证明") from exc
    if not isinstance(proof, dict) or proof.get("research_only") is not True:
        conn.close()
        raise ValueError("数据库不是 research-only Agent proposal 回放")
    return conn, proof


def _wilson_lower(successes: int, n: int) -> float | None:
    if n <= 0:
        return None
    rate = successes / n
    z2 = Z_ONE_SIDED_95 ** 2
    centre = rate + z2 / (2 * n)
    radius = Z_ONE_SIDED_95 * math.sqrt(
        rate * (1 - rate) / n + z2 / (4 * n * n))
    return (centre - radius) / (1 + z2 / n)


def evaluate_phase(db_path: str, phase: str) -> dict[str, Any]:
    start, end, min_runs, min_proposals = _bounds(phase)
    conn, proof = _open(db_path)
    try:
        runs = [dict(row) for row in conn.execute(
            "SELECT r.*,c.estimated_cost_usd FROM agent_proposal_runs r "
            "LEFT JOIN proposal_replay_costs c ON c.run_id=r.run_id "
            "WHERE r.created_ts>=? AND r.created_ts<? ORDER BY r.created_ts",
            [start, end]).fetchall()]
        rows = [dict(row) for row in conn.execute(
            "SELECT p.*,s.event_ts,s.entry,s.stop,s.tp,s.atr,s.horizon_hours,"
            "s.features,o.tp_first,o.sl_first,o.timeout,o.pnl_r,"
            "r.valid_count,c.estimated_cost_usd FROM agent_proposals p "
            "JOIN agent_proposal_runs r ON r.run_id=p.run_id "
            "JOIN signal_samples s ON s.signal_id=p.signal_id "
            "JOIN signal_outcomes o ON o.signal_id=p.signal_id "
            "LEFT JOIN proposal_replay_costs c ON c.run_id=p.run_id "
            "WHERE r.created_ts>=? AND r.created_ts<? "
            "AND p.geometry_valid=1 ORDER BY r.created_ts,p.base,p.direction",
            [start, end]).fetchall()]
    finally:
        conn.close()

    result: dict[str, Any] = {
        "phase": phase, "research_only": True, "execution_authority": False,
        "budget_expansion_allowed": False, "proof": proof,
        "run_count": len(runs), "completed_runs": 0,
        "schema_success_rate": None, "runtime_statuses": {},
        "n": 0, "tp_first": 0, "sl_first": 0, "timeout": 0,
        "tp_accuracy": None, "tp_accuracy_wilson_lower_95": None,
        "median_breakeven_win_rate": None, "mean_exchange_cost_r": None,
        "mean_model_cost_r": None, "net_ev_r": None,
        "clustered_run_net_ev_r": None, "net_ev_lower_95": None,
        "directions": {}, "max_direction_share": None,
        "symbol_concentration": None, "symbols": {},
        "positive_folds": 0, "folds": [], "gates": {},
        "status": "stop_no_promotion",
    }
    statuses = Counter(str(row.get("runtime_status")) for row in runs)
    result["runtime_statuses"] = dict(sorted(statuses.items()))
    result["completed_runs"] = statuses.get("completed", 0)
    if runs:
        result["schema_success_rate"] = round(
            result["completed_runs"] / len(runs), 6)

    usable = []
    for row in rows:
        try:
            features = json.loads(row.get("features") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        row["funding_rate"] = (features.get("factor_features") or {}).get(
            "funding_rate")
        exchange_cost = execution_cost_r(row)
        valid_count = int(row.get("valid_count") or 0)
        risk_pct = abs(float(row["entry"]) - float(row["stop"])) / float(row["entry"])
        if (exchange_cost is None or valid_count <= 0 or risk_pct <= 0 or
                row.get("estimated_cost_usd") is None):
            continue
        allocated_usd = float(row["estimated_cost_usd"]) / valid_count
        model_cost_r = allocated_usd / (
            MODEL_COST_ASSUMED_NOTIONAL_USD * risk_pct)
        item = dict(row)
        item["exchange_cost_r"] = float(exchange_cost)
        item["model_cost_r"] = model_cost_r
        item["net_r"] = (float(row["pnl_r"]) - float(exchange_cost) -
                         model_cost_r)
        item["breakeven"] = (1 + float(exchange_cost) + model_cost_r) / 3
        usable.append(item)

    result["n"] = len(usable)
    if usable:
        result["tp_first"] = sum(int(row["tp_first"]) for row in usable)
        result["sl_first"] = sum(int(row["sl_first"]) for row in usable)
        result["timeout"] = sum(int(row["timeout"]) for row in usable)
        result["tp_accuracy"] = round(result["tp_first"] / len(usable), 6)
        result["tp_accuracy_wilson_lower_95"] = round(float(
            _wilson_lower(result["tp_first"], len(usable))), 6)
        result["median_breakeven_win_rate"] = round(statistics.median(
            row["breakeven"] for row in usable), 6)
        result["mean_exchange_cost_r"] = round(statistics.fmean(
            row["exchange_cost_r"] for row in usable), 6)
        result["mean_model_cost_r"] = round(statistics.fmean(
            row["model_cost_r"] for row in usable), 6)
        result["net_ev_r"] = round(statistics.fmean(
            row["net_r"] for row in usable), 6)

        run_values: dict[str, list[float]] = defaultdict(list)
        symbol_values: dict[str, list[float]] = defaultdict(list)
        for row in usable:
            run_values[str(row["run_id"])].append(row["net_r"])
            symbol_values[str(row["base"])].append(row["net_r"])
        clusters = [statistics.fmean(values)
                    for _, values in sorted(run_values.items())]
        mean = statistics.fmean(clusters)
        variance = statistics.variance(clusters) if len(clusters) > 1 else 0.0
        lower = mean - Z_ONE_SIDED_95 * math.sqrt(variance / len(clusters))
        result["clustered_run_net_ev_r"] = round(mean, 6)
        result["net_ev_lower_95"] = round(lower, 6)

        direction_counts = Counter(str(row["direction"]) for row in usable)
        result["directions"] = dict(sorted(direction_counts.items()))
        result["max_direction_share"] = round(
            max(direction_counts.values()) / len(usable), 6)
        totals = {symbol: sum(values)
                  for symbol, values in symbol_values.items()}
        positive = [value for value in totals.values() if value > 0]
        concentration = max(positive) / sum(positive) if positive else 1.0
        result["symbol_concentration"] = round(concentration, 6)
        result["symbols"] = {
            symbol: {"n": len(values),
                     "net_ev_r": round(statistics.fmean(values), 6)}
            for symbol, values in sorted(symbol_values.items())}

        ordered = sorted(run_values.items())
        block = len(ordered) // FOLDS
        if block:
            for index in range(FOLDS):
                lo = index * block
                hi = ((index + 1) * block if index < FOLDS - 1
                      else len(ordered))
                values = [value for _, batch in ordered[lo:hi]
                          for value in batch]
                if values:
                    result["folds"].append({
                        "fold": index, "n": len(values),
                        "net_ev_r": round(statistics.fmean(values), 6),
                    })
        result["positive_folds"] = sum(
            fold["net_ev_r"] > 0 for fold in result["folds"])

    gates = {
        "completed_runs": result["completed_runs"] >= min_runs,
        "schema_success": (result["schema_success_rate"] or 0) >= MIN_SCHEMA_SUCCESS,
        "complete_proposals": result["n"] >= min_proposals,
        "tp_accuracy": (result["tp_accuracy"] or 0) >= MIN_ACCURACY,
        "precision_lower_above_breakeven": (
            (result["tp_accuracy_wilson_lower_95"] or 0) >
            (result["median_breakeven_win_rate"] or 1)),
        "net_ev": (result["net_ev_r"] or 0) > 0,
        "clustered_lower": (result["net_ev_lower_95"] or 0) > 0,
        "fold_consistency": (len(result["folds"]) == FOLDS and
                             result["positive_folds"] >= 4),
        "symbol_concentration": (
            (result["symbol_concentration"] if
             result["symbol_concentration"] is not None else 1) <=
            MAX_SYMBOL_CONCENTRATION),
        "direction_balance": (
            (result["max_direction_share"] if
             result["max_direction_share"] is not None else 1) <=
            MAX_DIRECTION_SHARE),
    }
    result["gates"] = gates
    if all(gates.values()):
        result["status"] = "passed"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--phase", choices=("training", "validation"),
                        default="training")
    args = parser.parse_args()
    print(json.dumps(evaluate_phase(args.db, args.phase),
                     ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
