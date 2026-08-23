"""Read-only storage interface used by service and runtime adapters.

SQL and schema knowledge stay in the storage layer.  Callers receive plain
snapshots and cannot mutate tables through this API.
"""

from __future__ import annotations

import json
from typing import Any

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
    return {
        "current_version": current.get("version"),
        "current_status": current.get("status"),
        "total_runs": len(rows),
        "completed_runs": sum(row["runtime_status"] == "completed"
                              for row in rows),
        "failed_runs": failed,
        "failure_rate": round(failed / len(rows), 4) if rows else 0.0,
        "shadow_enabled": True,
        "veto_enabled": current.get("status") == "active-veto",
    }


def list_agent_evaluations(db_path: str | None) -> list[dict[str, Any]]:
    _ready(db_path)
    return db.q("SELECT * FROM agent_evaluations", db_path=db_path)


def list_agent_runs(db_path: str | None, limit: int = 50) -> list[dict[str, Any]]:
    from storage.agent_harness import list_runs
    return list_runs(limit=max(1, min(limit, 500)), db_path=db_path)


def list_factor_trials(db_path: str | None, strategy_id: str,
                       limit: int = 50) -> list[dict[str, Any]]:
    _ready(db_path)
    return db.q(
        "SELECT id,ts,name,strategy_id,status,n_samples,n_folds,ic_tstat,net_spread,"
        "dsr,pbo,missing_rate,fold_consistency,redundant_with "
        "FROM factor_trials WHERE strategy_id=? ORDER BY id DESC LIMIT ?",
        [strategy_id, max(1, min(200, limit))], db_path=db_path)


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
