#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统体检（收敛保证机制 M2）——安全/数据不变量的自动检查器。

每一项 = 一条不变量；任一失败 → 退出码 1，供 launchd 定时运行（com.crypto.healthcheck，
每 5 分钟）+ 飞书告警（30 分钟内不重复轰炸）。
设计依据：docs/plans/2026-08-16_self_evolution_design.md §10（收敛保证机制）。
只读：不写任何生产状态。

运行：cd crypto-agent && python3 tools/health_check.py
"""
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "crypto_agent.db")
MARKET_DB = os.path.join(ROOT, "data", "market.db")
HEARTBEAT = os.path.join(ROOT, "heartbeat_directional.txt")
NOTIFY_STATE = "/tmp/crypto-healthcheck.notified"

# Phase 0 断点修复上线的时刻（此后平仓必须带复盘报告）
FIX_DEPLOY_TS = 1786879580
# 2026-08-16 签名演进: "测试专用标的"签名随采集加速退役(BTC 进生产扫描池)
TMP_KEY_MARKERS = ("/var/folders", "/tmp/", "tempfile")

passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


def q(db, sql, params=()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


# ---------- H1 复盘覆盖率（修复上线后新平仓必须 100% 复盘） ----------
rows = q(DB, "SELECT id, symbol, review FROM trades WHERE status='closed' "
             "AND exit_time IS NOT NULL AND exit_time > ?", [FIX_DEPLOY_TS])
miss = [r["id"] for r in rows if not r["review"]]
check("H1 复盘覆盖率=100%（修复后新平仓）", not miss,
      f"未复盘: {miss}" if miss else "自修复上线后尚无新平仓")

# ---------- H2 组合敞口对账（账本 vs journal，DEF-11） ----------
owns = q(DB, "SELECT COALESCE(SUM(notional),0) s FROM ownership")
jrn = q(DB, "SELECT COALESCE(SUM(COALESCE(notional_usdt, size*entry_price)),0) s "
            "FROM trades WHERE status='open'")
o, j = owns[0]["s"], jrn[0]["s"]
ok_h2 = j <= 0 or (o > 0 and abs(o - j) / j < 0.15)
check("H2 组合敞口对账（账本≈journal 未平仓名义）", ok_h2,
      f"账本 {o:.0f} vs journal {j:.0f}")

# ---------- H3 生产库零测试污染（DEF-8 签名；2026-08-16 签名演进:
# 采集加速后 BTC 进生产扫描池,"测试专用标的"签名退役,见 test_production_guard） ----------
import re as _re
bad = [r["key"] for r in q(DB, "SELECT key FROM thresholds")
       if any(m in r["key"] for m in TMP_KEY_MARKERS)
       or r["key"] not in ("threshold_state_dir.json",)]
_test_src = _re.compile(r"^(fake_.*|[a-z]\d+)$")
bad += [r["source_trade"] for r in
        q(DB, "SELECT source_trade FROM lessons WHERE source_trade IS NOT NULL")
        if _test_src.match(r["source_trade"] or "")]
check("H3 生产库零测试污染签名", not bad, f"发现 {bad}" if bad else "")

# ---------- H4 引擎心跳新鲜 ----------
try:
    age = time.time() - float(open(HEARTBEAT).read().strip())
except Exception:
    age = 1e9
check("H4 方向性心跳新鲜(<60s)", age < 60, f"年龄 {age:.0f}s")

# ---------- H5 行情数据新鲜（看板/研究用 market.db） ----------
try:
    row = q(MARKET_DB, "SELECT MAX(open_time) m FROM klines WHERE bar='1m'")
    mage = (time.time() * 1000 - (row[0]["m"] or 0)) / 1000
except Exception as e:
    mage = 1e9
check("H5 行情数据新鲜(<60min)", mage < 3600,
      f"最新 1m K 线 {mage/60:.0f} 分钟前" if mage < 1e8 else "market.db 不可读")

# ---------- H6 引擎错误（近 24h） ----------
errs = q(DB, "SELECT COUNT(*) c FROM engine_errors WHERE ts > ?",
         [time.time() - 86400])
check("H6 近 24h 引擎错误 ≤3", errs[0]["c"] <= 3, f"{errs[0]['c']} 条")

# ---------- H7 风控状态（以活体进程为准：/status.risk_halted） ----------
try:
    import urllib.request
    st = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:8090/status", timeout=5).read())
    check("H7 风控未处于熔断停手", not st.get("risk_halted", False),
          st.get("risk_reason", ""))
except Exception:
    # 2026-08-16 误报修复: 回退窗口 24h→1h——重启窗口内 API 短暂不可达时,
    # 曾把 7 小时前的旧 halt 事件误判为"当前熔断"并告警(用户收到假警报)。
    last = q(DB, "SELECT kind, ts FROM risk_events ORDER BY ts DESC LIMIT 1")
    if last and last[0]["kind"] == "halt" and time.time() - last[0]["ts"] < 3600:
        check("H7 风控未处于熔断停手", False, "最近 1h 内 halt（停手中）")
    else:
        check("H7 风控未处于熔断停手", True,
              "API 暂不可达;DB 无 1h 内 halt(以 /status 为准)")

# ---------- H8 日度分析新鲜 ----------
ana = q(DB, "SELECT MAX(ts) m FROM analyses")
a_age = time.time() - (ana[0]["m"] or 0)
check("H8 日度分析新鲜(<26h)", a_age < 26 * 3600,
      f"{a_age/3600:.1f} 小时前")

# ---------- H9 仓位快照新鲜 ----------
snap = q(DB, "SELECT MAX(ts) m FROM position_snapshots")
s_age = time.time() - (snap[0]["m"] or 0)
check("H9 仓位快照新鲜(<5min)", s_age < 300, f"{s_age:.0f}s 前")

# ---------- H10 特征缺失率（Phase 1 质量,生产目标 0%） ----------
badf = q(DB, "SELECT COUNT(*) c FROM trade_features "
            "WHERE features_missing IS NOT NULL AND features_missing != ''")
check("H10 特征缺失率=0（生产）", badf[0]["c"] == 0,
      f"{badf[0]['c']} 行有缺失字段")

# ---------- H11 下单失败（新增即告警,2026-08-17 用户要求:逐笔进报警链） ----------
OF_STATE = "/tmp/crypto-order-failures.last_id"
new_fails = []
try:
    last = q(DB, "SELECT MAX(id) m FROM order_failures")
    max_id = last[0]["m"] or 0
    prev = 0
    if os.path.exists(OF_STATE):
        try:
            prev = int(open(OF_STATE).read().strip() or 0)
        except Exception:
            prev = 0
    if max_id > prev:
        rows = q(DB, "SELECT base, side, stage, error FROM order_failures "
                     "WHERE id > ? ORDER BY id", [prev])
        new_fails = [f"下单失败 {r['base']} {r['side']} [{r['stage']}]: "
                     f"{r['error'][:60]}" for r in rows]
        open(OF_STATE, "w").write(str(max_id))
    check("H11 无新增下单失败", not new_fails,
          ("; ".join(new_fails[:3]) + ("…" if len(new_fails) > 3 else ""))
          if new_fails else "")
except Exception:
    check("H11 无新增下单失败", True)

print(f"\n体检结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    # 飞书告警 + AI 诊断桥（2026-08-16 用户方案;30 分钟去重;AI 失败自动退回纯文本）
    try:
        if not os.path.exists(NOTIFY_STATE) or \
                time.time() - os.path.getmtime(NOTIFY_STATE) > 1800:
            sys.path.insert(0, ROOT)
            from tools import alert_diag
            alert_diag.diagnose_and_alert(failed)
            open(NOTIFY_STATE, "w").write(str(time.time()))
    except Exception:
        pass
    sys.exit(1)
sys.exit(0)
