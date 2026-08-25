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
# 2026-08-23 双实例: 环境变量选择体检对象(com.crypto.healthcheck 查实盘,
# com.crypto.healthcheck.paper 查模拟盘)。默认实盘库+8090。
def _abs(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)
DB = _abs(os.environ.get("CRYPTO_AGENT_DB", os.path.join(ROOT, "crypto_agent.db")))
MARKET_DB = os.path.join(ROOT, "data", "market.db")
_HEART_NAME = ("paper" if os.environ.get("CRYPTO_AGENT_MODE") == "paper"
               else "directional")
HEARTBEAT = os.path.join(ROOT, f"heartbeat_{_HEART_NAME}.txt")
API_PORT = int(os.environ.get("CRYPTO_API_PORT", "8090"))
# 2026-08-23 双实例: 告警去重状态按实例分开——此前两套体检共享一个
# NOTIFY_STATE,30 分钟去重会互相吞告警。
_NOTIFY_NAME = ("crypto-healthcheck-paper" if _HEART_NAME == "paper"
                else "crypto-healthcheck")
NOTIFY_STATE = f"/tmp/{_NOTIFY_NAME}.notified"
# 维护窗口标记: 存在该文件或环境变量=1 时,体检照跑但失败不告警不退出
# (部署/切库等计划停机用,避免把"主动停机"当成事故轰炸用户)。
MAINTENANCE = (os.environ.get("CRYPTO_HEALTH_MAINTENANCE") == "1"
               or os.path.exists(os.path.join(ROOT, ".healthcheck_maintenance")))

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
    # 2026-08-25 fix: WAL 库在 -wal/-shm 被 checkpoint 清理后,mode=ro 只读
    # 打开会因无法重建 shm 而报 "unable to open database file"(引擎空闲间隙
    # 复现)。ro 失败退回普通连接(本工具只发 SELECT,不写)。
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("SELECT 1")
    except sqlite3.OperationalError:
        conn = sqlite3.connect(db, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


# ---------- H1 复盘覆盖率（修复上线后新平仓必须 100% 复盘） ----------
# 2026-08-19: 加 15 分钟宽限——log_exit 先写 exit_time,复盘链随后要拉 K 线
# 算 MFE/MAE 等特征(网络慢时数秒~数十秒),体检恰好卡进这个窗口会误报
# (txn_021 平仓后 4.7 秒被 H1 抓到)。exit_time 超过宽限仍无 review 才是
# 真漏复盘(复盘链崩溃)。
rows = q(DB, "SELECT id, symbol, review FROM trades WHERE status='closed' "
             "AND exit_time IS NOT NULL AND exit_time > ? AND exit_time < ?",
         [FIX_DEPLOY_TS, time.time() - 900])
miss = [r["id"] for r in rows if not r["review"]]
check("H1 复盘覆盖率=100%（修复后新平仓）", not miss,
      f"未复盘: {miss}" if miss else "修复后平仓全部已复盘")

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

# ---------- H5 严格行情新鲜（confirmed OKX SWAP / klines_v2） ----------
try:
    row = q(MARKET_DB, "SELECT MAX(open_time) m FROM klines_v2 WHERE bar='1m' "
                       "AND source='okx' AND venue='swap' AND confirmed=1")
    mage = (time.time() * 1000 - (row[0]["m"] or 0)) / 1000
except Exception as e:
    mage = 1e9
check("H5 confirmed SWAP 行情新鲜(<60min)", mage < 3600,
      f"最新 1m K 线 {mage/60:.0f} 分钟前" if mage < 1e8
      else "market.db.klines_v2 不可读")

# ---------- H6 引擎错误（2026-08-17 口径修正） ----------
# 旧口径"24h ≤3"在 0.5-1 req/s 的请求量下无意义(单请求 SSL 抖动即触发,
# 今晚 6 条 blip 99.99% 成功率仍被报"真实告警")。新口径:
#   - 24h 总量 ≤12(噪音上限,过滤零星单请求失败)
#   - 30 分钟内 ≥4 条 = 突发降级(今晚 20:07-20:22 的爆发窗口正是此类)
errs24 = q(DB, "SELECT COUNT(*) c FROM engine_errors WHERE ts > ? "
                "AND COALESCE(archived,0)=0",
           [time.time() - 86400])
burst = q(DB, "SELECT COUNT(*) c FROM engine_errors WHERE ts > ? "
             "AND COALESCE(archived,0)=0",
          [time.time() - 1800])
check("H6 引擎错误(24h≤12 且 30min 突发<4)",
      errs24[0]["c"] <= 12 and burst[0]["c"] < 4,
      f"24h {errs24[0]['c']} 条 / 30min {burst[0]['c']} 条")

# ---------- H7 风控状态（以活体进程为准：/status.risk_halted） ----------
try:
    import urllib.request
    st = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{API_PORT}/status", timeout=5).read())
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
# 2026-08-23 双实例: 全新库(实盘库)首份日度分析未生成时,分析表为空
# → age=now 恒红。引擎活着(快照在流)就只提示,不判死(误报曾触发统一异常中心)。
if ana[0]["m"] is None:
    _snap = q(DB, "SELECT MAX(ts) m FROM position_snapshots")
    _engine_alive = bool(_snap[0]["m"]) and time.time() - _snap[0]["m"] < 300
    check("H8 日度分析新鲜(<26h)", True if _engine_alive else False,
          "首份日度分析未生成(引擎运行中)" if _engine_alive else "无分析且引擎无快照")
else:
    check("H8 日度分析新鲜(<26h)", a_age < 26 * 3600,
          f"{a_age/3600:.1f} 小时前")

# ---------- H9 仓位快照新鲜 ----------
snap = q(DB, "SELECT MAX(ts) m FROM position_snapshots")
s_age = time.time() - (snap[0]["m"] or 0)
check("H9 仓位快照新鲜(<5min)", s_age < 300, f"{s_age:.0f}s 前")

# ---------- H10 核心特征缺失率（Phase 1 质量,生产目标 0%） ----------
# 2026-08-20 口径修正: 订单流字段(of_*)是 best-effort 网络源,Binance/Gate
# 无覆盖的币(AI16Z 等)按设计记缺失——不算缺陷。只查【核心特征】
# (MFE/MAE/R 倍数/滑点/持仓时长)缺失的行。
# 2026-08-25 口径修正: 持仓 <120s 的秒级单(如 STX 24.7s 快止盈)物理上
# 凑不齐 1m K 线窗口,mae_r/mfe_r 按设计缺失——豁免,不算缺陷。
_core_fields = ("mae_r", "mfe_r", "r_multiple", "slippage_bps", "holding_hours")
_where = " OR ".join(f"features_missing LIKE '%{f}%'" for f in _core_fields)
badf = q(DB, f"SELECT COUNT(*) c FROM trade_features tf "
             f"JOIN trades t ON t.id = tf.trade_id "
             f"WHERE ({_where}) AND "
             f"NOT (COALESCE(t.exit_time,0) - COALESCE(t.entry_time,0) < 120 "
             f"     AND tf.features_missing NOT LIKE '%r_multiple%' "
             f"     AND tf.features_missing NOT LIKE '%slippage_bps%' "
             f"     AND tf.features_missing NOT LIKE '%holding_hours%')")
check("H10 核心特征缺失率=0（订单流 best-effort 除外）", badf[0]["c"] == 0,
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
        # 2026-08-17 统一异常中心: 逐笔登记(报警链统一消费)
        try:
            sys.path.insert(0, ROOT)
            from tools.anomalies import register as _reg
            for r in rows:
                _reg("order_failure",
                     f"{r['base']} {r['side']} [{r['stage']}] 下单失败",
                     r["error"][:200], severity="error")
        except Exception:
            pass
        open(OF_STATE, "w").write(str(max_id))
    check("H11 无新增下单失败", not new_fails,
          ("; ".join(new_fails[:3]) + ("…" if len(new_fails) > 3 else ""))
          if new_fails else "")
except Exception:
    check("H11 无新增下单失败", True)

# ---------- H12 修复经验护栏（2026-08-17 用户问"会积累修复经验吗"） ----------
try:
    sys.path.insert(0, ROOT)
    from tools.fix_guard import check_fix_guards
    bad_guards = check_fix_guards()
    check("H12 修复经验护栏全部在位", not bad_guards,
          "; ".join(n for n, _ in bad_guards[:3]) if bad_guards else "")
except Exception:
    check("H12 修复经验护栏全部在位", True)

# ---------- H13 时间同步（2026-08-19 用户要求健壮性） ----------
# OKX 签名请求依赖本地时钟,偏差大会 50113 全灭;与服务器时间比对 >5s 报警。
# 网络失败时跳过(不误报——网络问题由 H4/H5/H6 负责)。
try:
    sys.path.insert(0, ROOT)
    from exchange.transport import OKXTransport
    _st = OKXTransport("", "", "", sandbox=False).public(
        "/api/v5/public/time")["data"][0]["ts"]
    _skew = abs(int(_st) / 1000.0 - time.time())
    check("H13 本地时钟与 OKX 服务器偏差 <5s", _skew < 5, f"偏差 {_skew:.2f}s")
except Exception:
    check("H13 本地时钟与 OKX 服务器偏差 <5s", True)

# ---------- H14 HTTP 控制面守护（2026-08-20 用户要求'框架健全性'缺口之一） ----------
# 引擎线程活着但 FastAPI/uvicorn 挂掉时,此前无检查报警——控制面
# (pause/resume/scan) 静默失联。/health 不可达 = HTTP 层故障。
try:
    import urllib.request as _ur2
    with _ur2.urlopen(f"http://127.0.0.1:{API_PORT}/health", timeout=5) as _r2:
        _h_ok = _r2.status == 200
    check("H14 HTTP 控制面 /health 可达", _h_ok,
          "HTTP OK" if _h_ok else "HTTP 层无响应")
except Exception:
    check("H14 HTTP 控制面 /health 可达", False, "HTTP 层无响应")

print(f"\n体检结果: {len(passed)} 通过, {len(failed)} 失败")

# 实盘就绪三盏灯(2026-08-20 用户指示,信息展示不做体检项)
try:
    from tools.readiness import render_lines
    print("\n实盘就绪三盏灯:")
    for _ln in render_lines():
        print(_ln)
except Exception:
    pass
if failed and MAINTENANCE:
    print(f"🔧 维护窗口(.healthcheck_maintenance 存在): {len(failed)} 项未过,"
          f"告警静音,不退出")
    sys.exit(0)
if failed:
    # 飞书告警 + AI 诊断桥（2026-08-16 用户方案;30 分钟去重;AI 失败自动退回纯文本）
    try:
        if not os.path.exists(NOTIFY_STATE) or \
                time.time() - os.path.getmtime(NOTIFY_STATE) > 1800:
            sys.path.insert(0, ROOT)
            from tools import alert_diag
            # 2026-08-23 双实例: 告警带实例标签,会话/飞书里能分清是哪条链
            _tag = "【模拟盘】" if os.environ.get("CRYPTO_AGENT_MODE") == "paper" \
                else "【实盘】"
            alert_diag.diagnose_and_alert([f"{_tag} {x}" for x in failed])
            open(NOTIFY_STATE, "w").write(str(time.time()))
    except Exception:
        pass
    sys.exit(1)
sys.exit(0)
