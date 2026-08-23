#!/usr/bin/env python3
"""对独立 15m/4h research-only 重放库执行完整、可复现的停止/晋升裁决。

该工具会写因子试验和候选模型制品，因此只接受带
``research.15m_replay.latest.research_only=true`` 证明的独立研究库；运行库、
普通临时库和缺少 provenance 的数据库一律拒绝。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

import config

RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}


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


def evaluate_research(db_path: str, strategy_id: str | None = None) -> dict[str, Any]:
    """执行因子挖掘与官方模型门，并输出不会授权预算扩大的研究裁决。"""
    replay = _research_metadata(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    from storage import db as sdb
    from factors.intraday_factor_mining import run_mining
    from factors.entry_model_training import train_entry_model
    from factors.extrema_model_training import train_extrema_model

    sdb.init_db(db_path)
    scope = [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
             config.SIGNAL_OUTCOME_HORIZON_HOURS]
    rows = sdb.q(
        "SELECT s.signal_id,s.event_ts,s.symbol,s.direction,s.entry,s.stop,"
        "s.features,o.pnl_r,o.tp_first,o.sl_first,o.timeout "
        "FROM signal_samples_canonical s "
        "JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=? "
        "ORDER BY s.event_ts",
        scope, db_path=db_path)
    calibration_rows = sdb.q(
        "SELECT c.p_hit_tp,c.p_hit_sl,c.p_timeout,c.hit_tp,c.hit_sl,c.timeout "
        "FROM forecast_calibration c JOIN signal_samples_canonical s "
        "ON s.signal_id=c.signal_id WHERE s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=?",
        scope, db_path=db_path)
    candidates = int(sdb.q1(
        "SELECT COUNT(*) n FROM signal_samples_canonical WHERE strategy_id=? "
        "AND timeframe=? AND horizon_hours=?",
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
                  "horizon_hours": scope[2]},
        "coverage": {"candidates": candidates, "outcomes": len(rows),
                     "symbols": len({row["symbol"] for row in rows}),
                     "months": dict(sorted(months.items())),
                     "regimes": dict(sorted(regimes.items()))},
        "outcomes": outcomes,
        "segments": segments,
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
