"""
多策略组合 — 把多个低相关策略的日收益等权叠加，平滑曲线、降低回撤。
组合逻辑：每策略独立跑，取各自日收益，等权平均后累积成组合净值。

组合的策略：
  1. 箱体突破+回踩确认（趋势/突破型，收益低回撤低）
  2. 双均线多空（趋势型，收益高回撤中）
  3. 动量轮动多空（动量型，收益中回撤中）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols
from backtest.engine import Backtest
from backtest.trend_follow import TrendFollow
from backtest.momentum import MomentumBacktest


def load_data():
    try:
        pool = build_observe_pool(config.OBSERVE_POOL_SIZE)
        symbols = [p["instId"] for p in pool]
    except Exception:
        symbols = list_cached_symbols()
    btc = fetch_btc_klines()
    pool_klines = {}
    for sym in symbols:
        try:
            k = fetch_klines(sym)
            if len(k) >= 90:
                pool_klines[sym] = k
        except Exception:
            pass
    return btc, pool_klines


def daily_returns(equity_curve):
    """净值序列 → 日收益序列。"""
    rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        cur = equity_curve[i]
        if prev > 0:
            rets.append(cur / prev - 1)
    return rets


def combine(returns_list):
    """多个策略的日收益序列 → 组合净值。"""
    # 对齐长度（取最短）
    min_len = min(len(r) for r in returns_list)
    combined_equity = [1.0]
    for i in range(min_len):
        avg_ret = sum(r[i] for r in returns_list) / len(returns_list)
        combined_equity.append(combined_equity[-1] * (1 + avg_ret))
    return combined_equity


def stats_from_equity(equity, initial=100_000):
    final = equity[-1] * initial
    peak = -1e18
    max_dd = 0
    for eq in equity:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    years = len(equity) / 365
    total = equity[-1] - 1
    annual = (equity[-1]) ** (1 / years) - 1 if years > 0 else 0
    return {
        "总收益率": total,
        "年化收益": annual,
        "最大回撤": max_dd,
    }


def print_row(name, stats):
    annual = stats.get('年化收益', 0)
    print(f"{name:<22} | 收益{stats['总收益率']*100:>+8.1f}% | "
          f"年化{annual*100:>+6.1f}% | 回撤{stats['最大回撤']*100:>5.1f}%")


def main():
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 70)

    # 1. 箱体突破+回踩确认
    bt = Backtest(btc, pool, initial_equity=100_000)
    s1 = bt.run()
    eq1 = [e[1] / 100_000 for e in bt.equity_curve]  # 归一化
    print_row("箱体突破(多空过滤)", s1)

    # 2. 双均线多空
    tf = TrendFollow(pool, btc)
    s2 = tf.run(allow_short=True)
    eq2 = [e / 100_000 for e in tf.equity_curve]
    print_row("双均线多空", s2)

    # 3. 动量多空
    mm = MomentumBacktest(pool, btc)
    s3 = mm.run(short_n=5)
    eq3 = [e / 100_000 for e in mm.equity_curve]
    print_row("动量多空", s3)

    print("-" * 70)

    # 组合
    r1 = daily_returns(eq1)
    r2 = daily_returns(eq2)
    r3 = daily_returns(eq3)
    combined = combine([r1, r2, r3])
    cs = stats_from_equity(combined)
    print_row("三策略等权组合", cs)

    print("=" * 70)
    print("\n说明：等权组合 = 每天把三个策略的收益取平均，")
    print("相当于把资金三等分投到三个策略。低相关性能平滑回撤。")


if __name__ == "__main__":
    main()
