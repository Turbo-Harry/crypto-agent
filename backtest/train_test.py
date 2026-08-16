"""
训练/测试分离回测 — 防过拟合验证。

方法：
  训练集：2020-08 ~ 2023-12（用固定默认参数跑）
  测试集：2024-01 ~ 2026-08（同样参数，完全没碰过的数据）

判据：
  - 训练集正期望 且 测试集正期望 → 策略可能有效
  - 训练集好、测试集差 → 过拟合（弃用）
  - 训练集就差 → 方向有问题

关键：不调参，用 config 里的默认值，只看逻辑是否在样本外成立。
"""
import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols
from backtest.engine import Backtest

SPLIT_TS = 1704067200000  # 2024-01-01 00:00:00 UTC


def load_okx_data():
    # 优先从 API 拉观察池；API 不可用（如服务器受限）则用缓存列表
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
            if len(k) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
                pool_klines[sym] = k
        except Exception:
            pass
    return btc, pool_klines


def split(btc, pool_klines):
    btc_train = [k for k in btc if k["open_time"] < SPLIT_TS]
    btc_test = [k for k in btc if k["open_time"] >= SPLIT_TS]
    pool_train = {}
    pool_test = {}
    for s, kl in pool_klines.items():
        tr = [k for k in kl if k["open_time"] < SPLIT_TS]
        te = [k for k in kl if k["open_time"] >= SPLIT_TS]
        if len(tr) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
            pool_train[s] = tr
        if len(te) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
            pool_test[s] = te
    return (btc_train, pool_train), (btc_test, pool_test)


def print_stats(name, stats, span):
    print(f"{name} ({span})")
    print(f"  交易 {stats['交易次数']} | 胜率 {stats['胜率']*100:.1f}% | "
          f"盈亏比 {stats['盈亏比']:.2f} | 收益 {stats['总收益率']*100:+.2f}% | "
          f"回撤 {stats['最大回撤']*100:.1f}%")


def main():
    print("拉取 OKX 数据...")
    btc, pool_klines = load_okx_data()
    print(f"观察池 {len(pool_klines)} 币，BTC {len(btc)} 根")
    (btc_tr, pool_tr), (btc_te, pool_te) = split(btc, pool_klines)
    print(f"训练集 {len(btc_tr)} 根 ({pool_tr and len(pool_tr)} 币) | "
          f"测试集 {len(btc_te)} 根 ({pool_te and len(pool_te)} 币)\n")

    print("=" * 70)
    print("默认参数（不调参）：回踩确认入场 箱体14天 放量1.5x RS前20% 止损3% 止盈5%")
    print("=" * 70)

    # 训练集
    bt = Backtest(btc_tr, pool_tr, initial_equity=100_000)
    s_tr = bt.run()
    print_stats("训练集", s_tr, "2020-08~2023-12")

    # 测试集（同样参数，样本外）
    bt = Backtest(btc_te, pool_te, initial_equity=100_000)
    s_te = bt.run()
    print_stats("测试集", s_te, "2024-01~2026-08")

    print("=" * 70)
    # 判读
    tr_pos = s_tr["总收益率"] > 0
    te_pos = s_te["总收益率"] > 0
    if tr_pos and te_pos:
        print("结论：两段都正 → 策略可能有效（仍建议纸面交易验证）")
    elif tr_pos and not te_pos:
        print("结论：训练集好、测试集差 → 过拟合/失效，弃用")
    elif not tr_pos:
        print("结论：训练集就负 → 策略方向有问题，需换思路")
    else:
        print("结论：样本外无正期望，不可用")


if __name__ == "__main__":
    main()
