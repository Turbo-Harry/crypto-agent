"""Versioned, fail-closed contracts for the trading-agent harness.

The model is a risk critic only.  These types deliberately keep the model
verdict, runtime health, and policy action separate so an infrastructure
failure can never be recorded as a model approval.

This module lives in the neutral interface layer so persistence adapters and
decision implementations depend on the same contract without a reverse
``storage -> decision`` dependency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


class RuntimeStatus(str, Enum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    NO_KEY = "no_key"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    SCHEMA_ERROR = "schema_error"
    TOOL_ERROR = "tool_error"


class FinalAction(str, Enum):
    BASELINE_REJECT = "baseline_reject"
    BASELINE_PASS = "baseline_pass"
    SHADOW_REJECT = "shadow_reject"
    AGENT_REJECT = "agent_reject"
    AGENT_ABSTAIN = "agent_abstain"


class RunRole(str, Enum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"


class LifecycleStatus(str, Enum):
    PENDING = "pending"
    MATURE = "mature"
    INVALID = "invalid"


class StepType(str, Enum):
    CONTEXT = "context"
    RETRIEVE = "retrieve"
    TOOL = "tool"
    MODEL = "model"
    VALIDATE = "validate"
    POLICY = "policy"


class StepStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ReasonCode(str, Enum):
    NEWS_DIRECTION_CONFLICT = "news_direction_conflict"
    EXTREME_MARKET_EVENT = "extreme_market_event"
    LIQUIDITY_FAILURE = "liquidity_failure"
    STALE_OR_MISSING_DATA = "stale_or_missing_data"
    SIGNAL_INCONSISTENCY = "signal_inconsistency"
    POSITION_RISK_CONFLICT = "position_risk_conflict"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return stable JSON used for hashes and replay identities."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _as_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_as_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class AgentInput:
    """Immutable input snapshot presented to the harness/model."""

    run_id: str
    signal_id: str
    event_ts: str
    kline_ts: str
    strategy_version: str
    prompt_version: str
    model_version: str
    context_version: str
    schema_version: str
    retrieval_version: str
    tool_policy_version: str = "tool-policy-v1"
    pricing_version: str = "unpriced"
    signal: Mapping[str, Any] = field(default_factory=dict)
    market: Mapping[str, Any] = field(default_factory=dict)
    news: Mapping[str, Any] = field(default_factory=dict)
    account: Mapping[str, Any] = field(default_factory=dict)
    health: Mapping[str, Any] = field(default_factory=dict)
    memory: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    field_provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _as_plain(asdict(self))

    @property
    def input_hash(self) -> str:
        return stable_hash(self.to_dict())

    @property
    def evidence_hash(self) -> str:
        """Hash frozen market evidence while deliberately excluding versions.

        Prompt/model/context versions must differ between champion and
        challenger.  Pairing on ``input_hash`` would therefore yield zero
        matches even when both evaluated the same market snapshot.
        """

        source = self.to_dict()
        return stable_hash({name: source[name] for name in (
            "signal_id", "event_ts", "kline_ts", "strategy_version",
            "signal", "market", "news", "account", "health", "memory",
            "field_provenance",
        )})


@dataclass(frozen=True)
class AgentDecision:
    verdict: Verdict
    risk_probability: float
    confidence: float
    reason_codes: tuple[ReasonCode, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    abstain_reason: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.risk_probability <= 1.0:
            raise ValueError("risk_probability must be in [0, 1]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.verdict is Verdict.ABSTAIN and not self.abstain_reason:
            raise ValueError("abstain_reason is required for abstain")
        if self.verdict is Verdict.REJECT and not self.reason_codes:
            raise ValueError("reason_codes are required for reject")
        if self.verdict is Verdict.REJECT and not self.evidence_ids:
            raise ValueError("evidence_ids are required for reject")

    def to_dict(self) -> dict[str, Any]:
        return _as_plain(asdict(self))


@dataclass(frozen=True)
class AgentStep:
    run_id: str
    step_no: int
    step_type: StepType
    status: StepStatus
    started_at: str
    finished_at: str | None = None
    tool_name: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    evidence_ids: tuple[str, ...] = ()
    retry_count: int = 0
    error_type: str | None = None
    fallback_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _as_plain(asdict(self))


@dataclass(frozen=True)
class HarnessRun:
    run_id: str
    signal_id: str
    runtime_status: RuntimeStatus
    final_action: FinalAction
    model_verdict: Verdict | None = None
    run_role: RunRole = RunRole.CHAMPION
    parent_run_id: str | None = None
    input_hash: str | None = None
    response_hash: str | None = None
    error_type: str | None = None
    latency_ms: int | None = None
    model_latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    pricing_version: str | None = None
    # Provider invoice estimate in USD.  Trading evaluation converts it to R
    # only when the frozen input contains a reproducible paper risk budget.
    estimated_cost: float | None = None
    risk_probability: float | None = None
    confidence: float | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    abstain_reason: str | None = None
    decision_reason: str | None = None

    def __post_init__(self) -> None:
        if self.runtime_status is RuntimeStatus.COMPLETED and self.model_verdict is None:
            raise ValueError("completed run requires model_verdict")
        if self.runtime_status is not RuntimeStatus.COMPLETED and self.model_verdict is not None:
            raise ValueError("failed/disabled run cannot claim model_verdict")
        if self.runtime_status is RuntimeStatus.COMPLETED and self.error_type:
            raise ValueError("completed run cannot have error_type")

    def to_dict(self) -> dict[str, Any]:
        return _as_plain(asdict(self))


@dataclass(frozen=True)
class ModelCallResult:
    """Provider-neutral model content plus billable usage metadata."""

    content: Any
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    estimated_cost: float | None = None
    pricing_version: str | None = None


@dataclass(frozen=True)
class PolicyContext:
    baseline_passed: bool
    model_enabled: bool = True
    model_verdict: Verdict | None = None
    runtime_status: RuntimeStatus = RuntimeStatus.DISABLED


@dataclass(frozen=True)
class HarnessConfig:
    version: str = "harness-v1"
    max_steps: int = 8
    max_tools: int = 3
    timeout_ms: int = 4000
    max_context_chars: int = 24000
    allowed_tools: tuple[str, ...] = (
        "get_signal_snapshot",
        "get_market_regime",
        "get_risk_state",
    )


def parse_verdict(payload: Mapping[str, Any]) -> AgentDecision:
    """Strictly validate a model JSON object; malformed output fails closed."""

    try:
        verdict = Verdict(str(payload["verdict"]).lower())
        risk_probability = float(payload["risk_probability"])
        confidence = float(payload["confidence"])
        reason_codes = tuple(ReasonCode(str(v)) for v in payload.get("reason_codes", ()))
        evidence_ids = tuple(str(v) for v in payload.get("evidence_ids", ()))
        missing = tuple(str(v) for v in payload.get("missing_information", ()))
        abstain_reason = payload.get("abstain_reason")
        reason = str(payload.get("reason", ""))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid agent decision: {exc}") from exc
    if abstain_reason is not None:
        abstain_reason = str(abstain_reason)
    return AgentDecision(
        verdict=verdict,
        risk_probability=risk_probability,
        confidence=confidence,
        reason_codes=reason_codes,
        evidence_ids=evidence_ids,
        missing_information=missing,
        abstain_reason=abstain_reason,
        reason=reason,
    )


def strict_parse_model_output(raw: str | bytes | Mapping[str, Any]) -> AgentDecision:
    if isinstance(raw, Mapping):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("model output must be a JSON object") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("model output must be a JSON object")
    return parse_verdict(payload)


def apply_policy(context: PolicyContext) -> FinalAction:
    """Map model/runtime state to action; baseline hard gates always win."""

    if not context.baseline_passed:
        return FinalAction.BASELINE_REJECT
    if not context.model_enabled or context.runtime_status is not RuntimeStatus.COMPLETED:
        return FinalAction.BASELINE_PASS
    if context.model_verdict is Verdict.REJECT:
        return FinalAction.AGENT_REJECT
    if context.model_verdict is Verdict.ABSTAIN:
        return FinalAction.AGENT_ABSTAIN
    return FinalAction.BASELINE_PASS


def idempotency_key(signal_id: str, harness_version: str) -> str:
    return stable_hash({"signal_id": signal_id, "harness_version": harness_version})
