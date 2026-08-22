"""Fail-closed, read-only tool router for the Agent Harness."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from decision.agent_contracts import canonical_json, stable_hash


class ToolRouterError(RuntimeError):
    pass


class ToolNotAllowed(ToolRouterError):
    pass


class ToolBudgetExceeded(ToolRouterError):
    pass


class ToolSchemaError(ToolRouterError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[Mapping[str, Any]], Any]
    required_keys: tuple[str, ...] = ()
    max_output_chars: int = 8000


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    input_hash: str
    output_hash: str | None
    output: Any = None
    error_type: str | None = None
    elapsed_ms: int = 0


class ReadOnlyToolRouter:
    """Execute only injected, pre-registered snapshot readers.

    The router intentionally accepts handlers rather than exchange/database
    objects.  This keeps the harness unable to discover execution methods or
    run arbitrary SQL/URLs/files from model-controlled input.
    """

    def __init__(self, tools: Mapping[str, ToolSpec | Callable[[Mapping[str, Any]], Any]], *,
                 allowed_tools: tuple[str, ...] = (), max_calls: int = 3,
                 deadline_ms: int = 4000):
        self._tools: dict[str, ToolSpec] = {}
        for name, value in tools.items():
            if isinstance(value, ToolSpec):
                spec = value
            else:
                spec = ToolSpec(name=name, handler=value)
            if spec.name != name:
                raise ValueError("tool key/name mismatch")
            self._tools[name] = spec
        self.allowed_tools = tuple(allowed_tools) or tuple(self._tools)
        self.max_calls = max(0, int(max_calls))
        self.deadline_ms = max(1, int(deadline_ms))
        self.calls = 0
        self.started = time.monotonic()
        self.trace: list[ToolResult] = []

    def _check_budget(self) -> None:
        if self.calls >= self.max_calls:
            raise ToolBudgetExceeded("tool call budget exhausted")
        if (time.monotonic() - self.started) * 1000 >= self.deadline_ms:
            raise ToolBudgetExceeded("tool time budget exhausted")

    def call(self, name: str, args: Mapping[str, Any] | None = None) -> ToolResult:
        self._check_budget()
        if name not in self.allowed_tools or name not in self._tools:
            raise ToolNotAllowed(name)
        if not isinstance(args, Mapping):
            raise ToolSchemaError("tool args must be an object")
        spec = self._tools[name]
        missing = [key for key in spec.required_keys if key not in args]
        if missing:
            raise ToolSchemaError(f"missing tool keys: {','.join(missing)}")
        # Canonicalization is both a JSON-schema boundary and an audit hash.
        try:
            input_hash = stable_hash(dict(args))
        except (TypeError, ValueError) as exc:
            raise ToolSchemaError("tool args must be JSON-safe") from exc
        self.calls += 1
        started = time.monotonic()
        try:
            output = spec.handler(dict(args))
            encoded = canonical_json(output)
            if len(encoded) > spec.max_output_chars:
                raise ToolSchemaError("tool output exceeds configured bound")
            result = ToolResult(name=name, ok=True, input_hash=input_hash,
                                output_hash=stable_hash(output), output=output,
                                elapsed_ms=round((time.monotonic() - started) * 1000))
        except ToolRouterError as exc:
            result = ToolResult(name=name, ok=False, input_hash=input_hash,
                                output_hash=None, error_type=type(exc).__name__,
                                elapsed_ms=round((time.monotonic() - started) * 1000))
            self.trace.append(result)
            raise
        except Exception as exc:
            result = ToolResult(name=name, ok=False, input_hash=input_hash,
                                output_hash=None, error_type=type(exc).__name__,
                                elapsed_ms=round((time.monotonic() - started) * 1000))
        self.trace.append(result)
        return result

    def call_many(self, calls: list[tuple[str, Mapping[str, Any]]]) -> list[ToolResult]:
        results = []
        for name, args in calls:
            results.append(self.call(name, args))
        return results


def snapshot_tools(*, signal: Callable[[Mapping[str, Any]], Any] | None = None,
                   regime: Callable[[Mapping[str, Any]], Any] | None = None,
                   risk: Callable[[Mapping[str, Any]], Any] | None = None,
                   **extra: Callable[[Mapping[str, Any]], Any]) -> dict[str, ToolSpec]:
    """Build the approved standard tool set from caller-owned read functions."""

    handlers: dict[str, Callable[[Mapping[str, Any]], Any] | None] = {
        "get_signal_snapshot": signal,
        "get_market_regime": regime,
        "get_risk_state": risk,
    }
    handlers.update(extra)
    return {name: ToolSpec(name=name, handler=handler)
            for name, handler in handlers.items() if handler is not None}


def tool_trace_payload(router: ReadOnlyToolRouter) -> list[dict[str, Any]]:
    return [{"name": item.name, "ok": item.ok, "input_hash": item.input_hash,
             "output_hash": item.output_hash, "error_type": item.error_type,
             "elapsed_ms": item.elapsed_ms} for item in router.trace]

