"""
自动因子挖掘（Automated Factor Discovery）— 用遗传编程进化出未知因子。
gplearn 的 SymbolicTransformer 自动组合基础算子，进化出 IC 高的因子表达式。

原理：
  基础变量（价格/量/指标/情绪）
    ↓ 遗传进化组合（+ − × ÷ min max lag abs log...）
  候选因子表达式（进化出人类没预设过的）
    ↓ Spearman IC 检验（因子值 vs 未来收益）
  有效因子（IC 显著，训练/测试分离防过拟合）
"""
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import numpy as np
from data.fetch_okx import fetch_btc_klines
from data.fetch_fear_greed import fetch_fng

SPLIT_TS = 1704067200000  # 训练/测试切分


def rsi(closes, period=14):
    n = len(closes)
    out = [50.0] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, n):
        out[i] = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
    return out


def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def build_base_variables(klines, fng_map):
    """构造基础变量矩阵（每行一天）。"""
    closes = np.array([k["close"] for k in klines])
    highs = np.array([k["high"] for k in klines])
    lows = np.array([k["low"] for k in klines])
    vols = np.array([k["volume"] for k in klines])
    n = len(closes)

    # 日期 → 恐惧贪婪
    fng_vals = []
    for k in klines:
        d = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        fng_vals.append(fng_map.get(d, 50))
    fng_vals = np.array(fng_vals)

    rsi14 = np.array(rsi(list(closes), 14))
    ema20 = np.array(ema(list(closes), 20))
    ema50 = np.array(ema(list(closes), 50))

    X = {}
    X["close"] = closes
    X["ret1"] = np.zeros(n); X["ret1"][1:] = closes[1:] / closes[:-1] - 1
    X["ret7"] = np.zeros(n); X["ret7"][7:] = closes[7:] / closes[:-7] - 1
    X["ret30"] = np.zeros(n); X["ret30"][30:] = closes[30:] / closes[:-30] - 1
    X["dist20"] = (closes - ema20) / ema20
    X["dist50"] = (closes - ema50) / ema50
    X["rsi"] = rsi14 / 100
    X["vol"] = vols / (np.mean(vols) + 1e-9)
    X["fng"] = fng_vals / 100
    X["range"] = (highs - lows) / closes
    return X, closes


def main():
    from gplearn.genetic import SymbolicTransformer
    from gplearn.functions import make_function

    print("加载数据...")
    btc = fetch_btc_klines()
    fng = fetch_fng()
    fng_map = {f["date"]: f["value"] for f in fng}
    X_dict, closes = build_base_variables(btc, fng_map)

    # 特征矩阵
    names = list(X_dict.keys())
    X = np.column_stack([X_dict[name] for name in names])
    n = len(closes)

    # 标签：未来 7 天收益
    horizon = 7
    y = np.zeros(n)
    y[:n - horizon] = closes[horizon:] / closes[:-horizon] - 1

    # 训练/测试切分
    dates = np.array([k["open_time"] for k in btc])
    train = dates < SPLIT_TS
    test = ~train
    X_tr, y_tr = X[train][:-horizon], y[train][:-horizon]
    X_te, y_te = X[test][:-horizon], y[test][:-horizon]
    print(f"基础变量 {len(names)} 个: {names}")
    print(f"训练 {len(y_tr)} 天 | 测试 {len(y_te)} 天\n")

    # 遗传编程挖掘因子（目标：最大化 Spearman IC）
    print("遗传编程进化中（约 1-2 分钟）...")
    st = SymbolicTransformer(
        population_size=2000,
        generations=15,
        tournament_size=20,
        function_set=["add", "sub", "mul", "div", "min", "max", "neg", "abs", "log", "sqrt"],
        metric="spearman",
        parsimony_coefficient=0.002,
        random_state=42,
        n_jobs=-1,
        verbose=0,
    )
    st.fit(X_tr, y_tr)

    # 挖出的因子（训练集 + 测试集 IC）
    factors_tr = st.transform(X_tr)
    factors_te = st.transform(X_te)
    from scipy.stats import spearmanr

    print("=" * 66)
    print("挖出的未知因子（进化出的表达式）:")
    print("=" * 66)
    results = []
    for i, expr in enumerate(st._best_programs):
        ic_tr, _ = spearmanr(factors_tr[:, i], y_tr)
        ic_te, _ = spearmanr(factors_te[:, i], y_te)
        results.append((ic_te, ic_tr, str(expr), factors_te[:, i]))
    results.sort(key=lambda x: -abs(x[0]))
    for ic_te, ic_tr, expr, _ in results[:8]:
        verdict = "✅" if abs(ic_te) > 0.03 else ("⚠️" if abs(ic_te) > 0.015 else "❌")
        print(f"{verdict} 测试IC {ic_te:+.4f} | 训练IC {ic_tr:+.4f} | {expr}")

    # 最佳因子的分层收益（测试集）
    if results:
        _, _, _, best_f = results[0]
        order = np.argsort(best_f)
        third = len(order) // 3
        for gname, idx in [("低因子组", order[:third]), ("中因子组", order[third:2*third]), ("高因子组", order[2*third:])]:
            if len(idx):
                print(f"  {gname}: 未来7天平均收益 {y_te[idx].mean()*100:+.2f}%")


if __name__ == "__main__":
    main()
