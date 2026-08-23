"""因果行情状态影子分类。

输出是可解释 softmax 权重，不是已经过校准的真实概率。它只用于按
``regime × strategy`` 留样和分层评价；在通过 T5/T6 前没有交易权限。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

import config


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _softmax(scores: Mapping[str, float]) -> Dict[str, float]:
    temperature = max(float(config.MARKET_REGIME_SOFTMAX_TEMPERATURE), 1e-9)
    peak = max(scores.values())
    weights = {name: math.exp((value - peak) / temperature)
               for name, value in scores.items()}
    total = sum(weights.values())
    return {name: value / total for name, value in weights.items()}


def classify_market_regime(
        legacy_regime: Optional[Mapping[str, Any]],
        factors: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """用信号时点已有信息形成 trend/range/vol/disorder 影子权重。

    理论轴：时间序列动量刻画趋势，ATR 百分位与 vol-of-vol 刻画波动扩张，
    市场宽度和相关性集中度刻画横截面一致性。缺核心输入时明确 unknown。
    """
    legacy = dict(legacy_regime or {})
    features = dict(factors or {})
    vol_pct = _number(legacy.get("vol_pct"))
    slope = _number(legacy.get("trend_slope"))
    spread_4h = _number(legacy.get("tf4h_spread"))
    breadth = _number(features.get("market_breadth"))
    concentration = _number(features.get("correlation_concentration"))
    realized_vol = _number(features.get("realized_vol_5m"))
    vol_of_vol = _number(features.get("vol_of_vol"))
    adx = _number(features.get("adx"))
    efficiency = _number(features.get("kaufman_efficiency_ratio"))
    bb_width_pct = _number(features.get("bb_width_percentile"))
    squeeze_release = _number(features.get("bb_squeeze_release"))
    vwap_crossing = _number(features.get("vwap_crossing_rate"))

    core_n = sum(value is not None for value in (vol_pct, slope))
    if core_n < int(config.MARKET_REGIME_MIN_CORE_INPUTS):
        return {
            "version": config.MARKET_REGIME_VERSION,
            "method": "heuristic_softmax",
            "calibrated": False,
            "ready": False,
            "state": "unknown",
            "confidence": 0.0,
            "margin": 0.0,
            "weights": {},
            "reason": "missing_core_inputs",
        }

    trend_parts = [
        _clip01(abs(slope) / config.MARKET_REGIME_TREND_SLOPE_REF)
    ]
    if spread_4h is not None:
        trend_parts.append(_clip01(
            abs(spread_4h) / config.MARKET_REGIME_TF4H_SPREAD_REF))
    if adx is not None:
        trend_parts.append(_clip01(adx))
    if efficiency is not None:
        trend_parts.append(_clip01(efficiency))
    trend_strength = sum(trend_parts) / len(trend_parts)
    breadth_coherence = (_clip01(abs(breadth - 0.5) * 2.0)
                         if breadth is not None else trend_strength)
    correlation = (_clip01(concentration) if concentration is not None
                   else breadth_coherence)
    vol_parts = [_clip01(vol_pct)]
    if bb_width_pct is not None:
        vol_parts.append(_clip01(bb_width_pct))
    vol_level = sum(vol_parts) / len(vol_parts)
    instability = 0.0
    if realized_vol is not None and realized_vol > 0 and vol_of_vol is not None:
        instability = _clip01(
            (vol_of_vol / realized_vol) /
            config.MARKET_REGIME_VOL_INSTABILITY_REF)
    if squeeze_release is not None:
        instability = max(instability, _clip01(squeeze_release))
    crossing = (_clip01(vwap_crossing) if vwap_crossing is not None
                else 1.0 - trend_strength)

    # 等权组合避免在尚无标签时“调参拟合”。softmax 仅把相对适配度归一化。
    raw_scores = {
        "trend": (trend_strength + breadth_coherence + correlation +
                  (1.0 - instability)) / 4.0,
        "range": ((1.0 - trend_strength) + (1.0 - vol_level) +
                  (1.0 - breadth_coherence) + (1.0 - instability) +
                  crossing) / 5.0,
        "vol_expansion": (vol_level + instability + trend_strength) / 3.0,
        "disorder": (vol_level + instability + (1.0 - breadth_coherence) +
                     (1.0 - correlation)) / 4.0,
    }
    weights = _softmax(raw_scores)
    ordered = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    state, confidence = ordered[0]
    margin = confidence - ordered[1][1]
    return {
        "version": config.MARKET_REGIME_VERSION,
        "method": "heuristic_softmax",
        "calibrated": False,
        "ready": True,
        "state": state,
        "confidence": round(confidence, 6),
        "margin": round(margin, 6),
        "weights": {name: round(value, 6) for name, value in weights.items()},
        "components": {
            "trend_strength": round(trend_strength, 6),
            "vol_level": round(vol_level, 6),
            "vol_instability": round(instability, 6),
            "breadth_coherence": round(breadth_coherence, 6),
            "correlation_concentration": round(correlation, 6),
            "vwap_crossing_rate": round(crossing, 6),
        },
    }
