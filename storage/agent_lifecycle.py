"""Persistent version lifecycle for champion/challenger Harness policies."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

import config
from storage import db


TRANSITIONS = {
    "candidate": {"shadow", "rolled-back"},
    "shadow": {"validated", "rolled-back"},
    "validated": {"active-veto", "rolled-back"},
    "active-veto": {"observing", "rolled-back"},
    "observing": {"kept", "rolled-back"},
    "kept": set(),
    "rolled-back": set(),
}


def register(version: str, *, role: str = "challenger", parent_version: str | None = None,
             strategy_id: str | None = None,
             db_path: str | None = None) -> dict[str, Any]:
    if not version or role not in {"champion", "challenger"}:
        raise ValueError("invalid agent version")
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    db.init_db(db_path)
    db.x("INSERT OR IGNORE INTO agent_versions "
         "(version,strategy_id,role,status,parent_version,created_ts) "
         "VALUES (?,?,?,?,?,?)",
         [version, strategy_id, role, "candidate", parent_version, time.time()],
         db_path=db_path)
    return get(version, strategy_id=strategy_id, db_path=db_path) or {}


def get(version: str, *, strategy_id: str | None = None,
        db_path: str | None = None) -> dict[str, Any] | None:
    db.init_db(db_path)
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    return db.q1("SELECT * FROM agent_versions WHERE version=? AND strategy_id=?",
                 [version, strategy_id], db_path=db_path)


def transition(version: str, target: str, *, reason: str = "",
               metrics: Mapping[str, Any] | None = None,
               strategy_id: str | None = None,
               db_path: str | None = None) -> dict[str, Any]:
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    row = get(version, strategy_id=strategy_id, db_path=db_path)
    if not row:
        raise ValueError("unknown agent version")
    if target not in TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"illegal transition {row['status']} -> {target}")
    now = time.time()
    activated = now if target in {"active-veto", "observing", "kept"} else row.get("activated_ts")
    rolled = now if target == "rolled-back" else row.get("rollback_ts")
    # 激活/观察等纯状态迁移没有新指标时必须保留验证证据；清空 metrics_json
    # 会让后续审计无法证明这个版本为何获准进入 active-veto。
    metrics_json = (row.get("metrics_json") if metrics is None else
                    json.dumps(dict(metrics), sort_keys=True))
    db.x("UPDATE agent_versions SET status=?, activated_ts=?, rollback_ts=?, metrics_json=?, reason=? "
         "WHERE version=? AND strategy_id=?",
         [target, activated, rolled, metrics_json, reason, version, strategy_id],
         db_path=db_path)
    return get(version, strategy_id=strategy_id, db_path=db_path) or {}


def promotion_ready(metrics: Mapping[str, Any]) -> tuple[bool, str]:
    required = (("n", config.AGENT_EVAL_MIN_VALID),
                ("reject_n", config.AGENT_EVAL_MIN_REJECT))
    for key, minimum in required:
        if int(metrics.get(key, 0)) < minimum:
            return False, f"{key}<{minimum}"
    if float(metrics.get("incremental_ev_lower_bound", metrics.get("incremental_ev", 0))) <= 0:
        return False, "incremental_ev_lower_bound<=0"
    if float(metrics.get("max_segment_share", 1.0)) > 0.8:
        return False, "single_segment_dominates"
    return True, "sample_gate_passed"


def rollback_needed(metrics: Mapping[str, Any]) -> tuple[bool, str]:
    if float(metrics.get("incremental_ev", 0)) < 0:
        return True, "incremental_ev_negative"
    if float(metrics.get("missed_profit", 0)) > float(metrics.get("saved_loss", 0)):
        return True, "missed_profit_exceeds_saved_loss"
    if float(metrics.get("max_segment_share", 0)) > 0.8:
        return True, "segment_concentration"
    return False, "within_guardrails"
