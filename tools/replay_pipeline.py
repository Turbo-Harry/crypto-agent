#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整管线回测（2026-08-21 用户要求'策略经过历史回测'的补完）——
裸信号重放(scan_signal)之外,把实盘引擎的【每日筛选层】加回来:

  每日: 24h 成交额排名 + 1h 趋势偏离 + ATR 甜区 + 4h 共振 → 选前 N 个
  次日: 只在这 N 个币上回放信号(带冷却)
  成交: 市价=bar 收盘,止损 1×ATR,止盈 2×ATR,最长 48 根 1H,10bps 成本

数据只来自 market.db 的 1H/4H K 线;筛选严格只用当日之前的 bar
(无未来函数)。与裸信号重放同口径对比,回答:
  '实盘近 3 天盈利,是筛选层的 edge,还是近段行情的运气?'

权限边界同 replay_signals.py: 证伪权,无证实权。
运行: python3 tools/replay_pipeline.py
"""
import bisect
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))

from strategy.indicators import ema, atr as atr_fn

MARKET_DB = os.path.join(ROOT, "data", "market.db")
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX",
           "BNB", "LTC"]
WATCH_N = 5                # 与实盘 WATCH_N 同口径(回测用 5 减少噪音)
MIN_VOL = 1_000_000
MIN_TREND_DEV = 0.003
ATR_SWEET_LOW = 0.003
ATR_SWEET_HIGH = 0.08
COST_BPS = 10.0
MAX_HOLD_BARS = 48
COOLDOWN_BARS = 2         # 30 分钟冷却 ≈ 每 2 根 1H 最多 1 信号(保守)
REJECT_WICK_RATIO = 1.0


def _kl(symbol, bar):
    conn = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT open_time, open, high, low, close, volume, quote_volume "
            "FROM klines WHERE inst_id=? AND bar=? ORDER BY open_time",
            [f"{symbol}-USDT", bar]).fetchall()
        return [list(r) for r in rows]
    finally:
        conn.close()


def _screen_day(kl1, kl4, day_end_idx):
    """在 [0, day_end_idx] 的 1H 数据上做当日筛选(只用当日及之前的数据)。
    返回按评分降序的 [(symbol, score), ...]。"""
    out = []
    for sym in SYMBOLS:
        k = kl1[sym]
        if day_end_idx < 60:
            continue
        win = k[:day_end_idx + 1]
        # 24h 成交额(quote_volume 近 24 根)
        vol24 = sum((x[6] or 0) for x in win[-24:])
        if vol24 < MIN_VOL:
            continue
        closes = [x[4] for x in win]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        if not e20 or not e50 or e50[-1] == 0:
            continue
        dev = (e20[-1] - e50[-1]) / e50[-1]
        if abs(dev) < MIN_TREND_DEV:
            continue
        a = atr_fn([{"open": x[1], "high": x[2], "low": x[3],
                     "close": x[4]} for x in win], 14)
        last_close = closes[-1]
        atr_pct = a / last_close if last_close else 0
        if not (ATR_SWEET_LOW <= atr_pct <= ATR_SWEET_HIGH):
            continue
        # 4h 共振(同向趋势)
        k4 = kl4[sym]
        if k4:
            idx4 = bisect.bisect_right([x[0] for x in k4], win[-1][0]) - 1
            if idx4 >= 50:
                c4 = [x[4] for x in k4[:idx4 + 1]]
                e20_4, e50_4 = ema(c4, 20), ema(c4, 50)
                if e20_4 and e50_4 and e50_4[-1]:
                    dir4 = 1 if e20_4[-1] > e50_4[-1] else -1
                    if dir4 != (1 if dev > 0 else -1):
                        continue
        out.append((sym, abs(dev)))
    out.sort(key=lambda x: -x[1])
    return out[:WATCH_N]


def _scan_signal(win, sym, tf4h_ok=True):
    """裸信号逻辑(与 engines/signal_scan 同构,输入限定为窗口数据)。"""
    closes = [x[4] for x in win]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if not e20 or not e50:
        return None
    last = win[-1]
    body = abs(last[4] - last[1])
    a = atr_fn([{"open": x[1], "high": x[2], "low": x[3], "close": x[4]}
                for x in win], 14)
    entry_ref = last[4]
    if e20[-1] > e50[-1] and last[3] <= e20[-1] and last[4] > e20[-1]:
        lower_wick = min(last[1], last[4]) - last[3]
        if lower_wick >= body * REJECT_WICK_RATIO and a > 0:
            return {"dir": "long", "entry": entry_ref,
                    "stop": entry_ref - 1.0 * a, "tp": entry_ref + 2.0 * a}
    if e20[-1] < e50[-1] and last[2] >= e20[-1] and last[4] < e20[-1]:
        upper_wick = last[2] - max(last[1], last[4])
        if upper_wick >= body * REJECT_WICK_RATIO and a > 0:
            return {"dir": "short", "entry": entry_ref,
                    "stop": entry_ref + 1.0 * a, "tp": entry_ref - 2.0 * a}
    return None


def _simulate(sig, kline_after):
    """从信号后的 bar 序列模拟平仓。返回 pnl_r / exit_reason / bars_held。"""
    entry = sig["entry"]
    stop, tp = sig["stop"], sig["tp"]
    is_long = sig["dir"] == "long"
    for i, bar in enumerate(kline_after):
        if i >= MAX_HOLD_BARS:
            return (bar[4] - entry) / (abs(entry - stop) or 1), "max_hold", i + 1
        if is_long:
            if bar[3] <= stop:
                return (stop - entry) / abs(entry - stop), "stop", i + 1
            if bar[2] >= tp:
                return (tp - entry) / abs(entry - stop), "tp", i + 1
        else:
            if bar[2] >= stop:
                return (entry - stop) / abs(entry - stop), "stop", i + 1
            if bar[3] <= tp:
                return (entry - tp) / abs(entry - stop), "tp", i + 1
    return (kline_after[-1][4] - entry) / (abs(entry - stop) or 1), "eod", MAX_HOLD_BARS


def run():
    kl1 = {s: _kl(s, "1H") for s in SYMBOLS}
    kl4 = {s: _kl(s, "4H") for s in SYMBOLS}
    n = min(len(kl1[s]) for s in SYMBOLS)
    if n < 500:
        print("数据不足,先跑 tools/backfill_history.py")
        return
    trades = []
    # 滚动: 每 24 根 1H(1 天)做一次筛选,次日用该名单交易
    day = 240          # 先暖机 10 天(EMA/ATR 需要历史)
    while day + 24 <= n:
        watch = [s for s, _ in _screen_day(kl1, kl4, day - 1)]
        cooldown = {s: -99 for s in SYMBOLS}
        for i in range(day, day + 24):
            for sym in watch:
                if i - cooldown.get(sym, -99) < COOLDOWN_BARS:
                    continue
                win = kl1[sym][:i + 1]
                if len(win) < 60:
                    continue
                sig = _scan_signal(win, sym)
                if not sig:
                    continue
                cooldown[sym] = i
                after = kl1[sym][i + 1:i + 1 + MAX_HOLD_BARS]
                if not after:
                    continue
                r, reason, held = _simulate(sig, after)
                cost_r = COST_BPS / 1e4 / max(abs(sig["entry"] - sig["stop"]) / sig["entry"], 1e-6)
                trades.append({"symbol": sym, "dir": sig["dir"], "pnl_r": r - cost_r,
                               "reason": reason, "bars": held,
                               "ts": win[-1][0]})
        day += 24
    _report(trades)


def _report(trades):
    if not trades:
        print("无交易")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl_r"] > 0)
    mean_r = sum(t["pnl_r"] for t in trades) / n
    var = sum((t["pnl_r"] - mean_r) ** 2 for t in trades) / (n - 1)
    sqn = (n ** 0.5) * mean_r / (var ** 0.5) if var > 0 else 0
    print(f"===== 完整管线回测(每日筛选+冷却+裸信号) =====")
    print(f"交易 {n} 笔 | 胜率 {wins/n*100:.1f}% | 期望 {mean_r:+.3f}R | SQN {sqn:.2f}")
    from collections import Counter
    for sym in sorted(set(t["symbol"] for t in trades)):
        ts = [t for t in trades if t["symbol"] == sym]
        if not ts:
            continue
        m = sum(t["pnl_r"] for t in ts) / len(ts)
        print(f"  {sym}: {len(ts)} 笔, 期望 {m:+.3f}R")
    reasons = Counter(t["reason"] for t in trades)
    print("出场分布:", dict(reasons))
    print("【证伪权】以上结果不得用于宣称改进或自动变更;SQN<0/期望为负 → 退役评估。")


if __name__ == "__main__":
    run()
