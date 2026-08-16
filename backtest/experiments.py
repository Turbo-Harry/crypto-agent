"""
参数扫描实验 — 批量跑不同配置的回测，量化每项改进的影响。
用于策略优化：找出把回测从亏损拉到正期望的关键参数。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch import build_observe_pool, fetch_klines, fetch_btc_klines
from backtest.engine import Backtest


def load_data():
    pool_tickers = build_observe_pool(config.OBSERVE_POOL_SIZE)
    btc = fetch_btc_klines()
    pool = {}
    for t in pool_tickers:
        try:
            k = fetch_klines(t["symbol"])
            if len(k) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
                pool[t["symbol"]] = k
        except Exception:
            pass
    return btc, pool


def run_one(btc, pool, **overrides):
    """跑一次回测，允许临时覆盖 config 参数。"""
    saved = {}
    for k, v in overrides.items():
        saved[k] = getattr(config, k)
        setattr(config, k, v)
    try:
        bt = Backtest(btc, pool, initial_equity=100_000)
        stats = bt.run()
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
    return stats


def print_row(name, stats):
    print(f"{name:<24} | 交易{stats['交易次数']:>3} | 胜率{stats['胜率']*100:>5.1f}% | "
          f"盈亏比{stats['盈亏比']:>5.2f} | 收益{stats['总收益率']*100:>+7.2f}% | "
          f"回撤{stats['最大回撤']*100:>5.1f}%")


def main():
    print("加载数据...")
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币，BTC {len(btc)} 根\n")
    print("=" * 100)

    # 1. 基线（含手续费滑点，追高 8%）
    stats = run_one(btc, pool)
    print_row("基线(追高8%,含成本)", stats)

    # 2. 收紧追高保护
    for gap in [0.05, 0.03, 0.02]:
        stats = run_one(btc, pool, MAX_ENTRY_GAP=gap)
        print_row(f"追高{gap*100:.0f}%", stats)

    # 3. 更严格放量
    stats = run_one(btc, pool, VOLUME_BREAKOUT_MULT=2.0)
    print_row("放量2.0x", stats)

    # 4. 更长期箱体
    stats = run_one(btc, pool, BOX_MIN_DAYS=21)
    print_row("箱体>=21天", stats)

    # 5. 收紧 RS 分位
    stats = run_one(btc, pool, RS_TOP_PERCENT=0.10)
    print_row("RS前10%", stats)

    # 6. 组合：收紧追高+更严放量
    stats = run_one(btc, pool, MAX_ENTRY_GAP=0.03, VOLUME_BREAKOUT_MULT=2.0)
    print_row("追高3%+放量2x", stats)

    print("=" * 100)


if __name__ == "__main__":
    main()
