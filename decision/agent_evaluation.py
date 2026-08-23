"""Leakage-safe path evaluation and champion/challenger metrics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
from typing import Iterable, Mapping, Sequence

import config


@dataclass(frozen=True)
class PathOutcome:
    label: str                         # tp_first / sl_first / timeout / ambiguous
    pnl_r: float
    mfe_r: float
    mae_r: float
    tp_first: bool = False
    sl_first: bool = False
    timeout: bool = False
    ambiguous: bool = False


@dataclass(frozen=True)
class EvaluationMetrics:
    n: int
    reject_n: int
    saved_loss: float
    missed_profit: float
    model_cost: float
    incremental_ev: float
    brier: float | None
    max_segment_share: float


def evaluate_path(*, entry: float, stop: float, target: float, direction: str,
                  path: Sequence[tuple[float, float]], horizon_ts: float | None = None) -> PathOutcome:
    """Evaluate first-touch using only prices at or after the decision event."""

    if entry <= 0 or stop <= 0 or target <= 0 or direction not in ("long", "short"):
        raise ValueError("invalid path inputs")
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("stop must differ from entry")
    points = [(float(ts), float(px)) for ts, px in path
              if float(ts) >= 0 and (horizon_ts is None or float(ts) <= horizon_ts)]
    if not points:
        return PathOutcome("timeout", 0.0, 0.0, 0.0, timeout=True)
    signed = (lambda px: (px - entry) / risk) if direction == "long" else (lambda px: (entry - px) / risk)
    tp_level = target if direction == "long" else target
    stop_level = stop
    mfe = max(0.0, max(signed(px) for _, px in points))
    mae = min(0.0, min(signed(px) for _, px in points))
    first: str | None = None
    for _, px in points:
        hit_tp = px >= tp_level if direction == "long" else px <= tp_level
        hit_sl = px <= stop_level if direction == "long" else px >= stop_level
        if hit_tp and hit_sl:
            return PathOutcome("ambiguous", 0.0, mfe, mae, ambiguous=True)
        if hit_tp:
            first = "tp_first"
            break
        if hit_sl:
            first = "sl_first"
            break
    if first == "tp_first":
        return PathOutcome(first, signed(target), mfe, mae, tp_first=True)
    if first == "sl_first":
        return PathOutcome(first, signed(stop), mfe, mae, sl_first=True)
    return PathOutcome("timeout", signed(points[-1][1]), mfe, mae, timeout=True)


def incremental_ev(*, saved_loss: float, missed_profit: float, model_cost: float = 0.0) -> float:
    return float(saved_loss) - float(missed_profit) - float(model_cost)


def brier_score(probabilities: Iterable[float], labels: Iterable[bool]) -> float | None:
    pairs = list(zip(probabilities, labels))
    if not pairs:
        return None
    return sum((max(0.0, min(1.0, float(p))) - float(bool(label))) ** 2 for p, label in pairs) / len(pairs)


def summarize(rows: Iterable[Mapping[str, object]], *, model_cost: float = 0.0) -> EvaluationMetrics:
    """Summarize frozen outcomes; rows must already be settled and deduplicated."""

    material = list(rows)
    rejects = [row for row in material if row.get("verdict") == "reject"]
    saved = sum(max(0.0, -float(row.get("pnl_r") or 0)) for row in rejects)
    missed = sum(max(0.0, float(row.get("pnl_r") or 0)) for row in rejects)
    segments = {}
    for row in rejects:
        key = (row.get("base"), row.get("direction"), row.get("regime"))
        segments[key] = segments.get(key, 0) + 1
    share = max(segments.values()) / len(rejects) if rejects else 0.0
    probs = [float(row["risk_probability"]) for row in material if row.get("risk_probability") is not None]
    labels = [float(row.get("pnl_r") or 0) < 0 for row in material if row.get("risk_probability") is not None]
    return EvaluationMetrics(
        n=len(material), reject_n=len(rejects), saved_loss=saved,
        missed_profit=missed, model_cost=float(model_cost),
        incremental_ev=incremental_ev(saved_loss=saved, missed_profit=missed, model_cost=model_cost),
        brier=brier_score(probs, labels), max_segment_share=share)


def compare_same_inputs(champion: Sequence[Mapping[str, object]],
                        challenger: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Compare only paired frozen inputs; unpaired rows are excluded."""

    left = {str(row["input_hash"]): row for row in champion if row.get("input_hash")}
    right = {str(row["input_hash"]): row for row in challenger if row.get("input_hash")}
    keys = sorted(set(left) & set(right))
    disagreements = sum(left[key].get("verdict") != right[key].get("verdict") for key in keys)
    return {"paired_n": len(keys), "disagreements": disagreements,
            "agreement_rate": (1.0 - disagreements / len(keys)) if keys else None,
            "input_hashes": keys}


def evaluate_agent(db_path=None):
    """评价旧 AI 把关相对纯量化基线的 15m/4h 反事实增量。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q(
        "SELECT a.base,a.direction,a.verdict,a.reason_code,a.risk_probability,"
        "a.outcome_r,s.entry,s.stop,s.features,s.horizon_hours "
        "FROM ai_judgments a JOIN signal_samples_canonical s "
        "ON s.signal_id=a.signal_id WHERE a.call_status='valid' "
        "AND a.outcome_r IS NOT NULL AND s.timeframe=? AND s.horizon_hours=?",
        [config.SIGNAL_SAMPLE_TIMEFRAME, config.SIGNAL_OUTCOME_HORIZON_HOURS],
        db_path=db_path)
    from decision.entry_probability import execution_cost_r
    for row in rows:
        row["net_outcome_r"] = (float(row["outcome_r"]) -
                                float(execution_cost_r(row) or 0.0))
    rejects = [row for row in rows if row["verdict"] == "reject"]
    counts = sdb.q(
        "SELECT COALESCE(call_status,'legacy') status,COUNT(*) n "
        "FROM ai_judgments GROUP BY COALESCE(call_status,'legacy')",
        db_path=db_path)
    result = {"status": "insufficient_data", "valid_n": len(rows),
              "reject_n": len(rejects),
              "call_status_counts": {row["status"]: row["n"] for row in counts},
              "blocked_loss_precision": None, "opportunity_cost_r": None,
              "avoided_loss_r": None, "incremental_ev_r": None,
              "baseline_ev_r": None, "agent_policy_ev_r": None,
              "stability": {}}
    if (len(rows) < config.AGENT_EVAL_MIN_VALID or
            len(rejects) < config.AGENT_EVAL_MIN_REJECT):
        return result
    blocked_losses = [row for row in rejects if row["net_outcome_r"] < 0]
    opportunity = sum(max(0.0, row["net_outcome_r"]) for row in rejects)
    avoided = sum(max(0.0, -row["net_outcome_r"]) for row in rejects)
    baseline = sum(row["net_outcome_r"] for row in rows) / len(rows)
    policy = sum(row["net_outcome_r"] for row in rows
                 if row["verdict"] != "reject") / len(rows)
    grouped = defaultdict(list)
    for row in rows:
        grouped[f"direction:{row['direction']}"].append(row)
        grouped[f"symbol:{row['base']}"].append(row)
        grouped[f"reason:{row['reason_code'] or 'none'}"].append(row)
    stability = {}
    for key, group in grouped.items():
        rejected = [row for row in group if row["verdict"] == "reject"]
        stability[key] = {"n": len(group), "reject_n": len(rejected),
                          "incremental_ev_r": round(
                              -sum(row["net_outcome_r"] for row in rejected) /
                              len(group), 6)}
    result.update({"status": "evaluated",
                   "blocked_loss_precision": len(blocked_losses) / len(rejects),
                   "opportunity_cost_r": opportunity,
                   "avoided_loss_r": avoided,
                   "incremental_ev_r": policy - baseline,
                   "baseline_ev_r": baseline, "agent_policy_ev_r": policy,
                   "stability": stability})
    return result


def _harness_version(row: Mapping[str, object]) -> str:
    """模型 + prompt/context/schema/retrieval 共同定义一个可比较 Agent 版本。"""
    return config.AGENT_EVALUATION_VERSION + ":harness:" + ":".join(
        str(row.get(name) or "unknown") for name in (
        "model_version", "prompt_version", "context_version",
        "schema_version", "retrieval_version"))


def evaluate_harness(db_path=None, version=None):
    """按成熟的 15m/4h 路径评价 Harness；费用后、分版本、带 EV 下界。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q(
        "SELECT r.model_version,r.prompt_version,r.context_version,r.schema_version,"
        "r.retrieval_version,r.model_verdict,r.final_action,r.risk_probability,"
        "r.reason_codes,r.created_ts,s.symbol,s.direction,s.entry,s.stop,s.features,"
        "s.horizon_hours,"
        "e.pnl_r FROM agent_runs r JOIN agent_evaluations e ON e.run_id=r.run_id "
        "JOIN signal_samples_canonical s ON s.signal_id=r.signal_id "
        "WHERE e.lifecycle_status='mature' AND r.runtime_status='completed' "
        "AND r.model_verdict IS NOT NULL AND s.timeframe=? AND s.horizon_hours=?",
        [config.SIGNAL_SAMPLE_TIMEFRAME, config.SIGNAL_OUTCOME_HORIZON_HOURS],
        db_path=db_path)
    groups = defaultdict(list)
    for row in rows:
        groups[_harness_version(row)].append(row)
    if not groups:
        return {"status": "insufficient_data", "version": version,
                "n": 0, "reject_n": 0, "incremental_ev": None,
                "incremental_ev_lower_bound": None}
    if version is None:
        version = max(groups, key=lambda key: max(
            float(row.get("created_ts") or 0) for row in groups[key]))
    material = groups.get(version, [])
    if not material:
        return {"status": "insufficient_data", "version": version,
                "n": 0, "reject_n": 0, "incremental_ev": None,
                "incremental_ev_lower_bound": None}
    impacts, net_returns, rejects = [], [], []
    brier_pairs = []
    segments = defaultdict(int)
    reason_counts = defaultdict(int)
    from decision.entry_probability import execution_cost_r
    for row in material:
        cost_r = float(execution_cost_r(row) or 0.0)
        net_r = float(row.get("pnl_r") or 0) - cost_r
        rejected = row.get("model_verdict") == "reject"
        impact = -net_r if rejected else 0.0
        impacts.append(impact)
        net_returns.append(net_r)
        if rejected:
            rejects.append(row)
            try:
                regime = json.loads(row.get("features") or "{}").get("regime")
            except (TypeError, ValueError, json.JSONDecodeError):
                regime = None
            segments[(row.get("symbol"), row.get("direction"), regime)] += 1
            try:
                codes = json.loads(row.get("reason_codes") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                codes = []
            for code in codes:
                reason_counts[str(code)] += 1
        if row.get("risk_probability") is not None:
            brier_pairs.append((float(row["risk_probability"]), net_r < 0))
    n = len(material)
    mean = sum(impacts) / n
    variance = (sum((value - mean) ** 2 for value in impacts) / (n - 1)
                if n > 1 else 0.0)
    lower = mean - config.AGENT_EVAL_EV_Z * math.sqrt(variance / n)
    saved = sum(max(0.0, -value) for value, row in zip(net_returns, material)
                if row.get("model_verdict") == "reject")
    missed = sum(max(0.0, value) for value, row in zip(net_returns, material)
                 if row.get("model_verdict") == "reject")
    reject_n = len(rejects)
    baseline_ev = sum(net_returns) / n
    policy_ev = sum(value for value, row in zip(net_returns, material)
                    if row.get("model_verdict") != "reject") / n
    return {
        "status": ("evaluated" if n >= config.AGENT_EVAL_MIN_VALID and
                   reject_n >= config.AGENT_EVAL_MIN_REJECT else
                   "insufficient_data"),
        "version": version, "n": n, "reject_n": reject_n,
        "saved_loss": round(saved, 6), "missed_profit": round(missed, 6),
        "blocked_loss_precision": (round(sum(
            1 for value, row in zip(net_returns, material)
            if row.get("model_verdict") == "reject" and value < 0) / reject_n, 6)
            if reject_n else None),
        "incremental_ev": round(policy_ev - baseline_ev, 6),
        "incremental_ev_lower_bound": round(lower, 6),
        "baseline_ev_r": round(baseline_ev, 6),
        "agent_policy_ev_r": round(policy_ev, 6),
        "brier": (round(brier_score(
            [pair[0] for pair in brier_pairs], [pair[1] for pair in brier_pairs]), 6)
            if brier_pairs else None),
        "max_segment_share": (round(max(segments.values()) / reject_n, 6)
                              if reject_n else 0.0),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


def sync_harness_lifecycle(db_path=None):
    """自动登记/验证 Harness 版本；只到 validated，绝不自动开启 veto。"""
    from decision import agent_lifecycle
    from storage.agent_lifecycle import transition
    metrics = evaluate_harness(db_path=db_path)
    if not metrics.get("version"):
        return {"status": "no_mature_samples", "metrics": metrics}
    version = metrics["version"]
    row = agent_lifecycle.get(version, db_path=db_path)
    if not row:
        row = agent_lifecycle.register(version, db_path=db_path)
    if row["status"] == "candidate":
        row = transition(version, "shadow", reason="mature_samples_available",
                         metrics=metrics, db_path=db_path)
    if (row["status"] == "shadow" and
            metrics["n"] >= config.AGENT_EVAL_MIN_VALID and
            metrics["reject_n"] >= config.AGENT_EVAL_MIN_REJECT):
        row = agent_lifecycle.validate(version, metrics, db_path=db_path)
    return {"status": row["status"], "version": version,
            "metrics": metrics}
