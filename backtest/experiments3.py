"""
实验3 — 回踩确认入场下的参数组合验证。
重点：确认 +14.73% 是否稳健，以及叠加参数能否更好。
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
        return Backtest(btc, pool, initial_equity=100_000).run()
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def print_row(name, stats):
    print(f"{name:<26} | 交易{stats['交易次数']:>3} | 胜率{stats['胜率']*100:>5.1f}% | "
          f"盈亏比{stats['盈亏比']:>5.2f} | 收益{stats['总收益率']*100:>+7.2f}% | "
          f"回撤{stats['最大回撤']*100:>5.1f}%")


def main():
    print("加载数据...")
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 104)

    combos = [
        ("回踩确认(基线14天)", {}),
        ("回踩+箱体21天", {"BOX_MIN_DAYS": 21}),
        ("回踩+RS前15%", {"RS_TOP_PERCENT": 0.15}),
        ("回踩+放量1.8x", {"VOLUME_BREAKOUT_MULT": 1.8}),
        ("回踩+箱体21天+RS15%", {"BOX_MIN_DAYS": 21, "RS_TOP_PERCENT": 0.15}),
        ("回踩+箱体21天+放量1.8x", {"BOX_MIN_DAYS": 21, "VOLUME_BREAKOUT_MULT": 1.8}),
        ("回踩+回踩窗口3天", {"PULLBACK_WINDOW": 3}),
        ("回踩+回踩窗口8天", {"PULLBACK_WINDOW": 8}),
    ]

    for name, ov in combos:
        stats = run_one(btc, pool, **ov)
        print_row(name, stats)

    print("=" * 104)
    print("\n说明：回踩确认入场是【结构改进】（改逻辑），参数组合是【微调】。")
    print("若基线(回踩确认)已稳健转正，则方向成立，参数微调只做小幅增益。")


if __name__ == "__main__":
    main()
