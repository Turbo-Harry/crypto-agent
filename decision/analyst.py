"""
分析师（Analyst）— 系统的"看账人"。

自我进化的核心：不只记录，还要**定期回头看**，发现问题时**感知并反馈**。
反馈分三档（不直接下单，只走既有经验库闸门——遵守 AGENTS.md 红线 7）：
  1. 报告落库（analyses 表，API 可查）
  2. 感知到的问题落库（issues 字段 + 飞书通知）
  3. 结构化教训 → lessons 表（初始 unverified，需 3 次真实交易验证才能影响决策）

分析窗口：近 7 天（样本少时如实标注 insufficient，不硬凑结论——防过拟合）。
规则全部保守、可解释：样本不足不下结论。

用法：
  python3 -m decision.analyst --daily   跑一次每日分析
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WINDOW_DAYS = 7
MIN_TRADES_FOR_STATS = 5      # 统计结论最少样本
MIN_SAMPLES_FOR_ISSUE = 3     # 感知问题最少样本
LOSS_STREAK_ALERT = 3         # 连亏笔数告警线
STOP_BREACH_RATIO = 1.3       # 实亏/预设风险 > 1.3 视为止损被击穿
WIN_RATE_FLOOR = 0.30         # 胜率下限（样本≥5 时）


def _collect():
    """汇总近 7 天全量数据（SQLite）。"""
    import storage.db as sdb
    sdb.init_db()
    since = time.time() - WINDOW_DAYS * 86400
    trades = sdb.q("SELECT * FROM trades WHERE entry_time >= ? ORDER BY entry_time", [since])
    scans = sdb.q("SELECT * FROM scan_decisions WHERE ts >= ?", [since])
    risks = sdb.q("SELECT * FROM risk_events WHERE ts >= ?", [since])
    errors = sdb.q("SELECT * FROM engine_errors WHERE ts >= ?", [since])
    lessons = sdb.q("SELECT * FROM lessons")
    return {"trades": trades, "scans": scans, "risks": risks,
            "errors": errors, "lessons": lessons}


def analyze():
    """跑一轮分析，返回 (report, issues)。"""
    d = _collect()
    trades, scans, risks, errors = d["trades"], d["scans"], d["risks"], d["errors"]

    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    sufficient = len(closed) >= MIN_TRADES_FOR_STATS

    report = {
        "window_days": WINDOW_DAYS,
        "ts": time.time(),
        "sufficient": sufficient,
        "trades_total": len(trades),
        "closed": len(closed),
        "open": sum(1 for t in trades if t["status"] == "open"),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "total_pnl_pct": round(sum(t.get("pnl") or 0 for t in closed) * 100, 2),
        "avg_risk_usdt": round(sum(t.get("risk_usdt") or 0 for t in trades) /
                               len(trades), 2) if trades else 0,
        "notional_total": round(sum(t.get("notional_usdt") or 0 for t in trades), 2),
        "scan_rounds": len(scans),
        "scan_signals": sum(1 for s in scans if s["has_signal"]),
        "scan_opens": sum(1 for s in scans if s["decision"] == "open"),
        "risk_halt_count": sum(1 for r in risks if r["kind"] == "halt"),
        "engine_errors": len(errors),
        "lessons_total": len(d["lessons"]),
    }

    issues = []
    # 1. 连亏
    streak = 0
    for t in closed:
        if (t.get("pnl") or 0) < 0:
            streak += 1
        else:
            streak = 0
    if streak >= LOSS_STREAK_ALERT:
        issues.append({"level": "warn", "category": "连亏",
                       "detail": f"最近连续 {streak} 笔亏损，检查信号质量/是否需要冷却",
                       "lesson": f"连亏 {streak} 笔后应主动冷却，不硬接信号"})
    # 2. 止损被击穿（实亏远大于预设风险）
    breaches = []
    for t in closed:
        risk = t.get("risk_usdt") or 0
        if risk <= 0 or t.get("pnl") is None:
            continue
        actual_loss = abs(t["pnl"]) * (t.get("notional_usdt") or 0)
        if t["pnl"] < 0 and actual_loss > risk * STOP_BREACH_RATIO:
            breaches.append(t["symbol"])
    if len(breaches) >= MIN_SAMPLES_FOR_ISSUE:
        issues.append({"level": "error", "category": "止损",
                       "detail": f"{len(breaches)} 笔实亏超过预设风险 1.3×（{set(breaches)}），"
                                 f"止损可能被滑点/插针击穿",
                       "lesson": "止损位被反复击穿，需加缓冲（放宽 0.2×ATR）并缩小仓位"})
    # 3. 胜率过低
    if sufficient and report["win_rate"] is not None and report["win_rate"] < WIN_RATE_FLOOR:
        issues.append({"level": "error", "category": "胜率",
                       "detail": f"近 7 天胜率 {report['win_rate']*100:.0f}% < 30%",
                       "lesson": "当前信号模式胜率过低，收紧信号条件或暂停该类信号"})
    # 4. 风控熔断
    if report["risk_halt_count"] > 0:
        issues.append({"level": "error", "category": "风控",
                       "detail": f"近 7 天熔断 {report['risk_halt_count']} 次",
                       "lesson": "风控熔断说明仓位/止损配置有问题，复盘触发原因"})
    # 5. 引擎异常
    if report["engine_errors"] > 0:
        issues.append({"level": "warn", "category": "工程",
                       "detail": f"近 7 天引擎异常 {report['engine_errors']} 次",
                       "lesson": None})   # 工程问题不进交易经验库
    return report, issues


def run_daily():
    """每日分析：报告+问题落库、教训进经验库、飞书反馈。"""
    report, issues = analyze()
    import storage.db as sdb
    sdb.init_db()
    sdb.x("INSERT INTO analyses (ts, kind, report, issues) VALUES (?,?,?,?)",
          [time.time(), "daily", json.dumps(report, ensure_ascii=False),
           json.dumps(issues, ensure_ascii=False)])
    # 结构化教训 → lessons 表（unverified，走既有验证闸门）
    # 去重：同类别同内容已存在则跳过（避免每次看账重复堆积同一条教训）
    lesson_ids = []
    for it in issues:
        if not it.get("lesson"):
            continue
        dup = sdb.q1("SELECT id FROM lessons WHERE category=? AND content=?",
                     [it["category"], it["lesson"]])
        if dup:
            lesson_ids.append(dup["id"])
            continue
        lid = sdb.x("INSERT INTO lessons (symbol, category, content, score, "
                    "adoptions, status, source_trade, ts, last_update) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ["*", it["category"], it["lesson"], 50, 0, "unverified",
                     f"analyst:{time.strftime('%Y-%m-%d')}", time.time(), time.time()])
        lesson_ids.append(lid)
    # 飞书反馈
    lines = [f"📊 系统每日看账 [{time.strftime('%m-%d %H:%M')}]",
             f"交易 {report['closed']}/{report['trades_total']} 笔 | 胜率 "
             f"{report['win_rate'] if report['win_rate'] is not None else '—'} | "
             f"总盈亏 {report['total_pnl_pct']:+.2f}%",
             f"扫描 {report['scan_rounds']} 轮 | 信号 {report['scan_signals']} | "
             f"开仓 {report['scan_opens']} | 熔断 {report['risk_halt_count']} | "
             f"异常 {report['engine_errors']}"]
    if issues:
        lines.append("⚠️ 感知到问题:")
        for it in issues:
            lines.append(f"  [{it['level']}] {it['category']}: {it['detail'][:60]}")
    else:
        lines.append("✅ 无异常")
    try:
        from decision.notify import notify
        notify("\n".join(lines))
    except Exception:
        pass
    return {"report": report, "issues": issues, "lesson_ids": lesson_ids}


if __name__ == "__main__":
    r = run_daily()
    print(json.dumps(r["report"], ensure_ascii=False, indent=2))
    for it in r["issues"]:
        print(f"⚠️ [{it['level']}] {it['category']}: {it['detail']}")
