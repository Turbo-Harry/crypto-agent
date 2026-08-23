"""Evidence-scoped memory storage and retrieval for the Agent Harness."""

from __future__ import annotations

import json
import math
import time
from typing import Any, Mapping

from interfaces.agent import stable_hash
from storage import db


def _evidence_id(memory_type: str, source_id: str) -> str:
    return f"{memory_type}:{stable_hash({'type': memory_type, 'source': source_id})[:24]}"


def upsert_memory(*, memory_type: str, source_id: str, content: str,
                  status: str, created_ts: float | None = None,
                  mature_ts: float | None = None, outcome_pnl: float | None = None,
                  outcome_r: float | None = None,
                  evidence_strength: float = 0.0, run_id: str | None = None,
                  signal_id: str | None = None, strategy_version: str | None = None,
                  base: str | None = None, asset_class: str | None = None,
                  direction: str | None = None, timeframe: str | None = None,
                  regime: str | None = None, metadata: Mapping[str, Any] | None = None,
                  db_path: str | None = None) -> str:
    """Write one memory item; only callers can promote it to a mature status."""

    if memory_type not in {"episodic", "semantic", "procedural"}:
        raise ValueError("unknown memory_type")
    if status not in {"pending", "mature", "trusted", "discarded", "stale"}:
        raise ValueError("unknown memory status")
    evidence_id = _evidence_id(memory_type, source_id)
    ts = time.time() if created_ts is None else float(created_ts)
    db.init_db(db_path)
    db.x(
        "INSERT OR REPLACE INTO agent_memories (evidence_id,memory_type,run_id,signal_id,"
        "strategy_version,base,asset_class,direction,timeframe,regime,status,created_ts,"
        "mature_ts,outcome_pnl,outcome_r,evidence_strength,content,metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [evidence_id, memory_type, run_id, signal_id, strategy_version, base,
         asset_class, direction, timeframe, regime, status, ts, mature_ts,
         outcome_pnl, outcome_r,
         max(0.0, min(float(evidence_strength), 1.0)), str(content)[:2000],
         json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True)],
        db_path=db_path)
    return evidence_id


def promote_mature_legacy_memories(*, db_path: str | None = None,
                                   now_ts: float | None = None,
                                   min_age_hours: float = 24.0) -> int:
    """Import only settled legacy judgments and verified lessons.

    Pending/open judgments are intentionally excluded to prevent future leakage.
    """

    db.init_db(db_path)
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - max(0.0, float(min_age_hours)) * 3600.0
    count = 0
    for row in db.q(
        "SELECT a.id,a.ts,a.base,a.direction,a.verdict,a.reason,a.signal_id,"
        "a.outcome_pnl,a.outcome_r,s.strategy_version,s.timeframe,s.features "
        "FROM ai_judgments a LEFT JOIN signal_samples s "
        "ON s.signal_id=a.signal_id WHERE "
        "(a.outcome_r IS NOT NULL OR a.outcome_pnl IS NOT NULL) AND a.ts <= ?",
        [cutoff], db_path=db_path):
        evidence_id = _evidence_id("episodic", f"ai_judgment:{row['id']}")
        existing = db.q1("SELECT status FROM agent_memories WHERE evidence_id=?",
                          [evidence_id], db_path=db_path)
        if existing:
            continue
        has_r = row.get("outcome_r") is not None
        outcome = (float(row["outcome_r"]) if has_r else
                   float(row.get("outcome_pnl") or 0.0))
        try:
            snapshot = json.loads(row.get("features") or "{}")
            raw_regime = snapshot.get("regime")
            regime = (raw_regime.get("tag") if isinstance(raw_regime, dict)
                      else raw_regime)
        except (TypeError, ValueError, json.JSONDecodeError):
            regime = None
        strength = min(1.0, 0.5 + min(abs(outcome), 0.5))
        unit = "R" if has_r else "legacy_pnl"
        upsert_memory(
            memory_type="episodic", source_id=f"ai_judgment:{row['id']}",
            content=(f"{row.get('base')} {row.get('direction')} verdict={row.get('verdict')} "
                     f"outcome_{unit}={outcome:+.6f}; {row.get('reason') or ''}"),
            status="mature", created_ts=row.get("ts") or now, mature_ts=now,
            outcome_pnl=None if has_r else outcome, outcome_r=outcome if has_r else None,
            evidence_strength=strength, signal_id=row.get("signal_id"),
            strategy_version=row.get("strategy_version"), base=row.get("base"),
            direction=row.get("direction"), timeframe=row.get("timeframe"),
            regime=regime, metadata={"outcome_unit": unit}, db_path=db_path)
        count += 1
    for row in db.q(
        "SELECT id,ts,symbol,status,content,good,bad,regime,conditions FROM lessons "
        "WHERE status IN ('trusted','discarded')", db_path=db_path):
        evidence_id = _evidence_id("semantic", f"lesson:{row['id']}")
        existing = db.q1("SELECT status FROM agent_memories WHERE evidence_id=?",
                          [evidence_id], db_path=db_path)
        if existing and existing.get("status") == "stale":
            continue
        strength = min(1.0, max(0.0, (int(row.get("good") or 0) + int(row.get("bad") or 0)) / 20.0))
        upsert_memory(
            memory_type="semantic", source_id=f"lesson:{row['id']}",
            content=str(row.get("content") or ""), status=str(row.get("status")),
            created_ts=row.get("ts") or now, mature_ts=now,
            evidence_strength=strength, base=row.get("symbol"), regime=row.get("regime"),
            metadata={"good": row.get("good"), "bad": row.get("bad"),
                      "conditions": row.get("conditions")}, db_path=db_path)
        count += 1
    return count


def promote_mature_harness_memories(*, db_path: str | None = None,
                                    now_ts: float | None = None,
                                    min_age_hours: float = 24.0) -> int:
    """把已成熟 Harness 路径评价转为有 scope 的 episodic memory。

    评价成熟只说明 4h 路径已经可见；额外年龄门避免刚结算结果立即回喂同一
    市场状态。已存在或 stale 的证据都不被重复覆盖/复活。
    """
    db.init_db(db_path)
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - max(0.0, float(min_age_hours)) * 3600.0
    rows = db.q(
        "SELECT r.run_id,r.signal_id,r.created_ts,r.final_action,r.model_verdict,"
        "e.label,e.settle_ts,e.pnl_r,e.mfe_r,e.mae_r,s.symbol base,s.direction,"
        "s.strategy_version,s.timeframe,s.features FROM agent_runs r "
        "JOIN agent_evaluations e ON e.run_id=r.run_id "
        "LEFT JOIN signal_samples s ON s.signal_id=r.signal_id "
        "WHERE e.lifecycle_status='mature' AND e.pnl_r IS NOT NULL "
        "AND r.created_ts<=? ORDER BY r.created_ts",
        [cutoff], db_path=db_path)
    count = 0
    for row in rows:
        source_id = f"agent_run:{row['run_id']}"
        evidence_id = _evidence_id("episodic", source_id)
        if db.q1("SELECT status FROM agent_memories WHERE evidence_id=?",
                 [evidence_id], db_path=db_path):
            continue
        try:
            snapshot = json.loads(row.get("features") or "{}")
            raw_regime = snapshot.get("regime")
            regime = (raw_regime.get("tag") if isinstance(raw_regime, dict)
                      else raw_regime)
        except (TypeError, ValueError, json.JSONDecodeError):
            regime = None
        outcome = float(row.get("pnl_r") or 0.0)
        upsert_memory(
            memory_type="episodic", source_id=source_id,
            content=(f"{row.get('base')} {row.get('direction')} "
                     f"action={row.get('final_action')} label={row.get('label')} "
                     f"outcome_R={outcome:+.6f}"),
            status="mature", created_ts=row.get("created_ts") or now,
            mature_ts=row.get("settle_ts") or now, outcome_r=outcome,
            evidence_strength=min(1.0, 0.5 + min(abs(outcome), 0.5)),
            run_id=row.get("run_id"), signal_id=row.get("signal_id"),
            strategy_version=row.get("strategy_version"), base=row.get("base"),
            direction=row.get("direction"), timeframe=row.get("timeframe"),
            regime=regime, metadata={"outcome_unit": "R",
                                     "model_verdict": row.get("model_verdict")},
            db_path=db_path)
        count += 1
    return count


def decay_memories(*, episodic_ttl_days: float, semantic_ttl_days: float,
                   min_strength: float, now_ts: float | None = None,
                   db_path: str | None = None) -> int:
    """Demote old/weak memories without deleting audit evidence.

    ``stale`` rows remain queryable for audit and can be re-promoted only by a
    new independently settled observation; retrieval excludes them.
    """

    db.init_db(db_path)
    now = time.time() if now_ts is None else float(now_ts)
    changed = 0
    rows = db.q("SELECT evidence_id,memory_type,status,created_ts,evidence_strength,metadata_json "
                "FROM agent_memories WHERE status IN ('mature','trusted','discarded')",
                db_path=db_path)
    for row in rows:
        ttl = float(episodic_ttl_days) if row["memory_type"] == "episodic" else float(semantic_ttl_days)
        age_days = max(0.0, (now - float(row.get("created_ts") or now)) / 86400.0)
        weak = float(row.get("evidence_strength") or 0.0) < float(min_strength)
        if age_days <= ttl and not weak:
            continue
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        metadata.update({"decay_reason": "ttl" if age_days > ttl else "weak_evidence",
                         "decayed_at": now, "age_days": round(age_days, 3)})
        db.x("UPDATE agent_memories SET status='stale', metadata_json=? WHERE evidence_id=?",
             [json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["evidence_id"]],
             db_path=db_path)
        changed += 1
    return changed


def procedural_policies() -> list[dict[str, Any]]:
    """Immutable policy memory; never sourced from model output or DB writes."""

    policies = (
        ("hard-risk-gate", "Rules and risk gates always outrank model output."),
        ("no-execution-tools", "Agent tools are read-only; no order, cancel, leverage or config mutation."),
        ("fail-closed-schema", "Malformed reject output is invalid and cannot veto or alter a trade."),
    )
    return [{"evidence_id": _evidence_id("procedural", key), "memory_type": "procedural",
             "status": "trusted", "content": content, "evidence_strength": 1.0}
            for key, content in policies]


def retrieve(query: Mapping[str, Any], *, limit: int = 5,
             db_path: str | None = None) -> list[dict[str, Any]]:
    """Retrieve mature evidence with hard filters and diversity-aware ranking."""

    db.init_db(db_path)
    now = float(query.get("as_of_ts") or time.time())
    strategy = query.get("strategy_version")
    direction = query.get("direction")
    timeframe = query.get("timeframe")
    regime = query.get("regime")
    base = query.get("base")
    rows = db.q(
        "SELECT * FROM agent_memories WHERE status IN ('mature','trusted','discarded') "
        "AND (mature_ts IS NULL OR mature_ts <= ?)", [now], db_path=db_path)
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        # Procedural rules are always available and have no market filter.
        if row["memory_type"] != "procedural":
            if strategy and row.get("strategy_version") not in (None, strategy):
                continue
            if direction and row.get("direction") not in (None, direction):
                continue
            if timeframe and row.get("timeframe") not in (None, timeframe):
                continue
            if regime and row.get("regime") not in (None, regime):
                continue
        age_days = max(0.0, (now - float(row.get("created_ts") or now)) / 86400.0)
        score = float(row.get("evidence_strength") or 0.0) * math.exp(-age_days / 30.0)
        for field, wanted, bonus in (("base", base, 0.30), ("direction", direction, 0.15),
                                     ("timeframe", timeframe, 0.10), ("regime", regime, 0.15)):
            if wanted and row.get(field) == wanted:
                score += bonus
        candidates.append((score, row))
    candidates.sort(key=lambda item: (-item[0], item[1]["evidence_id"]))
    chosen: list[dict[str, Any]] = []
    groups: set[tuple[Any, ...]] = set()
    max_items = max(0, min(int(limit), 50))
    for score, row in candidates:
        group = (row.get("base"), row.get("direction"), row.get("regime"))
        if row["memory_type"] != "procedural" and group in groups:
            continue
        groups.add(group)
        item = dict(row)
        item["retrieval_score"] = round(score, 6)
        chosen.append(item)
        if len(chosen) >= max_items:
            break
    if len(chosen) < max_items:
        for item in procedural_policies():
            if not any(x["evidence_id"] == item["evidence_id"] for x in chosen):
                chosen.append(item)
                if len(chosen) >= max_items:
                    break
    return chosen
