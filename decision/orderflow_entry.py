"""Paper-only 日内最终确认纯决策；没有交易所或执行权限。"""
import config


def paper_intraday_entry_decision(sig, *, one_minute, five_minute,
                                  orderflow, microstructure, realtime):
    """按方向确认 1m/5m、连续 OFI/成交、成本与波动；缺失即失败关闭。"""
    direction = str(sig.get("dir") or sig.get("direction") or "")
    sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
    result = {"passed": False, "reason": "invalid_direction",
              "direction": direction, "size_factor": 1.0,
              "one_minute": one_minute, "five_minute": five_minute,
              "orderflow": orderflow, "microstructure": microstructure,
              "realtime": realtime}
    if not sign:
        return result

    def aligned_candles(rows):
        needed = len(rows or [])
        if needed < 1:
            return False
        return all(sign * (float(row["close"]) - float(row["open"])) > 0
                   for row in rows)

    if not aligned_candles(one_minute):
        result["reason"] = "one_minute_not_confirmed"
        return result
    if not aligned_candles(five_minute):
        result["reason"] = "five_minute_not_confirmed"
        return result
    if (orderflow or {}).get("status") != "ready":
        result["reason"] = "orderflow_not_ready"
        return result
    ofi = (orderflow or {}).get("ofi_event_multilevel")
    if ofi is None or sign * float(ofi) < config.PAPER_ENTRY_MIN_ALIGNED_OFI:
        result["reason"] = "ofi_not_aligned"
        return result
    cancel = (orderflow or {}).get("ofi_event_cancel_imbalance")
    if (cancel is not None and sign * float(cancel) <
            -config.PAPER_ENTRY_MAX_CANCEL_CONTRADICTION):
        result["reason"] = "cancel_flow_contradiction"
        return result
    taker_buy = (realtime or {}).get("taker_buy_60s")
    trade_count = (realtime or {}).get("trade_flow_count_60s")
    if (taker_buy is None or trade_count is None or
            int(trade_count) < config.PAPER_ENTRY_MIN_TRADES_60S):
        result["reason"] = "trade_flow_missing"
        return result
    taker_aligned = (float(taker_buy) if direction == "long"
                     else 1.0 - float(taker_buy))
    if taker_aligned < config.PAPER_ENTRY_MIN_TAKER_RATIO:
        result["reason"] = "trade_flow_not_aligned"
        return result
    spread = (microstructure or {}).get("spread_bps")
    slippage = (microstructure or {}).get("expected_slippage_bps")
    if spread is None or float(spread) >= config.PAPER_ENTRY_MAX_SPREAD_BPS:
        result["reason"] = "spread_too_wide"
        return result
    if (slippage is None or
            float(slippage) >= config.PAPER_ENTRY_MAX_SLIPPAGE_BPS):
        result["reason"] = "slippage_too_high"
        return result
    volatility = (realtime or {}).get("vol_15m")
    if volatility is None:
        result["reason"] = "volatility_missing"
        return result
    if float(volatility) >= config.PAPER_ENTRY_VOL_REJECT_THRESHOLD:
        result["reason"] = "volatility_too_high"
        return result
    if float(volatility) >= config.PAPER_ENTRY_VOL_REDUCE_THRESHOLD:
        result["size_factor"] = config.PAPER_ENTRY_VOL_SIZE_FACTOR
    result["passed"] = True
    result["reason"] = "intraday_confirmation_pass"
    result["aligned_ofi"] = sign * float(ofi)
    result["aligned_taker_ratio"] = taker_aligned
    result["trade_flow_count_60s"] = int(trade_count)
    return result
