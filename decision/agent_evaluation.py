"""Leakage-safe path evaluation and champion/challenger metrics."""

from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Iterable, Mapping, Sequence


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

