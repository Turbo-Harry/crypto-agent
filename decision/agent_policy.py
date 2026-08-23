"""Deterministic policy kernel: model suggestions never widen risk."""

from __future__ import annotations

from dataclasses import dataclass

from decision.agent_contracts import (
    AgentDecision,
    FinalAction,
    RuntimeStatus,
    Verdict,
)


@dataclass(frozen=True)
class PolicyResult:
    final_action: FinalAction
    veto: bool
    reason: str


class PolicyKernel:
    """Map a validated model decision to a paper-only policy action.

    ``veto_enabled`` is deliberately explicit and defaults to False.  Shadow
    rejects are observable but cannot affect the existing strategy decision.
    """

    def __init__(self, *, veto_enabled: bool = False, shadow: bool = True,
                 min_reject_risk: float = 0.0,
                 min_reject_confidence: float = 0.0):
        self.veto_enabled = bool(veto_enabled)
        self.shadow = bool(shadow)
        self.min_reject_risk = max(0.0, min(1.0, float(min_reject_risk)))
        self.min_reject_confidence = max(
            0.0, min(1.0, float(min_reject_confidence)))

    def evaluate(self, *, baseline_passed: bool, runtime_status: RuntimeStatus,
                 decision: AgentDecision | None) -> PolicyResult:
        if not baseline_passed:
            return PolicyResult(FinalAction.BASELINE_REJECT, False, "baseline hard gate")
        if runtime_status is not RuntimeStatus.COMPLETED or decision is None:
            return PolicyResult(FinalAction.BASELINE_PASS, False, "runtime fallback")
        if decision.verdict is Verdict.REJECT:
            qualified = (
                float(decision.risk_probability) >= self.min_reject_risk and
                float(decision.confidence) >= self.min_reject_confidence)
            if self.veto_enabled and qualified:
                return PolicyResult(FinalAction.AGENT_REJECT, True, decision.reason)
            if self.veto_enabled and not qualified:
                return PolicyResult(
                    FinalAction.SHADOW_REJECT, False,
                    "reject below calibrated risk/confidence threshold")
            if self.shadow:
                return PolicyResult(FinalAction.SHADOW_REJECT, False, decision.reason)
            return PolicyResult(FinalAction.BASELINE_PASS, False, "veto disabled")
        if decision.verdict is Verdict.ABSTAIN:
            return PolicyResult(FinalAction.AGENT_ABSTAIN, False, decision.abstain_reason or "abstain")
        return PolicyResult(FinalAction.BASELINE_PASS, False, decision.reason or "agent approve")
