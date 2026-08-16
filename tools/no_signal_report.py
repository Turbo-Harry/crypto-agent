#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
未触发信号复盘报告 —— 回答"为什么没触发信号"（2026-08-17 用户建议）。

数据源: signal_profiles 表（每轮 no_signal 的四环节条件画像）。
输出: 瓶颈分布(趋势/触线/影线/量能哪一环是主要约束)、近失统计、分币画像、
最近样例。瓶颈分布是策略改进的直接证据(如"80% 卡在趋势"→ 换突破型信号;
"40% 近失"→ 门槛略宽即可显著提频)。

运行: cd crypto-agent && python3 tools/no_signal_report.py [--hours 24]
只读生产库。
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "crypto_agent.db")


def q(sql, params=()):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    since = time.time() - args.hours * 3600
    rows = q("SELECT * FROM signal_profiles WHERE ts > ?", [since])
    n = len(rows)
    print("=" * 60)
    print(f"未触发信号复盘报告（近 {args.hours}h，样本 {n}）")
    print("=" * 60)
    if n == 0:
        print("暂无画像数据（采集启动后累积；或时间窗无扫描）")
        return
    from collections import Counter
    bn = Counter(r["bottleneck"] for r in rows)
    nm = sum(r["near_miss"] for r in rows)
    print(f"\n瓶颈分布（信号断在哪一环）:")
    for k in ("trend", "touch", "wick", "vol", "none"):
        c = bn.get(k, 0)
        if c:
            pct = c / n * 100
            print(f"  {k:8} {c:5}  ({pct:4.1f}%)  {'◀ 主要约束' if pct == max(
                bn.get(x, 0) / n * 100 for x in bn)}")
    print(f"\n近失（差一点就触发）: {nm} 次 ({nm/n*100:.1f}%)"
          + ("  ← 门槛微调即可显著提频" if nm / n > 0.2 else ""))
    by_sym = Counter(r["base"] for r in rows)
    print(f"\n分币画像（前 10）:")
    for b, c in by_sym.most_common(10):
        sub = [r for r in rows if r["base"] == b]
        top = Counter(r["bottleneck"] for r in sub).most_common(1)[0][0]
        print(f"  {b:8} {c:5} 次, 主瓶颈 {top}")
    print("\n结论性建议:")
    top = max(bn, key=bn.get)
    if top == "trend":
        print("  → 主要卡在趋势环节: 多数币无 1h 趋势排列,回踩策略天然缺料。")
        print("    方案: 策略 B(突破)影子已在补位;或引入区间市策略(设计 §4 候选 C)。")
    elif top == "touch":
        print("  → 主要卡在触线环节: 价格未回踩 EMA20。等待回踩是策略纪律,属正常。")
    elif top == "wick":
        print("  → 主要卡在影线环节: 拒绝K线形态稀缺。近失占比高则可考虑"
              "REJECT_WICK_RATIO 微降(experiments 留痕)。")
    else:
        print("  → 各环节均匀,信号稀缺由行情整体决定,无需干预。")
    recent = q("SELECT base, bottleneck, near_miss, vol_ratio FROM "
               "signal_profiles WHERE ts > ? ORDER BY ts DESC LIMIT 5", [since])
    print("\n最近样例: " + "; ".join(
        f"{r['base']}[{r['bottleneck']}{'/近失' if r['near_miss'] else ''}]"
        for r in recent))


if __name__ == "__main__":
    main()
