"""15m 开仓准确率计划的只读统计完成度审计。

它与 ``decision/readiness.py`` 的“是否可上实盘”三盏灯刻意分离：这里逐项回答
自然模拟盘、六维特征、候选类别、因子、概率/极值模型、校准、Agent 增量和
长期 EV 预算锁是否满足权威实施计划。默认只报告；``--require-complete`` 可供
自动化在任一统计门未满足时返回非零。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import config
from decision.signal_identity import config_identity, research_scope_version


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在: {path}")
    # 活库若有 WAL/SHM 必须走普通 ro 才能看到最新提交；封存 backup 没有
    # sidecar 时加 immutable，避免 WAL 标记让 SQLite 尝试创建共享内存。
    has_sidecar = Path(str(path) + "-wal").exists() or \
        Path(str(path) + "-shm").exists()
    query = "?mode=ro" if has_sidecar else "?mode=ro&immutable=1"
    conn = sqlite3.connect(path.as_uri() + query, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def _count(conn: sqlite3.Connection, sql: str, params=()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _json_dict(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _complete_six_dims(raw: Any) -> bool:
    dims = _json_dict(raw)
    return all(name in dims and dims[name] is not None for name in config.SHADOW_DIMS)


def _gate(passed: bool, actual: Any, required: Any, reason: str) -> dict[str, Any]:
    return {"passed": bool(passed), "actual": actual,
            "required": required, "reason": reason}


def _configured_harness_version(strategy_id: str) -> str:
    """Return the exact configured Harness identity whose gate is reported."""
    from decision.agent_lifecycle import configured_version
    return configured_version(strategy_id)


def audit_status(db_path: str | None = None,
                 strategy_id: str | None = None) -> dict[str, Any]:
    """返回统计完成度；只打开 SQLite ``mode=ro``，不迁移、不写 KV。"""
    if db_path is None:
        from storage.db import DB_PATH
        db_path = DB_PATH
    timeframe = config.SIGNAL_SAMPLE_TIMEFRAME
    horizon = config.SIGNAL_OUTCOME_HORIZON_HOURS
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    allowed_strategies = tuple(config.ENTRY_ACCURACY_RESEARCH_STRATEGIES)
    if strategy_id not in allowed_strategies:
        raise ValueError(
            f"未知 strategy_id={strategy_id}; allowed={allowed_strategies}")
    # 与采样器使用同一个配置身份算法；B 不能在观测结果里伪装成 A 的
    # pullback 版本。这里只计算哈希，不访问交易所、不写库。
    strategy_version = config_identity(strategy_id)[0]
    research_version = research_scope_version(strategy_id)
    harness_version = _configured_harness_version(strategy_id)
    plain_scope_sql = " AND strategy_version=?" if research_version else ""
    joined_scope_sql = " AND s.strategy_version=?" if research_version else ""
    harness_eligibility_sql = (
        " AND s.rule_decision='pass'"
        if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID else
        " AND s.rule_decision IN ('pass','shadow')"
    ) + " AND (s.reject_reason IS NULL OR s.reject_reason NOT LIKE 'ai_reject:%')"
    conn = _connect_read_only(db_path)
    try:
        scope = [strategy_id, timeframe, horizon,
                 *([research_version] if research_version else [])]
        raw_candidates = _count(
            conn, "SELECT COUNT(*) FROM signal_samples WHERE strategy_id=? "
            "AND timeframe=? AND horizon_hours=?" + plain_scope_sql, scope)
        candidates = _count(
            conn, "SELECT COUNT(*) FROM signal_samples WHERE strategy_id=? "
            "AND timeframe=? AND horizon_hours=?" + plain_scope_sql, scope)
        duplicate_version_snapshots = max(0, raw_candidates - candidates)
        outcomes = _count(
            conn, "SELECT COUNT(*) FROM signal_outcomes o "
            "JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql,
            scope)
        tp = _count(
            conn, "SELECT COUNT(*) FROM signal_outcomes o "
            "JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql +
            " AND o.tp_first=1", scope)
        sl = _count(
            conn, "SELECT COUNT(*) FROM signal_outcomes o "
            "JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql +
            " AND o.sl_first=1", scope)
        timeout = _count(
            conn, "SELECT COUNT(*) FROM signal_outcomes o "
            "JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql +
            " AND o.timeout=1", scope)
        six_dim_outcomes = _count(
            conn, "SELECT COUNT(*) FROM signal_outcomes o "
            "JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql + " "
            "AND s.wick IS NOT NULL AND s.depth IS NOT NULL "
            "AND s.trend IS NOT NULL AND s.volume IS NOT NULL "
            "AND s.funding IS NOT NULL AND s.book IS NOT NULL", scope)
        direction_rows = _rows(
            conn, "SELECT s.direction,COUNT(*) n,SUM(o.tp_first) tp_first,"
            "SUM(o.sl_first) sl_first,SUM(o.timeout) timeout "
            "FROM signal_outcomes o JOIN signal_samples s "
            "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql +
            " GROUP BY s.direction", scope)
        directions = {
            direction: {"n": 0, "tp_first": 0, "sl_first": 0, "timeout": 0}
            for direction in ("long", "short")}
        for row in direction_rows:
            direction = str(row.get("direction") or "")
            if direction in directions:
                directions[direction] = {
                    "n": int(row.get("n") or 0),
                    "tp_first": int(row.get("tp_first") or 0),
                    "sl_first": int(row.get("sl_first") or 0),
                    "timeout": int(row.get("timeout") or 0),
                }

        closed_rows = _rows(
            conn, "SELECT id,shadow_dims FROM trades WHERE status='closed' "
            "AND strategy_id=? AND strategy_timeframe=? AND max_hold_hours=?",
            [strategy_id, timeframe, horizon])
        paper_closed = len(closed_rows)
        paper_six_dim_closed = sum(
            _complete_six_dims(row.get("shadow_dims")) for row in closed_rows)
        calibration_n = _count(
            conn, "SELECT COUNT(*) FROM forecast_calibration f "
            "JOIN signal_samples s ON s.signal_id=f.signal_id "
            "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=?" +
            joined_scope_sql, scope)
        validated_factors = _count(
            conn, "SELECT COUNT(*) FROM factor_trials WHERE strategy_id=? "
            "AND timeframe=? AND horizon_hours=?" + plain_scope_sql +
            " AND status='validated'", scope)

        models = _rows(
            conn, "SELECT model_id,model_type,direction,state,metrics "
            "FROM model_artifacts WHERE strategy_id=?" + plain_scope_sql +
            " ORDER BY created_at DESC",
            [strategy_id, *([research_version] if research_version else [])])
        model_states = Counter(
            f"{row['model_type']}:{row['state']}" for row in models)
        entry_kept = [row for row in models
                      if row["model_type"] == "entry_probability"
                      and row["state"] == "kept"]
        # 极值模型按安全配置永久只展示；accepted 已表示通过训练截止点之后的
        # 独立 pinball/coverage shadow 门。不能为了让审计变绿而授予 active 权限。
        extrema_observed_states = (("accepted", "kept")
                                   if config.EXTREMA_MODEL_SHADOW_ONLY
                                   else ("kept",))
        extrema_observed = [row for row in models
                            if row["model_type"] == "extrema" and
                            row["state"] in extrema_observed_states]
        active_entry = next((row for row in models
                             if row["model_type"] == "entry_probability"
                             and row["state"] in ("active", "observing", "kept")), None)
        active_metrics = _json_dict(active_entry.get("metrics")) if active_entry else {}
        long_term_ev = active_metrics.get("long_term_backtest_ev_r")
        budget_allowed = bool(
            active_entry and long_term_ev is not None and
            float(long_term_ev) > config.MODEL_BUDGET_EXPANSION_MIN_LONG_TERM_EV_R)
        budget_lock_safe = (budget_allowed if long_term_ev is not None and
                            float(long_term_ev) >
                            config.MODEL_BUDGET_EXPANSION_MIN_LONG_TERM_EV_R
                            else not budget_allowed)

        legacy_rows = _rows(
            conn, "SELECT a.signal_id,a.verdict FROM ai_judgments a "
            "JOIN signal_samples s ON s.signal_id=a.signal_id "
            "WHERE a.call_status='valid' AND a.outcome_r IS NOT NULL "
            "AND s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=?" +
            joined_scope_sql, scope)
        harness_rows = _rows(
            conn, "SELECT r.signal_id,r.final_action FROM agent_evaluations e "
            "JOIN agent_runs r ON r.run_id=e.run_id "
            "JOIN signal_samples s ON s.signal_id=r.signal_id "
            "WHERE e.lifecycle_status='mature' AND r.runtime_status='completed' "
            "AND r.model_verdict IS NOT NULL AND s.strategy_id=? "
            "AND s.timeframe=? AND s.horizon_hours=?" + joined_scope_sql +
            harness_eligibility_sql, scope)
        harness_all_signal_ids = {row["signal_id"] for row in harness_rows}
        harness_all_reject_ids = {
            row["signal_id"] for row in harness_rows
            if row.get("final_action") in ("shadow_reject", "agent_reject")}

        agent_version = conn.execute(
            "SELECT version,status,metrics_json FROM agent_versions "
            "WHERE strategy_id=? AND version=? LIMIT 1",
            [strategy_id, harness_version]).fetchone()
        agent_version_dict = dict(agent_version) if agent_version else None
        agent_metrics = _json_dict(
            agent_version_dict.get("metrics_json")) if agent_version_dict else {}
        # Harness 的统计门必须按一个完整版本身份计数。legacy AI 与旧
        # prompt/context/schema 版本只作旁路诊断，不能拼成 100/30 晋升样本。
        agent_valid = int(agent_metrics.get("n", 0))
        agent_reject = int(agent_metrics.get("reject_n", 0))
        agent_state_proven = bool(
            agent_version_dict and
            agent_version_dict["status"] in
            ("validated", "active-veto", "observing", "kept") and
            int(agent_metrics.get("n", 0)) >= config.AGENT_EVAL_MIN_VALID and
            int(agent_metrics.get("reject_n", 0)) >= config.AGENT_EVAL_MIN_REJECT and
            float(agent_metrics.get("incremental_ev_lower_bound") or 0) > 0 and
            float(agent_metrics.get("max_segment_share", 1.0) or 1.0) <=
            config.AGENT_EVAL_MAX_SEGMENT_SHARE and
            float(agent_metrics.get("max_direction_share", 1.0) or 1.0) <=
            config.AGENT_EVAL_MAX_SEGMENT_SHARE and
            bool(agent_metrics.get("model_cost_data_complete", False)) and
            float(agent_metrics.get("trace_coverage", 0)) >= 1.0 and
            float(agent_metrics.get("probability_coverage", 0)) >= 1.0 and
            float(agent_metrics.get("reject_evidence_coverage", 0) or 0) >= 1.0 and
            float(agent_metrics.get("brier_skill")
                  if agent_metrics.get("brier_skill") is not None else -1) >= 0 and
            float(agent_metrics.get("probability_std", 0) or 0) >=
            config.AGENT_HARNESS_MIN_PROBABILITY_STD and
            float(agent_metrics.get("saved_loss", 0)) >
            float(agent_metrics.get("missed_profit", 0)) +
            float(agent_metrics.get("model_cost_r", 0)))

        gates = {
            "paper_closed": _gate(
                paper_closed >= config.ENTRY_ACCURACY_MIN_PAPER_CLOSED,
                paper_closed, config.ENTRY_ACCURACY_MIN_PAPER_CLOSED,
                "15m/4h 自然模拟盘已平仓"),
            "paper_six_dim_closed": _gate(
                paper_six_dim_closed >= config.ENTRY_ACCURACY_MIN_SIX_DIM_CLOSED,
                paper_six_dim_closed, config.ENTRY_ACCURACY_MIN_SIX_DIM_CLOSED,
                "自然平仓且六维子分全部可用"),
            "candidate_training_sample": _gate(
                candidates >= config.ENTRY_MODEL_MIN_SAMPLES,
                candidates, config.ENTRY_MODEL_MIN_SAMPLES,
                "当前 15m/4h 去重候选"),
            "tp_class_sample": _gate(tp >= config.ENTRY_MODEL_MIN_TP, tp,
                                      config.ENTRY_MODEL_MIN_TP, "TP first 类别"),
            "sl_class_sample": _gate(sl >= config.ENTRY_MODEL_MIN_SL, sl,
                                      config.ENTRY_MODEL_MIN_SL, "SL first 类别"),
            "validated_factor": _gate(validated_factors > 0, validated_factors, 1,
                                        "至少一个因子通过完整样本外验证门"),
            "forecast_calibration": _gate(
                calibration_n >= config.FORECAST_MIN_CALIBRATION,
                calibration_n, config.FORECAST_MIN_CALIBRATION,
                "当前 scope 的真实路径校准样本"),
            "entry_model_observed": _gate(bool(entry_kept), len(entry_kept), 1,
                                           "入场模型完成独立影子观察且 kept"),
            "extrema_model_observed": _gate(
                bool(extrema_observed), len(extrema_observed), 1,
                "极值模型完成独立影子观察；shadow-only 时 accepted 即通过"),
            "agent_sample": _gate(agent_valid >= config.AGENT_EVAL_MIN_VALID,
                                   agent_valid, config.AGENT_EVAL_MIN_VALID,
                                   "当前完整 Harness 版本的自然成熟判断"),
            "agent_reject_sample": _gate(
                agent_reject >= config.AGENT_EVAL_MIN_REJECT,
                agent_reject, config.AGENT_EVAL_MIN_REJECT,
                "当前完整 Harness 版本的 reject 反事实样本"),
            "agent_incremental_proven": _gate(
                agent_state_proven,
                agent_version_dict["status"] if agent_version_dict else None,
                "validated/active-veto/observing/kept + 增量下界>0 + 非单段主导",
                "Agent 版本生命周期的增量证明"),
            "budget_lock_safe": _gate(
                budget_lock_safe, budget_allowed,
                "长期 EV 未转正时必须为 false",
                "预算扩大锁与长期样本外 EV 一致"),
        }
        blockers = [f"{name}: {gate['reason']} "
                    f"({gate['actual']}/{gate['required']})"
                    for name, gate in gates.items() if not gate["passed"]]
        return {
            "generated_ts": round(time.time(), 3),
            "db_path": str(Path(db_path).expanduser().resolve()),
            "scope": {"timeframe": timeframe, "horizon_hours": horizon,
                      "strategy_id": strategy_id,
                      "strategy_version": strategy_version,
                      "harness_version": harness_version},
            "counts": {
                "raw_candidate_snapshots": raw_candidates,
                "duplicate_version_snapshots": duplicate_version_snapshots,
                "paper_closed": paper_closed,
                "paper_six_dim_closed": paper_six_dim_closed,
                "candidates": candidates, "outcomes": outcomes,
                "six_dim_outcomes": six_dim_outcomes,
                "tp_first": tp, "sl_first": sl, "timeout": timeout,
                "directions": directions,
                "forecast_calibration": calibration_n,
                "validated_factors": validated_factors,
                "legacy_agent_valid": len(legacy_rows),
                "harness_agent_mature_valid": len(harness_rows),
                "harness_all_version_distinct_signals":
                    len(harness_all_signal_ids),
                "harness_all_version_reject_distinct_signals":
                    len(harness_all_reject_ids),
                "agent_valid_distinct_signals": agent_valid,
                "agent_reject_distinct_signals": agent_reject,
            },
            "model_states": dict(sorted(model_states.items())),
            "agent_version": agent_version_dict,
            "budget": {"expansion_allowed": budget_allowed,
                       "long_term_backtest_ev_r": long_term_ev},
            "gates": gates,
            "blockers": blockers,
            "statistically_complete": all(gate["passed"] for gate in gates.values()),
            "research_only_samples_do_not_count_as_paper": True,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="要审计的运行/研究 SQLite 库")
    parser.add_argument(
        "--strategy", default=config.ENTRY_SIGNAL_STRATEGY_ID,
        choices=config.ENTRY_ACCURACY_RESEARCH_STRATEGIES,
        help="要审计的策略证据域")
    parser.add_argument("--require-complete", action="store_true",
                        help="统计门未全部通过时返回退出码 2")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = audit_status(args.db, strategy_id=args.strategy)
    print(json.dumps(result, ensure_ascii=False,
                     indent=None if args.compact else 2, sort_keys=True))
    return 2 if args.require_complete and not result["statistically_complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
