"""Stable strategy/config identity shared by decision and engine modules."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, Tuple

import config


CONFIG_FIELDS = (
    "SIGNAL_FEATURE_SCHEMA_VERSION",
    "SIGNAL_SCORE", "DECIDE_MIN_SCORE", "THRESHOLD_INITIAL",
    "REJECT_WICK_RATIO", "STOP_ATR_MULT", "TP_ATR_MULT",
    "MTF_ENABLED", "SHADOW_WEIGHTS", "SHADOW_VOL_LOOKBACK",
    "SHADOW_BOOK_DEPTH", "FLAG_USE_SHADOW_SCORE_GATE",
    "SIGNAL_SAMPLE_TIMEFRAME", "SIGNAL_CONTEXT_TIMEFRAME",
    "SIGNAL_REGIME_TIMEFRAME", "MAX_HOLD_HOURS",
    "SIGNAL_OUTCOME_HORIZON_HOURS",
    "FEE_RATE_TAKER", "SLIPPAGE", "FUNDING_EXPECTED_INTERVAL_HOURS",
    "ENTRY_COST_MODEL_VERSION",
    "FORECAST_BAR", "FORECAST_HORIZON_BARS", "FORECAST_HORIZON_HOURS",
    "MARKET_REGIME_VERSION", "MARKET_REGIME_TREND_SLOPE_REF",
    "MARKET_REGIME_TF4H_SPREAD_REF", "MARKET_REGIME_VOL_INSTABILITY_REF",
    "MARKET_REGIME_SOFTMAX_TEMPERATURE",
    "MARKET_REGIME_ROUTE_MIN_CONFIDENCE", "MARKET_REGIME_ROUTE_MIN_MARGIN",
    "MARKET_REGIME_MIN_CORE_INPUTS", "MARKET_REGIME_STRATEGY_MAP",
    "MARKET_REGIME_IMPLEMENTED_STRATEGIES",
    "AGENT_PROPOSAL_PROMPT_VERSION", "AGENT_PROPOSAL_SCHEMA_VERSION",
    "AGENT_PROPOSAL_MAX_SYMBOLS", "AGENT_PROPOSAL_MAX_PROPOSALS",
    "AGENT_PROPOSAL_MIN_CONFIDENCE", "AGENT_PROPOSAL_MIN_BARS",
    "AGENT_PROPOSAL_THESIS_MAX_CHARS", "AGENT_PROPOSAL_MAX_OUTPUT_TOKENS",
    "AGENT_PROPOSAL_TEMPERATURE",
)

AGENT_PROPOSAL_CONFIG_FIELDS = (
    "AGENT_PROPOSAL_PROMPT_VERSION", "AGENT_PROPOSAL_SCHEMA_VERSION",
    "AGENT_PROPOSAL_MAX_SYMBOLS", "AGENT_PROPOSAL_MAX_PROPOSALS",
    "AGENT_PROPOSAL_MIN_CONFIDENCE", "AGENT_PROPOSAL_MIN_BARS",
    "AGENT_PROPOSAL_THESIS_MAX_CHARS", "AGENT_PROPOSAL_MAX_OUTPUT_TOKENS",
    "AGENT_PROPOSAL_TEMPERATURE",
)


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item)
                for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return str(value)


def config_identity(strategy_id: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(strategy_version, config_hash)`` for decision inputs."""
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    snapshot = {name: jsonable(getattr(config, name, None))
                for name in CONFIG_FIELDS}
    if strategy_id != config.AGENT_PROPOSAL_STRATEGY_ID:
        # A/B 的 legacy 哈希已经含有 C-only 键，直接删键会让当前 v5
        # 自然样本再次清零。用冻结兼容投影保留相同哈希，同时阻断未来
        # C Prompt/Schema/吞吐参数变更对 A/B 研究身份的无关扰动。
        compatibility = config.SIGNAL_IDENTITY_AB_AGENT_PROPOSAL_COMPAT
        snapshot.update({name: jsonable(compatibility[name])
                         for name in AGENT_PROPOSAL_CONFIG_FIELDS})
    if strategy_id == config.BREAKOUT_SIGNAL_STRATEGY_ID:
        snapshot.update({
            "BREAKOUT_LOOKBACK": jsonable(config.BREAKOUT_LOOKBACK),
            "BREAKOUT_VOL_RATIO": jsonable(config.BREAKOUT_VOL_RATIO),
        })
    if (strategy_id == config.AGENT_PROPOSAL_STRATEGY_ID and
            config.AGENT_PROPOSAL_PROMPT_VERSION != "agent-proposal-v1"):
        snapshot["AGENT_PROPOSAL_IMPLEMENTATION_VERSION"] = jsonable(
            config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION)
    snapshot["STRATEGY_ID"] = strategy_id
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("utf-8")
    config_hash = hashlib.sha256(raw).hexdigest()
    version = (f"{config.ENTRY_STRATEGY_VERSION}:{strategy_id}:"
               f"{config_hash[:12]}")
    return version, config_hash


def research_scope_version(strategy_id: Optional[str] = None) -> str:
    """Return the exact sample identity required by versioned research lines.

    Feature formulas, strategy parameters and C prompt/implementation versions
    all participate in ``config_identity``.  Every research line must therefore
    consume only the exact identity that produced its frozen samples.  Within
    one identity the signal table's unique key already enforces one row per
    natural event; the cross-version canonical view remains an audit view and
    is not a valid source for current-identity training.
    """
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    return config_identity(strategy_id)[0]
