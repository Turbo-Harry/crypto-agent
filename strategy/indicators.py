"""
技术指标计算 — 纯函数，无外部依赖（pandas 可选，这里用标准库+简单列表实现）
输入统一为按时间升序的 K 线列表，每根 K 线为 dict：
{open, high, low, close, volume, open_time}
"""


def ema(values, period):
    """指数移动平均，返回与 values 等长的列表（前面不足期用 SMA 填充）"""
    if not values:
        return []
    k = 2 / (period + 1)
    out = []
    sma = None
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
        else:
            out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values, period):
    out = []
    window = []
    for v in values:
        window.append(v)
        if len(window) > period:
            window.pop(0)
        out.append(sum(window) / len(window))
    return out


def detect_box(klines, min_days, amp_min, amp_max, end_index):
    """
    在 end_index 之前检测吸筹箱体（横盘整理区）。
    只检查"紧邻突破日"的近期窗口（min_days ~ 3*min_days 天），
    避免把超长趋势段误判为箱体。
    返回 (box_high, box_low, box_days, amp) 或 None。
    """
    for window in range(min_days, min_days * 3 + 1):
        start = end_index - window
        if start < 0:
            break
        segment = klines[start:end_index]  # 不含 end_index（突破当日）
        highs = [k["high"] for k in segment]
        lows = [k["low"] for k in segment]
        box_high = max(highs)
        box_low = min(lows)
        if box_low <= 0:
            continue
        amp = (box_high - box_low) / box_low
        if amp_min <= amp <= amp_max:
            return (box_high, box_low, window, amp)
    return None


def relative_strength(coin_close, btc_close, period):
    """
    相对强度：币近 period 日涨幅 - BTC 近 period 日涨幅。
    返回最后一天的 RS 值（%）。
    """
    if len(coin_close) <= period or len(btc_close) <= period:
        return 0.0
    coin_chg = (coin_close[-1] - coin_close[-1 - period]) / coin_close[-1 - period]
    btc_chg = (btc_close[-1] - btc_close[-1 - period]) / btc_close[-1 - period]
    return (coin_chg - btc_chg) * 100


def atr(klines, period=14):
    """平均真实波幅（用于波动率参考）"""
    trs = []
    for i in range(1, len(klines)):
        h = klines[i]["high"]
        l = klines[i]["low"]
        pc = klines[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(period, len(trs))
