"""Small deterministic orchestration loop for the trading Agent Harness."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from decision.agent_context import build_context, serialize_context
from decision.agent_contracts import (
    AgentDecision,
    AgentInput,
    AgentStep,
    FinalAction,
    HarnessConfig,
    HarnessRun,
    RuntimeStatus,
    StepStatus,
    StepType,
    strict_parse_model_output,
    stable_hash,
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


def _runtime_error(exc: Exception) -> RuntimeStatus:
    if isinstance(exc, TimeoutError):
        return RuntimeStatus.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return RuntimeStatus.HTTP_ERROR
    return RuntimeStatus.TOOL_ERROR if exc.__class__.__name__.lower().endswith("toolerror") else RuntimeStatus.HTTP_ERROR


def run_harness(agent_input: AgentInput, *, baseline_passed: bool,
                model_call: Callable[[str], Any] | None,
                config: HarnessConfig | None = None,
                policy_kernel: PolicyKernel | None = None,
                enabled: bool = True,
                db_path: str | None = None,
                memory_limit: int = 5,
                tool_router: ReadOnlyToolRouter | None = None,
                tool_calls: list[tuple[str, dict[str, Any]]] | None = None) -> HarnessResult:
    """Run one candidate through context, memory, model validation and policy.

    The caller owns the baseline rule gate.  This function never calls an
    exchange adapter and never returns an order instruction.
    """

    cfg = config or HarnessConfig()
    kernel = policy_kernel or PolicyKernel(veto_enabled=False, shadow=True)
    started = time.monotonic()
    steps: list[AgentStep] = []
    decision: AgentDecision | None = None
    runtime = RuntimeStatus.COMPLETED
    response_hash: str | None = None
    prompt = ""
    memory: tuple[dict[str, Any], ...] = ()

    def step(step_no: int, kind: StepType, status: StepStatus, start: str,
             *, finish: str | None = None, error: str | None = None,
             fallback: str | None = None, input_hash: str | None = None,
             output_hash: str | None = None, evidence: tuple[str, ...] = ()) -> None:
        item = AgentStep(run_id=agent_input.run_id, step_no=step_no, step_type=kind,
                         status=status, started_at=start, finished_at=finish,
                         input_hash=input_hash, output_hash=output_hash,
                         error_type=error, fallback_action=fallback,
                         evidence_ids=evidence)
        steps.append(item)
        try:
            trace_store.record_step(item, db_path=db_path)
        except Exception:
            # Trace persistence cannot make a trading scan fail open/closed.
            pass

    if not baseline_passed:
        runtime = RuntimeStatus.DISABLED
        policy = kernel.evaluate(baseline_passed=False, runtime_status=runtime, decision=None)
        run = HarnessRun(agent_input.run_id, agent_input.signal_id, runtime,
                         policy.final_action, error_type="baseline_reject",
                         input_hash=agent_input.input_hash)
        result = HarnessResult(run, None, policy, "", (),)
        try:
            trace_store.record_run(run, agent_input, db_path=db_path)
        except Exception:
            pass
        return result

    context_started = _stamp()
    try:
        context = build_context(agent_input)
        serialized = serialize_context(agent_input, max_chars=cfg.max_context_chars)
        step(1, StepType.CONTEXT, StepStatus.COMPLETED, context_started,
             finish=_stamp(), input_hash=agent_input.input_hash,
             output_hash=stable_hash(context))
    except Exception as exc:
        runtime = RuntimeStatus.SCHEMA_ERROR
        step(1, StepType.CONTEXT, StepStatus.FAILED, context_started,
             finish=_stamp(), error=type(exc).__name__, fallback="baseline_pass")
        serialized = ""

    if runtime is RuntimeStatus.COMPLETED:
        retrieve_started = _stamp()
        try:
            memory = tuple(retrieve_for_input(agent_input, limit=memory_limit, db_path=db_path))
            step(2, StepType.RETRIEVE, StepStatus.COMPLETED, retrieve_started,
                 finish=_stamp(), output_hash=stable_hash(memory),
                 evidence=tuple(str(x.get("evidence_id")) for x in memory))
        except Exception as exc:
            runtime = RuntimeStatus.TOOL_ERROR
            step(2, StepType.RETRIEVE, StepStatus.FAILED, retrieve_started,
                 finish=_stamp(), error=type(exc).__name__, fallback="baseline_pass")

    tool_payload: list[dict[str, Any]] = []
    model_step_no = 3
    policy_step_no = 4
    if runtime is RuntimeStatus.COMPLETED and tool_router is not None and tool_calls:
        tool_started = _stamp()
        model_step_no = 4
        policy_step_no = 5
        try:
            if len(tool_calls) > cfg.max_tools:
                raise ToolRouterError("harness tool budget exceeded")
            tool_router.call_many(tool_calls)
            tool_payload = tool_trace_payload(tool_router)
            if any(not item["ok"] for item in tool_payload):
                raise ToolRouterError("tool call failed")
            step(3, StepType.TOOL, StepStatus.COMPLETED, tool_started,
                 finish=_stamp(), output_hash=stable_hash(tool_payload),
                 evidence=tuple(str(item.get("output_hash")) for item in tool_payload))
        except Exception as exc:
            runtime = RuntimeStatus.TOOL_ERROR
            tool_payload = tool_trace_payload(tool_router)
            step(3, StepType.TOOL, StepStatus.FAILED, tool_started,
                 finish=_stamp(), error=type(exc).__name__, fallback="baseline_pass",
                 output_hash=stable_hash(tool_payload))

    if runtime is RuntimeStatus.COMPLETED:
        prompt = json.dumps({"context": json.loads(serialized), "memory": list(memory)},
                            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if tool_payload:
            prompt = json.dumps({"context": json.loads(serialized), "memory": list(memory),
                                 "tools": tool_payload}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
        model_started = _stamp()
        if not enabled or model_call is None:
            runtime = RuntimeStatus.DISABLED if not enabled else RuntimeStatus.NO_KEY
            step(model_step_no, StepType.MODEL, StepStatus.SKIPPED, model_started,
                 finish=_stamp(), fallback="baseline_pass", input_hash=stable_hash(prompt))
        else:
            try:
                raw = model_call(prompt)
                response_hash = stable_hash(raw)
                decision = strict_parse_model_output(raw)
                step(model_step_no, StepType.MODEL, StepStatus.COMPLETED, model_started,
                     finish=_stamp(), input_hash=stable_hash(prompt),
                     output_hash=response_hash)
                runtime = RuntimeStatus.COMPLETED
            except ValueError as exc:
                runtime = RuntimeStatus.SCHEMA_ERROR
                step(model_step_no, StepType.MODEL, StepStatus.FAILED, model_started,
                     finish=_stamp(), error=type(exc).__name__, fallback="baseline_pass",
                     input_hash=stable_hash(prompt))
            except Exception as exc:
                runtime = _runtime_error(exc)
                step(model_step_no, StepType.MODEL, StepStatus.FAILED, model_started,
                     finish=_stamp(), error=type(exc).__name__, fallback="baseline_pass",
                     input_hash=stable_hash(prompt))

    policy = kernel.evaluate(baseline_passed=True, runtime_status=runtime, decision=decision)
    policy_started = _stamp()
    step(policy_step_no, StepType.POLICY, StepStatus.COMPLETED, policy_started,
         finish=_stamp(), output_hash=stable_hash(policy.final_action.value),
         fallback="baseline_pass" if not policy.veto else None)
    run = HarnessRun(
        run_id=agent_input.run_id, signal_id=agent_input.signal_id,
        runtime_status=runtime, final_action=policy.final_action,
        model_verdict=decision.verdict if decision else None,
        input_hash=agent_input.input_hash, response_hash=response_hash,
        latency_ms=round((time.monotonic() - started) * 1000),
        risk_probability=decision.risk_probability if decision else None,
        reason_codes=decision.reason_codes if decision else (),
        error_type=None if runtime in (RuntimeStatus.COMPLETED, RuntimeStatus.DISABLED, RuntimeStatus.NO_KEY)
        else runtime.value)
    try:
        trace_store.record_run(run, agent_input, db_path=db_path)
        trace_store.record_evaluation(run.run_id, db_path=db_path)
    except Exception:
        pass
    return HarnessResult(run, decision, policy, prompt, memory)
