"""SQLite persistence for Agent Harness traces and mature outcome labels."""

from __future__ import annotations

import json
import time
from typing import Any

from decision.agent_contracts import (
    AgentInput,
    AgentStep,
    HarnessRun,
    LifecycleStatus,
    stable_hash,
)
from storage import db


def record_run(run: HarnessRun, agent_input: AgentInput | None = None,
               *, created_ts: float | None = None, db_path: str | None = None) -> dict[str, Any]:
    """Insert a run idempotently and return the durable row.

    ``INSERT OR IGNORE`` makes retries safe: a repeated signal/harness pair
    cannot create a second billable or auditable run.
    """

    db.init_db(db_path)
    now = time.time() if created_ts is None else float(created_ts)
    harness_version = "unknown"
    versions: dict[str, Any] = {}
    if agent_input is not None:
        harness_version = stable_hash({
            "prompt": agent_input.prompt_version,
            "model": agent_input.model_version,
            "context": agent_input.context_version,
            "schema": agent_input.schema_version,
            "retrieval": agent_input.retrieval_version,
        })[:16]
        versions = {
            "prompt_version": agent_input.prompt_version,
            "model_version": agent_input.model_version,
            "context_version": agent_input.context_version,
            "schema_version": agent_input.schema_version,
            "retrieval_version": agent_input.retrieval_version,
        }
    idempotency = stable_hash({"signal_id": run.signal_id, "harness_version": harness_version})
    db.x(
        "INSERT OR IGNORE INTO agent_runs ("
        "run_id,signal_id,idempotency_key,created_ts,completed_ts,runtime_status,"
        "final_action,model_verdict,run_role,parent_run_id,prompt_version,model_version,"
        "context_version,schema_version,retrieval_version,input_hash,response_hash,"
        "latency_ms,model_latency_ms,input_tokens,output_tokens,estimated_cost,error_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [run.run_id, run.signal_id, idempotency, now, now,
         run.runtime_status.value, run.final_action.value,
         run.model_verdict.value if run.model_verdict else None, run.run_role.value,
         run.parent_run_id, versions.get("prompt_version"), versions.get("model_version"),
         versions.get("context_version"), versions.get("schema_version"),
         versions.get("retrieval_version"), run.input_hash,
         run.response_hash, run.latency_ms, run.model_latency_ms, run.input_tokens,
         run.output_tokens, run.estimated_cost, run.error_type], db_path=db_path)
    return db.q1("SELECT * FROM agent_runs WHERE idempotency_key=?", [idempotency], db_path=db_path) or {}


def record_step(step: AgentStep, *, db_path: str | None = None) -> None:
    db.init_db(db_path)
    db.x(
        "INSERT OR REPLACE INTO agent_steps (run_id,step_no,step_type,status,"
        "started_ts,finished_ts,tool_name,input_hash,output_hash,evidence_ids,"
        "retry_count,error_type,fallback_action) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [step.run_id, step.step_no, step.step_type.value, step.status.value,
         step.started_at, step.finished_at, step.tool_name, step.input_hash,
         step.output_hash, json.dumps(list(step.evidence_ids), ensure_ascii=False),
         step.retry_count, step.error_type, step.fallback_action], db_path=db_path)


def record_evaluation(run_id: str, *, lifecycle_status: LifecycleStatus = LifecycleStatus.PENDING,
                      label: str | None = None, settle_ts: float | None = None,
                      pnl_r: float | None = None, mfe_r: float | None = None,
                      mae_r: float | None = None, incremental_ev: float | None = None,
                      saved_loss: float | None = None, missed_profit: float | None = None,
                      evaluation_version: str = "eval-v1", label_source: str | None = None,
                      db_path: str | None = None) -> None:
    db.init_db(db_path)
    db.x(
        "INSERT OR REPLACE INTO agent_evaluations (run_id,lifecycle_status,label,"
        "settle_ts,pnl_r,mfe_r,mae_r,incremental_ev,saved_loss,missed_profit,"
        "evaluation_version,label_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [run_id, lifecycle_status.value, label, settle_ts, pnl_r, mfe_r, mae_r,
         incremental_ev, saved_loss, missed_profit, evaluation_version, label_source],
        db_path=db_path)


def get_run(run_id: str, *, db_path: str | None = None) -> dict[str, Any] | None:
    db.init_db(db_path)
    return db.q1("SELECT * FROM agent_runs WHERE run_id=?", [run_id], db_path=db_path)


def list_runs(limit: int = 50, *, db_path: str | None = None) -> list[dict[str, Any]]:
    db.init_db(db_path)
    safe_limit = max(1, min(int(limit), 500))
    return db.q("SELECT * FROM agent_runs ORDER BY created_ts DESC LIMIT ?", [safe_limit], db_path=db_path)
