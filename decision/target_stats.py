# -*- coding: utf-8 -*-
"""
目标命中率（2026-08-23 用户问"会预测会升到什么价位吗"）——
不预测点位,只给【实证概率】: 历史同向信号里,+1R/+2R 在止损前被触达的
比例(trade_features.mfe_r 已记录每笔最大有利偏离)。给目标的"可信度",
不给"保证价"。纯读库,零网络。
"""


def hit_rates(db_path=None, direction=None, min_n=5):
    """历史命中率: {n, p1r, p2r, median_mfe_r}。
    p1r = mfe_r≥1 的占比(P(先摸到 +1R 再谈止损)),p2r 同理 ≥2。
    样本 < min_n 返回 None(小样本不给概率,宁可说不知道)。"""
    import storage.db as sdb
    try:
        sdb.init_db(db_path)
        if direction:
            rows = sdb.q("SELECT mfe_r FROM trade_features "
                         "WHERE mfe_r IS NOT NULL AND direction=?",
                         [direction], db_path=db_path)
        else:
            rows = sdb.q("SELECT mfe_r FROM trade_features "
                         "WHERE mfe_r IS NOT NULL", db_path=db_path)
        vals = sorted(float(r["mfe_r"]) for r in rows)
        n = len(vals)
        if n < min_n:
            return {"n": n, "p1r": None, "p2r": None, "median_mfe_r": None}
        p1 = sum(1 for v in vals if v >= 1.0) / n
        p2 = sum(1 for v in vals if v >= 2.0) / n
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        return {"n": n, "p1r": round(p1, 3), "p2r": round(p2, 3),
                "median_mfe_r": round(med, 2)}
    except Exception:
        return {"n": 0, "p1r": None, "p2r": None, "median_mfe_r": None}


def describe(db_path=None, direction=None):
    """中文一句话(开仓通知用)。样本不足时诚实说明。"""
    h = hit_rates(db_path, direction)
    if not h or h["p1r"] is None:
        return f"历史命中样本不足({h['n'] if h else 0}笔),不给概率"
    return (f"历史{h['n']}笔同向信号: 先摸+1R 概率 {h['p1r']*100:.0f}% · "
            f"+2R 概率 {h['p2r']*100:.0f}% · 中位最大有利偏离 {h['median_mfe_r']}R")
