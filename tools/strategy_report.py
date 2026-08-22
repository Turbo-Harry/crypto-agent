#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略体检报告（Phase 2 评估引擎）—— 滚动标准指标 + 数据质量，供人/看板/AI 读。

指标与门槛（业界标准，见设计文档 §2）:
  expectancy/R 倍数、Profit Factor、胜率、最大回撤（累计盈亏口径）、
  SQN（Tharp: ≥30 笔才报,<2.0 不达标/≥2.5 好）、MAE/MFE 分布（S4,来自
  trade_features）、特征缺失率（Phase 1 质量）。
未达样本门槛的指标如实显示"样本不足"——不夸大（红线 5）。

运行: cd crypto-agent && python3 tools/strategy_report.py [--json 输出路径]
只读生产库;输出控制台 + 可选 JSON(默认 data/strategy_report_latest.json)。
"""
import json
import math
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, os.environ.get("CRYPTO_AGENT_DB") or "crypto_agent.db")
OUT = os.path.join(ROOT, "data", "strategy_report_latest.json")
MIN_SAMPLES = 30          # Tharp 最低样本门槛(S2)


def q(sql, params=()):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def compute():
    closed = q("SELECT * FROM trades WHERE status='closed' "
               "AND pnl IS NOT NULL ORDER BY exit_time")
    n = len(closed)
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = round(len(wins) / n, 4) if n else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = round(gross_profit / gross_loss, 4) if gross_loss > 0 else (
        None if not gross_profit else 99.0)
    expectancy = (sum(pnls) / n) if n else None

    # 最大回撤（累计盈亏口径）
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    max_dd = round(max_dd, 6)

    # SQN = √N × mean(R)/std(R)；R 来自 trade_features.r_multiple（无则回退
    # pnl/止损距——与 tools/readiness.py 同口径,避免特征缺失的币被排除在
    # 样本外(SQN 长期"样本不足"的根因,2026-08-22)。
    feats = {f["trade_id"]: f for f in q(
        "SELECT trade_id, r_multiple, mfe_r, mae_r, features_missing, regime_tag "
        "FROM trade_features")}
    rs = []
    for t in closed:
        f = feats.get(t["id"])
        if f and f["r_multiple"] is not None:
            rs.append(f["r_multiple"])
            continue
        e, s, pnl = t.get("entry_price"), t.get("stop_loss"), t.get("pnl")
        if e and s and pnl is not None and abs(e - s) > 0:
            sd_ = abs(e - s) / e
            if sd_ > 0:
                rs.append(pnl / sd_)
    sqn = None
    if n >= MIN_SAMPLES and len(rs) >= MIN_SAMPLES:
        m = sum(rs) / len(rs)
        sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) \
            if len(rs) > 1 else 0
        if sd > 0:
            sqn = round(math.sqrt(len(rs)) * m / sd, 3)

    # MAE/MFE 分布（R 计）
    mfe = [feats[t["id"]]["mfe_r"] for t in closed
           if t["id"] in feats and feats[t["id"]]["mfe_r"] is not None]
    mae = [feats[t["id"]]["mae_r"] for t in closed
           if t["id"] in feats and feats[t["id"]]["mae_r"] is not None]

    def dist(xs):
        if not xs:
            return None
        xs = sorted(xs)
        return {"n": len(xs), "min": round(xs[0], 3),
                "p25": round(xs[len(xs) // 4], 3),
                "median": round(xs[len(xs) // 2], 3),
                "p75": round(xs[3 * len(xs) // 4], 3),
                "max": round(xs[-1], 3)}

    # 特征缺失率（质量: 生产目标 0%）
    missing_total = 0
    for t in closed:
        if t["id"] in feats:
            missing_total += len((feats[t["id"]]["features_missing"] or "").split(",")) \
                if feats[t["id"]]["features_missing"] else 0
    missing_rate = round(missing_total / max(1, len(feats)) / 21, 4)

    report = {
        "generated_ts": time.time(),
        "n_closed": n,
        "sample_status": ("insufficient" if n < MIN_SAMPLES else "ok"),
        "min_samples": MIN_SAMPLES,
        "win_rate": win_rate,
        "profit_factor": pf,
        "expectancy_pnl": round(expectancy, 6) if expectancy is not None else None,
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
        "max_drawdown": max_dd,
        "sqn": sqn,
        "sqn_verdict": (None if sqn is None else
                        ("好" if sqn >= 2.5 else
                         "不达标(<2.0)" if sqn < 2.0 else "均值区间")),
        "mfe_dist": dist(mfe),
        "mae_dist": dist(mae),
        "features_missing_rate": missing_rate,
        "regimes": {r["regime_tag"]: r["c"] for r in
                    q("SELECT regime_tag, COUNT(*) c FROM trade_features "
                      "GROUP BY regime_tag") if r["regime_tag"]},
    }
    return report


def main():
    rep = compute()
    with open(OUT, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("=" * 56)
    print(f"策略体检报告（{time.strftime('%Y-%m-%d %H:%M:%S')}）")
    print("=" * 56)
    print(f"已平仓: {rep['n_closed']} 笔（样本门槛 {MIN_SAMPLES} → "
          f"{rep['sample_status']}）")
    print(f"胜率: {rep['win_rate'] if rep['win_rate'] is not None else '—'} | "
          f"Profit Factor: {rep['profit_factor']} | "
          f"期望值: {rep['expectancy_pnl']} | 最大回撤: {rep['max_drawdown']}")
    print(f"SQN: {rep['sqn'] if rep['sqn'] is not None else '样本不足不报'} "
          f"{('(' + rep['sqn_verdict'] + ')') if rep['sqn_verdict'] else ''}")
    if rep["mfe_dist"]:
        print(f"MFE 分布(R): {rep['mfe_dist']}")
    if rep["mae_dist"]:
        print(f"MAE 分布(R): {rep['mae_dist']}")
    print(f"特征缺失率: {rep['features_missing_rate']*100:.1f}% "
          f"(生产目标 0%) | regime 分布: {rep['regimes']}")
    print(f"报告已写: {OUT}")
    if rep["n_closed"] < MIN_SAMPLES:
        print("⚠️ 样本不足: 以上统计仅为过程性观察,不得用于任何参数变更"
              "(设计文档 S2 门槛)。")


if __name__ == "__main__":
    main()
