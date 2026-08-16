"""
实时信号扫描器 — 用当前真实市场数据跑五层否决制关卡，
输出：大盘状态 + 观察池各币共振结果 + 当前操作建议（空仓/入场）。
这是"宁可做对"哲学的实时落地：大部分时间它会告诉你"空仓"。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch import build_observe_pool, fetch_klines, fetch_btc_klines
from strategy.filters import market_gate, coin_resonance
from strategy.indicators import relative_strength


def scan(verbose=True):
    print("=" * 62)
    print("实时信号扫描  —  " + time.strftime("%Y-%m-%d %H:%M"))
    print("=" * 62)

    # 1. 数据
    pool_tickers = build_observe_pool(config.OBSERVE_POOL_SIZE)
    btc = fetch_btc_klines()
    btc_close = [k["close"] for k in btc]

    # 2. 大盘关
    mg_ok, mg_reason = market_gate(btc)
    status = "✅ 通过" if mg_ok else "❌ 否决"
    print(f"\n【关卡1 · 大盘环境】{status}")
    print(f"    {mg_reason}")
    if not mg_ok:
        print("\n>>> 结论：大盘不给信号，【空仓】。不看任何个币。")
        print(">>> 依据：宁可错过，不可做错。")
        return {"大盘": mg_reason, "信号": [], "建议": "空仓"}

    # 3. 个币共振关（逐个币）
    print(f"\n【关卡2 · 个币共振】扫描观察池 {len(pool_tickers)} 个币...")
    results = []
    rs_map = {}
    for t in pool_tickers:
        sym = t["symbol"]
        try:
            klines = fetch_klines(sym)
            if len(klines) < config.EMA_SLOW + config.BOX_MIN_DAYS:
                continue
            rs_map[sym] = relative_strength(
                [k["close"] for k in klines], btc_close, 20)
        except Exception:
            continue

    # RS 分位
    sorted_syms = sorted(rs_map.keys(), key=lambda s: rs_map[s], reverse=True)
    denom = max(len(sorted_syms) - 1, 1)
    rank_map = {sym: i / denom for i, sym in enumerate(sorted_syms)}

    for t in pool_tickers:
        sym = t["symbol"]
        if sym not in rs_map:
            continue
        try:
            klines = fetch_klines(sym)
            ok, reason, detail = coin_resonance(klines, btc, rank_map[sym])
            if ok:
                results.append((sym, rs_map[sym], detail))
        except Exception:
            continue

    if not results:
        print("\n>>> 结论：大盘通过，但无任何币满足五项共振。【空仓】。")
        return {"大盘": mg_reason, "信号": [], "建议": "空仓"}

    results.sort(key=lambda x: x[1], reverse=True)
    print(f"\n发现 {len(results)} 个 A+ 级共振信号：")
    for sym, rs, detail in results:
        print(f"  • {sym:<12} RS={rs:+.1f}%  箱体{detail['box_days']}天 "
              f"幅度{detail['box_amp']:.0%}  放量{detail['vol_ratio']:.1f}x  "
              f"收盘{detail['close']:.4g} > 箱体上沿{detail['box_high']:.4g}")

    print("\n>>> 结论：存在 A+ 信号，可进入【入场】流程（次日开盘，止损箱体上沿/-3%）。")
    return {"大盘": mg_reason, "信号": [r[0] for r in results], "建议": "入场"}


if __name__ == "__main__":
    scan()
