#!/usr/bin/env python3
"""Evaluate the frozen A/B strategies on the fixed high-volatility alt panel."""

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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.entry_probability import execution_cost_r


PANEL = ("AAVE", "CRV", "INJ", "NEAR", "ZRO")
STRATEGIES = ("A_pullback", "B_breakout")
VALIDATION_CUTOFF_TS = 1_784_966_400.0  # 2026-07-25 08:00 UTC
MIN_VALIDATION_N = 300
FOLDS = 5
MIN_POSITIVE_FOLDS = 4
MAX_SYMBOL_CONCENTRATION = 0.50
Z_ONE_SIDED_95 = 1.645
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open(path: str) -> tuple[Path, sqlite3.Connection, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    if resolved.name in RUNTIME_DB_NAMES or not resolved.is_file():
        raise ValueError("必须使用独立 alt research DB")
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'"
        ).fetchone()
        proof = json.loads(row[0]) if row else {}
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        conn.close()
        raise ValueError("数据库没有有效 research-only 重放证明") from exc
    if not isinstance(proof, dict) or proof.get("research_only") is not True:
        conn.close()
        raise ValueError("数据库不是 research-only 重放")
    return resolved, conn, proof


def _rows(conn: sqlite3.Connection, strategy_id: str, *,
          validation: bool) -> list[dict[str, Any]]:
    comparison = ">=" if validation else "<"
    placeholders = ",".join("?" for _ in PANEL)
    raw_rows = conn.execute(
        "SELECT s.signal_id,s.symbol,s.direction,s.event_ts,s.entry,s.stop,s.tp,"
        "s.atr,s.horizon_hours,s.features,o.tp_first,o.sl_first,o.timeout,o.pnl_r "
        "FROM signal_samples s JOIN signal_outcomes o USING(signal_id) "
        f"WHERE s.strategy_id=? AND s.symbol IN ({placeholders}) "
        f"AND s.event_ts {comparison} ? ORDER BY s.event_ts,s.symbol,s.signal_id",
        [strategy_id, *PANEL, VALIDATION_CUTOFF_TS]).fetchall()
    result = []
    for raw in raw_rows:
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
        result.append(row)
    return result


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "n": len(rows), "tp_first": 0, "sl_first": 0, "timeout": 0,
        "tp_accuracy": None, "gross_ev_r": None, "mean_cost_r": None,
        "net_ev_r": None, "clustered_event_net_ev_r": None,
        "net_ev_lower_95": None, "positive_folds": 0, "folds": [],
        "symbol_concentration": None, "symbols": {},
    }
    if not rows:
        return result
    result["tp_first"] = sum(int(row["tp_first"]) for row in rows)
    result["sl_first"] = sum(int(row["sl_first"]) for row in rows)
    result["timeout"] = sum(int(row["timeout"]) for row in rows)
    result["tp_accuracy"] = round(result["tp_first"] / len(rows), 6)
    result["gross_ev_r"] = round(statistics.fmean(
        float(row["pnl_r"]) for row in rows), 6)
    result["mean_cost_r"] = round(statistics.fmean(
        float(row["cost_r"]) for row in rows), 6)
    result["net_ev_r"] = round(statistics.fmean(
        float(row["net_r"]) for row in rows), 6)

    event_values: dict[float, list[float]] = defaultdict(list)
    symbol_values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        event_values[float(row["event_ts"])].append(float(row["net_r"]))
        symbol_values[str(row["symbol"])].append(float(row["net_r"]))
    clusters = [statistics.fmean(values)
                for _, values in sorted(event_values.items())]
    mean = statistics.fmean(clusters)
    variance = statistics.variance(clusters) if len(clusters) > 1 else 0.0
    lower = mean - Z_ONE_SIDED_95 * math.sqrt(variance / len(clusters))
    result["clustered_event_net_ev_r"] = round(mean, 6)
    result["net_ev_lower_95"] = round(lower, 6)

    totals = {symbol: sum(values) for symbol, values in symbol_values.items()}
    positive = [value for value in totals.values() if value > 0]
    concentration = max(positive) / sum(positive) if positive else 1.0
    result["symbol_concentration"] = round(concentration, 6)
    result["symbols"] = {
        symbol: {"n": len(values),
                 "net_ev_r": round(statistics.fmean(values), 6)}
        for symbol, values in sorted(symbol_values.items())}

    ordered_events = sorted(event_values.items())
    block = len(ordered_events) // FOLDS
    if block:
        for index in range(FOLDS):
            lo = index * block
            hi = ((index + 1) * block if index < FOLDS - 1
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


def _passes(summary: dict[str, Any]) -> bool:
    return bool(
        summary["n"] >= MIN_VALIDATION_N and
        (summary["net_ev_r"] or 0) > 0 and
        (summary["net_ev_lower_95"] or 0) > 0 and
        len(summary["folds"]) == FOLDS and
        summary["positive_folds"] >= MIN_POSITIVE_FOLDS and
        (summary["symbol_concentration"] if
         summary["symbol_concentration"] is not None else 1) <=
        MAX_SYMBOL_CONCENTRATION)


def evaluate(db_path: str) -> dict[str, Any]:
    path, conn, proof = _open(db_path)
    try:
        strategies = {}
        for strategy_id in STRATEGIES:
            training = _summary(_rows(conn, strategy_id, validation=False))
            validation = _summary(_rows(conn, strategy_id, validation=True))
            validation["passed"] = _passes(validation)
            strategies[strategy_id] = {
                "training_reference_only": training,
                "validation": validation,
                "status": ("eligible_for_model_research" if
                           validation["passed"] else "stop_no_promotion"),
            }
    finally:
        conn.close()
    eligible = [name for name, item in strategies.items()
                if item["validation"]["passed"]]
    return {
        "research_only": True, "execution_authority": False,
        "budget_expansion_allowed": False, "panel": list(PANEL),
        "validation_cutoff_ts": VALIDATION_CUTOFF_TS,
        "db_path": str(path), "db_sha256": _sha256(path), "proof": proof,
        "strategies": strategies, "eligible_strategies": eligible,
        "status": ("eligible_for_model_research" if eligible
                   else "stop_no_promotion"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2,
                     sort_keys=True))


if __name__ == "__main__":
    main()
