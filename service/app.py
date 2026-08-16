"""
HTTP 接口层 — FastAPI 应用（完整功能的服务端外壳）。

暴露三大类接口：
  观测：/health /status /watchlist /signals/{base} /journal /realtime/{base} /arb/status
  控制：/pause /resume（暂停/恢复方向性开仓；止损监控永不暂停）
  运维：/scan/daily（手动触发全市场候选扫描）/error

【禁止】暴露"下单"类接口：交易决策只由后台引擎的既定策略做出，
HTTP 只是观测窗口与最小控制面，不是交易入口 —— 宁可做对，也不做错。

自动文档：GET /docs（Swagger UI，AI 可读 OpenAPI schema）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException

from service.models import (HealthOut, BalanceOut, PositionOut, OpenTradeOut,
                            StatusOut, WatchItem, WatchlistOut, SignalOut,
                            TradeItem, JournalOut, ControlOut, ArbStatusOut,
                            RealtimeOut, ScanOut)

app = FastAPI(
    title="Crypto Agent 交易服务",
    description=(
        "完整交易系统服务端：方向性日内短线引擎 + 资金费率套利引擎 + 实时行情。\n\n"
        "- 方向性引擎：2s 止损监控 + 15min 回踩信号扫描（后台线程）\n"
        "- 套利引擎：60s 事件检测/费率告警/套利持仓管理（后台线程）\n"
        "- 本接口只读观测 + 暂停/恢复开仓，不提供手动下单\n"
        "- 模拟盘（OKX sandbox），虚拟资金"),
    version="2.0.0")


def _worker():
    return app.state.worker


def _trader():
    return _worker().trader


def _arb():
    return _worker().arb


@app.get("/health", response_model=HealthOut, tags=["观测"])
def health():
    """服务健康：两引擎心跳年龄超时判定 degraded。"""
    import config
    w = _worker()
    hb_d, hb_a = w.heartbeat_age(), w.arb_heartbeat_age()
    ok = (hb_d >= 0 and hb_d < 30) and (hb_a < 0 or hb_a < 300)
    return HealthOut(status="ok" if ok else "degraded",
                     adapter=_trader().exchange.name,
                     uptime_seconds=round(w.uptime(), 1),
                     directional_heartbeat_age=round(hb_d, 1),
                     arb_heartbeat_age=round(hb_a, 1),
                     arb_enabled=config.ENABLE_FUNDING_ARB,
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
    t = _trader()
    trades = t.journal.trades[-limit:]
    closed = [x for x in t.journal.trades if x["status"] == "closed"]
    wins = [x for x in closed if (x.get("pnl") or 0) > 0]
    return JournalOut(
        total=len(t.journal.trades), closed=len(closed),
        win_rate=round(len(wins) / len(closed), 3) if closed else None,
        trades=[TradeItem(id=x["id"], symbol=x["symbol"],
                          direction=x.get("direction") or "long",
                          entry_price=x.get("entry_price"),
                          exit_price=x.get("exit_price"),
                          pnl_pct=round(x["pnl"] * 100, 2) if x.get("pnl") is not None else None,
                          status=x["status"],
                          entry_time=x.get("entry_time"),
                          exit_time=x.get("exit_time"),
                          venue=x.get("venue") or "swap",
                          notional_usdt=x.get("notional_usdt")) for x in trades])


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


@app.get("/arb/status", response_model=ArbStatusOut, tags=["观测"])
def arb_status():
    """套利引擎状态：配置开关、持仓台账、事件快照。"""
    import config
    a = _arb()
    return ArbStatusOut(
        enabled=config.ENABLE_FUNDING_ARB,
        positions_ledger=len(a.arb_positions),
        risk_halted=not a.risk.can_trade(),
        decision_threshold=a.threshold_learner.threshold,
        last_events=[f"{b}: {a.signal_state.get(b, {})}" for b in a.signal_state][-10:])


@app.post("/scan/daily", response_model=ScanOut, tags=["运维"])
def scan_daily():
    """手动触发一次全市场候选扫描（刷新 watchlist，覆盖 123 个标的）。
    耗时约 1-2 分钟；调用会阻塞等待完成。"""
    from engines.daily_scan import screen_daily
    try:
        w = screen_daily()
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")
    # 同步刷新两引擎的候选池（避免等跨天自动刷新）
    t = _trader()
    t.watchlist = [c["base"] for c in w]
    t.watch_scores = {c["base"]: c["score"] for c in w}
    t._watch_date = time.strftime("%Y-%m-%d")
    t._last_watch_refresh = time.time()
    return ScanOut(date=time.strftime("%Y-%m-%d"), fallback=bool(w and w[0].get("score") == 0.0),
                   candidates=[{"base": c["base"], "dir": c.get("dir"),
                                "score": round(c.get("score", 0), 3),
                                "atr_pct": round(c.get("atr_pct", 0), 4),
                                "price": c.get("price")} for c in w])


@app.post("/pause", response_model=ControlOut, tags=["控制"])
def pause():
    """暂停方向性开仓（止损监控不暂停）。信号扫描循环内部跳过。"""
    _trader().pause()
    return ControlOut(action="pause", paused=True,
                      message="已暂停开仓信号扫描；止损止盈监控继续运行")


@app.post("/resume", response_model=ControlOut, tags=["控制"])
def resume():
    """恢复方向性开仓信号扫描。"""
    _trader().resume()
    return ControlOut(action="resume", paused=False, message="已恢复开仓信号扫描")


@app.get("/error", response_model=dict, tags=["观测"])
def last_error():
    """方向性引擎最近一次异常堆栈（无异常返回空串）。"""
    return {"last_error": _trader().last_error}
