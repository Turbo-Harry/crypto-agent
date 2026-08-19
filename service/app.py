"""
HTTP 接口层 — FastAPI 应用（完整功能的服务端外壳）。

暴露三大类接口：
  观测：/health /status /watchlist /signals/{base} /journal /realtime/{base}
  控制：/pause /resume（暂停/恢复方向性开仓；止损监控永不暂停）
    运维：/scan/daily（手动触发全市场候选扫描）/scan/evolve（扫描尺子进化）/error
（2026-08-16 用户决定：套利引擎移除，/arb/status 已下线，代码归档 legacy/。）

【禁止】暴露"下单"类接口：交易决策只由后台引擎的既定策略做出，
HTTP 只是观测窗口与最小控制面，不是交易入口 —— 宁可做对，也不做错。

自动文档：GET /docs（Swagger UI，AI 可读 OpenAPI schema）。
"""
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List

from fastapi import FastAPI, HTTPException, Depends, Request

from service.models import (HealthOut, BalanceOut, PositionOut, OpenTradeOut,
                            StatusOut, WatchItem, WatchlistOut, SignalOut,
                            TradeItem, JournalOut, ControlOut,
                            RealtimeOut, ScanOut, ReconcileOut, RiskEventOut,
                            ScanEvolveOut)

app = FastAPI(
    title="Crypto Agent 交易服务",
    description=(
        "交易系统服务端：方向性日内短线引擎 + 实时行情。\n\n"
        "- 方向性引擎：2s 止损监控 + 15min 回踩信号扫描（后台线程）\n"
        "- 本接口只读观测 + 暂停/恢复开仓，不提供手动下单\n"
        "- 模拟盘（OKX sandbox），虚拟资金"),
    version="2.1.0")


def require_control(request: Request):
    """控制面最小防护(审计 B-H1):
    1) Host 白名单:只允许回环(防 DNS rebinding / localhost CSRF);
    2) 若配置 CRYPTO_AGENT_API_TOKEN,则必须携带 x-api-token 且相等。"""
    host_hdr = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host_hdr not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(403, "控制端点仅限本机访问")
    token = os.environ.get("CRYPTO_AGENT_API_TOKEN", "")
    if token:
        provided = request.headers.get("x-api-token", "")
        if not secrets.compare_digest(provided, token):
            raise HTTPException(401, "无效 API token")


def _worker():
    return app.state.worker


def _trader():
    return _worker().trader


@app.get("/health", response_model=HealthOut, tags=["观测"])
def health():
    """服务健康：方向性引擎心跳年龄超时判定 degraded。"""
    w = _worker()
    hb_d = w.heartbeat_age()
    ok = hb_d >= 0 and hb_d < 30
    return HealthOut(status="ok" if ok else "degraded",
                     adapter=_trader().exchange.name,
                     uptime_seconds=round(w.uptime(), 1),
                     directional_heartbeat_age=round(hb_d, 1),
                     paused=_trader().paused)


@app.get("/status", response_model=StatusOut, tags=["观测"])
def status():
    """账户全景：余额 + 交易所持仓 + journal 未平仓 + 风控状态。"""
    t = _trader()
    try:
        bal = t.exchange.fetch_balance()
    except Exception:
        bal = None
    try:
        positions = t.exchange.fetch_positions()
    except Exception:
        positions = []
    open_trades = [x for x in t.journal.trades if x["status"] == "open"]
    today = time.strftime("%Y-%m-%d")
    today_n = sum(1 for x in t.journal.trades
                  if x.get("entry_time")
                  and time.strftime("%Y-%m-%d", time.localtime(x["entry_time"])) == today)
    def notional(x):
        return x.get("notional_usdt") if x.get("notional_usdt") is not None \
            else round(float(x.get("size") or 0) * float(x.get("entry_price") or 0), 2)
    total_notional = sum(notional(x) for x in t.journal.trades)
    open_notional = sum(notional(x) for x in open_trades)
    today_notional = sum(notional(x) for x in t.journal.trades
                         if x.get("entry_time")
                         and time.strftime("%Y-%m-%d", time.localtime(x["entry_time"])) == today)
    return StatusOut(
        balance=BalanceOut(total_equity=bal.total_eq if bal else 0,
                           usdt_free=bal.usdt_free if bal else 0,
                           usdt_total=bal.usdt_total if bal else 0),
        positions=[PositionOut(inst_id=p.inst_id, side=p.side,
                               contracts=p.contracts, base_qty=p.base_qty,
                               avg_px=p.avg_px) for p in positions],
        open_trades=[OpenTradeOut(id=x["id"], symbol=x["symbol"],
                                  direction=x.get("direction") or "long",
                                  size=x.get("size"), entry_price=x.get("entry_price"),
                                  stop_loss=x.get("stop_loss"),
                                  take_profit=x.get("take_profit"),
                                  venue=x.get("venue") or "swap",
                                  notional_usdt=x.get("notional_usdt"),
                                  risk_usdt=x.get("risk_usdt")) for x in open_trades],
        risk_halted=not t.risk.can_trade(),
        risk_reason=t.risk.halt_reason,
        decision_threshold=t.threshold_learner.threshold,
        today_trade_count=today_n,
        total_notional_usdt=round(total_notional, 2),
        open_notional_usdt=round(open_notional, 2),
        today_notional_usdt=round(today_notional, 2))


@app.get("/watchlist", response_model=WatchlistOut, tags=["观测"])
def watchlist():
    """今日候选池（评分 → 允许笔数）。"""
    t = _trader()
    items = [WatchItem(base=b, score=t.watch_scores.get(b),
                       budget=t._trade_budget(b)) for b in t.watchlist]
    return WatchlistOut(date=time.strftime("%Y-%m-%d"), items=items)


@app.get("/signals/{base}", response_model=SignalOut, tags=["观测"])
def signal_for(base: str):
    """按需跑一次某币的回踩确认信号检查（只读，不下单、不消耗冷却）。"""
    t = _trader()
    try:
        sig = t.scan_signal(base.upper())
    except Exception as e:
        raise HTTPException(500, f"信号检查失败: {e}")
    venue = t.exchange.venue_for(base.upper())
    return SignalOut(base=base.upper(), venue=venue, signal=sig,
                     message="有信号" if sig else "无回踩确认信号")


@app.get("/journal", response_model=JournalOut, tags=["观测"])
def journal(limit: int = 20):
    """最近交易台账（含盈亏）。"""
    from execution.trade_journal import realized_pnl_usdt, total_realized_pnl_usdt
    t = _trader()
    trades = t.journal.trades[-limit:]
    closed = [x for x in t.journal.trades if x["status"] == "closed"]
    wins = [x for x in closed if (x.get("pnl") or 0) > 0]
    return JournalOut(
        total=len(t.journal.trades), closed=len(closed),
        win_rate=round(len(wins) / len(closed), 3) if closed else None,
        total_pnl_usdt=total_realized_pnl_usdt(closed),
        trades=[TradeItem(id=x["id"], symbol=x["symbol"],
                          direction=x.get("direction") or "long",
                          entry_price=x.get("entry_price"),
                          exit_price=x.get("exit_price"),
                          pnl_pct=round(x["pnl"] * 100, 2) if x.get("pnl") is not None else None,
                          pnl_usdt=realized_pnl_usdt(x),
                          status=x["status"],
                          entry_time=x.get("entry_time"),
                          exit_time=x.get("exit_time"),
                          venue=x.get("venue") or "swap",
                          notional_usdt=x.get("notional_usdt"),
                          review=x.get("review")) for x in trades])


@app.get("/realtime/{base}", response_model=RealtimeOut, tags=["观测"])
def realtime(base: str):
    """某币实时行情快照（WebSocket 数据，stale 字段剔除）。"""
    t = _trader()
    base = base.upper()
    data = {}
    if t.rt is not None:
        data = t.rt.get(base, max_age=60)
    fresh = bool(data.get("price"))
    return RealtimeOut(base=base,
                       price=data.get("price"),
                       swap_price=data.get("swap_price"),
                       funding=data.get("funding"),
                       vol_15m=data.get("vol_15m"),
                       fresh=fresh)


@app.get("/anomalies", tags=["观测"])
def anomalies():
    """统一异常中心(2026-08-17 用户要求:所有异常统一输出到一个接口)。
    消费端只读此端点/表,不接触各业务表。"""
    import storage.db as sdb
    sdb.init_db()
    return sdb.q("SELECT id, ts, source, severity, title, detail, status "
                 "FROM anomalies ORDER BY ts DESC LIMIT 50")


@app.post("/scan/daily", response_model=ScanOut, tags=["运维"],
           dependencies=[Depends(require_control)])
def scan_daily():
    """手动触发一次全市场候选扫描（刷新 watchlist，覆盖 123 个标的）。
    耗时约 1-2 分钟；调用会阻塞等待完成。"""
    from engines.daily_scan import screen_daily
    t = _trader()
    try:
        w = screen_daily(exchange=t.exchange)
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")
    # 同步刷新引擎的候选池（避免等跨天自动刷新）
    t.watchlist = [c["base"] for c in w]
    t.watch_scores = {c["base"]: c["score"] for c in w}
    t._watch_date = time.strftime("%Y-%m-%d")
    t._last_watch_refresh = time.time()
    return ScanOut(date=time.strftime("%Y-%m-%d"), fallback=bool(w and w[0].get("score") == 0.0),
                   candidates=[{"base": c["base"], "dir": c.get("dir"),
                                "score": round(c.get("score", 0), 3),
                                "atr_pct": round(c.get("atr_pct", 0), 4),
                                "price": c.get("price")} for c in w])


@app.get("/scan/evolve", response_model=ScanEvolveOut, tags=["观测"])
def scan_evolve_status():
    """扫描尺子进化状态：现役/活体/候选影线比、影子样本、是否待批准。
    只读；不下单、不改尺子。落库走引擎 db_path（测试隔离、防写活体库）。"""
    from decision.scan_evolve import snapshot
    return ScanEvolveOut(**snapshot(_trader()._db_path))


@app.post("/scan/evolve/approve", response_model=ScanEvolveOut, tags=["控制"],
           dependencies=[Depends(require_control)])
def scan_evolve_approve():
    """批准已通过影子验证门的扫描尺子（目前仅 REJECT_WICK_RATIO）。
    未通过验证门的提案一律拒绝。不改 config.py，覆盖写在 kv，可回滚。"""
    from decision.scan_evolve import approve, snapshot
    db = _trader()._db_path
    ok, msg = approve(db_path=db)
    if not ok:
        raise HTTPException(409, msg)
    out = ScanEvolveOut(**snapshot(db))
    out.message = msg
    return out


@app.post("/scan/evolve/rollback", response_model=ScanEvolveOut, tags=["控制"],
           dependencies=[Depends(require_control)])
def scan_evolve_rollback():
    """撤销活体影线比覆盖，回到 config.REJECT_WICK_RATIO。"""
    from decision.scan_evolve import rollback, snapshot
    db = _trader()._db_path
    _, msg = rollback(db_path=db)
    out = ScanEvolveOut(**snapshot(db))
    out.message = msg
    return out


@app.post("/pause", response_model=ControlOut, tags=["控制"],
           dependencies=[Depends(require_control)])
def pause():
    """暂停方向性开仓（止损监控不暂停）。信号扫描循环内部跳过。"""
    _trader().pause()
    return ControlOut(action="pause", paused=True,
                      message="已暂停开仓信号扫描；止损止盈监控继续运行")


@app.post("/resume", response_model=ControlOut, tags=["控制"],
           dependencies=[Depends(require_control)])
def resume():
    """恢复方向性开仓信号扫描。"""
    _trader().resume()
    return ControlOut(action="resume", paused=False, message="已恢复开仓信号扫描")


@app.get("/reconcile", response_model=ReconcileOut, tags=["观测"])
def reconcile():
    """对账：journal 记账（本地） vs 交易所真实持仓（唯一事实源）。
    legacy 记录 size 是张数，按 ct_val 折算币数后对比；不一致即报告，不静默。"""
    import json as _json
    from collections import defaultdict
    t = _trader()
    # 快照（最近一次本地落库）
    import storage.db as sdb
    snap = None
    try:
        row = sdb.q1("SELECT MAX(ts) ts FROM position_snapshots")
        if row and row["ts"]:
            snap = {"ts": row["ts"]}
    except Exception:
        pass
    # journal 未平仓 → 折算币数
    from execution.trade_journal import LEGACY_CT_VAL
    journal_open, j_by_sym = [], defaultdict(float)
    notes = []
    for x in t.journal.trades:
        if x["status"] != "open":
            continue
        size = float(x.get("size") or 0)
        if x.get("size_unit") == "contracts(legacy)":
            ct_val = float(x.get("ct_val") or LEGACY_CT_VAL.get(x["symbol"], 1.0))
            base = size * ct_val
            notes.append(f"{x['id']} {x['symbol']} 为 legacy 单位（{size} 张 × ctVal {ct_val}），已折算")
        else:
            base = size
        j_by_sym[x["symbol"]] += base
        journal_open.append({"id": x["id"], "symbol": x["symbol"],
                             "base_qty": round(base, 8), "venue": x.get("venue") or "swap",
                             "notional_usdt": x.get("notional_usdt")})
    # 交易所持仓 → 币数
    positions = t.exchange.fetch_positions()
    exchange_positions, e_by_sym = [], defaultdict(float)
    for p in positions:
        e_by_sym[p.base] += p.base_qty
        exchange_positions.append({"inst_id": p.inst_id, "side": p.side,
                                   "contracts": p.contracts,
                                   "base_qty": round(p.base_qty, 8), "avg_px": p.avg_px})
    syms = sorted(set(j_by_sym) | set(e_by_sym))
    per_symbol = [{"symbol": s,
                   "journal_base": round(j_by_sym.get(s, 0.0), 8),
                   "exchange_base": round(e_by_sym.get(s, 0.0), 8),
                   "diff": round(e_by_sym.get(s, 0.0) - j_by_sym.get(s, 0.0), 8)}
                  for s in syms]
    balanced = all(abs(p["diff"]) < 1e-9 for p in per_symbol)
    return ReconcileOut(snapshot_ts=snap.get("ts") if snap else None,
                        journal_open=journal_open,
                        exchange_positions=exchange_positions,
                        per_symbol=per_symbol, balanced=balanced, notes=notes)


@app.get("/analysis/latest", response_model=dict, tags=["观测"])
def analysis_latest():
    """最近一次看账报告（报告 + 感知到的问题 + 生成的教训 id）。"""
    import storage.db as sdb
    sdb.init_db()
    row = sdb.q1("SELECT * FROM analyses ORDER BY id DESC LIMIT 1")
    if not row:
        return {"report": None, "issues": [], "message": "尚无分析记录"}
    import json as _json
    return {"ts": row["ts"], "kind": row["kind"],
            "report": _json.loads(row["report"]), "issues": _json.loads(row["issues"])}


@app.post("/analysis/daily", response_model=dict, tags=["运维"],
           dependencies=[Depends(require_control)])
def analysis_daily():
    """手动触发一次每日看账（分析 + 问题感知 + 教训入经验库 + 飞书反馈）。"""
    from decision.analyst import run_daily
    return run_daily()


@app.get("/risk/events", response_model=List[RiskEventOut], tags=["观测"])
def risk_events(limit: int = 20):
    """风控事件复盘记录：熔断/恢复，含触发时净值与持仓数快照。"""
    import storage.db as sdb
    sdb.init_db()
    rows = sdb.q("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", [limit])
    return [RiskEventOut(**r) for r in reversed(rows)]


@app.get("/error", response_model=dict, tags=["观测"])
def last_error():
    """方向性引擎最近一次异常堆栈（无异常返回空串）。"""
    return {"last_error": _trader().last_error}
