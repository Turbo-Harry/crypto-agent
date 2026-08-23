"""Policy lifecycle facade with conservative promotion and rollback gates."""

from __future__ import annotations

from typing import Any, Mapping

from storage.agent_lifecycle import (
    get, promotion_ready, register, rollback_needed, transition,
)


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


__all__ = ["activate", "get", "observe", "promotion_ready", "register", "validate"]
