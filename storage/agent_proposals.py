"""Persistence boundary for paper-only Agent proposal run audits."""

from __future__ import annotations

import json
from typing import Any, Mapping

from storage import db


_AUDIT_PREFIX = "agent_proposal_audit:"


def begin_run(run: Mapping[str, Any], audit: Mapping[str, Any], *,
              db_path: str | None = None) -> None:
    """Atomically create the idempotent run row and its frozen input audit."""
    db.init_db(db_path)
    key = _AUDIT_PREFIX + str(run["run_id"])
    with db.tx(db_path=db_path) as conn:
        conn.execute(
            "INSERT INTO agent_proposal_runs (run_id,cycle_key,created_ts,kline_ts,"
            "timeframe,runtime_status,prompt_version,model_version,schema_version,"
            "input_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [run["run_id"], run["cycle_key"], run["created_ts"],
             run["kline_ts"], run["timeframe"], run["runtime_status"],
             run["prompt_version"], run["model_version"],
             run["schema_version"], run["input_hash"]])
        conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            [key, json.dumps(dict(audit), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")),
             run["created_ts"]])


def finish_run(run_id: str, *, runtime_status: str,
               response_hash: str | None, proposal_count: int,
               valid_count: int, latency_ms: int,
               error_type: str | None, abstain_reason: str | None,
               finished_ts: float, db_path: str | None = None) -> dict[str, Any]:
    """Finish the durable run and append the parsed output audit atomically."""
    db.init_db(db_path)
    key = _AUDIT_PREFIX + str(run_id)
    with db.tx(db_path=db_path) as conn:
        conn.execute(
            "UPDATE agent_proposal_runs SET runtime_status=?,response_hash=?,"
            "proposal_count=?,valid_count=?,latency_ms=?,error_type=? WHERE run_id=?",
            [runtime_status, response_hash, int(proposal_count), int(valid_count),
             int(latency_ms), error_type, run_id])
        row = conn.execute("SELECT value FROM kv WHERE key=?", [key]).fetchone()
        try:
            audit = json.loads(row["value"]) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            audit = {}
        audit["output"] = {
            "runtime_status": runtime_status,
            "response_hash": response_hash,
            "proposal_count": int(proposal_count),
            "valid_count": int(valid_count),
            "abstain_reason": abstain_reason,
            "error_type": error_type,
            "latency_ms": int(latency_ms),
            "finished_ts": float(finished_ts),
        }
        conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            [key, json.dumps(audit, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")), float(finished_ts)])
    return db.q1("SELECT * FROM agent_proposal_runs WHERE run_id=?",
                 [run_id], db_path=db_path) or {}


def read_run_audits(run_ids: list[str], *,
                    db_path: str | None = None) -> dict[str, dict[str, Any]]:
    """Return parsed frozen audits keyed by proposal run id."""
    if not run_ids:
        return {}
    db.init_db(db_path)
    keys = [_AUDIT_PREFIX + str(run_id) for run_id in run_ids]
    rows = db.q(
        f"SELECT key,value FROM kv WHERE key IN ({','.join('?' for _ in keys)})",
        keys, db_path=db_path)
    audits = {}
    for row in rows:
        try:
            value = json.loads(row.get("value") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {"audit_error": "invalid_json"}
        audits[str(row["key"])[len(_AUDIT_PREFIX):]] = value
    return audits


def protocol_summary(implementation_version: str, *,
                     db_path: str | None = None) -> dict[str, Any]:
    """Summarize only runs carrying the exact audited implementation identity."""
    db.init_db(db_path)
    all_runs = db.q(
        "SELECT run_id,runtime_status,proposal_count,valid_count "
        "FROM agent_proposal_runs", db_path=db_path)
    audits = read_run_audits(
        [str(row["run_id"]) for row in all_runs], db_path=db_path)
    current = [row for row in all_runs
               if (audits.get(str(row["run_id"])) or {}).get(
                   "implementation_version") == implementation_version]
    run_ids = [str(row["run_id"]) for row in current]
    proposal_count = mature_count = 0
    if run_ids:
        marks = ",".join("?" for _ in run_ids)
        proposal_count = db.q1(
            f"SELECT COUNT(*) n FROM agent_proposals WHERE run_id IN ({marks})",
            run_ids, db_path=db_path)["n"]
        mature_count = db.q1(
            "SELECT COUNT(*) n FROM agent_proposals p JOIN signal_outcomes o "
            f"ON o.signal_id=p.signal_id WHERE p.run_id IN ({marks})",
            run_ids, db_path=db_path)["n"]
    completed = [row for row in current
                 if row.get("runtime_status") == "completed"]
    abstain_count = sum(int(row.get("proposal_count") or 0) == 0
                        for row in completed)
    micro_present = sum(int((audits.get(str(row["run_id"])) or {}).get(
        "microstructure_present") or 0) for row in current)
    micro_total = sum(int((audits.get(str(row["run_id"])) or {}).get(
        "microstructure_total") or 0) for row in current)
    return {
        "audits": audits,
        "current_run_ids": run_ids,
        "auditable_run_count": len(audits),
        "current_protocol_run_count": len(current),
        "current_protocol_completed_count": len(completed),
        "current_protocol_abstain_count": abstain_count,
        "current_protocol_proposal_count": proposal_count,
        "current_protocol_mature_count": mature_count,
        "current_protocol_proposal_coverage": (
            round((len(completed) - abstain_count) / len(completed), 6)
            if completed else 0.0),
        "current_protocol_microstructure_coverage": (
            round(micro_present / micro_total, 6) if micro_total else 0.0),
    }
