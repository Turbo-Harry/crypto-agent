"""
小型机器学习量化模型 — 用随机森林从技术指标特征中学习"未来上涨"的规律。
对比人工规则策略，这是"数据驱动"的量化方法。

流程：
  1. 特征工程：每个币每天算一组技术指标特征
  2. 标签：未来 N 天收益率 > 阈值 → 1（会涨），否则 0
  3. 训练：随机森林，训练/测试时间分离（防过拟合）
  4. 评估：准确率、AUC、测试集模拟交易

特征（13个）：RSI、动量(5/10/20)、均线偏离(20/50)、ATR比率、量比、价格位置、波动率
标签：未来 5 天收益 > 2%
"""
import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols

SPLIT_TS = 1704067200000  # 2024-01-01，训练/测试切分


def rsi(closes, period=14):
    """RSI 序列（与 mean_reversion.py 一致）。"""
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


def build_dataset(klines, lookback=20, horizon=5, threshold=0.02):
    """从单币 K 线构造特征矩阵 X 和标签 y。
    每行 = 某一天的特征 + 未来 horizon 天是否涨超 threshold。
    """
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    vols = [k["volume"] for k in klines]
    n = len(klines)

    rsi_14 = rsi(closes, 14)
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    X, y, dates = [], [], []
    for i in range(lookback, n - horizon):
        c = closes[i]
        # 动量
        m5 = (c - closes[i - 5]) / closes[i - 5] if i >= 5 else 0
        m10 = (c - closes[i - 10]) / closes[i - 10] if i >= 10 else 0
        m20 = (c - closes[i - 20]) / closes[i - 20] if i >= 20 else 0
        # 均线偏离
        d20 = (c - ema20[i]) / ema20[i]
        d50 = (c - ema50[i]) / ema50[i]
        # ATR 比率（波动率）
        trs = []
        for j in range(max(1, i - 13), i + 1):
            tr = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
            trs.append(tr)
        atr_val = sum(trs) / len(trs)
        atr_ratio = atr_val / c
        # 量比
        vol_ratio = vols[i] / (sum(vols[i - 20:i]) / 20) if i >= 20 else 1.0
        # 价格在 20 天区间的位置（0-1）
        hh = max(highs[i - 20:i + 1])
        ll = min(lows[i - 20:i + 1])
        pos = (c - ll) / (hh - ll) if hh > ll else 0.5

        feat = [rsi_14[i] / 100, m5, m10, m20, d20, d50, atr_ratio,
                vol_ratio, pos, rsi_14[i] / 50 - 1, (c - closes[i - 1]) / closes[i - 1],
                (highs[i] - lows[i]) / c]
        X.append(feat)
        # 标签：未来 horizon 天收益
        future_ret = (closes[i + horizon] - c) / c
        y.append(1 if future_ret > threshold else 0)
        dates.append(klines[i]["open_time"])
    return np.array(X), np.array(y), dates


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
            if len(k) >= 200:
                pool_klines[sym] = k
        except Exception:
            pass
    return btc, pool_klines


def main():
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, roc_auc_score, confusion_matrix

    print("加载数据...")
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币")

    # 构造全量特征集
    X_all, y_all, dates_all = [], [], []
    for sym, kl in pool.items():
        X, y, dates = build_dataset(kl)
        X_all.append(X)
        y_all.append(y)
        dates_all.append(dates)
    X = np.vstack(X_all)
    y = np.concatenate(y_all)
    dates_flat = np.concatenate(dates_all)
    print(f"总样本 {len(y)} 条，正例(涨>2%) {y.sum()} 条 ({y.mean()*100:.1f}%)")

    # 训练/测试时间切分
    train_mask = dates_flat < SPLIT_TS
    test_mask = dates_flat >= SPLIT_TS
    X_tr, y_tr = X[train_mask], y[train_mask]
    X_te, y_te = X[test_mask], y[test_mask]
    print(f"训练集 {len(y_tr)} 条 | 测试集 {len(y_te)} 条")

    # 训练随机森林
    model = RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=50,
                                   random_state=42, n_jobs=-1)
    model.fit(X_tr, y_tr)

    # 评估
    y_pred_tr = model.predict(X_tr)
    y_prob_te = model.predict_proba(X_te)[:, 1]
    y_pred_te = model.predict(X_te)
    print("\n=== 模型评估 ===")
    print(f"训练集准确率: {accuracy_score(y_tr, y_pred_tr)*100:.1f}%")
    print(f"测试集准确率: {accuracy_score(y_te, y_pred_te)*100:.1f}%")
    print(f"测试集 AUC: {roc_auc_score(y_te, y_prob_te):.3f}")
    tn, fp, fn, tp = confusion_matrix(y_te, y_pred_te).ravel()
    print(f"混淆矩阵: TP={tp} FP={fp} TN={tn} FN={fn}")
    print(f"精确率: {tp/(tp+fp)*100:.1f}%  召回率: {tp/(tp+fn)*100:.1f}%")

    # 特征重要性
    feat_names = ['RSI','mom5','mom10','mom20','dist20','dist50','atr_ratio',
                  'vol_ratio','pos20','rsi_dev','ret1','range']
    imp = sorted(zip(feat_names, model.feature_importances_), key=lambda x: -x[1])
    print("\n特征重要性 Top5:")
    for name, v in imp[:5]:
        print(f"  {name}: {v:.3f}")

    # 测试集模拟交易：只在高置信度（prob>0.6）时买入
    print("\n=== 测试集模拟交易（高置信度 prob>0.6 才买）===")
    for prob_th in [0.55, 0.60, 0.65, 0.70]:
        picks = y_prob_te >= prob_th
        if picks.sum() == 0:
            continue
        # 这些被选中的样本，实际未来收益（用标签近似，标签=涨>2%）
        win_rate = y_te[picks].mean()
        print(f"  prob>{prob_th}: 选 {picks.sum()} 次, 实际涨(>2%)概率 {win_rate*100:.1f}%")


if __name__ == "__main__":
    main()
