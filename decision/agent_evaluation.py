"""Leakage-safe path evaluation and champion/challenger metrics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Iterable, Mapping, Sequence

import config
from interfaces.agent import stable_hash


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

    def pairing_key(row: Mapping[str, object]) -> str | None:
        value = row.get("evidence_hash") or row.get("input_hash")
        return str(value) if value else None

    left = {key: row for row in champion if (key := pairing_key(row))}
    right = {key: row for row in challenger if (key := pairing_key(row))}
    common = sorted(set(left) & set(right))
    mismatches = [key for key in common
                  if (left[key].get("pnl_r") is not None and
                      right[key].get("pnl_r") is not None and
                      float(left[key]["pnl_r"]) != float(right[key]["pnl_r"]))]
    keys = [key for key in common if key not in set(mismatches)]
    disagreements = sum(left[key].get("verdict") != right[key].get("verdict")
                        for key in keys)
    champion_rows = [left[key] for key in keys]
    challenger_rows = [right[key] for key in keys]
    champion_cost = sum(float(row.get("model_cost_r") or 0)
                        for row in champion_rows)
    challenger_cost = sum(float(row.get("model_cost_r") or 0)
                          for row in challenger_rows)
    champion_metrics = summarize(champion_rows, model_cost=champion_cost)
    challenger_metrics = summarize(challenger_rows, model_cost=challenger_cost)
    champion_precision = (
        sum(float(left[key].get("pnl_r") or 0) < 0 for key in keys
            if left[key].get("verdict") == "reject") /
        champion_metrics.reject_n if champion_metrics.reject_n else None)
    challenger_precision = (
        sum(float(right[key].get("pnl_r") or 0) < 0 for key in keys
            if right[key].get("verdict") == "reject") /
        challenger_metrics.reject_n if challenger_metrics.reject_n else None)
    return {
        "paired_n": len(keys), "outcome_mismatch_n": len(mismatches),
        "disagreements": disagreements,
        "agreement_rate": (1.0 - disagreements / len(keys)) if keys else None,
        "champion": champion_metrics.__dict__,
        "challenger": challenger_metrics.__dict__,
        "reject_coverage_delta": (
            (challenger_metrics.reject_n - champion_metrics.reject_n) / len(keys)
            if keys else None),
        "blocked_loss_precision_delta": (
            challenger_precision - champion_precision
            if challenger_precision is not None and champion_precision is not None
            else None),
        "missed_profit_delta": (challenger_metrics.missed_profit -
                                champion_metrics.missed_profit),
        "incremental_ev_delta": (challenger_metrics.incremental_ev -
                                 champion_metrics.incremental_ev),
        "input_hashes": keys,
    }


def evaluate_agent(db_path=None, strategy_id=None):
    """评价指定当前策略身份下旧 AI 把关相对量化基线的反事实增量。"""
    import storage.db as sdb
    from decision.signal_identity import research_scope_version
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    strategy_version = research_scope_version(strategy_id)
    rows = sdb.q(
        "SELECT a.base,a.direction,a.verdict,a.reason_code,a.risk_probability,"
        "a.outcome_r,s.entry,s.stop,s.features,s.horizon_hours "
        "FROM ai_judgments a JOIN signal_samples s "
        "ON s.signal_id=a.signal_id WHERE a.call_status='valid' "
        "AND a.outcome_r IS NOT NULL AND s.strategy_id=? "
        "AND s.strategy_version=? AND s.timeframe=? AND s.horizon_hours=?",
        [strategy_id, strategy_version, config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS],
        db_path=db_path)
    from decision.entry_probability import execution_cost_r
    for row in rows:
        row["net_outcome_r"] = (float(row["outcome_r"]) -
                                float(execution_cost_r(row) or 0.0))
    rejects = [row for row in rows if row["verdict"] == "reject"]
    counts = sdb.q(
        "SELECT COALESCE(a.call_status,'legacy') status,COUNT(*) n "
        "FROM ai_judgments a JOIN signal_samples s ON s.signal_id=a.signal_id "
        "WHERE s.strategy_id=? AND s.strategy_version=? "
        "GROUP BY COALESCE(a.call_status,'legacy')",
        [strategy_id, strategy_version],
        db_path=db_path)
    result = {"status": "insufficient_data", "strategy_id": strategy_id,
              "strategy_version": strategy_version, "valid_n": len(rows),
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
    """策略、模型、上下文、工具和价格口径共同定义可比较版本。"""
    from decision.agent_lifecycle import version_for_identity
    return version_for_identity(
        strategy_id=str(row.get("strategy_id") or "unknown"),
        strategy_version=str(row.get("strategy_version") or "unknown"),
        model_version=str(row.get("model_version") or "unknown"),
        prompt_version=str(row.get("prompt_version") or "unknown"),
        context_version=str(row.get("context_version") or "unknown"),
        schema_version=str(row.get("schema_version") or "unknown"),
        retrieval_version=str(row.get("retrieval_version") or "unknown"),
        tool_policy_version=str(row.get("tool_policy_version") or "unknown"),
        pricing_version=str(row.get("pricing_version") or "unknown"))


def _baseline_eligible_for_harness(row: Mapping[str, object]) -> bool:
    """Mirror the candidate set on which Harness can change the decision.

    A reaches Harness consumption only after the quantitative, 2:1, entry
    model and evolver gates have all passed.  B is an explicitly shadow-only
    baseline, so its ``shadow`` decision is its research eligibility marker.
    A legacy AI rejection happens after Harness and would reject the order
    anyway; it therefore cannot be credited as Harness incremental value.
    A real Harness veto remains eligible because its reject reason is recorded
    separately as ``harness_reject`` while ``rule_decision`` stays ``pass``.
    """
    strategy_id = str(row.get("strategy_id") or "")
    rule_decision = str(row.get("rule_decision") or "")
    if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID:
        if rule_decision != "pass":
            return False
    elif rule_decision not in {"pass", "shadow"}:
        return False
    reject_reason = str(row.get("reject_reason") or "")
    return not reject_reason.startswith("ai_reject:")


def _qualified_harness_reject(row: Mapping[str, object]) -> bool:
    """A model reject counts only when it meets the deployed veto contract."""
    try:
        return (
            row.get("model_verdict") == "reject" and
            float(row.get("risk_probability")) >=
            config.AGENT_HARNESS_REJECT_MIN_RISK and
            float(row.get("confidence")) >=
            config.AGENT_HARNESS_REJECT_MIN_CONFIDENCE)
    except (TypeError, ValueError):
        return False


def _json_object(raw: object) -> dict[str, object]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _regime_label(raw_features: object) -> str:
    """把冻结的结构化 regime 压成稳定、可哈希的分段标签。"""
    regime = _json_object(raw_features).get("regime")
    if isinstance(regime, Mapping):
        for name in ("tag", "state"):
            if regime.get(name):
                return str(regime[name])
        market = regime.get("market_state")
        if isinstance(market, Mapping) and market.get("state"):
            return str(market["state"])
        return "unknown"
    return str(regime or "unknown")


def _risk_budget_usdt(row: Mapping[str, object]) -> float | None:
    """Rebuild the paper risk amount frozen at decision time.

    The model invoice is denominated in USD while outcomes are in R.  A cost
    can only enter incremental R when equity, risk fraction and notional cap
    were frozen in the input snapshot; otherwise promotion must remain blocked.
    """

    snapshot = _json_object(row.get("input_snapshot"))
    account = snapshot.get("account")
    signal = snapshot.get("signal")
    if not isinstance(account, Mapping) or not isinstance(signal, Mapping):
        return None
    try:
        entry = float(signal.get("entry") or row.get("entry") or 0)
        stop = float(signal.get("stop") or row.get("stop") or 0)
        equity = float(account.get("equity_usdt") or 0)
        risk_fraction = float(account.get("risk_per_trade") or 0)
        notional_cap = float(account.get("max_notional_per_trade_usdt") or 0)
    except (TypeError, ValueError):
        return None
    if min(entry, stop, equity, risk_fraction, notional_cap) <= 0:
        return None
    stop_fraction = abs(entry - stop) / entry
    risk = min(equity * risk_fraction, notional_cap * stop_fraction)
    return risk if risk > 0 else None


def _calibration_bins(pairs: Sequence[tuple[float, bool]]) -> list[dict[str, object]]:
    bins: list[dict[str, object]] = []
    for idx in range(5):
        low, high = idx / 5, (idx + 1) / 5
        selected = [(p, label) for p, label in pairs
                    if low <= p <= high and (idx == 4 or p < high)]
        if not selected:
            continue
        bins.append({
            "low": low, "high": high, "n": len(selected),
            "mean_probability": round(sum(p for p, _ in selected) /
                                      len(selected), 6),
            "actual_loss_rate": round(sum(bool(label) for _, label in selected) /
                                      len(selected), 6),
        })
    return bins


def evaluate_harness(db_path=None, version=None, strategy_id=None):
    """按成熟的 15m/4h 路径评价 Harness；费用后、分版本、带 EV 下界。"""
    import storage.db as sdb
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    if version is None:
        from decision.agent_lifecycle import configured_version
        version = configured_version(strategy_id)
    rows = sdb.q(
        "SELECT r.model_version,r.prompt_version,r.context_version,r.schema_version,"
        "r.retrieval_version,r.tool_policy_version,r.model_verdict,r.final_action,"
        "r.risk_probability,r.confidence,r.reason_codes,r.evidence_ids,"
        "r.missing_information,r.input_hash,r.input_snapshot,r.estimated_cost,"
        "r.pricing_version,r.created_ts,s.strategy_id,s.strategy_version,"
        "s.symbol,s.direction,s.entry,s.stop,s.features,s.event_ts,s.kline_ts,"
        "s.horizon_hours,s.rule_decision,s.ai_verdict,s.final_decision,"
        "s.reject_reason,"
        "e.pnl_r FROM agent_runs r JOIN agent_evaluations e ON e.run_id=r.run_id "
        "JOIN signal_samples s ON s.signal_id=r.signal_id "
        "WHERE e.lifecycle_status='mature' AND r.runtime_status='completed' "
        "AND r.model_verdict IS NOT NULL AND s.strategy_id=? "
        "AND s.timeframe=? AND s.horizon_hours=?",
        [strategy_id, config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS],
        db_path=db_path)
    groups = defaultdict(list)
    for row in rows:
        groups[_harness_version(row)].append(row)
    if not groups:
        return {"status": "insufficient_data", "version": version,
                "strategy_id": strategy_id,
                "n": 0, "reject_n": 0, "incremental_ev": None,
                "incremental_ev_lower_bound": None,
                "observed_mature_n": 0, "baseline_eligible_n": 0,
                "excluded_nonbaseline_n": 0}
    observed = groups.get(version, [])
    material = [row for row in observed
                if _baseline_eligible_for_harness(row)]
    if not material:
        return {"status": "insufficient_data", "version": version,
                "strategy_id": strategy_id,
                "n": 0, "reject_n": 0, "incremental_ev": None,
                "incremental_ev_lower_bound": None,
                "observed_mature_n": len(observed),
                "baseline_eligible_n": 0,
                "excluded_nonbaseline_n": len(observed)}
    trade_impacts, net_returns, rejects = [], [], []
    effective_rejects: list[bool] = []
    model_cost_r_values: list[float] = []
    model_cost_usd = 0.0
    missing_cost_n = 0
    brier_pairs: list[tuple[float, bool]] = []
    segments = defaultdict(int)
    direction_segments = defaultdict(int)
    reason_counts = defaultdict(int)
    stability_groups: dict[str, list[tuple[bool, float, float]]] = defaultdict(list)
    replayable_n = 0
    reject_evidence_n = 0
    from decision.entry_probability import execution_cost_r
    for row in material:
        cost_r = float(execution_cost_r(row) or 0.0)
        net_r = float(row.get("pnl_r") or 0) - cost_r
        rejected = _qualified_harness_reject(row)
        effective_rejects.append(rejected)
        trade_impact = -net_r if rejected else 0.0
        trade_impacts.append(trade_impact)
        net_returns.append(net_r)
        snapshot = _json_object(row.get("input_snapshot"))
        if (snapshot and row.get("input_hash") and
                stable_hash(snapshot) == row.get("input_hash")):
            replayable_n += 1
        estimated_usd = row.get("estimated_cost")
        if estimated_usd is None:
            missing_cost_n += 1
        else:
            usd = max(0.0, float(estimated_usd))
            model_cost_usd += usd
            risk_budget = _risk_budget_usdt(row)
            if usd == 0:
                model_cost_r_values.append(0.0)
            elif risk_budget is None:
                missing_cost_n += 1
            else:
                model_cost_r_values.append(usd / risk_budget)
        regime = _regime_label(row.get("features"))
        month = datetime.fromtimestamp(
            float(row.get("event_ts") or 0), tz=timezone.utc).strftime("%Y-%m")
        for key in (
                f"direction:{row.get('direction') or 'unknown'}",
                f"symbol:{row.get('symbol') or 'unknown'}",
                f"regime:{regime}", f"month:{month}"):
            stability_groups[key].append((rejected, net_r, trade_impact))
        if rejected:
            rejects.append(row)
            segments[(row.get("symbol"), row.get("direction"), regime)] += 1
            direction_segments[str(row.get("direction") or "unknown")] += 1
            try:
                codes = json.loads(row.get("reason_codes") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                codes = []
            for code in codes:
                reason_counts[str(code)] += 1
            try:
                evidence = json.loads(row.get("evidence_ids") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = []
            if evidence:
                reject_evidence_n += 1
        if row.get("risk_probability") is not None:
            brier_pairs.append((float(row["risk_probability"]), net_r < 0))
    n = len(material)
    cost_complete = missing_cost_n == 0 and len(model_cost_r_values) == n
    impacts = ([trade - model_cost for trade, model_cost in
                zip(trade_impacts, model_cost_r_values)] if cost_complete else [])
    mean = sum(impacts) / n if impacts else None
    variance = (sum((value - mean) ** 2 for value in impacts) / (n - 1)
                if impacts and n > 1 and mean is not None else 0.0)
    lower = (mean - config.AGENT_EVAL_EV_Z * math.sqrt(variance / n)
             if mean is not None else None)
    pre_cost_mean = sum(trade_impacts) / n
    pre_cost_variance = (sum((value - pre_cost_mean) ** 2
                             for value in trade_impacts) / (n - 1)
                         if n > 1 else 0.0)
    pre_cost_lower = pre_cost_mean - config.AGENT_EVAL_EV_Z * math.sqrt(
        pre_cost_variance / n)
    saved = sum(max(0.0, -value) for value, rejected in
                zip(net_returns, effective_rejects) if rejected)
    missed = sum(max(0.0, value) for value, rejected in
                 zip(net_returns, effective_rejects) if rejected)
    reject_n = len(rejects)
    baseline_ev = sum(net_returns) / n
    policy_before_model_cost = sum(
        value for value, rejected in zip(net_returns, effective_rejects)
        if not rejected) / n
    policy_ev = (policy_before_model_cost - sum(model_cost_r_values) / n
                 if cost_complete else None)
    loss_rate = sum(label for _, label in brier_pairs) / len(brier_pairs) \
        if brier_pairs else None
    brier = (brier_score([pair[0] for pair in brier_pairs],
                         [pair[1] for pair in brier_pairs])
             if brier_pairs else None)
    baseline_brier = (brier_score([loss_rate] * len(brier_pairs),
                                  [pair[1] for pair in brier_pairs])
                      if loss_rate is not None else None)
    brier_skill = (1 - brier / baseline_brier
                   if brier is not None and baseline_brier else None)
    risk_values = [probability for probability, _ in brier_pairs]
    probability_mean = (sum(risk_values) / len(risk_values)
                        if risk_values else None)
    probability_std = (math.sqrt(sum(
        (value - probability_mean) ** 2 for value in risk_values) /
        len(risk_values)) if risk_values and probability_mean is not None
        else None)
    stability = {}
    for key, group in sorted(stability_groups.items()):
        group_rejects = [item for item in group if item[0]]
        stability[key] = {
            "n": len(group), "reject_n": len(group_rejects),
            "false_accept_n": sum(not rejected and net_r < 0
                                  for rejected, net_r, _ in group),
            "false_reject_n": sum(rejected and net_r > 0
                                  for rejected, net_r, _ in group),
            "incremental_ev_before_model_cost": round(
                sum(item[2] for item in group) / len(group), 6),
        }
    evaluated = (
        n >= config.AGENT_EVAL_MIN_VALID and
        reject_n >= config.AGENT_EVAL_MIN_REJECT and cost_complete and
        replayable_n == n and len(brier_pairs) == n)
    return {
        "status": "evaluated" if evaluated else "insufficient_data",
        "version": version, "strategy_id": strategy_id,
        "observed_mature_n": len(observed),
        "baseline_eligible_n": n,
        "excluded_nonbaseline_n": len(observed) - n,
        "n": n, "reject_n": reject_n,
        "reject_coverage": round(reject_n / n, 6),
        "saved_loss": round(saved, 6), "missed_profit": round(missed, 6),
        "blocked_loss_precision": (round(sum(
            1 for value, rejected in zip(net_returns, effective_rejects)
            if rejected and value < 0) / reject_n, 6)
            if reject_n else None),
        "model_cost_usd": round(model_cost_usd, 8),
        "model_cost_r": (round(sum(model_cost_r_values), 8)
                         if cost_complete else None),
        "model_cost_data_complete": cost_complete,
        "model_cost_missing_n": missing_cost_n,
        "incremental_ev_before_model_cost": round(pre_cost_mean, 6),
        "incremental_ev_before_model_cost_lower_bound": round(pre_cost_lower, 6),
        "incremental_ev": round(mean, 6) if mean is not None else None,
        "incremental_ev_lower_bound": round(lower, 6) if lower is not None else None,
        "baseline_ev_r": round(baseline_ev, 6),
        "agent_policy_ev_r": round(policy_ev, 6) if policy_ev is not None else None,
        "brier": round(brier, 6) if brier is not None else None,
        "baseline_brier": (round(baseline_brier, 6)
                           if baseline_brier is not None else None),
        "brier_skill": round(brier_skill, 6) if brier_skill is not None else None,
        "probability_coverage": round(len(brier_pairs) / n, 6),
        "probability_mean": (round(probability_mean, 6)
                             if probability_mean is not None else None),
        "probability_std": (round(probability_std, 6)
                            if probability_std is not None else None),
        "calibration_bins": _calibration_bins(brier_pairs),
        "replayable_n": replayable_n,
        "trace_coverage": round(replayable_n / n, 6),
        "reject_evidence_coverage": (round(reject_evidence_n / reject_n, 6)
                                     if reject_n else None),
        "false_accept_n": sum(not rejected and value < 0 for value, rejected in
                              zip(net_returns, effective_rejects)),
        "false_reject_n": sum(rejected and value > 0 for value, rejected in
                              zip(net_returns, effective_rejects)),
        "max_segment_share": (round(max(segments.values()) / reject_n, 6)
                              if reject_n else 0.0),
        "max_direction_share": (
            round(max(direction_segments.values()) / reject_n, 6)
            if reject_n else 0.0),
        "reason_counts": dict(sorted(reason_counts.items())),
        "stability": stability,
    }


def sync_harness_lifecycle(db_path=None, strategy_id=None, *,
                           allow_activation=True):
    """自动登记/验证 Harness 版本；只到 validated，绝不自动开启 veto。"""
    from decision import agent_lifecycle
    from storage.agent_lifecycle import refresh_metrics, transition
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    current_version = agent_lifecycle.configured_version(strategy_id)
    metrics = evaluate_harness(
        db_path=db_path, strategy_id=strategy_id, version=current_version)
    if not metrics.get("version") or int(metrics.get("n", 0)) <= 0:
        return {"status": "no_mature_samples", "metrics": metrics}
    version = metrics["version"]
    row = agent_lifecycle.get(version, strategy_id=strategy_id, db_path=db_path)
    if not row:
        row = agent_lifecycle.register(
            version, strategy_id=strategy_id, db_path=db_path)
    if row["status"] == "candidate":
        row = transition(version, "shadow", reason="mature_samples_available",
                         metrics=metrics, strategy_id=strategy_id, db_path=db_path)
    elif row["status"] == "shadow":
        # mature outcome 持续增长时状态仍是 shadow，但 readiness 必须消费
        # 当前证据，不能永远停在第一次 candidate→shadow 的少量样本快照。
        row = refresh_metrics(
            version, metrics, reason="shadow_metrics_refresh",
            strategy_id=strategy_id, db_path=db_path)
    if (row["status"] == "shadow" and
            metrics["n"] >= config.AGENT_EVAL_MIN_VALID and
            metrics["reject_n"] >= config.AGENT_EVAL_MIN_REJECT):
        row = agent_lifecycle.validate(
            version, metrics, strategy_id=strategy_id, db_path=db_path)
    if row["status"] == "validated":
        ready, reason = agent_lifecycle.promotion_ready(metrics)
        if ready:
            row = refresh_metrics(
                version, metrics, reason="validated_metrics_refresh",
                strategy_id=strategy_id, db_path=db_path)
        else:
            # 人工激活前若新增证据破坏了验证门，立即撤销已验证身份；
            # 不能继续展示旧的正面快照等待获权。
            row = transition(
                version, "rolled-back", reason=reason, metrics=metrics,
                strategy_id=strategy_id, db_path=db_path)
    if (row["status"] == "validated" and allow_activation and
            config.AGENT_HARNESS_VETO_ENABLED):
        row = agent_lifecycle.activate(
            version, strategy_id=strategy_id, db_path=db_path)
    return {"status": row["status"], "version": version,
            "metrics": metrics}


def sync_harness_lifecycles(db_path=None):
    """Sync every sampled strategy while keeping B permanently non-executing."""
    strategies = [(config.ENTRY_SIGNAL_STRATEGY_ID, True)]
    if (config.STRATEGY_B_SHADOW_ENABLED and
            config.BREAKOUT_SIGNAL_STRATEGY_ID !=
            config.ENTRY_SIGNAL_STRATEGY_ID):
        strategies.append((config.BREAKOUT_SIGNAL_STRATEGY_ID, False))
    return {
        strategy_id: sync_harness_lifecycle(
            db_path=db_path, strategy_id=strategy_id,
            allow_activation=allow_activation)
        for strategy_id, allow_activation in strategies
    }
