"""Single LangGraph/LangChain runtime for the constrained trading Harness.

LangGraph owns orchestration only. Immutable contracts, the read-only tool
router, deterministic policy kernel and SQLite trace store remain authoritative.
This module cannot accept an exchange or produce an order instruction.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, TypedDict

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

import config
from decision.agent_context import build_context, serialize_context
from decision.agent_contracts import (
    AgentDecision, AgentInput, AgentSemanticError, AgentStep, FinalAction,
    HarnessConfig, HarnessRun, ModelCallResult, ReasonCode, RuntimeStatus,
    StepStatus, StepType, Verdict, strict_parse_model_output, stable_hash,
)
from decision.agent_memory import retrieve_for_input
from decision.agent_policy import PolicyKernel, PolicyResult
from decision.agent_tools import ReadOnlyToolRouter, ToolRouterError, tool_trace_payload
from storage import agent_harness as trace_store


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HarnessResult:
    run: HarnessRun
    decision: AgentDecision | None
    policy: PolicyResult
    prompt: str
    memory: tuple[dict[str, Any], ...]


class _DecisionPayload(BaseModel):
    """LangChain parser boundary; domain validation is applied afterwards."""

    verdict: str
    risk_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    abstain_reason: str | None = None
    reason: str = ""


class _GraphState(TypedDict, total=False):
    agent_input: AgentInput
    baseline_passed: bool
    runtime_status: RuntimeStatus
    serialized_context: str
    memory: tuple[dict[str, Any], ...]
    tool_payload: list[dict[str, Any]]
    prompt: str
    raw_response: Any
    response_hash: str | None
    decision: AgentDecision | None
    policy: PolicyResult
    run: HarnessRun
    steps: tuple[AgentStep, ...]
    model_step_no: int
    policy_step_no: int
    model_outcome: str
    model_error_type: str | None
    model_started_at: str
    model_latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    estimated_cost: float | None
    pricing_version: str | None
    model_retry_count: int
    semantic_errors: tuple[str, ...]
    retry_model: bool
    started_monotonic: float


def _runtime_error(exc: Exception) -> RuntimeStatus:
    if isinstance(exc, ValueError):
        return RuntimeStatus.SCHEMA_ERROR
    if isinstance(exc, TimeoutError):
        return RuntimeStatus.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return RuntimeStatus.HTTP_ERROR
    if exc.__class__.__name__.lower().endswith("toolerror"):
        return RuntimeStatus.TOOL_ERROR
    return RuntimeStatus.HTTP_ERROR


def _durable_result(agent_input: AgentInput, *, db_path: str | None) -> HarnessResult | None:
    """Return an existing completed attempt without calling the model again."""

    try:
        row = trace_store.get_run(agent_input.run_id, db_path=db_path)
    except Exception:
        return None
    if not row:
        return None
    runtime = RuntimeStatus(str(row["runtime_status"]))
    action = FinalAction(str(row["final_action"]))
    verdict = (Verdict(str(row["model_verdict"]))
               if row.get("model_verdict") else None)
    try:
        reason_codes = tuple(json.loads(row.get("reason_codes") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        reason_codes = ()
    def _json_tuple(name: str) -> tuple[str, ...]:
        try:
            value = json.loads(row.get(name) or "[]")
            return tuple(str(item) for item in value) if isinstance(value, list) else ()
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
    run = HarnessRun(
        run_id=str(row["run_id"]), signal_id=str(row["signal_id"]),
        runtime_status=runtime, final_action=action, model_verdict=verdict,
        input_hash=row.get("input_hash"), response_hash=row.get("response_hash"),
        latency_ms=row.get("latency_ms"),
        model_latency_ms=row.get("model_latency_ms"),
        input_tokens=row.get("input_tokens"), output_tokens=row.get("output_tokens"),
        prompt_cache_hit_tokens=row.get("prompt_cache_hit_tokens"),
        prompt_cache_miss_tokens=row.get("prompt_cache_miss_tokens"),
        pricing_version=row.get("pricing_version"),
        estimated_cost=row.get("estimated_cost"), error_type=row.get("error_type"),
        risk_probability=row.get("risk_probability"),
        confidence=row.get("confidence"), reason_codes=reason_codes,
        evidence_ids=_json_tuple("evidence_ids"),
        missing_information=_json_tuple("missing_information"),
        abstain_reason=row.get("abstain_reason"),
        decision_reason=row.get("decision_reason"))
    policy = PolicyResult(
        final_action=action, veto=action is FinalAction.AGENT_REJECT,
        reason="idempotent durable result")
    return HarnessResult(run=run, decision=None, policy=policy, prompt="", memory=())


def _domain_decision(raw: Any) -> AgentDecision:
    """Use LangChain structured parsing without weakening strict JSON rules."""

    if isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("model output must be a JSON object") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("model output must be a JSON object")
    parser = PydanticOutputParser(pydantic_object=_DecisionPayload)
    parsed = parser.invoke(json.dumps(payload, ensure_ascii=False))
    return strict_parse_model_output(parsed.model_dump(exclude_none=True))


_GOVERNANCE_ONLY_MARKERS = (
    "no_validated_active_model", "validated active model", "entry model",
    "uncalibrated forecast", "strategy_route", "无已验证模型",
    "缺少已验证模型", "缺乏已验证模型", "缺少已验证的入场模型",
    "缺乏已验证的入场模型", "缺少入场概率模型", "缺乏入场概率模型",
    "入场模型正期望证据", "入场概率模型", "预测未校准",
)


def _evidence_ids(state: _GraphState) -> set[str]:
    """Return exact evidence identifiers visible in the frozen prompt."""

    allowed: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value:
            allowed.add(value)

    collect(state["agent_input"].field_provenance)
    for memory in state.get("memory", ()):
        evidence_id = memory.get("evidence_id")
        if evidence_id:
            allowed.add(str(evidence_id))
    for tool in state.get("tool_payload", ()):
        output_hash = tool.get("output_hash")
        if output_hash:
            allowed.add(str(output_hash))
    return allowed


def _validate_decision_semantics(decision: AgentDecision,
                                 state: _GraphState) -> None:
    """Reject structurally valid answers that misuse governance metadata."""

    semantic_text = " ".join((
        *decision.missing_information,
        decision.abstain_reason or "",
    )).lower()
    markers = [marker for marker in _GOVERNANCE_ONLY_MARKERS
               if marker.lower() in semantic_text]
    if markers:
        raise AgentSemanticError(
            "governance metadata cannot justify missing evidence: " +
            ",".join(markers))
    agent_input = state["agent_input"]
    forecast = agent_input.signal.get("forecast")
    if isinstance(forecast, Mapping):
        prior = forecast.get("p_loss_prior")
        if (prior is not None and decision.verdict is Verdict.ABSTAIN and
                agent_input.prompt_version ==
                "harness-risk-v5-forecast-loss-prior"):
            try:
                delta = abs(decision.risk_probability - float(prior))
            except (TypeError, ValueError) as exc:
                raise AgentSemanticError(
                    "forecast p_loss_prior must be numeric") from exc
            if delta > config.AGENT_HARNESS_ABSTAIN_PRIOR_TOLERANCE:
                raise AgentSemanticError(
                    "abstain risk_probability must track frozen "
                    f"p_loss_prior={float(prior):.4f}")
    if decision.verdict is Verdict.REJECT and (
            decision.risk_probability < config.AGENT_HARNESS_REJECT_MIN_RISK or
            decision.confidence < config.AGENT_HARNESS_REJECT_MIN_CONFIDENCE):
        raise AgentSemanticError(
            "reject must meet configured risk and confidence thresholds")
    if (decision.verdict is Verdict.APPROVE and
            decision.risk_probability > config.AGENT_HARNESS_APPROVE_MAX_RISK):
        raise AgentSemanticError(
            "approve exceeds configured maximum loss probability")
    if (decision.verdict is Verdict.ABSTAIN and
            decision.risk_probability >= config.AGENT_HARNESS_REJECT_MIN_RISK and
            decision.confidence >= config.AGENT_HARNESS_REJECT_MIN_CONFIDENCE):
        raise AgentSemanticError(
            "high-risk high-confidence decision must be reject or lower confidence")
    if decision.verdict is Verdict.REJECT:
        provenance = state["agent_input"].field_provenance
        allowed = _evidence_ids(state) if provenance else set()
        unknown = [item for item in decision.evidence_ids
                   if provenance and item not in allowed]
        if unknown:
            raise AgentSemanticError(
                "reject evidence_ids are not anchored: " + ",".join(unknown))
        if agent_input.prompt_version in \
                config.AGENT_HARNESS_DIRECTIONAL_EVIDENCE_PROMPT_VERSIONS:
            _validate_directional_reject_evidence(decision, agent_input)


def _frozen_factor_features(agent_input: AgentInput) -> Mapping[str, Any]:
    frozen = agent_input.market.get("frozen_features")
    if isinstance(frozen, Mapping):
        features = frozen.get("factor_features")
        if isinstance(features, Mapping):
            return features
    features = agent_input.signal.get("factor_features")
    return features if isinstance(features, Mapping) else {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _has_directional_momentum_conflict(agent_input: AgentInput) -> bool:
    direction = str(agent_input.signal.get("direction") or
                    agent_input.signal.get("dir") or "").lower()
    features = _frozen_factor_features(agent_input)
    momentum = [_as_float(features.get(name))
                for name in ("momentum_1h", "momentum_4h")]
    usable = [value for value in momentum if value is not None]
    if direction == "long":
        return any(value < 0 for value in usable)
    if direction == "short":
        return any(value > 0 for value in usable)
    return False


def _signal_inconsistency_factors(agent_input: AgentInput) -> tuple[str, ...]:
    """Return frozen signed factors that oppose the candidate direction."""

    direction = str(agent_input.signal.get("direction") or
                    agent_input.signal.get("dir") or "").lower()
    features = _frozen_factor_features(agent_input)
    signed = [(name, _as_float(features.get(name))) for name in (
        "momentum_1h", "momentum_4h", "trend_band_atr",
        "directional_index_spread",
    )]
    if direction == "long":
        return tuple(name for name, value in signed
                     if value is not None and value < 0)
    if direction == "short":
        return tuple(name for name, value in signed
                     if value is not None and value > 0)
    return ()


def _has_signal_inconsistency(agent_input: AgentInput) -> bool:
    return bool(_signal_inconsistency_factors(agent_input))


def _has_position_risk_conflict(agent_input: AgentInput) -> bool:
    health = agent_input.health
    if health.get("risk_halted") is True or health.get("risk_can_trade") is False:
        return True
    account = agent_input.account
    current = _as_float(account.get("portfolio_notional_usdt"))
    maximum = _as_float(account.get("max_total_notional_usdt"))
    return bool(current is not None and maximum is not None and current >= maximum)


def _has_liquidity_failure(agent_input: AgentInput) -> bool:
    features = _frozen_factor_features(agent_input)
    spread = _as_float(features.get("spread_bps"))
    slippage = _as_float(features.get("expected_slippage_bps"))
    return bool(
        (spread is not None and spread >=
         config.AGENT_HARNESS_LIQUIDITY_FAILURE_MIN_SPREAD_BPS) or
        (slippage is not None and slippage >=
         config.AGENT_HARNESS_LIQUIDITY_FAILURE_MIN_SLIPPAGE_BPS)
    )


def _funding_is_adverse_cost(agent_input: AgentInput) -> bool | None:
    """Interpret funding from the frozen candidate direction."""

    direction = str(agent_input.signal.get("direction") or
                    agent_input.signal.get("dir") or "").lower()
    rate = _as_float(_frozen_factor_features(agent_input).get("funding_rate"))
    if rate is None or direction not in ("long", "short"):
        return None
    return bool((direction == "long" and rate > 0) or
                (direction == "short" and rate < 0))


def _has_news_direction_conflict(agent_input: AgentInput) -> bool | None:
    """Interpret the frozen [-1,+1] sentiment score from candidate direction."""

    direction = str(agent_input.signal.get("direction") or
                    agent_input.signal.get("dir") or "").lower()
    score = _as_float(agent_input.news.get("news_score"))
    if score is None:
        score = _as_float(agent_input.news.get("composite"))
    if score is None or direction not in ("long", "short"):
        return None
    neutral = config.AGENT_HARNESS_NEWS_NEUTRAL_SCORE
    return bool((direction == "long" and score < neutral) or
                (direction == "short" and score > neutral))


def _has_explicit_extreme_market_event(agent_input: AgentInput) -> bool:
    """Require an explicit frozen boolean; routine volatility is not an event."""

    for source in (agent_input.news, agent_input.health, agent_input.market):
        if source.get("extreme_market_event") is True:
            return True
    return False


def _initial_decision_contract(state: _GraphState) -> dict[str, Any]:
    """Expose validator-owned facts before the first bounded model call."""

    agent_input = state["agent_input"]
    qualifiers = {
        "directional_momentum_conflict":
            _has_directional_momentum_conflict(agent_input),
        "position_risk_conflict_qualified":
            _has_position_risk_conflict(agent_input),
        "liquidity_failure_qualified":
            _has_liquidity_failure(agent_input),
        "funding_is_adverse_cost":
            _funding_is_adverse_cost(agent_input),
    }
    if (agent_input.tool_policy_version in
            config.AGENT_HARNESS_NEWS_EVENT_CONTRACT_TOOL_POLICIES):
        qualifiers.update({
            "news_direction_conflict_qualified":
                _has_news_direction_conflict(agent_input),
            "extreme_market_event_qualified":
                _has_explicit_extreme_market_event(agent_input),
        })
    if (agent_input.tool_policy_version in
            config.AGENT_HARNESS_SIGNAL_CONSISTENCY_CONTRACT_TOOL_POLICIES):
        qualifiers["signal_inconsistency_qualified"] = \
            _has_signal_inconsistency(agent_input)
    factor_specific = (agent_input.tool_policy_version in
                       config.AGENT_HARNESS_FACTOR_SPECIFIC_CONTRACT_TOOL_POLICIES)
    contract = {
        "allowed_evidence_ids": sorted(_evidence_ids(state)),
        "deterministic_qualifiers": qualifiers,
        "reject_thresholds": {
            "minimum_risk_probability":
                config.AGENT_HARNESS_REJECT_MIN_RISK,
            "minimum_confidence":
                config.AGENT_HARNESS_REJECT_MIN_CONFIDENCE,
            "minimum_ordinary_risk_families":
                config.AGENT_HARNESS_MIN_ORDINARY_REJECT_FAMILIES,
        },
        "instruction": (
            "This contract is derived from the same frozen input used by the "
            "deterministic validator. Copy evidence_ids only from "
            "allowed_evidence_ids. A false qualifier cannot support its named "
            "risk family; directional_momentum_conflict only governs 1H/4H "
            "momentum claims. funding_is_adverse_cost=false means funding "
            "cannot be cited as a cost for this candidate direction. "
            "news_direction_conflict_qualified uses the frozen [-1,+1] "
            "sentiment sign. extreme_market_event_qualified requires an "
            "explicit frozen boolean and is never implied by volatility or "
            "regime alone. signal_inconsistency_qualified only uses signed "
            "1H/4H momentum, trend_band_atr and directional_index_spread; "
            "regime, volatility and strategy_route alone never qualify."),
    }
    if factor_specific:
        contract["signal_inconsistency_conflicting_factors"] = list(
            _signal_inconsistency_factors(agent_input))
    return contract


def _cites_favorable_funding(decision: AgentDecision,
                             agent_input: AgentInput) -> bool:
    text = decision.reason.lower()
    if "funding" not in text and "资金费" not in text:
        return False
    direction = str(agent_input.signal.get("direction") or
                    agent_input.signal.get("dir") or "").lower()
    rate = _as_float(_frozen_factor_features(agent_input).get("funding_rate"))
    if rate is None:
        return True
    return ((direction == "short" and rate >= 0) or
            (direction == "long" and rate <= 0))


def _validate_directional_reject_evidence(decision: AgentDecision,
                                          agent_input: AgentInput) -> None:
    """Enforce v7 direction and independent-family claims deterministically."""

    codes = set(decision.reason_codes)
    reason_text = decision.reason.lower()
    cites_momentum = "momentum" in reason_text or "动量" in reason_text
    if (agent_input.prompt_version in
            config.AGENT_HARNESS_SIGNAL_CONSISTENCY_EVIDENCE_PROMPT_VERSIONS):
        if (ReasonCode.SIGNAL_INCONSISTENCY in codes and not
                _has_signal_inconsistency(agent_input)):
            raise AgentSemanticError(
                "signal_inconsistency lacks an opposite-sign frozen factor")
        if (ReasonCode.SIGNAL_INCONSISTENCY in codes and
                agent_input.prompt_version in
                config.AGENT_HARNESS_FACTOR_SPECIFIC_REASON_PROMPT_VERSIONS):
            conflicts = set(_signal_inconsistency_factors(agent_input))
            aligned_claims = []
            if cites_momentum and not conflicts.intersection(
                    {"momentum_1h", "momentum_4h"}):
                aligned_claims.append("momentum")
            if (any(marker in reason_text for marker in
                    ("trend_band_atr", "trend band", "ema20", "ema50")) and
                    "trend_band_atr" not in conflicts):
                aligned_claims.append("trend_band_atr")
            if (any(marker in reason_text for marker in
                    ("directional_index_spread", "dmi", "+di", "-di")) and
                    "directional_index_spread" not in conflicts):
                aligned_claims.append("directional_index_spread")
            if aligned_claims:
                raise AgentSemanticError(
                    "signal_inconsistency reason cites direction-aligned "
                    "factor family: " + ",".join(aligned_claims))
    elif (ReasonCode.SIGNAL_INCONSISTENCY in codes and cites_momentum and not
          _has_directional_momentum_conflict(agent_input)):
        raise AgentSemanticError(
            "signal_inconsistency cites direction-aligned 1H/4H momentum")
    if ReasonCode.POSITION_RISK_CONFLICT in codes and not \
            _has_position_risk_conflict(agent_input):
        raise AgentSemanticError(
            "position_risk_conflict lacks frozen account or risk conflict")
    if (agent_input.prompt_version in
            config.AGENT_HARNESS_LIQUIDITY_EVIDENCE_PROMPT_VERSIONS and
            ReasonCode.LIQUIDITY_FAILURE in codes and not
            _has_liquidity_failure(agent_input)):
        raise AgentSemanticError(
            "liquidity_failure lacks severe frozen spread or expected slippage")
    if _cites_favorable_funding(decision, agent_input):
        raise AgentSemanticError(
            "reject cites funding that is favorable for the candidate direction")
    if (agent_input.prompt_version in
            config.AGENT_HARNESS_NEWS_EVENT_EVIDENCE_PROMPT_VERSIONS):
        if (ReasonCode.NEWS_DIRECTION_CONFLICT in codes and
                _has_news_direction_conflict(agent_input) is not True):
            raise AgentSemanticError(
                "news_direction_conflict lacks opposite-sign frozen sentiment")
        if (ReasonCode.EXTREME_MARKET_EVENT in codes and not
                _has_explicit_extreme_market_event(agent_input)):
            raise AgentSemanticError(
                "extreme_market_event lacks explicit frozen event flag")
        if len(set(decision.evidence_ids)) != len(decision.evidence_ids):
            raise AgentSemanticError(
                "reject evidence_ids must be unique")
    ordinary = codes - {
        ReasonCode.EXTREME_MARKET_EVENT,
        ReasonCode.INSUFFICIENT_EVIDENCE,
    }
    if (ReasonCode.EXTREME_MARKET_EVENT not in codes and
            len(ordinary) <
            config.AGENT_HARNESS_MIN_ORDINARY_REJECT_FAMILIES):
        raise AgentSemanticError(
            "reject requires two distinct ordinary risk families or one "
            "extreme_market_event")


class _Nodes:
    """Graph nodes with runtime collaborators captured outside graph state."""

    def __init__(self, *, model_call: Callable[[str], Any] | None,
                 config: HarnessConfig, policy_kernel: PolicyKernel,
                 enabled: bool, db_path: str | None, memory_limit: int,
                 tool_router: ReadOnlyToolRouter | None,
                 tool_calls: list[tuple[str, dict[str, Any]]] | None):
        self.model_call = model_call
        self.config = config
        self.policy_kernel = policy_kernel
        self.enabled = enabled
        self.db_path = db_path
        self.memory_limit = memory_limit
        self.tool_router = tool_router
        self.tool_calls = tool_calls

    def append(self, state: _GraphState, item: AgentStep) -> tuple[AgentStep, ...]:
        try:
            trace_store.record_step(item, db_path=self.db_path)
        except Exception:
            # Trace persistence must not change the baseline trading action.
            pass
        return tuple(state.get("steps", ())) + (item,)

    def context(self, state: _GraphState) -> dict[str, Any]:
        if not state["baseline_passed"]:
            return {"runtime_status": RuntimeStatus.DISABLED,
                    "serialized_context": ""}
        started = _stamp()
        try:
            context = build_context(state["agent_input"])
            serialized = serialize_context(
                state["agent_input"], max_chars=self.config.max_context_chars)
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=1,
                step_type=StepType.CONTEXT, status=StepStatus.COMPLETED,
                started_at=started, finished_at=_stamp(),
                input_hash=state["agent_input"].input_hash,
                output_hash=stable_hash(context))
            return {"serialized_context": serialized,
                    "steps": self.append(state, item)}
        except Exception as exc:
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=1,
                step_type=StepType.CONTEXT, status=StepStatus.FAILED,
                started_at=started, finished_at=_stamp(),
                error_type=type(exc).__name__, fallback_action="baseline_pass")
            return {"runtime_status": RuntimeStatus.SCHEMA_ERROR,
                    "serialized_context": "", "steps": self.append(state, item)}

    def retrieve(self, state: _GraphState) -> dict[str, Any]:
        if state["runtime_status"] is not RuntimeStatus.COMPLETED:
            return {}
        started = _stamp()
        try:
            memory = tuple(retrieve_for_input(
                state["agent_input"], limit=self.memory_limit,
                db_path=self.db_path))
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=2,
                step_type=StepType.RETRIEVE, status=StepStatus.COMPLETED,
                started_at=started, finished_at=_stamp(),
                output_hash=stable_hash(memory),
                evidence_ids=tuple(str(row.get("evidence_id")) for row in memory))
            return {"memory": memory, "steps": self.append(state, item)}
        except Exception as exc:
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=2,
                step_type=StepType.RETRIEVE, status=StepStatus.FAILED,
                started_at=started, finished_at=_stamp(),
                error_type=type(exc).__name__, fallback_action="baseline_pass")
            return {"runtime_status": RuntimeStatus.TOOL_ERROR,
                    "steps": self.append(state, item)}

    def tools(self, state: _GraphState) -> dict[str, Any]:
        if state["runtime_status"] is not RuntimeStatus.COMPLETED:
            return {}
        if self.tool_router is None or not self.tool_calls:
            return {"model_step_no": 3, "policy_step_no": 4}
        started = _stamp()
        try:
            if len(self.tool_calls) > self.config.max_tools:
                raise ToolRouterError("harness tool budget exceeded")
            self.tool_router.call_many(self.tool_calls)
            payload = tool_trace_payload(self.tool_router)
            if any(not row["ok"] for row in payload):
                raise ToolRouterError("tool call failed")
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=3,
                step_type=StepType.TOOL, status=StepStatus.COMPLETED,
                started_at=started, finished_at=_stamp(),
                output_hash=stable_hash(payload),
                evidence_ids=tuple(str(row.get("output_hash")) for row in payload))
            return {"tool_payload": payload, "model_step_no": 4,
                    "policy_step_no": 5, "steps": self.append(state, item)}
        except Exception as exc:
            payload = tool_trace_payload(self.tool_router)
            item = AgentStep(
                run_id=state["agent_input"].run_id, step_no=3,
                step_type=StepType.TOOL, status=StepStatus.FAILED,
                started_at=started, finished_at=_stamp(),
                output_hash=stable_hash(payload), error_type=type(exc).__name__,
                fallback_action="baseline_pass")
            return {"runtime_status": RuntimeStatus.TOOL_ERROR,
                    "tool_payload": payload, "model_step_no": 4,
                    "policy_step_no": 5, "steps": self.append(state, item)}

    def model(self, state: _GraphState) -> dict[str, Any]:
        if state["runtime_status"] is not RuntimeStatus.COMPLETED:
            return {}
        payload: dict[str, Any] = {
            "context": json.loads(state["serialized_context"]),
            "memory": list(state.get("memory", ())),
        }
        if state.get("tool_payload"):
            payload["tools"] = state["tool_payload"]
        if (state["agent_input"].tool_policy_version in
                config.AGENT_HARNESS_INITIAL_CONTRACT_TOOL_POLICIES):
            payload["decision_contract"] = _initial_decision_contract(state)
        retry_count = int(state.get("model_retry_count", 0))
        if retry_count:
            payload["semantic_repair"] = {
                "attempt": retry_count,
                "violations": list(state.get("semantic_errors", ())),
                "previous_response": state.get("raw_response"),
                "allowed_evidence_ids": sorted(_evidence_ids(state)),
                "instruction": (
                    "Return exactly one corrected JSON object for the same "
                    "frozen candidate, without Markdown or extra fields. "
                    "Include verdict, risk_probability, confidence, "
                    "reason_codes, evidence_ids, missing_information, "
                    "abstain_reason, and reason. Fill concrete market "
                    "missing_information when using insufficient_evidence; "
                    "do not cite model readiness, forecast calibration, or "
                    "strategy routing as evidence. Treat forecast.p_loss_prior "
                    "as one unvalidated feature, not the answer; recompute the "
                    "loss probability from the frozen market evidence and make "
                    "the verdict consistent with configured thresholds. Copy "
                    "evidence_ids only from allowed_evidence_ids, using each "
                    "identifier exactly as written."),
            }
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))
        started_at = _stamp()
        started = time.monotonic()
        common = {"prompt": prompt, "model_started_at": started_at}
        if not self.enabled or self.model_call is None:
            return {**common,
                    "runtime_status": (RuntimeStatus.DISABLED if not self.enabled
                                       else RuntimeStatus.NO_KEY),
                    "model_outcome": "skipped"}
        try:
            elapsed_total_ms = round(
                (time.monotonic() - state["started_monotonic"]) * 1000)
            remaining_ms = self.config.timeout_ms - elapsed_total_ms
            if remaining_ms <= 0:
                raise TimeoutError("harness total time budget exhausted")

            def invoke_with_budget(value):
                if getattr(self.model_call,
                           "supports_timeout_budget", False) is True:
                    return self.model_call(
                        value, timeout_seconds=max(0.001, remaining_ms / 1000.0))
                return self.model_call(value)

            runnable = RunnableLambda(invoke_with_budget).with_config(
                {"run_name": "trading_risk_critic"})
            provider_result = runnable.invoke(prompt)
            if ((time.monotonic() - state["started_monotonic"]) * 1000 >=
                    self.config.timeout_ms):
                raise TimeoutError(
                    "model response arrived after harness total time budget")
            if isinstance(provider_result, ModelCallResult):
                raw = provider_result.content
                current_usage = {
                    "input_tokens": provider_result.input_tokens,
                    "output_tokens": provider_result.output_tokens,
                    "prompt_cache_hit_tokens": provider_result.prompt_cache_hit_tokens,
                    "prompt_cache_miss_tokens": provider_result.prompt_cache_miss_tokens,
                    "estimated_cost": provider_result.estimated_cost,
                    "pricing_version": provider_result.pricing_version,
                }
            else:
                raw = provider_result
                current_usage = {}
            usage = {}
            for name in ("input_tokens", "output_tokens",
                         "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                         "estimated_cost"):
                previous = state.get(name)
                current = current_usage.get(name)
                if previous is not None or current is not None:
                    usage[name] = (previous or 0) + (current or 0)
            usage["pricing_version"] = (
                current_usage.get("pricing_version") or
                state.get("pricing_version"))
            elapsed_ms = round((time.monotonic() - started) * 1000)
            return {**common, **usage, "raw_response": raw,
                    "response_hash": stable_hash(raw),
                    "model_latency_ms": (
                        int(state.get("model_latency_ms") or 0) + elapsed_ms),
                    "model_outcome": "completed", "retry_model": False,
                    "semantic_errors": ()}
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            return {**common, "runtime_status": _runtime_error(exc),
                    "model_error_type": type(exc).__name__,
                    "model_latency_ms": (
                        int(state.get("model_latency_ms") or 0) + elapsed_ms),
                    "model_outcome": "failed", "retry_model": False,
                    "semantic_errors": ()}

    def validate(self, state: _GraphState) -> dict[str, Any]:
        outcome = state.get("model_outcome")
        if not outcome:
            return {}
        step_no = int(state.get("model_step_no", 3))
        input_hash = stable_hash(state.get("prompt", ""))
        base = dict(run_id=state["agent_input"].run_id, step_no=step_no,
                    step_type=StepType.MODEL,
                    started_at=state["model_started_at"], finished_at=_stamp(),
                    input_hash=input_hash)
        if outcome == "skipped":
            item = AgentStep(**base, status=StepStatus.SKIPPED,
                             fallback_action="baseline_pass")
            return {"retry_model": False, "steps": self.append(state, item)}
        if outcome == "failed":
            item = AgentStep(**base, status=StepStatus.FAILED,
                             error_type=state.get("model_error_type"),
                             fallback_action="baseline_pass")
            return {"retry_model": False, "steps": self.append(state, item)}
        try:
            decision = _domain_decision(state.get("raw_response"))
            _validate_decision_semantics(decision, state)
            item = AgentStep(**base, status=StepStatus.COMPLETED,
                             output_hash=state.get("response_hash"),
                             retry_count=int(state.get("model_retry_count", 0)))
            return {"decision": decision, "retry_model": False,
                    "semantic_errors": (), "steps": self.append(state, item)}
        except AgentSemanticError as exc:
            retry_count = int(state.get("model_retry_count", 0))
            item = AgentStep(
                **base, status=StepStatus.FAILED,
                output_hash=state.get("response_hash"),
                retry_count=retry_count,
                error_type=f"AgentSemanticError:{exc}",
                fallback_action="baseline_pass")
            steps = self.append(state, item)
            if retry_count < max(0, self.config.max_semantic_retries):
                return {
                    "decision": None, "retry_model": True,
                    "semantic_errors": (str(exc),),
                    "model_retry_count": retry_count + 1,
                    "model_step_no": step_no + 1,
                    "policy_step_no": int(
                        state.get("policy_step_no", step_no + 1)) + 1,
                    "steps": steps,
                }
            return {"runtime_status": RuntimeStatus.SCHEMA_ERROR,
                    "decision": None, "retry_model": False,
                    "semantic_errors": (str(exc),), "steps": steps}
        except Exception as exc:
            retry_count = int(state.get("model_retry_count", 0))
            error = f"{type(exc).__name__}:{str(exc)[:240]}"
            item = AgentStep(
                **base, status=StepStatus.FAILED,
                output_hash=state.get("response_hash"),
                retry_count=retry_count, error_type=error,
                fallback_action="baseline_pass")
            steps = self.append(state, item)
            # Malformed/truncated JSON is repairable in the same way as a
            # semantic violation.  It remains fail-closed after the bounded
            # retry and can never turn an invalid reject into a veto.
            if retry_count < max(0, self.config.max_semantic_retries):
                return {
                    "decision": None, "retry_model": True,
                    "semantic_errors": (error,),
                    "model_retry_count": retry_count + 1,
                    "model_step_no": step_no + 1,
                    "policy_step_no": int(
                        state.get("policy_step_no", step_no + 1)) + 1,
                    "steps": steps,
                }
            return {"runtime_status": RuntimeStatus.SCHEMA_ERROR,
                    "decision": None, "retry_model": False,
                    "semantic_errors": (error,), "steps": steps}

    def policy(self, state: _GraphState) -> dict[str, Any]:
        policy = self.policy_kernel.evaluate(
            baseline_passed=state["baseline_passed"],
            runtime_status=state["runtime_status"],
            decision=state.get("decision"))
        if not state["baseline_passed"]:
            return {"policy": policy}
        started = _stamp()
        item = AgentStep(
            run_id=state["agent_input"].run_id,
            step_no=int(state.get("policy_step_no", 4)),
            step_type=StepType.POLICY, status=StepStatus.COMPLETED,
            started_at=started, finished_at=_stamp(),
            output_hash=stable_hash(policy.final_action.value),
            fallback_action="baseline_pass" if not policy.veto else None)
        return {"policy": policy, "steps": self.append(state, item)}

    def record(self, state: _GraphState) -> dict[str, Any]:
        decision = state.get("decision")
        if not state["baseline_passed"]:
            run = HarnessRun(
                state["agent_input"].run_id, state["agent_input"].signal_id,
                state["runtime_status"], state["policy"].final_action,
                error_type="baseline_reject",
                input_hash=state["agent_input"].input_hash)
        else:
            runtime = state["runtime_status"]
            run = HarnessRun(
                run_id=state["agent_input"].run_id,
                signal_id=state["agent_input"].signal_id,
                runtime_status=runtime, final_action=state["policy"].final_action,
                model_verdict=decision.verdict if decision else None,
                input_hash=state["agent_input"].input_hash,
                response_hash=state.get("response_hash"),
                latency_ms=round(
                    (time.monotonic() - state["started_monotonic"]) * 1000),
                model_latency_ms=state.get("model_latency_ms"),
                input_tokens=state.get("input_tokens"),
                output_tokens=state.get("output_tokens"),
                prompt_cache_hit_tokens=state.get("prompt_cache_hit_tokens"),
                prompt_cache_miss_tokens=state.get("prompt_cache_miss_tokens"),
                pricing_version=(state.get("pricing_version") or
                                 state["agent_input"].pricing_version),
                estimated_cost=state.get("estimated_cost"),
                risk_probability=decision.risk_probability if decision else None,
                confidence=decision.confidence if decision else None,
                reason_codes=decision.reason_codes if decision else (),
                evidence_ids=decision.evidence_ids if decision else (),
                missing_information=(decision.missing_information
                                     if decision else ()),
                abstain_reason=decision.abstain_reason if decision else None,
                decision_reason=decision.reason if decision else None,
                error_type=(None if runtime in (
                    RuntimeStatus.COMPLETED, RuntimeStatus.DISABLED,
                    RuntimeStatus.NO_KEY) else runtime.value))
        try:
            trace_store.record_run(run, state["agent_input"], db_path=self.db_path)
            if state["baseline_passed"]:
                trace_store.record_evaluation(run.run_id, db_path=self.db_path)
        except Exception as exc:
            # Trace 是 veto 的审计前提。持久化失败不能静默，更不能让一个
            # 无法追溯的 Agent reject 改变基线交易动作。
            error = f"TracePersistenceError:{type(exc).__name__}"
            print(f"Agent Harness trace persistence failed: {error}: {exc}",
                  flush=True)
            run = replace(run, runtime_status=RuntimeStatus.TOOL_ERROR,
                          final_action=FinalAction.BASELINE_PASS,
                          model_verdict=None, error_type=error)
            policy = PolicyResult(
                final_action=FinalAction.BASELINE_PASS, veto=False,
                reason="trace persistence failed; baseline preserved")
            return {"run": run, "policy": policy}
        return {"run": run}


def build_harness_graph(*, model_call: Callable[[str], Any] | None,
                        config: HarnessConfig, policy_kernel: PolicyKernel,
                        enabled: bool, db_path: str | None, memory_limit: int,
                        tool_router: ReadOnlyToolRouter | None,
                        tool_calls: list[tuple[str, dict[str, Any]]] | None):
    """Compile the one shared paper/live Harness graph."""

    nodes = _Nodes(
        model_call=model_call, config=config, policy_kernel=policy_kernel,
        enabled=enabled, db_path=db_path, memory_limit=memory_limit,
        tool_router=tool_router, tool_calls=tool_calls)
    graph = StateGraph(_GraphState)
    graph.add_node("context", nodes.context)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("tools", nodes.tools)
    graph.add_node("model", nodes.model)
    graph.add_node("validate", nodes.validate)
    graph.add_node("policy", nodes.policy)
    graph.add_node("record", nodes.record)
    graph.add_edge(START, "context")
    graph.add_edge("context", "retrieve")
    graph.add_edge("retrieve", "tools")
    graph.add_edge("tools", "model")
    graph.add_edge("model", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda state: "retry" if state.get("retry_model") else "policy",
        {"retry": "model", "policy": "policy"})
    graph.add_edge("policy", "record")
    graph.add_edge("record", END)
    return graph.compile()


def run_graph_harness(agent_input: AgentInput, *, baseline_passed: bool,
                      model_call: Callable[[str], Any] | None,
                      config: HarnessConfig | None = None,
                      policy_kernel: PolicyKernel | None = None,
                      enabled: bool = True, db_path: str | None = None,
                      memory_limit: int = 5,
                      tool_router: ReadOnlyToolRouter | None = None,
                      tool_calls: list[tuple[str, dict[str, Any]]] | None = None) -> HarnessResult:
    """Run the only Harness implementation; default policy remains shadow."""

    # A later baseline rejection must never be replaced by an earlier Agent
    # result that happened to share the same stable run id. Baseline remains
    # the higher-authority gate and also avoids a second model call below.
    if baseline_passed:
        existing = _durable_result(agent_input, db_path=db_path)
        if existing is not None:
            return existing
    cfg = config or HarnessConfig()
    kernel = policy_kernel or PolicyKernel(veto_enabled=False, shadow=True)
    graph = build_harness_graph(
        model_call=model_call, config=cfg, policy_kernel=kernel,
        enabled=enabled, db_path=db_path, memory_limit=memory_limit,
        tool_router=tool_router, tool_calls=tool_calls)
    final = graph.invoke({
        "agent_input": agent_input, "baseline_passed": bool(baseline_passed),
        "runtime_status": RuntimeStatus.COMPLETED,
        "serialized_context": "", "memory": (), "tool_payload": [],
        "prompt": "", "decision": None, "steps": (),
        "model_step_no": 3, "policy_step_no": 4,
        "model_retry_count": 0, "semantic_errors": (),
        "retry_model": False,
        "started_monotonic": time.monotonic()})
    return HarnessResult(
        run=final["run"], decision=final.get("decision"),
        policy=final["policy"], prompt=final.get("prompt", ""),
        memory=tuple(final.get("memory", ())))


__all__ = ["HarnessResult", "build_harness_graph", "run_graph_harness"]
