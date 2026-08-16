"""
五层否决制选币信号 — 核心理念：宁可做对，也不做错。

每一层都是一个"否决"关卡，返回 (通过?, 原因)。
任何一层不通过 => 空仓，不往下看。全部通过 => 产生 A+ 级入场信号。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.indicators import ema, sma, detect_box, relative_strength


# ============ 关卡 0：全局熔断（风控层，见 risk/risk_manager.py） ============


# ============ 关卡 1：大盘环境 ============
def market_gate(btc_klines, fear_greed=None):
    """
    大盘关：BTC 趋势 + 情绪 + 无暴跌。
    fear_greed: 恐惧贪婪指数（0-100），None 表示无数据则跳过该子项。
    """
    if len(btc_klines) < config.BTC_EMA_SLOW + 5:
        return False, "BTC 历史数据不足"

    closes = [k["close"] for k in btc_klines]
    ema_fast = ema(closes, config.BTC_EMA_FAST)
    ema_slow = ema(closes, config.BTC_EMA_SLOW)

    price = closes[-1]
    # 1) 趋势向上：价格 > EMA50 且 EMA20 > EMA50
    if not (price > ema_slow[-1] and ema_fast[-1] > ema_slow[-1]):
        return False, f"大盘趋势非多头：price={price:.2f} EMA20={ema_fast[-1]:.2f} EMA50={ema_slow[-1]:.2f}"

    # 2) 情绪不过热
    if fear_greed is not None and fear_greed >= config.FEAR_GREED_MAX:
        return False, f"恐惧贪婪指数过热：{fear_greed}"

    # 3) 近 3 日无放量暴跌
    for i in range(-3, 0):
        if i >= -len(klines_ctx := closes):
            break
        chg = (closes[i] - closes[i - 1]) / closes[i - 1] if i - 1 >= -len(closes) else 0
        vol_ratio = btc_klines[i]["volume"] / (sum(k["volume"] for k in btc_klines[-20:]) / 20) if sum(k["volume"] for k in btc_klines[-20:]) > 0 else 1
        if chg < -0.05 and vol_ratio > 1.5:
            return False, f"近 3 日出现放量暴跌（{chg*100:.1f}%）"

    return True, "大盘多头，情绪正常，无暴跌"


# ============ 关卡 3：个币共振 ============
def coin_resonance(coin_klines, btc_klines, rs_rank_percent):
    """
    个币共振关：5 项条件全部满足才入场（回踩不破作为持仓期止损逻辑，见引擎）。
    参数：
      coin_klines: 该币日线（升序）
      btc_klines:  BTC 日线（升序）
      rs_rank_percent: 该币 RS 在观察池中的分位（0~1，越小越强）
    返回 (通过?, 原因, 信号详情 dict)
    """
    if len(coin_klines) < config.EMA_SLOW + config.BOX_MIN_DAYS:
        return False, "K 线数据不足", None

    n = len(coin_klines)
    closes = [k["close"] for k in coin_klines]
    ema_fast = ema(closes, config.EMA_FAST)
    ema_slow = ema(closes, config.EMA_SLOW)

    # 在最后一根 K 线（当前）判断；"突破日"用最后一根，箱体检测在其之前
    # ① 吸筹箱体（突破前）
    box = detect_box(coin_klines, config.BOX_MIN_DAYS, config.BOX_AMP_MIN, config.BOX_AMP_MAX, n - 1)
    if box is None:
        return False, "① 无有效吸筹箱体", None
    box_high, box_low, box_days, amp = box

    # ② 有效突破：收盘价站上箱体上沿
    last_close = closes[-1]
    if not (last_close > box_high):
        return False, f"② 未突破：收盘 {last_close:.4g} <= 箱体上沿 {box_high:.4g}", None

    # ③ 放量确认：突破当日成交量 >= 20日均量 × 1.5
    vol_20 = sum(k["volume"] for k in coin_klines[-20:]) / 20
    if vol_20 <= 0 or coin_klines[-1]["volume"] < vol_20 * config.VOLUME_BREAKOUT_MULT:
        return False, f"③ 未放量：{coin_klines[-1]['volume']:.0f} < 1.5×均量 {vol_20*config.VOLUME_BREAKOUT_MULT:.0f}", None

    # ④ 回踩确认：不在此处判断（回踩是入场后的事，跌破箱体上沿=假突破=止损）
    #    见回测引擎中的止损价设置：min(entry×(1-3%), box_high)

    # ⑤ 相对强度：进观察池前 20%
    if rs_rank_percent is None:
        return False, "⑤ 相对强度数据缺失", None
    if rs_rank_percent > config.RS_TOP_PERCENT:
        return False, f"⑤ 相对强度不足：分位 {rs_rank_percent:.0%}", None

    # ⑥ 趋势多头：价格 > EMA20 > EMA50
    if not (last_close > ema_fast[-1] > ema_slow[-1]):
        return False, "⑥ 均线非多头排列", None

    signal = {
        "box_high": box_high,
        "box_low": box_low,
        "box_days": box_days,
        "box_amp": amp,
        "close": last_close,
        "vol_ratio": coin_klines[-1]["volume"] / vol_20,
        "rs_percent": rs_rank_percent,
    }
    return True, "五项共振全满足（A+ 信号）", signal


if __name__ == "__main__":
    # 自测：拉 BTC 判断大盘关
    from data.fetch import fetch_btc_klines
    btc = fetch_btc_klines()
    ok, reason = market_gate(btc)
    print(f"大盘关: {'✅ 通过' if ok else '❌ 否决'} — {reason}")
