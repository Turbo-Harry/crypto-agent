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

import config
from execution.trade_journal import realized_pnl_usdt, total_realized_pnl_usdt

WINDOW_DAYS = config.WINDOW_DAYS
MIN_TRADES_FOR_STATS = config.MIN_TRADES_FOR_STATS
MIN_SAMPLES_FOR_ISSUE = config.MIN_SAMPLES_FOR_ISSUE
LOSS_STREAK_ALERT = config.LOSS_STREAK_ALERT
STOP_BREACH_RATIO = config.STOP_BREACH_RATIO
WIN_RATE_FLOOR = config.WIN_RATE_FLOOR


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
        # 总盈亏必须用实际 USDT（名义×比例）。百分比相加会失真。
        "total_pnl_usdt": total_realized_pnl_usdt(closed),
        "total_pnl_pct": round(sum(t.get("pnl") or 0 for t in closed) * 100, 2),
        "avg_risk_usdt": round(sum(t.get("risk_usdt") or 0 for t in trades) /
                               len(trades), 2) if trades else 0,
        "notional_total": round(sum(t.get("notional_usdt") or 0 for t in trades), 2),
        "scan_rounds": len(scans),
        "scan_signals": sum(1 for s in scans if s["has_signal"]),
        # scan_opens = 扫描日志里 decision=open 的次数。2026-08-20 起 open
        # 只在成交入账后才记,应与 trades_total 对齐;历史窗口仍含"想开但没成交"。
        "scan_opens": sum(1 for s in scans if s["decision"] == "open"),
        "scan_open_failed": sum(1 for s in scans if s["decision"] == "open_failed"),
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
        actual_loss = abs(realized_pnl_usdt(t) or 0)
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
    # 场景归纳(2026-08-17 用户要求'多维度经验总结'): 同 symbol+类别+场景
    # 条件的 trusted 教训 ≥ROLLUP_MIN_MEMBERS 时沉淀归纳结论(只读汇总)。
    try:
        from decision.experience_scoring import rollup_lessons
        report["lesson_rollups"] = rollup_lessons()
    except Exception:
        report["lesson_rollups"] = []
    # 飞书反馈
    wr = report['win_rate']
    wr_s = f"{wr:.0%}" if isinstance(wr, (int, float)) else "—"
    pnl_u = report['total_pnl_usdt']
    sign = "+" if pnl_u >= 0 else ""
    lines = [f"📊 系统每日看账 [{time.strftime('%m-%d %H:%M')}]",
             f"成交 {report['trades_total']} 笔（已平 {report['closed']}）  ·  "
             f"胜率 {wr_s}  ·  总盈亏 **{sign}{pnl_u:.2f} USDT**",
             f"扫描 {report['scan_rounds']} 轮  ·  信号 {report['scan_signals']}  ·  "
             f"熔断 {report['risk_halt_count']}  ·  "
             f"异常 {report['engine_errors']}"]
    if issues:
        lines.append("⚠️ 感知到问题")
        for it in issues:
            lines.append(f"- [{it['level']}] {it['category']}: {it['detail'][:60]}")
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
