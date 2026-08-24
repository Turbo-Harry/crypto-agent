"""Read-only storage interface used by service and runtime adapters.

SQL and schema knowledge stay in the storage layer.  Callers receive plain
snapshots and cannot mutate tables through this API.
"""

from __future__ import annotations

import json
from typing import Any

import config
from storage import db


def _ready(db_path: str | None) -> None:
    db.init_db(db_path)


def live_pnl_baseline(db_path: str | None) -> float | None:
    _ready(db_path)
    row = db.q1("SELECT value FROM kv WHERE key='live_pnl_start'",
                db_path=db_path)
    if not row:
        return None
    try:
        value = json.loads(row["value"])
        equity = value.get("equity") if isinstance(value, dict) else None
        return float(equity) if equity is not None else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def latest_position_snapshot_ts(db_path: str | None) -> float | None:
    _ready(db_path)
    row = db.q1("SELECT MAX(ts) ts FROM position_snapshots", db_path=db_path)
    return row.get("ts") if row else None


def list_anomalies(db_path: str | None, limit: int = 50) -> list[dict[str, Any]]:
    _ready(db_path)
    return db.q("SELECT id, ts, source, severity, title, detail, status "
                "FROM anomalies ORDER BY ts DESC LIMIT ?",
                [max(1, min(limit, 500))], db_path=db_path)


def agent_status_summary(db_path: str | None) -> dict[str, Any]:
    _ready(db_path)
    rows = db.q("SELECT runtime_status FROM agent_runs", db_path=db_path)
    failed = sum(row["runtime_status"] not in
                 ("completed", "disabled", "no_key") for row in rows)
    versions = db.q("SELECT version,status FROM agent_versions "
                    "ORDER BY created_ts DESC LIMIT 1", db_path=db_path)
    current = versions[0] if versions else {}
    latest_runs = db.q(
        "SELECT r.prompt_version,r.tool_policy_version,r.runtime_status,"
        "r.created_ts,e.lifecycle_status FROM agent_runs r "
        "LEFT JOIN agent_evaluations e ON e.run_id=r.run_id "
        "ORDER BY r.created_ts DESC LIMIT 1", db_path=db_path)
    latest = latest_runs[0] if latest_runs else {}
    return {
        "current_version": current.get("version"),
        "current_status": current.get("status"),
        "lifecycle_version": current.get("version"),
        "lifecycle_status": current.get("status"),
        "configured_prompt_version": config.AGENT_HARNESS_PROMPT_VERSION,
        "configured_tool_policy_version":
            config.AGENT_HARNESS_TOOL_POLICY_VERSION,
        "latest_run_prompt_version": latest.get("prompt_version"),
        "latest_run_tool_policy_version": latest.get("tool_policy_version"),
        "latest_run_runtime_status": latest.get("runtime_status"),
        "latest_run_lifecycle_status": latest.get("lifecycle_status"),
        "latest_run_created_ts": latest.get("created_ts"),
        "total_runs": len(rows),
        "completed_runs": sum(row["runtime_status"] == "completed"
                              for row in rows),
        "failed_runs": failed,
        "failure_rate": round(failed / len(rows), 4) if rows else 0.0,
        "shadow_enabled": bool(config.AGENT_HARNESS_ENABLED),
        "veto_enabled": current.get("status") in
            {"active-veto", "observing", "kept"},
    }


def list_agent_evaluations(db_path: str | None) -> list[dict[str, Any]]:
    _ready(db_path)
    return db.q("SELECT * FROM agent_evaluations", db_path=db_path)


def list_agent_runs(db_path: str | None, limit: int = 50) -> list[dict[str, Any]]:
    from storage.agent_harness import list_runs
    return list_runs(limit=max(1, min(limit, 500)), db_path=db_path)


def list_factor_trials(db_path: str | None, strategy_id: str,
                       limit: int = 50,
                       strategy_version: str | None = None) -> list[dict[str, Any]]:
    _ready(db_path)
    scope_sql = " AND strategy_version=?" if strategy_version else ""
    return db.q(
        "SELECT id,ts,name,strategy_id,strategy_version,status,n_samples,n_folds,"
        "ic_tstat,net_spread,"
        "dsr,pbo,missing_rate,fold_consistency,redundant_with "
        "FROM factor_trials WHERE strategy_id=?" + scope_sql +
        " ORDER BY id DESC LIMIT ?",
        [strategy_id, *([strategy_version] if strategy_version else []),
         max(1, min(200, limit))], db_path=db_path)


def research_outcome_counts(db_path: str | None, strategy_id: str, *,
                            strategy_version: str | None = None
                            ) -> list[dict[str, Any]]:
    """按方向返回当前研究 scope 的成熟路径类别计数。"""
    _ready(db_path)
    scope_sql = " AND s.strategy_version=?" if strategy_version else ""
    return db.q(
        "SELECT s.direction,COUNT(*) n,SUM(o.tp_first) tp,SUM(o.sl_first) sl "
        "FROM signal_outcomes o JOIN signal_samples_canonical s "
        "ON s.signal_id=o.signal_id WHERE s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=?" + scope_sql +
        " GROUP BY s.direction",
        [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS,
         *([strategy_version] if strategy_version else [])],
        db_path=db_path)


def latest_analysis(db_path: str | None) -> dict[str, Any] | None:
    _ready(db_path)
    row = db.q1("SELECT * FROM analyses ORDER BY id DESC LIMIT 1",
                db_path=db_path)
    if not row:
        return None
    return {"ts": row["ts"], "kind": row["kind"],
            "report": json.loads(row["report"]),
            "issues": json.loads(row["issues"])}


def list_risk_events(db_path: str | None,
                     limit: int = 20) -> list[dict[str, Any]]:
    _ready(db_path)
    rows = db.q("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?",
                [max(1, min(limit, 500))], db_path=db_path)
    return list(reversed(rows))
