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
sys.path.insert(0, ROOT)
import config
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
    max_pct = max((bn.get(x, 0) / n * 100) for x in bn)
    for k in ("trend", "touch", "wick", "vol", "none"):
        c = bn.get(k, 0)
        if c:
            pct = c / n * 100
            mark = "  ◀ 主要约束" if abs(pct - max_pct) < 1e-9 else ""
            print(f"  {k:8} {c:5}  ({pct:4.1f}%){mark}")
    print(f"\n近失（差一点就触发）: {nm} 次 ({nm/n*100:.1f}%)"
          + ("  ← 门槛微调即可显著提频" if nm / n > 0.2 else ""))
    by_sym = Counter(r["base"] for r in rows)
    print(f"\n分币画像（前 10）:")
    for b, c in by_sym.most_common(10):
        sub = [r for r in rows if r["base"] == b]
        top = Counter(r["bottleneck"] for r in sub).most_common(1)[0][0]
        print(f"  {b:8} {c:5} 次, 主瓶颈 {top}")
    print("\n归因反哺提案（全部过 experiments 验证门 + 人工放行,不自动生效）:")
    for rule, action, evidence, triggered in generate_feedback(rows):
        mark = "✅ 触发" if triggered else "— 未达阈值"
        print(f"  {rule} [{mark}] {action}   ({evidence})")
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


# ---------- 归因反哺（2026-08-17 用户问:未触发归因如何反哺决策系统） ----------

def generate_feedback(rows):
    """把未触发归因转成结构化反哺提案（只提案、永不自动生效——防过拟合红线）。

    规则（全部需要 experiments 验证门 + 人工放行,见设计文档 Phase 3）:
      R1 近失率高(≥20%) 且主瓶颈=wick → 影线门槛微调候选(×0.9,下限 0.8)
      R2 主瓶颈=trend 且占比 ≥60% → 策略 B(突破)转正评估启动信号
      R3 主瓶颈=touch 且占比 ≥70% → 纪律性等待结论(显式抑制调参冲动)
      R4 主瓶颈=vol → 量能确认条件观察(策略 B 的量能门槛关联)
    返回 [(rule, action, evidence, triggered)]。"""
    n = len(rows)
    if n < config.FB_MIN_PROFILES:
        return [("R0", f"样本不足(<{config.FB_MIN_PROFILES}),反哺提案搁置——等待画像积累",
                 f"n={n}", False)]
    from collections import Counter
    bn = Counter(r["bottleneck"] for r in rows)
    top, top_n = bn.most_common(1)[0]
    top_pct = top_n / n
    near = sum(r["near_miss"] for r in rows) / n
    out = []
    out.append(("R1", f"REJECT_WICK_RATIO 微调候选(×{config.SCAN_EVOLVE_WICK_STEP},"
                f"下限 {config.SCAN_EVOLVE_WICK_FLOOR})",
                f"近失率 {near:.0%}, 主瓶颈 {top} {top_pct:.0%}",
                near >= config.FB_NEAR_MISS_RATE and top == "wick"))
    out.append(("R2", "策略 B(突破)转正评估启动",
                f"主瓶颈 {top} {top_pct:.0%}(≥{config.FB_R2_TREND_PCT:.0%} 才触发)",
                top == "trend" and top_pct >= config.FB_R2_TREND_PCT))
    out.append(("R3", "纪律性等待(显式抑制调参)——回踩是策略纪律",
                f"主瓶颈 {top} {top_pct:.0%}(≥{config.FB_R3_TOUCH_PCT:.0%} 才触发)",
                top == "touch" and top_pct >= config.FB_R3_TOUCH_PCT))
    out.append(("R4", "量能确认条件观察(与策略 B 量能门槛关联)",
                f"主瓶颈 {top} {top_pct:.0%}",
                top == "vol" and top_pct >= config.FB_R4_VOL_PCT))
    return out


def propose_feedback(rows, db_path=None):
    """把触发的反哺规则写入 experiments 注册表(proposed,等待验证门)。"""
    from decision.experiments import propose
    done = []
    for rule, action, evidence, triggered in generate_feedback(rows):
        if triggered:
            propose(f"attribution_{rule.lower()}",
                    "attribution_feedback",
                    f'{{"action": "{action}", "evidence": "{evidence}"}}'
                    f"|未触发归因自动提案,待验证门+人工放行",
                    db_path=db_path)
            done.append(rule)
    return done

if __name__ == "__main__":
    main()

