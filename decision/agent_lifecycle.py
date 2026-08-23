"""Policy lifecycle facade with conservative promotion and rollback gates."""

from __future__ import annotations

from typing import Any, Mapping

import config
from storage.agent_lifecycle import (
    get, promotion_ready, register, rollback_needed, transition,
)


def version_for_identity(*, strategy_id: str, model_version: str,
                         prompt_version: str, context_version: str,
                         schema_version: str, retrieval_version: str,
                         tool_policy_version: str,
                         pricing_version: str) -> str:
    """Return the one auditable identity used by evaluation and execution."""
    return config.AGENT_EVALUATION_VERSION + ":harness:" + ":".join(
        str(value or "unknown") for value in (
            strategy_id, model_version, prompt_version, context_version,
            schema_version, retrieval_version, tool_policy_version,
            pricing_version))


def veto_effective(version: str, *, strategy_id: str | None = None,
                   db_path: str | None = None) -> bool:
    """Authorization intent is insufficient without a promoted version."""
    if not config.AGENT_HARNESS_VETO_ENABLED:
        return False
    try:
        row = get(version, strategy_id=strategy_id, db_path=db_path)
    except Exception:
        return False
    return bool(row and row.get("status") in
                {"active-veto", "observing", "kept"})


def validate(version: str, metrics: Mapping[str, Any], *,
             strategy_id: str | None = None,
             db_path: str | None = None) -> dict[str, Any]:
    ok, reason = promotion_ready(metrics)
    return transition(version, "validated" if ok else "rolled-back",
                      reason=reason, metrics=metrics,
                      strategy_id=strategy_id, db_path=db_path)


def activate(version: str, *, strategy_id: str | None = None,
             db_path: str | None = None) -> dict[str, Any]:
    return transition(version, "active-veto", reason="validated sample gate",
                      strategy_id=strategy_id, db_path=db_path)


def observe(version: str, metrics: Mapping[str, Any], *,
            strategy_id: str | None = None,
            db_path: str | None = None) -> dict[str, Any]:
    bad, reason = rollback_needed(metrics)
    return transition(version, "rolled-back" if bad else "kept", reason=reason,
                      metrics=metrics, strategy_id=strategy_id, db_path=db_path)


__all__ = [
    "activate", "get", "observe", "promotion_ready", "register", "validate",
    "version_for_identity", "veto_effective",
]
