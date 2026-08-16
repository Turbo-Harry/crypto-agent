"""
回测入口 — 拉取观察池历史数据 → 运行回测 → 输出报告。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch import build_observe_pool, fetch_klines, fetch_btc_klines
from backtest.engine import Backtest


def main():
    print("=" * 60)
    print("加密货币自动化交易系统 — 回测")
    print(f"策略：五层否决制 / 宁可做对 / 激进档")
    print("=" * 60)

    # 1. 观察池
    pool_tickers = build_observe_pool(config.OBSERVE_POOL_SIZE)
    symbols = [t["symbol"] for t in pool_tickers]
    print(f"\n[1/3] 观察池 {len(symbols)} 个币，开始拉取历史日线...")

    btc = fetch_btc_klines()
    pool_klines = {}
    for i, sym in enumerate(symbols, 1):
        try:
            k = fetch_klines(sym)
            if len(k) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
                pool_klines[sym] = k
        except Exception as e:
            pass  # 跳过拉取失败的币
        if i % 20 == 0:
            print(f"  已拉取 {i}/{len(symbols)}")
        time.sleep(0.15)  # 温和限速，避免触发限流

    print(f"  完成：{len(pool_klines)} 个币数据可用，BTC {len(btc)} 根日线")

    # 2. 回测
    print(f"\n[2/3] 运行回测...")
    bt = Backtest(btc, pool_klines, initial_equity=100_000)
    stats = bt.run()

    # 3. 报告
    print(f"\n[3/3] 回测结果\n" + "=" * 60)
    for k, v in stats.items():
        if k == "交易明细":
            continue
        if isinstance(v, float):
            print(f"  {k:<12}: {v:,.4f}")
        else:
            print(f"  {k:<12}: {v}")
    print("=" * 60)
    print("\n交易明细（按时间）:")
    for t in stats["交易明细"]:
        print(f"  {t['entry_date']} → {t['exit_date']}  {t['symbol']:<12} "
              f"盈亏 {t['pnl_pct']*100:+.2f}%  原因: {t['reason']}")


if __name__ == "__main__":
    main()
