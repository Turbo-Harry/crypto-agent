"""Deterministic context construction for Agent Harness replays."""

from __future__ import annotations

import json
from typing import Any

from decision.agent_contracts import AgentInput, canonical_json, stable_hash


CONTEXT_SECTIONS = (
    "identity",
    "versions",
    "signal",
    "market",
    "news",
    "account",
    "health",
    "memory",
    "field_provenance",
)


def build_context(agent_input: AgentInput) -> dict[str, Any]:
    """Build a fixed-order, JSON-safe snapshot without consulting live state."""

    source = agent_input.to_dict()
    return {
        "identity": {
            "run_id": source["run_id"],
            "signal_id": source["signal_id"],
            "event_ts": source["event_ts"],
            "kline_ts": source["kline_ts"],
            "strategy_version": source["strategy_version"],
        },
        "versions": {
            "prompt_version": source["prompt_version"],
            "model_version": source["model_version"],
            "context_version": source["context_version"],
            "schema_version": source["schema_version"],
            "retrieval_version": source["retrieval_version"],
        },
        "signal": source["signal"],
        "market": source["market"],
        "news": source["news"],
        "account": source["account"],
        "health": source["health"],
        "memory": list(source["memory"]),
        "field_provenance": source["field_provenance"],
    }


def serialize_context(agent_input: AgentInput, *, max_chars: int = 24000) -> str:
    """Serialize a snapshot and reject oversized context before model use."""

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    payload = build_context(agent_input)
    encoded = canonical_json(payload)
    if len(encoded) > max_chars:
        raise ValueError(f"context exceeds max_chars ({len(encoded)} > {max_chars})")
    return encoded


def context_hash(agent_input: AgentInput, *, max_chars: int = 24000) -> str:
    return stable_hash(json.loads(serialize_context(agent_input, max_chars=max_chars)))


def missing_fields(agent_input: AgentInput) -> tuple[str, ...]:
    """Return explicit missing/empty top-level evidence, in stable order."""

    payload = build_context(agent_input)
    missing: list[str] = []
    for section in ("signal", "market", "news", "account", "health"):
        if not payload[section]:
            missing.append(section)
    if not payload["field_provenance"]:
        missing.append("field_provenance")
    return tuple(missing)

