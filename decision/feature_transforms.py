"""信号时点可复算的派生特征；训练与消费共用同一公式。"""
import math
from typing import Dict, Mapping, Optional

import config


DERIVED_FEATURE_NAMES = (
    "trend_volume_confirmation",
    "wick_volume_absorption",
    "pullback_volume_confirmation",
    "trend_wick_alignment",
    "momentum_volume_confirmation",
    "rv_to_har_ratio",
    "bb_squeeze_volume_confirmation",
)


def _log_returns(closes):
    values = [float(value) for value in closes]
    return [math.log(values[idx] / values[idx - 1])
            for idx in range(1, len(values))
            if values[idx] > 0 and values[idx - 1] > 0]


def volatility_5m_features(closes) -> Dict[str, Optional[float]]:
    """从已收线 5m 收盘统一计算短波动、vol-of-vol 与 HAR-RV。

    调用方必须先完成 as-of 截止；本函数不读时钟，也不填补缺失 bar。
    """
    result = {"realized_vol_5m": None, "vol_of_vol": None, "har_rv": None}
    try:
        returns = _log_returns(closes)
        one_hour = int(config.FACTOR_HAR_WINDOWS[0])
        if len(returns) >= one_hour:
            result["realized_vol_5m"] = math.sqrt(
                sum(value * value for value in returns[-one_hour:]))
            rolling = [math.sqrt(sum(value * value for value in
                                     returns[end - one_hour:end]))
                       for end in range(one_hour, len(returns) + 1)]
            if len(rolling) > 1:
                mean_value = sum(rolling) / len(rolling)
                result["vol_of_vol"] = math.sqrt(
                    sum((value - mean_value) ** 2 for value in rolling) /
                    (len(rolling) - 1))
        windows = tuple(int(value) for value in config.FACTOR_HAR_WINDOWS)
        if windows and len(returns) >= max(windows):
            components = [math.sqrt(sum(value * value for value in
                                        returns[-window:]) / window)
                          for window in windows]
            result["har_rv"] = sum(components) / len(components)
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def _ema_last(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _correlation(left, right):
    length = min(len(left), len(right))
    if length < 3:
        return 0.0
    a, b = left[-length:], right[-length:]
    mean_a, mean_b = sum(a) / length, sum(b) / length
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    return cov / math.sqrt(var_a * var_b) if var_a > 0 and var_b > 0 else 0.0


def cross_sectional_snapshot(closes_by_symbol: Mapping[str, object]) -> Dict:
    """计算信号时点的市场宽度、相关集中度与逐币 BTC 残差动量。

    输入仅包含各币已收线 15m close；少于配置的最小资产数时全部缺失，
    防止用单币或残缺横截面冒充市场状态。
    """
    empty = {"market_breadth": None, "correlation_concentration": None,
             "by_symbol": {}}
    try:
        lookback = int(config.FACTOR_CROSS_SECTION_LOOKBACK_BARS)
        period = int(config.FACTOR_BREADTH_EMA_PERIOD)
        cleaned = {}
        for symbol, raw_values in closes_by_symbol.items():
            values = [float(value) for value in list(raw_values)[-lookback:]
                      if value is not None and float(value) > 0]
            if len(values) >= max(period, 5):
                cleaned[str(symbol)] = values
        if len(cleaned) < int(config.FACTOR_CROSS_SECTION_MIN_ASSETS):
            return empty
        returns = {symbol: _log_returns(values)
                   for symbol, values in cleaned.items()}
        symbols = sorted(symbol for symbol, values in returns.items()
                         if len(values) >= 4)
        if len(symbols) < int(config.FACTOR_CROSS_SECTION_MIN_ASSETS):
            return empty
        breadth = sum(
            cleaned[symbol][-1] > _ema_last(cleaned[symbol], period)
            for symbol in symbols) / len(symbols)
        matrix = [[1.0 if left == right else _correlation(
                   returns[left], returns[right])
                   for right in symbols] for left in symbols]
        vector = [1.0 / math.sqrt(len(symbols))] * len(symbols)
        for _ in range(int(config.FACTOR_CORRELATION_POWER_ITERATIONS)):
            product = [sum(value * weight for value, weight in zip(row, vector))
                       for row in matrix]
            norm = math.sqrt(sum(value * value for value in product))
            if norm <= 0:
                break
            vector = [value / norm for value in product]
        product = [sum(value * weight for value, weight in zip(row, vector))
                   for row in matrix]
        leading = sum(value * weight for value, weight in zip(vector, product))
        concentration = max(0.0, min(1.0, leading / len(symbols)))
        momentum = {symbol: sum(returns[symbol][-4:]) for symbol in symbols}
        btc_returns = returns.get("BTC")
        by_symbol = {}
        for symbol in symbols:
            rank = (sum(value <= momentum[symbol]
                        for value in momentum.values()) / len(momentum))
            beta = residual = None
            own_returns = returns[symbol]
            if btc_returns is not None:
                length = min(len(own_returns), len(btc_returns))
                if length >= 10:
                    own, btc = own_returns[-length:], btc_returns[-length:]
                    mean_own, mean_btc = sum(own) / length, sum(btc) / length
                    covariance = sum((a - mean_own) * (b - mean_btc)
                                     for a, b in zip(own, btc))
                    variance = sum((b - mean_btc) ** 2 for b in btc)
                    beta = covariance / variance if variance > 0 else 0.0
                    residual = sum(own[-4:]) - beta * sum(btc[-4:])
            by_symbol[symbol] = {
                "cross_sectional_rank": rank,
                "btc_beta": beta,
                "btc_residual_momentum": residual,
            }
        return {"market_breadth": breadth,
                "correlation_concentration": concentration,
                "by_symbol": by_symbol}
    except (TypeError, ValueError, OverflowError):
        return empty


def _bar_value(row, name, index):
    if isinstance(row, Mapping):
        return float(row[name])
    return float(row[index])


def _mean_std(values):
    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    return mean_value, math.sqrt(max(0.0, variance))


def technical_regime_features(rows) -> Dict[str, Optional[float]]:
    """从已收线 15m OHLCV 计算布林、ADX、效率比与 VWAP 状态特征。"""
    names = (
        "bb_width_percentile", "bb_percent_b", "bb_squeeze_release",
        "adx", "directional_index_spread", "kaufman_efficiency_ratio",
        "vwap_distance_atr", "vwap_crossing_rate", "volume_zscore",
    )
    result = {name: None for name in names}
    try:
        bars = [{"high": _bar_value(row, "high", 2),
                 "low": _bar_value(row, "low", 3),
                 "close": _bar_value(row, "close", 4),
                 "volume": _bar_value(row, "volume", 5)} for row in rows]
        closes = [row["close"] for row in bars]
        period = int(config.FACTOR_BB_PERIOD)
        if len(bars) < max(period + 1, 2 * int(config.FACTOR_ADX_PERIOD) + 1):
            return result

        width_start = max(period, len(closes) -
                          int(config.FACTOR_BB_PERCENTILE_LOOKBACK))
        widths = []
        for end in range(width_start, len(closes) + 1):
            mean_value, std_value = _mean_std(closes[end - period:end])
            widths.append((2.0 * config.FACTOR_BB_STDDEV_MULT * std_value /
                           mean_value) if mean_value > 0 else 0.0)
        current_width = widths[-1]
        result["bb_width_percentile"] = (
            sum(value <= current_width for value in widths) / len(widths))
        mean_value, std_value = _mean_std(closes[-period:])
        lower = mean_value - config.FACTOR_BB_STDDEV_MULT * std_value
        upper = mean_value + config.FACTOR_BB_STDDEV_MULT * std_value
        result["bb_percent_b"] = ((closes[-1] - lower) / (upper - lower)
                                  if upper > lower else 0.5)
        squeeze_n = int(config.FACTOR_BB_SQUEEZE_LOOKBACK)
        if len(widths) >= squeeze_n + 1:
            prior = widths[-squeeze_n - 1:-1]
            prior_min = min(prior)
            prior_rank = sum(value <= prior_min for value in widths[:-1]) / max(
                1, len(widths) - 1)
            result["bb_squeeze_release"] = (
                max(0.0, (current_width - widths[-2]) / widths[-2])
                if prior_rank <= config.FACTOR_BB_SQUEEZE_MAX_PERCENTILE and
                widths[-2] > 0 else 0.0)

        adx_period = int(config.FACTOR_ADX_PERIOD)
        true_ranges, plus_dm, minus_dm = [], [], []
        for previous, current in zip(bars, bars[1:]):
            true_ranges.append(max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"])))
            up = current["high"] - previous["high"]
            down = previous["low"] - current["low"]
            plus_dm.append(up if up > down and up > 0 else 0.0)
            minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_sum = sum(true_ranges[:adx_period])
        plus_sum = sum(plus_dm[:adx_period])
        minus_sum = sum(minus_dm[:adx_period])
        dx_values = []
        plus_index = minus_index = 0.0
        for idx in range(adx_period - 1, len(true_ranges)):
            if idx >= adx_period:
                tr_sum = tr_sum - tr_sum / adx_period + true_ranges[idx]
                plus_sum = plus_sum - plus_sum / adx_period + plus_dm[idx]
                minus_sum = minus_sum - minus_sum / adx_period + minus_dm[idx]
            plus_index = 100.0 * plus_sum / tr_sum if tr_sum > 0 else 0.0
            minus_index = 100.0 * minus_sum / tr_sum if tr_sum > 0 else 0.0
            denominator = plus_index + minus_index
            dx_values.append(100.0 * abs(plus_index - minus_index) / denominator
                             if denominator > 0 else 0.0)
        if len(dx_values) >= adx_period:
            result["adx"] = sum(dx_values[-adx_period:]) / adx_period / 100.0
            result["directional_index_spread"] = (
                plus_index - minus_index) / 100.0

        efficiency_period = int(config.FACTOR_EFFICIENCY_PERIOD)
        if len(closes) >= efficiency_period + 1:
            displacement = abs(closes[-1] - closes[-efficiency_period - 1])
            path = sum(abs(closes[idx] - closes[idx - 1])
                       for idx in range(len(closes) - efficiency_period,
                                        len(closes)))
            result["kaufman_efficiency_ratio"] = (
                displacement / path if path > 0 else 0.0)

        vwap_period = int(config.FACTOR_VWAP_PERIOD)
        recent = bars[-vwap_period:]
        volume_sum = sum(row["volume"] for row in recent)
        vwap = (sum(((row["high"] + row["low"] + row["close"]) / 3.0) *
                    row["volume"] for row in recent) / volume_sum
                if volume_sum > 0 else None)
        atr_value = sum(true_ranges[-adx_period:]) / adx_period
        if vwap is not None and atr_value > 0:
            result["vwap_distance_atr"] = (closes[-1] - vwap) / atr_value
        signs = []
        for end in range(vwap_period, len(bars) + 1):
            window = bars[end - vwap_period:end]
            total = sum(row["volume"] for row in window)
            if total <= 0:
                continue
            rolling_vwap = sum(
                ((row["high"] + row["low"] + row["close"]) / 3.0) *
                row["volume"] for row in window) / total
            distance = window[-1]["close"] - rolling_vwap
            signs.append(1 if distance > 0 else -1 if distance < 0 else 0)
        nonzero = [value for value in signs if value]
        if len(nonzero) > 1:
            result["vwap_crossing_rate"] = sum(
                left != right for left, right in zip(nonzero, nonzero[1:])) / (
                    len(nonzero) - 1)

        z_period = int(config.FACTOR_VOLUME_Z_PERIOD)
        prior_volume = [row["volume"] for row in bars[-z_period - 1:-1]]
        if len(prior_volume) == z_period:
            volume_mean, volume_std = _mean_std(prior_volume)
            result["volume_zscore"] = ((bars[-1]["volume"] - volume_mean) /
                                       volume_std if volume_std > 0 else 0.0)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        pass
    return result


def _number(values: Mapping[str, object], name: str) -> Optional[float]:
    value = values.get(name)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _product(values: Mapping[str, object], left: str,
             right: str) -> Optional[float]:
    left_value = _number(values, left)
    right_value = _number(values, right)
    if left_value is None or right_value is None:
        return None
    return left_value * right_value


def materialize_derived_features(
        factor_features: Mapping[str, object],
        shadow_dims: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """只用冻结的信号时点输入生成预注册交互，不读取未来行情。"""
    result: Dict[str, object] = dict(factor_features or {})
    # 六维子分本身也是注册因子；此前它们只参与交互计算，却没有进入结果，
    # 导致因子挖掘把已有的实时证据误报为缺失。
    for name in config.SHADOW_DIMS:
        if result.get(name) is None and shadow_dims is not None:
            result[name] = shadow_dims.get(name)
    inputs = dict(result)
    inputs.update(dict(shadow_dims or {}))
    result.update({
        "trend_volume_confirmation": _product(inputs, "trend", "volume_ratio"),
        "wick_volume_absorption": _product(inputs, "wick", "volume_ratio"),
        "pullback_volume_confirmation": _product(inputs, "depth", "volume_ratio"),
        "trend_wick_alignment": _product(inputs, "trend", "wick"),
        "momentum_volume_confirmation": _product(
            inputs, "momentum_1h", "volume_ratio"),
        "rv_to_har_ratio": (
            _number(inputs, "realized_vol_5m") / _number(inputs, "har_rv")
            if _number(inputs, "realized_vol_5m") is not None and
            _number(inputs, "har_rv") not in (None, 0.0) else None),
        "bb_squeeze_volume_confirmation": _product(
            inputs, "bb_squeeze_release", "volume_zscore"),
    })
    return result
