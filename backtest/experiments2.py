"""
组合实验 — 叠加已验证有效的改进（箱体延长、放量加强、RS收紧），
找出稳健的正期望配置。注意：样本量小，结果需谨慎解读（存在过拟合风险）。
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
    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    try:
        bt = Backtest(btc, pool, initial_equity=100_000)
        return bt.run()
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def print_row(name, stats):
    print(f"{name:<30} | 交易{stats['交易次数']:>3} | 胜率{stats['胜率']*100:>5.1f}% | "
          f"盈亏比{stats['盈亏比']:>5.2f} | 收益{stats['总收益率']*100:>+7.2f}% | "
          f"回撤{stats['最大回撤']*100:>5.1f}%")


def main():
    print("加载数据...")
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 108)

    combos = [
        ("基线", {}),
        ("箱体21天", {"BOX_MIN_DAYS": 21}),
        ("箱体21天+放量2x", {"BOX_MIN_DAYS": 21, "VOLUME_BREAKOUT_MULT": 2.0}),
        ("箱体21天+RS前10%", {"BOX_MIN_DAYS": 21, "RS_TOP_PERCENT": 0.10}),
        ("箱体21天+放量2x+RS前10%", {"BOX_MIN_DAYS": 21, "VOLUME_BREAKOUT_MULT": 2.0, "RS_TOP_PERCENT": 0.10}),
        ("箱体28天", {"BOX_MIN_DAYS": 28}),
        ("箱体28天+放量2x", {"BOX_MIN_DAYS": 28, "VOLUME_BREAKOUT_MULT": 2.0}),
        ("箱体21天+放量1.8x", {"BOX_MIN_DAYS": 21, "VOLUME_BREAKOUT_MULT": 1.8}),
        ("箱体21天+RS前15%", {"BOX_MIN_DAYS": 21, "RS_TOP_PERCENT": 0.15}),
        ("箱体21天+追高5%", {"BOX_MIN_DAYS": 21, "MAX_ENTRY_GAP": 0.05}),
    ]

    for name, ov in combos:
        stats = run_one(btc, pool, **ov)
        print_row(name, stats)

    print("=" * 108)
    print("\n提示：样本量偏小（几十笔），+1%~+2% 的收益在统计上未必显著，")
    print("这些结果用于找方向，最终必须纸面交易验证，不可直接视为'能赚钱'。")


if __name__ == "__main__":
    main()
