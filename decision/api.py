"""Stable application-facing API for decision and research capabilities.

Imports are intentionally lazy: creating the HTTP app or OpenAPI schema must
not initialize models, storage, providers, or trading state.
"""

from __future__ import annotations

from typing import Any


def list_agent_proposals(*, limit: int, db_path: str | None) -> dict[str, Any]:
    from decision.agent_proposals import list_proposals
    return list_proposals(limit=limit, db_path=db_path)


def evaluate_agent(db_path: str | None) -> dict[str, Any]:
    from decision.agent_evaluation import evaluate_agent as evaluate
    return evaluate(db_path)


def evaluate_harness(db_path: str | None) -> dict[str, Any]:
    from decision.agent_evaluation import evaluate_harness as evaluate
    return evaluate(db_path)


def scan_evolution_snapshot(db_path: str | None) -> dict[str, Any]:
    from decision.scan_evolve import snapshot
    return snapshot(db_path)


def approve_scan_evolution(db_path: str | None):
    from decision.scan_evolve import approve
    return approve(db_path=db_path)


def rollback_scan_evolution(db_path: str | None):
    from decision.scan_evolve import rollback
    return rollback(db_path=db_path)


def weight_evolution_snapshot(db_path: str | None) -> dict[str, Any]:
    from decision.weight_evolve import snapshot
    return snapshot(db_path)


def propose_weight_evolution(db_path: str | None):
    from decision.weight_evolve import propose
    return propose(db_path=db_path, force=True)


def approve_weight_evolution(db_path: str | None):
    from decision.weight_evolve import approve
    return approve(db_path=db_path)


def rollback_weight_evolution(db_path: str | None):
    from decision.weight_evolve import rollback
    return rollback(db_path=db_path)


def model_snapshot(db_path: str | None) -> dict[str, Any]:
    from decision.model_lifecycle import snapshot
    return snapshot(db_path)


def rollback_entry_model(db_path: str | None):
    from decision.model_lifecycle import rollback
    return rollback(db_path=db_path)


def release_loss_cooling(db_path: str | None) -> bool:
    from decision.loss_cooling import release
    return release(db_path)


def forecast_calibration(db_path: str | None) -> dict[str, Any]:
    from decision.forecast import calibration
    return calibration(db_path)


def entry_accuracy_status(db_path: str | None, *,
                          strategy_id: str) -> dict[str, Any]:
    from decision.entry_accuracy_audit import audit_status
    return audit_status(db_path, strategy_id=strategy_id)


def live_readiness(db_path: str | None) -> dict[str, Any]:
    from decision.readiness import readiness_status
    return readiness_status(db_path)


def experience_combo_stats(db_path: str | None, *,
                           min_samples: int) -> list[dict[str, Any]]:
    from decision.experience_scoring import combo_stats
    return combo_stats(db_path, min_samples=min_samples)
