"""
因子挖掘（Factor Mining）— 从数据中系统性地发现能预测收益的因子。

方法（量化标准流程）：
  1. 候选因子：技术指标 + 情绪面 + 资金费率等
  2. IC 检验：因子值与未来收益的 Spearman 秩相关（IC > 0.03 通常有效）
  3. ICIR：IC 均值/标准差（稳定性，> 0.3 较好）
  4. 分层检验：按因子值分组，看未来收益是否单调

数据源：
  - 恐惧贪婪指数（alternative.me，8 年历史）
  - 资金费率（Gate.io）
  - BTC 日线（OKX，6 年）
"""
import sys
import os
import json
import math
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.fetch_fear_greed import fetch_fng
from data.fetch_okx import fetch_btc_klines


def spearman(x, y):
    """Spearman 秩相关系数（IC）。"""
    n = len(x)
    if n < 10:
        return 0.0, 0
    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        ranks = [0] * n
        for r, idx in enumerate(order):
            ranks[idx] = r
        return ranks
    rx = rank(x)
    ry = rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    vy = math.sqrt(sum((r - my) ** 2 for r in ry))
    if vx * vy == 0:
        return 0.0, n
    return cov / (vx * vy), n


def test_factor(name, factor_dates, factor_values, btc_klines, horizon_days=7):
    """
    单因子检验：因子值 vs 未来 horizon 天 BTC 收益。
    返回 {IC, ICIR, 分层收益}。
    """
    # 对齐：因子日期 → BTC 未来收益
    btc_by_date = {}
    for i, k in enumerate(btc_klines):
        d = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        btc_by_date[d] = (i, k["close"])

    pairs = []  # (因子值, 未来收益)
    for d, v in zip(factor_dates, factor_values):
        if d not in btc_by_date:
            continue
        i, close = btc_by_date[d]
        if i + horizon_days >= len(btc_klines):
            continue
        future = (btc_klines[i + horizon_days]["close"] - close) / close
        pairs.append((v, future))
    if len(pairs) < 30:
        return {"name": name, "IC": 0, "样本": len(pairs)}

    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    ic, n = spearman(x, y)

    # 分层检验：按因子值分 3 组，看未来收益
    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    third = len(sorted_pairs) // 3
    groups = {
        "低因子组": sorted_pairs[:third],
        "中因子组": sorted_pairs[third:2 * third],
        "高因子组": sorted_pairs[2 * third:],
    }
    layered = {}
    for g, gp in groups.items():
        if gp:
            layered[g] = sum(p[1] for p in gp) / len(gp)

    return {
        "name": name,
        "IC": ic,
        "样本": n,
        "分层平均收益": layered,
    }


def main():
    print("=" * 60)
    print("因子挖掘 — 单因子 IC 检验")
    print("=" * 60)

    btc = fetch_btc_klines()
    fng = fetch_fng()
    print(f"数据: BTC 日线 {len(btc)} 根, 恐惧贪婪 {len(fng)} 条\n")

    results = []

    # 因子 1: 恐惧贪婪指数（情绪因子）
    # 假设：恐惧（低分）→ 未来涨（反向指标），所以用 -value 检验
    fng_dates = [f["date"] for f in fng]
    fng_values = [f["value"] for f in fng]
    r = test_factor("恐惧贪婪(反向)", fng_dates, [-v for v in fng_values], btc, 7)
    results.append(r)

    # 因子 2: 恐惧贪婪变化（情绪转变）
    fng_chg = []
    for i in range(1, len(fng)):
        chg = fng[i]["value"] - fng[i - 1]["value"]
        fng_chg.append(-chg)  # 恐惧加深（值下降）→ 未来涨
    r = test_factor("恐惧贪婪变化(反向)", fng_dates[1:], fng_chg, btc, 7)
    results.append(r)

    # 因子 3-6: 技术指标（对比验证）
    closes = [k["close"] for k in btc]
    dates = [datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
             for k in btc]

    # 动量因子（过去 7 天收益，反向：涨多了跌）
    mom = []
    mom_dates = []
    for i in range(7, len(closes)):
        mom.append(-(closes[i] - closes[i - 7]) / closes[i - 7])
        mom_dates.append(dates[i])
    r = test_factor("动量7天(反向)", mom_dates, mom, btc, 7)
    results.append(r)

    # RSI 因子
    from strategy.indicators import ema
    def rsi_series(c, period=14):
        out = [50.0] * len(c)
        for i in range(1, len(c)):
            pass
        return out
    # 简化：用均线偏离因子（价格 vs EMA50，反向）
    ema50 = ema(closes, 50)
    dist = []
    dist_dates = []
    for i in range(50, len(closes)):
        dist.append(-(closes[i] - ema50[i]) / ema50[i])  # 高于均线 → 未来跌
        dist_dates.append(dates[i])
    r = test_factor("均线偏离50(反向)", dist_dates, dist, btc, 7)
    results.append(r)

    # 输出
    print(f"{'因子':<22} {'IC':>8} {'样本':>6} {'判断':>8}")
    print("-" * 52)
    for r in results:
        ic = r["IC"]
        verdict = "✅ 有效" if abs(ic) > 0.03 else ("⚠️ 弱" if abs(ic) > 0.015 else "❌ 无效")
        print(f"{r['name']:<22} {ic:>+8.4f} {r['样本']:>6} {verdict:>8}")
        if "分层平均收益" in r:
            for g, v in r["分层平均收益"].items():
                print(f"    {g}: 未来7天平均收益 {v*100:+.2f}%")
    print("=" * 60)
    print("IC 解读: |IC|>0.03 有效, 0.015-0.03 弱, <0.015 无效")
    print("分层收益应单调（低→高 或 高→低），否则因子无区分度")


if __name__ == "__main__":
    main()
