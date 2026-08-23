"""Compatibility facade for the single LangGraph/LangChain Harness runtime.

The handwritten orchestration loop has been removed. Callers retain the
stable ``run_harness`` API while paper and live resolve to the same graph.
"""

from __future__ import annotations

from typing import Any, Callable

from decision.agent_contracts import AgentInput, HarnessConfig
from decision.agent_graph import HarnessResult, run_graph_harness
from decision.agent_policy import PolicyKernel
from decision.agent_tools import ReadOnlyToolRouter


def run_harness(agent_input: AgentInput, *, baseline_passed: bool,
                model_call: Callable[[str], Any] | None,
                config: HarnessConfig | None = None,
                policy_kernel: PolicyKernel | None = None,
                enabled: bool = True, db_path: str | None = None,
                memory_limit: int = 5,
                tool_router: ReadOnlyToolRouter | None = None,
                tool_calls: list[tuple[str, dict[str, Any]]] | None = None) -> HarnessResult:
    return run_graph_harness(
        agent_input, baseline_passed=baseline_passed, model_call=model_call,
        config=config, policy_kernel=policy_kernel, enabled=enabled,
        db_path=db_path, memory_limit=memory_limit, tool_router=tool_router,
        tool_calls=tool_calls)


__all__ = ["HarnessResult", "run_harness"]
