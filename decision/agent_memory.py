"""Decision-layer facade for evidence-scoped Agent Harness memory."""

from __future__ import annotations

from typing import Any, Mapping

from decision.agent_contracts import AgentInput
from storage.agent_memory import promote_mature_legacy_memories, retrieve


def retrieve_for_input(agent_input: AgentInput, *, limit: int = 5,
                       db_path: str | None = None) -> list[dict[str, Any]]:
    signal = agent_input.signal
    market = agent_input.market
    return retrieve({
        "base": signal.get("base") or signal.get("symbol"),
        "direction": signal.get("direction") or signal.get("dir"),
        "timeframe": signal.get("timeframe"),
        "regime": market.get("regime"),
        "strategy_version": agent_input.strategy_version,
        "as_of_ts": agent_input.event_ts if isinstance(agent_input.event_ts, (int, float)) else None,
    }, limit=limit, db_path=db_path)


def refresh(db_path: str | None = None, *, min_age_hours: float = 24.0) -> int:
    return promote_mature_legacy_memories(db_path=db_path, min_age_hours=min_age_hours)

