# -*- coding: utf-8 -*-
"""
预测机制（2026-08-23 用户要求"最好能有预测机制"）——
不报"会涨到 12345"这种伪精确点位,报【概率分布】+【触达概率】,并自我校准:

  1. 分布预测: 用近 FORECAST_LOOKBACK_BARS 根 1h 对数收益做自助采样
     (bootstrap,当前波动率 regime 自适应的非参数方法),
     生成 FORECAST_HORIZON_HOURS 小时后的价格分布 → 中位/5%/95% 分位。
  2. 触达概率: 每条模拟路径上判定"先触 TP(2R) 还是先触 SL(1R)"
     → P(触TP)/P(触SL);与历史同向信号实证命中率(target_stats)按
     FORECAST_BLEND 混合(历史样本≥MIN_EMP_N 才混)。
  3. 自我校准: 每笔平仓把预测 vs 实际落 forecast_calibration,
     定期算 Brier 分数——预测准不准,数据说话,不准就降权。
"""
import json
import math
import random
import time

import config


def _returns(closes):
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def _quantile(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def forecast(entry, atr, direction, stop, tp, hourly_returns,
             horizon=24, paths=500, emp_p_tp=None, emp_p_sl=None,
             blend=0.5, seed=None):
    """纯函数: bootstrap 价格分布 + 触达概率。返回 dict。
    hourly_returns: 1h 对数收益序列(近 N 根,越近越代表当前 regime)。
    emp_p_tp/emp_p_sl: 历史同向信号实证概率(可为 None,不混)。
    blend: 实证概率的权重(0.5 = bootstrap 与历史各占一半)。"""
    if not hourly_returns or atr is None or atr <= 0 or entry <= 0:
        return None
    rng = random.Random(seed)
    n = len(hourly_returns)
    finals = []
    hit_tp = hit_sl = 0
    tp_r = tp - entry if direction == "long" else entry - tp
    sl_r = entry - stop if direction == "long" else stop - entry
    if tp_r <= 0 or sl_r <= 0:
        return None
    for _ in range(paths):
        px = entry
        tp_hit = sl_hit = False
        for _ in range(horizon):
            r = hourly_returns[rng.randrange(n)]
            px = px * math.exp(r)
            if direction == "long":
                if px >= tp:
                    tp_hit = True
                    break
                if px <= stop:
                    sl_hit = True
                    break
            else:
                if px <= tp:
                    tp_hit = True
                    break
                if px >= stop:
                    sl_hit = True
                    break
        finals.append(px)
        hit_tp += tp_hit
        hit_sl += sl_hit
    p_tp_b = hit_tp / paths
    p_sl_b = hit_sl / paths
    p_tp = p_tp_b
    p_sl = p_sl_b
    if emp_p_tp is not None and emp_p_sl is not None:
        p_tp = (1 - blend) * p_tp + blend * emp_p_tp
        p_sl = (1 - blend) * p_sl + blend * emp_p_sl
    return {
        "median": round(_quantile(finals, 0.5), 6),
        "q05": round(_quantile(finals, 0.05), 6),
        "q95": round(_quantile(finals, 0.95), 6),
        "p_hit_tp": round(min(1.0, max(0.0, p_tp)), 3),
        "p_hit_sl": round(min(1.0, max(0.0, p_sl)), 3),
        "horizon_hours": horizon,
        "paths": paths,
    }


def forecast_for_trade(sig, base, klines, db_path=None):
    """信号命中时的预测入口: klines=scan 已取的 1h K 线(复用,零额外网络)。
    返回 dict 或 None(数据不足)。"""
    if not getattr(config, "FORECAST_ENABLED", False):
        return None
    try:
        closes = [k.get("close") for k in (klines or []) if k.get("close")]
        if len(closes) < 60:
            return None
        rets = _returns(closes[-config.FORECAST_LOOKBACK_BARS:])
        if len(rets) < 30:
            return None
        emp_p_tp = emp_p_sl = None
        from decision.target_stats import hit_rates
        h = hit_rates(db_path, sig.get("dir"))
        if h and h["p2r"] is not None and h["n"] >= config.FORECAST_MIN_EMP_N:
            emp_p_tp = h["p2r"]
            emp_p_sl = round(1 - h["p1r"], 3) if h["p1r"] is not None else None
        fc = forecast(entry=float(sig.get("entry")),
                      atr=float(sig.get("atr") or 0),
                      direction=sig.get("dir"),
                      stop=float(sig.get("stop") or 0),
                      tp=float(sig.get("tp") or 0),
                      hourly_returns=rets,
                      horizon=config.FORECAST_HORIZON_HOURS,
                      paths=config.FORECAST_PATHS,
                      emp_p_tp=emp_p_tp, emp_p_sl=emp_p_sl,
                      blend=config.FORECAST_BLEND)
        return fc
    except Exception:
        return None


def describe(fc):
    """开仓通知/AI 快照用的一句话预测描述。"""
    if not fc:
        return "预测数据不足"
    return (f"{fc['horizon_hours']}h: 中位 {fc['median']} · "
            f"5-95% [{fc['q05']}, {fc['q95']}] · "
            f"P(触止盈)={fc['p_hit_tp']*100:.0f}% · "
            f"P(触止损)={fc['p_hit_sl']*100:.0f}%")


def record_outcome(trade_id, forecast_json, closed, db_path=None):
    """平仓时把预测与实际落表(自我校准数据)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        fc = json.loads(forecast_json) if forecast_json else None
        if not fc:
            return
        pnl = closed.get("pnl")
        hit_tp = 1 if (pnl or 0) >= 0.02 else 0      # ≥+2% ≈ 触到 2R 区间
        hit_sl = 1 if (pnl or 0) <= -0.01 else 0     # ≤-1% ≈ 触到 1R 止损
        sdb.x("INSERT INTO forecast_calibration (trade_id, ts, p_hit_tp, "
              "p_hit_sl, hit_tp, hit_sl, pnl) VALUES (?,?,?,?,?,?,?)",
              [trade_id, time.time(), fc.get("p_hit_tp"), fc.get("p_hit_sl"),
               hit_tp, hit_sl, round(float(pnl or 0), 6)], db_path=db_path)
    except Exception:
        pass


def calibration(db_path=None, min_n=10):
    """预测校准报告: Brier 分数(越低越准,0.25=瞎猜基线)+ 分桶校准。
    样本 < min_n 诚实返回 None 数据。"""
    import storage.db as sdb
    try:
        sdb.init_db(db_path)
        rows = sdb.q("SELECT p_hit_tp, p_hit_sl, hit_tp, hit_sl FROM "
                     "forecast_calibration WHERE p_hit_tp IS NOT NULL",
                     db_path=db_path)
        n = len(rows)
        if n < min_n:
            return {"n": n, "brier_tp": None, "brier_sl": None, "buckets": {}}
        b_tp = sum((r["p_hit_tp"] - r["hit_tp"]) ** 2 for r in rows) / n
        b_sl = sum((r["p_hit_sl"] - r["hit_sl"]) ** 2 for r in rows) / n
        # 分桶校准: 预测概率 0-0.2/0.2-0.4/... 的实际命中率
        buckets = {}
        for r in rows:
            for key, p, hit in (("tp", r["p_hit_tp"], r["hit_tp"]),
                                ("sl", r["p_hit_sl"], r["hit_sl"])):
                b = min(4, int(p * 5))   # 5 桶
                bk = buckets.setdefault(f"{key}_{b}", {"n": 0, "p_sum": 0.0,
                                                        "hit": 0})
                bk["n"] += 1
                bk["p_sum"] += p
                bk["hit"] += hit
        out = {k: {"n": v["n"], "avg_p": round(v["p_sum"] / max(v["n"], 1), 3),
                   "hit_rate": round(v["hit"] / max(v["n"], 1), 3)}
               for k, v in buckets.items()}
        return {"n": n, "brier_tp": round(b_tp, 4), "brier_sl": round(b_sl, 4),
                "buckets": out}
    except Exception:
        return {"n": 0, "brier_tp": None, "brier_sl": None, "buckets": {}}
