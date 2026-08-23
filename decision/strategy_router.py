"""行情状态到策略候选的影子路由；永不直接产生下单许可。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

import config


def route_strategy(regime: Mapping[str, Any],
                   available: Iterable[str] = ()) -> Dict[str, Any]:
    """返回可审计路由结果；低置信度、混乱或未实现策略一律 abstain。"""
    available_set = set(available or config.MARKET_REGIME_IMPLEMENTED_STRATEGIES)
    state = str((regime or {}).get("state") or "unknown")
    confidence = float((regime or {}).get("confidence") or 0.0)
    margin = float((regime or {}).get("margin") or 0.0)
    eligible = [name for name in config.MARKET_REGIME_STRATEGY_MAP.get(state, ())
                if name in available_set]
    reason = "shadow_only"
    selected = eligible[0] if eligible else None
    if not (regime or {}).get("ready"):
        selected, reason = None, "regime_not_ready"
    elif state == "disorder":
        selected, reason = None, "disorder_abstain"
    elif confidence < config.MARKET_REGIME_ROUTE_MIN_CONFIDENCE:
        selected, reason = None, "low_confidence"
    elif margin < config.MARKET_REGIME_ROUTE_MIN_MARGIN:
        selected, reason = None, "low_margin"
    elif not eligible:
        selected, reason = None, "strategy_not_implemented"
    return {
        "version": config.MARKET_REGIME_VERSION,
        "mode": "shadow",
        "state": state,
        "selected_strategy": selected,
        "eligible_strategies": eligible,
        "abstain": selected is None,
        "reason": reason,
        "has_execution_authority": False,
    }
