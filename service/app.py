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
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from typing import Callable, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Depends, Request

from service.models import (HealthOut, BalanceOut, PositionOut, OpenTradeOut,
                            StatusOut, WatchItem, WatchlistOut, SignalOut,
                            TradeItem, JournalOut, ControlOut,
                            RealtimeOut, ScanOut, ReconcileOut, RiskEventOut,
                            ScanEvolveOut, AgentStatusOut, AgentRunsOut,
                            AgentProposalsOut, AgentEvaluationOut, EntryModelsOut,
                            ForecastCalibrationOut, FactorTrialsOut,
                            EntryAccuracyAuditOut)

_APP_TITLE = "Crypto Agent 交易服务"
_APP_DESCRIPTION = (
    "交易系统服务端：方向性日内短线引擎 + 实时行情。\n\n"
    "- 方向性引擎：2s 止损监控 + 15min 回踩信号扫描（后台线程）\n"
    "- 本接口只读观测 + 暂停/恢复开仓，不提供手动下单\n"
    "- /journal 总盈亏为已平仓合计实际 USDT，不是百分比相加\n"
    "- 模拟盘（OKX sandbox），虚拟资金")
_WORKER_ENV_KEYS = ("WEB_CONCURRENCY", "UVICORN_WORKERS")


def ensure_single_worker() -> None:
    """交易引擎是单例；显式拒绝常见的 Uvicorn 多 worker 配置。

    引擎自身的 engine.lock 仍是最后一道跨进程防线；这里的检查
    让错误配置在连接交易所之前就 fail-closed。"""
    for key in _WORKER_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            count = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{key} 必须是整数，且本服务只允许 1") from exc
        if count != 1:
            raise RuntimeError(
                f"拒绝启动：{key}={count}；交易引擎只允许单 worker")


router = APIRouter()


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


def _worker(request: Request):
    return request.app.state.worker


def _trader(request: Request):
    return _worker(request).trader


@router.get("/health", response_model=HealthOut, tags=["观测"])
def health(request: Request):
    """服务健康：方向性引擎心跳年龄超时判定 degraded。"""
    w = _worker(request)
    hb_d = w.heartbeat_age()
    ok = hb_d >= 0 and hb_d < 30
    return HealthOut(status="ok" if ok else "degraded",
                     adapter=_trader(request).exchange.name,
                     uptime_seconds=round(w.uptime(), 1),
                     directional_heartbeat_age=round(hb_d, 1),
                     paused=_trader(request).paused)


@router.get("/status", response_model=StatusOut, tags=["观测"])
def status(request: Request):
    """账户全景：余额 + 交易所持仓 + journal 未平仓 + 风控状态。"""
    t = _trader(request)
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
    # 实盘盈亏（2026-08-23 用户指示"重新开始计盈亏"）：基线净值 kv 起算。
    # 仅实盘实例展示(模拟盘实例 live_mode=False,不显示实盘盈亏字段)。
    live_real, live_eq_pnl, live_start_eq = None, None, None
    if getattr(t, "live_mode", False):
        try:
            from execution.trade_journal import total_net_realized_pnl_usdt
            import storage.db as sdb
            closed = [x for x in t.journal.trades if x["status"] == "closed"]
            live_real = total_net_realized_pnl_usdt(
                [x for x in closed if (x.get("venue") == "live")])
            row = sdb.q1("SELECT value FROM kv WHERE key='live_pnl_start'",
                         db_path=t.journal.db_path)
            if row:
                import json as _json
                base = _json.loads(row["value"])
                live_start_eq = base.get("equity")
                if live_start_eq and bal and bal.total_eq > 0:
                    live_eq_pnl = round(bal.total_eq - live_start_eq, 2)
        except Exception:
            pass
    # 2026-08-23 连亏冷却状态(用户指示"连亏 6 笔后应主动冷却")
    _cooling = {"cooling": False, "remaining": 0.0, "streak": 0}
    try:
        from decision.loss_cooling import (is_cooling, cooling_remaining_hours,
                                           streak)
        _cooling["cooling"] = is_cooling(t._db_path)
        _cooling["remaining"] = cooling_remaining_hours(t._db_path)
        _cooling["streak"] = streak(t._db_path)
    except Exception:
        pass
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
        decision_threshold=t.effective_threshold(),
        today_trade_count=today_n,
        total_notional_usdt=round(total_notional, 2),
        open_notional_usdt=round(open_notional, 2),
        today_notional_usdt=round(today_notional, 2),
        live_realized_pnl_usdt=live_real,
        live_equity_pnl_usdt=live_eq_pnl,
        live_pnl_start_equity=live_start_eq,
        loss_cooling=_cooling["cooling"],
        loss_cooling_remaining_hours=_cooling["remaining"],
        loss_streak=_cooling["streak"])


@router.get("/watchlist", response_model=WatchlistOut, tags=["观测"])
def watchlist(request: Request):
    """今日加密/美股独立候选池（评分 → 允许笔数）。"""
    t = _trader(request)
    crypto_bases = list(getattr(t, "crypto_watchlist", []))
    stock_bases = list(getattr(t, "stock_watchlist", []))
    # 兼容测试/外部宿主只提供旧 watchlist 属性的情况。
    if not crypto_bases and not stock_bases:
        stock_set = set(config.STOCK_SWAP_TOKENS)
        crypto_bases = [b for b in t.watchlist if b not in stock_set]
        stock_bases = [b for b in t.watchlist if b in stock_set]
    crypto_items = [WatchItem(base=b, score=t.watch_scores.get(b),
                              budget=t._trade_budget(b), pool="crypto")
                    for b in crypto_bases]
    stock_items = [WatchItem(base=b, score=t.watch_scores.get(b),
                             budget=t._trade_budget(b), pool="stock")
                   for b in stock_bases]
    return WatchlistOut(date=time.strftime("%Y-%m-%d"),
                        crypto_items=crypto_items, stock_items=stock_items,
                        items=crypto_items + stock_items)


@router.get("/signals/{base}", response_model=SignalOut, tags=["观测"])
def signal_for(request: Request, base: str):
    """按需跑一次某币的回踩确认信号检查（只读，不下单、不消耗冷却）。"""
    t = _trader(request)
    try:
        sig = t.scan_signal(base.upper())
    except Exception as e:
        raise HTTPException(500, f"信号检查失败: {e}")
    venue = t.exchange.venue_for(base.upper())
    return SignalOut(base=base.upper(), venue=venue, signal=sig,
                     message="有信号" if sig else "无回踩确认信号")


@router.get("/journal", response_model=JournalOut, tags=["观测"])
def journal(request: Request, limit: int = 20):
    """最近交易台账（含盈亏）。"""
    from execution.trade_journal import (realized_pnl_usdt,
                                          total_realized_pnl_usdt,
                                          total_net_realized_pnl_usdt)
    t = _trader(request)
    trades = t.journal.trades[-limit:]
    closed = [x for x in t.journal.trades if x["status"] == "closed"]
    wins = [x for x in closed if (x.get("pnl") or 0) > 0]
    # 实盘盈亏单独计数(venue=live,2026-08-23 用户指示"重新开始计盈亏")
    live_total = total_net_realized_pnl_usdt(
        [x for x in closed if (x.get("venue") == "live")])
    return JournalOut(
        total=len(t.journal.trades), closed=len(closed),
        win_rate=round(len(wins) / len(closed), 3) if closed else None,
        total_pnl_usdt=total_realized_pnl_usdt(closed),
        live_total_pnl_usdt=live_total,
        trades=[TradeItem(id=x["id"], symbol=x["symbol"],
                          strategy_id=(x.get("strategy_id") or
                                       config.ENTRY_SIGNAL_STRATEGY_ID),
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
                          strategy_timeframe=x.get("strategy_timeframe"),
                          max_hold_hours=x.get("max_hold_hours"),
                          review=x.get("review")) for x in trades])


@router.get("/realtime/{base}", response_model=RealtimeOut, tags=["观测"])
def realtime(request: Request, base: str):
    """某币实时行情快照（WebSocket 数据，stale 字段剔除）。"""
    t = _trader(request)
    base = base.upper()
    data = {}
    orderflow = {"status": "missing", "ofi_event_multilevel": None,
                 "ofi_event_cancel_imbalance": None,
                 "ofi_event_count": 0, "ofi_event_age_ms": None}
    if t.rt is not None:
        data = t.rt.get(base, max_age=60)
        try:
            get_orderflow = getattr(t.rt, "get_orderflow", None)
            if get_orderflow:
                orderflow.update(get_orderflow(base) or {})
        except Exception:
            pass
    fresh = bool(data.get("price"))
    return RealtimeOut(base=base,
                       price=data.get("price"),
                       swap_price=data.get("swap_price"),
                       funding=data.get("funding"),
                       vol_15m=data.get("vol_15m"),
                       fresh=fresh,
                       orderflow_status=orderflow["status"],
                       ofi_event_multilevel=orderflow["ofi_event_multilevel"],
                       ofi_event_cancel_imbalance=orderflow[
                           "ofi_event_cancel_imbalance"],
                       ofi_event_count=orderflow["ofi_event_count"],
                       ofi_event_age_ms=orderflow["ofi_event_age_ms"])


@router.get("/anomalies", tags=["观测"])
def anomalies(request: Request):
    """统一异常中心(2026-08-17 用户要求:所有异常统一输出到一个接口)。
    消费端只读此端点/表,不接触各业务表。"""
    import storage.db as sdb
    db = _trader(request)._db_path
    sdb.init_db(db)
    return sdb.q("SELECT id, ts, source, severity, title, detail, status "
                 "FROM anomalies ORDER BY ts DESC LIMIT 50", db_path=db)


def _agent_db_path(request: Request):
    """Resolve the instance-scoped DB without reading a live/global default."""
    try:
        t = _trader(request)
        return getattr(t, "_db_path", None) or getattr(getattr(t, "journal", None), "db_path", None)
    except Exception:
        return None


@router.get("/agent/status", response_model=AgentStatusOut, tags=["Agent Harness"])
def agent_status(request: Request):
    """Agent Harness health and active version; read-only."""
    import storage.db as sdb
    path = _agent_db_path(request)
    sdb.init_db(path)
    rows = sdb.q("SELECT runtime_status FROM agent_runs", db_path=path)
    failed = sum(row["runtime_status"] not in ("completed", "disabled", "no_key") for row in rows)
    versions = sdb.q("SELECT version,status FROM agent_versions "
                     "ORDER BY created_ts DESC LIMIT 1", db_path=path)
    current = versions[0] if versions else {}
    return AgentStatusOut(
        current_version=current.get("version"), current_status=current.get("status"),
        total_runs=len(rows), completed_runs=sum(row["runtime_status"] == "completed" for row in rows),
        failed_runs=failed, failure_rate=round(failed / len(rows), 4) if rows else 0.0,
        shadow_enabled=True, veto_enabled=current.get("status") == "active-veto")


@router.get("/agent/runs", response_model=AgentRunsOut, tags=["Agent Harness"])
def agent_runs(request: Request, limit: int = 50):
    """Recent Harness runs and runtime failures; read-only."""
    from storage.agent_harness import list_runs
    return AgentRunsOut(runs=list_runs(limit=max(1, min(limit, 500)),
                                       db_path=_agent_db_path(request)))


@router.get("/agent/proposals", response_model=AgentProposalsOut,
            tags=["Agent Harness"])
def agent_proposals(request: Request, limit: int = 50):
    """AI 主动方向提案、确定性 2:1 验证与反事实结果；只读。"""
    from decision.agent_proposals import list_proposals
    return AgentProposalsOut(**list_proposals(
        limit=max(1, min(limit, 500)), db_path=_agent_db_path(request)))


@router.get("/agent/evaluation", response_model=AgentEvaluationOut, tags=["Agent Harness"])
def agent_evaluation(request: Request):
    """Harness 成熟结果与旧 AI 把关反事实增量的统一只读报告。"""
    import storage.db as sdb
    from decision.agent_evaluation import evaluate_agent, evaluate_harness
    path = _agent_db_path(request)
    sdb.init_db(path)
    rows = sdb.q("SELECT * FROM agent_evaluations", db_path=path)
    mature = [row for row in rows if row.get("lifecycle_status") == "mature"]
    saved = sum(float(row.get("saved_loss") or 0) for row in mature)
    missed = sum(float(row.get("missed_profit") or 0) for row in mature)
    counterfactual = evaluate_agent(path)
    return AgentEvaluationOut(
        samples=len(mature),
        reject_samples=sum(float(row.get("saved_loss") or 0) > 0 or float(row.get("missed_profit") or 0) > 0
                           for row in mature),
        saved_loss=round(saved, 8), missed_profit=round(missed, 8),
        incremental_ev=round(saved - missed, 8), mature_samples=len(mature),
        pending_samples=sum(row.get("lifecycle_status") == "pending" for row in rows),
        harness=evaluate_harness(path),
        **counterfactual)


@router.post("/scan/daily", response_model=ScanOut, tags=["运维"],
             dependencies=[Depends(require_control)])
def scan_daily(request: Request):
    """手动触发一次全市场候选扫描（刷新 watchlist，覆盖 123 个标的）。
    耗时约 1-2 分钟；调用会阻塞等待完成。"""
    from engines.daily_scan import screen_daily
    t = _trader(request)
    try:
        w = screen_daily(exchange=t.exchange, db_path=t._db_path)
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")
    # 同步刷新引擎的候选池（避免等跨天自动刷新）
    t.crypto_watchlist = [c["base"] for c in w if not c.get("is_stock")]
    t.stock_watchlist = [c["base"] for c in w if c.get("is_stock")]
    t.watchlist = t.crypto_watchlist + t.stock_watchlist
    t.watch_scores = {c["base"]: c["score"] for c in w}
    t._watch_date = time.strftime("%Y-%m-%d")
    t._last_watch_refresh = time.time()
    candidates = [{"base": c["base"], "dir": c.get("dir"),
                   "score": round(c.get("score", 0), 3),
                   "atr_pct": round(c.get("atr_pct", 0), 4),
                   "price": c.get("price"),
                   "pool": "stock" if c.get("is_stock") else "crypto"}
                  for c in w]
    return ScanOut(
        date=time.strftime("%Y-%m-%d"),
        fallback=any(c["pool"] == "crypto" and c["score"] == 0.0
                     for c in candidates),
        candidates=candidates,
        crypto_candidates=[c for c in candidates if c["pool"] == "crypto"],
        stock_candidates=[c for c in candidates if c["pool"] == "stock"])


@router.get("/scan/evolve", response_model=ScanEvolveOut, tags=["观测"])
def scan_evolve_status(request: Request):
    """扫描尺子进化状态：现役/活体/候选影线比、影子样本、是否待批准。
    只读；不下单、不改尺子。落库走引擎 db_path（测试隔离、防写活体库）。"""
    from decision.scan_evolve import snapshot
    return ScanEvolveOut(**snapshot(_trader(request)._db_path))


@router.post("/scan/evolve/approve", response_model=ScanEvolveOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def scan_evolve_approve(request: Request):
    """批准已通过影子验证门的扫描尺子（目前仅 REJECT_WICK_RATIO）。
    未通过验证门的提案一律拒绝。不改 config.py，覆盖写在 kv，可回滚。"""
    from decision.scan_evolve import approve, snapshot
    db = _trader(request)._db_path
    ok, msg = approve(db_path=db)
    if not ok:
        raise HTTPException(409, msg)
    out = ScanEvolveOut(**snapshot(db))
    out.message = msg
    return out


@router.post("/scan/evolve/rollback", response_model=ScanEvolveOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def scan_evolve_rollback(request: Request):
    """撤销活体影线比覆盖，回到 config.REJECT_WICK_RATIO。"""
    from decision.scan_evolve import rollback, snapshot
    db = _trader(request)._db_path
    _, msg = rollback(db_path=db)
    out = ScanEvolveOut(**snapshot(db))
    out.message = msg
    return out


@router.get("/weights/evolve", response_model=dict, tags=["观测"])
def weights_evolve_status(request: Request):
    """权重进化状态：活体权重(批准后 kv 覆盖)/config 基线/待处理提案与证据。
    只读;权重永不自动改,approve 是唯一写入口。"""
    from decision.weight_evolve import snapshot
    return snapshot(_trader(request)._db_path)


@router.post("/weights/evolve/propose", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_propose(request: Request):
    """按已平仓样本的逐维 IC 生成权重提案(证据达标=accepted 待批准)。
    不生效——必须再调 /weights/evolve/approve。"""
    from decision.weight_evolve import propose, snapshot
    db = _trader(request)._db_path
    status, msg, evidence = propose(db_path=db, force=True)
    out = snapshot(db)
    out.update({"status": status, "message": msg, "evidence": evidence})
    return out


@router.post("/weights/evolve/approve", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_approve(request: Request):
    """批准证据达标的权重提案 → kv 覆盖生效(评分立即用新权重)。"""
    from decision.weight_evolve import approve, snapshot
    db = _trader(request)._db_path
    ok, msg = approve(db_path=db)
    if not ok:
        raise HTTPException(409, msg)
    out = snapshot(db)
    out["message"] = msg
    return out


@router.post("/weights/evolve/rollback", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_rollback(request: Request):
    """撤销活体权重覆盖,回到 config.SHADOW_WEIGHTS 基线。"""
    from decision.weight_evolve import rollback, snapshot
    db = _trader(request)._db_path
    _, msg = rollback(db_path=db)
    out = snapshot(db)
    out["message"] = msg
    return out


@router.get("/models/entry", response_model=EntryModelsOut, tags=["观测"])
def entry_models(request: Request):
    """开仓概率模型版本、样本外指标、状态与预算扩张硬锁。"""
    from decision.model_lifecycle import snapshot
    result = snapshot(_trader(request)._db_path)
    result["models"] = [model for model in result["models"]
                        if model["model_type"] == "entry_probability"]
    return EntryModelsOut(**result)


@router.post("/models/entry/rollback", response_model=EntryModelsOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def entry_model_rollback(request: Request):
    """一键回滚当前 entry 模型；只改模型状态，不下单、不改风险预算。"""
    from decision.model_lifecycle import rollback, snapshot
    db = _trader(request)._db_path
    ok, message = rollback(db_path=db)
    if not ok:
        raise HTTPException(409, message)
    result = snapshot(db)
    result["models"] = [model for model in result["models"]
                        if model["model_type"] == "entry_probability"]
    return EntryModelsOut(**result)


@router.post("/cool/release", response_model=dict, tags=["控制"],
            dependencies=[Depends(require_control)])
def cool_release(request: Request):
    """手动解除连亏冷却(用户指示'解除冷却'): 清冷却计时+连亏计数归零。"""
    from decision.loss_cooling import release
    db = _trader(request)._db_path
    ok = release(db)
    return {"message": "冷却已解除,连亏计数归零" if ok else "解除失败"}


@router.get("/forecast/calibration", response_model=ForecastCalibrationOut, tags=["观测"])
def forecast_calibration(request: Request):
    """预测校准报告：首触 Brier + 极值分位 pinball/coverage。"""
    from decision.forecast import calibration
    from decision.model_lifecycle import snapshot
    db = _trader(request)._db_path
    result = calibration(db)
    result["extrema"] = {
        "models": [model for model in snapshot(db)["models"]
                   if model["model_type"] == "extrema"]}
    return ForecastCalibrationOut(**result)


@router.get("/factors/trials", response_model=FactorTrialsOut, tags=["观测"])
def factor_trials(request: Request, limit: int = 50,
                  strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """最近日内因子试验、OOS 证据与拒绝原因；不触发训练。"""
    import storage.db as sdb
    rows = sdb.q(
        "SELECT id,ts,name,strategy_id,status,n_samples,n_folds,ic_tstat,net_spread,"
        "dsr,pbo,missing_rate,fold_consistency,redundant_with "
        "FROM factor_trials WHERE strategy_id=? ORDER BY id DESC LIMIT ?",
        [strategy_id, max(1, min(200, limit))],
        db_path=_trader(request)._db_path)
    return FactorTrialsOut(trials=rows)


@router.get("/research/readiness", response_model=EntryAccuracyAuditOut,
            tags=["观测"])
def entry_accuracy_readiness(
        request: Request,
        strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """15m 开仓准确率/因子/极值/Agent 计划统计门；纯只读，不触发训练。"""
    from tools.entry_accuracy_audit import audit_status
    try:
        result = audit_status(_trader(request)._db_path,
                              strategy_id=strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return EntryAccuracyAuditOut(**result)


@router.post("/pause", response_model=ControlOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def pause(request: Request):
    """暂停方向性开仓（止损监控不暂停）。信号扫描循环内部跳过。"""
    _trader(request).pause()
    return ControlOut(action="pause", paused=True,
                      message="已暂停开仓信号扫描；止损止盈监控继续运行")


@router.post("/resume", response_model=ControlOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def resume(request: Request):
    """恢复方向性开仓信号扫描。"""
    _trader(request).resume()
    return ControlOut(action="resume", paused=False, message="已恢复开仓信号扫描")


@router.get("/reconcile", response_model=ReconcileOut, tags=["观测"])
def reconcile(request: Request):
    """对账：journal 记账（本地） vs 交易所真实持仓（唯一事实源）。
    legacy 记录 size 是张数，按 ct_val 折算币数后对比；不一致即报告，不静默。"""
    import json as _json
    from collections import defaultdict
    t = _trader(request)
    # 快照（最近一次本地落库）
    import storage.db as sdb
    snap = None
    try:
        row = sdb.q1("SELECT MAX(ts) ts FROM position_snapshots",
                     db_path=t._db_path)
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


@router.get("/analysis/latest", response_model=dict, tags=["观测"])
def analysis_latest(request: Request):
    """最近一次看账报告（报告 + 感知到的问题 + 生成的教训 id）。"""
    import storage.db as sdb
    db = _trader(request)._db_path
    sdb.init_db(db)
    row = sdb.q1("SELECT * FROM analyses ORDER BY id DESC LIMIT 1", db_path=db)
    if not row:
        return {"report": None, "issues": [], "message": "尚无分析记录"}
    import json as _json
    return {"ts": row["ts"], "kind": row["kind"],
            "report": _json.loads(row["report"]), "issues": _json.loads(row["issues"])}


@router.post("/analysis/daily", response_model=dict, tags=["运维"],
             dependencies=[Depends(require_control)])
def analysis_daily(request: Request):
    """手动触发一次每日看账（分析 + 问题感知 + 教训入经验库 + 飞书反馈）。"""
    from decision.analyst import run_daily
    t = _trader(request)
    return run_daily(db_path=t._db_path, notifier=t._notify)


@router.get("/risk/events", response_model=List[RiskEventOut], tags=["观测"])
def risk_events(request: Request, limit: int = 20):
    """风控事件复盘记录：熔断/恢复，含触发时净值与持仓数快照。"""
    import storage.db as sdb
    db = _trader(request)._db_path
    sdb.init_db(db)
    rows = sdb.q("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", [limit],
                 db_path=db)
    return [RiskEventOut(**r) for r in reversed(rows)]


@router.get("/error", response_model=dict, tags=["观测"])
def last_error(request: Request):
    """方向性引擎最近一次异常堆栈（无异常返回空串）。"""
    return {"last_error": _trader(request).last_error}


@router.get("/readiness", response_model=dict, tags=["观测"])
def readiness(request: Request):
    """实盘就绪三盏灯(2026-08-20 用户指示)——样本/稳定/反哺,全绿才可上实盘。"""
    from tools.readiness import readiness_status
    return readiness_status(_trader(request)._db_path)


@router.get("/combos", response_model=dict, tags=["观测"])
def combos(request: Request, min_samples: int = 3):
    """组合试验统计(2026-08-21 用户洞察'单条不盈利,combo 可能盈利')——
    只观测;达标组合走 experiments 提案,不自动改决策。"""
    from decision.experience_scoring import combo_stats
    return {"combos": combo_stats(_trader(request)._db_path,
                                  min_samples=min_samples)}


def create_app(
        worker_factory: Optional[Callable[[], object]] = None) -> FastAPI:
    """构建一个完全独立的 ASGI 应用。

    worker 只在 ASGI lifespan 启动后创建，因此导入模块、生成
    OpenAPI 或创建测试 app 都不会连接 OKX/WS。测试可注入假 worker。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_single_worker()
        factory = worker_factory
        if factory is None:
            # 延迟 import：纯 API schema/测试导入不应装配交易引擎。
            from service.worker import TraderWorker
            factory = TraderWorker

        worker = factory()
        application.state.worker = worker
        try:
            worker.start()
        except Exception:
            # start() 可能已启动部分线程；失败时也要尽力收敛。
            try:
                worker.stop()
            except Exception:
                pass
            raise

        try:
            yield
        finally:
            # 顺序由 TraderWorker.stop 保证：停主循环→停监控→join。
            try:
                print("[shutdown] 停方向性引擎…")
                stopped = worker.stop()
                if stopped is False:
                    print("[shutdown] 引擎线程未在超时内全部停止")
                else:
                    print("[shutdown] 引擎线程已停止")
            except Exception as exc:
                print(f"[shutdown] 停机异常(忽略): {exc}")

    application = FastAPI(
        title=_APP_TITLE,
        description=_APP_DESCRIPTION,
        version="2.1.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    return application


# Uvicorn 默认导入入口；导入本身不启动 worker。
app = create_app()
