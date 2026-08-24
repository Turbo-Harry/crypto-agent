"""
HTTP 接口层 — FastAPI 应用（完整功能的服务端外壳）。

暴露三大类接口：
  观测：运行、对账、研究、模型与 Agent 审计快照；
  控制：暂停/恢复、通过验证门的批准/回滚（止损监控永不暂停）；
  运维：候选扫描、每日分析、冷却解除和异常检查。
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
from decision import api as decision_api
from typing import Callable, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Depends, Request

from engines.runtime_api import runtime_api
from interfaces.trading import TradingRuntimePort
from storage.query_api import (
    agent_status_summary,
    latest_analysis,
    list_agent_evaluations,
    list_agent_runs,
    list_anomalies,
    list_factor_trials,
    list_risk_events,
)

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
    "- 方向性引擎：1s 风控节拍 + 每 5min 检查已收线 15m 信号（后台线程）\n"
    "- 本接口提供观测 + 有限控制/运维，不提供手动下单或撤单\n"
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


def _runtime(request: Request) -> TradingRuntimePort:
    """Resolve the engine exclusively through its stable service contract."""
    trader = _trader(request)
    return getattr(trader, "service_api", None) or runtime_api(trader)


@router.get("/health", response_model=HealthOut, tags=["观测"])
def health(request: Request):
    """服务健康：方向性引擎心跳年龄超时判定 degraded。"""
    w = _worker(request)
    hb_d = w.heartbeat_age()
    ok = hb_d >= 0 and hb_d < 30
    return HealthOut(status="ok" if ok else "degraded",
                     adapter=_runtime(request).adapter_name,
                     uptime_seconds=round(w.uptime(), 1),
                     directional_heartbeat_age=round(hb_d, 1),
                     paused=_runtime(request).paused)


@router.get("/status", response_model=StatusOut, tags=["观测"])
def status(request: Request):
    """账户全景：余额 + 交易所持仓 + journal 未平仓 + 风控状态。"""
    snapshot = _runtime(request).status_snapshot()
    bal = snapshot["balance"]
    positions = snapshot["positions"]
    open_trades = snapshot["open_trades"]
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
        risk_halted=snapshot["risk_halted"],
        risk_reason=snapshot["risk_reason"],
        decision_threshold=snapshot["decision_threshold"],
        today_trade_count=snapshot["today_trade_count"],
        total_notional_usdt=snapshot["total_notional_usdt"],
        open_notional_usdt=snapshot["open_notional_usdt"],
        today_notional_usdt=snapshot["today_notional_usdt"],
        live_realized_pnl_usdt=snapshot["live_realized_pnl_usdt"],
        live_equity_pnl_usdt=snapshot["live_equity_pnl_usdt"],
        live_pnl_start_equity=snapshot["live_pnl_start_equity"],
        loss_cooling=snapshot["loss_cooling"],
        loss_cooling_remaining_hours=snapshot["loss_cooling_remaining_hours"],
        loss_streak=snapshot["loss_streak"])


@router.get("/watchlist", response_model=WatchlistOut, tags=["观测"])
def watchlist(request: Request):
    """今日加密/美股独立候选池（评分 → 允许笔数）。"""
    snapshot = _runtime(request).watchlist_snapshot()
    return WatchlistOut(**snapshot)


@router.get("/signals/{base}", response_model=SignalOut, tags=["观测"])
def signal_for(request: Request, base: str):
    """按需跑一次某币的回踩确认信号检查（只读，不下单、不消耗冷却）。"""
    try:
        snapshot = _runtime(request).inspect_signal(base)
    except Exception as e:
        raise HTTPException(500, f"信号检查失败: {e}")
    return SignalOut(**snapshot)


@router.get("/journal", response_model=JournalOut, tags=["观测"])
def journal(request: Request, limit: int = 20):
    """最近交易台账（含盈亏）。"""
    snapshot = _runtime(request).journal_snapshot(limit)
    return JournalOut(
        total=snapshot["total"], closed=snapshot["closed"],
        win_rate=snapshot["win_rate"],
        total_pnl_usdt=snapshot["total_pnl_usdt"],
        live_total_pnl_usdt=snapshot["live_total_pnl_usdt"],
        trades=[TradeItem(id=x["id"], symbol=x["symbol"],
                          strategy_id=(x.get("strategy_id") or
                                       config.ENTRY_SIGNAL_STRATEGY_ID),
                          direction=x.get("direction") or "long",
                          entry_price=x.get("entry_price"),
                          exit_price=x.get("exit_price"),
                          pnl_pct=round(x["pnl"] * 100, 2) if x.get("pnl") is not None else None,
                          pnl_usdt=x["pnl_usdt"],
                          status=x["status"],
                          entry_time=x.get("entry_time"),
                          exit_time=x.get("exit_time"),
                          venue=x.get("venue") or "swap",
                          notional_usdt=x.get("notional_usdt"),
                          strategy_timeframe=x.get("strategy_timeframe"),
                          max_hold_hours=x.get("max_hold_hours"),
                          review=x.get("review")) for x in snapshot["trades"]])


@router.get("/realtime/{base}", response_model=RealtimeOut, tags=["观测"])
def realtime(request: Request, base: str):
    """某币实时行情快照（WebSocket 数据，stale 字段剔除）。"""
    snapshot = dict(_runtime(request).realtime_snapshot(base))
    snapshot["orderflow_status"] = snapshot.pop("status")
    return RealtimeOut(**snapshot)


@router.get("/anomalies", tags=["观测"])
def anomalies(request: Request):
    """统一异常中心(2026-08-17 用户要求:所有异常统一输出到一个接口)。
    消费端只读此端点/表,不接触各业务表。"""
    return list_anomalies(_runtime(request).db_path)


def _agent_db_path(request: Request):
    """Resolve the instance-scoped DB without reading a live/global default."""
    try:
        return _runtime(request).db_path
    except Exception:
        return None


@router.get("/agent/status", response_model=AgentStatusOut, tags=["Agent Harness"])
def agent_status(
        request: Request,
        strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """Agent Harness health and active version; read-only."""
    path = _agent_db_path(request)
    try:
        strategy_version = decision_api.research_strategy_version(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    configured_version = decision_api.configured_harness_version(strategy_id)
    return AgentStatusOut(**agent_status_summary(
        path, strategy_id=strategy_id, strategy_version=strategy_version,
        configured_version=configured_version))


@router.get("/agent/runs", response_model=AgentRunsOut, tags=["Agent Harness"])
def agent_runs(request: Request, limit: int = 50):
    """Recent Harness runs and runtime failures; read-only."""
    return AgentRunsOut(runs=list_agent_runs(
        _agent_db_path(request), max(1, min(limit, 500))))


@router.get("/agent/proposals", response_model=AgentProposalsOut,
            tags=["Agent Harness"])
def agent_proposals(request: Request, limit: int = 50):
    """AI 主动方向提案、确定性 2:1 验证与反事实结果；只读。"""
    return AgentProposalsOut(**decision_api.list_agent_proposals(
        limit=max(1, min(limit, 500)), db_path=_agent_db_path(request)))


@router.get("/agent/evaluation", response_model=AgentEvaluationOut, tags=["Agent Harness"])
def agent_evaluation(
        request: Request,
        strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """Harness 成熟结果与旧 AI 把关反事实增量的统一只读报告。"""
    path = _agent_db_path(request)
    try:
        strategy_version = decision_api.research_strategy_version(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = list_agent_evaluations(
        path, strategy_id=strategy_id, strategy_version=strategy_version)
    mature = [row for row in rows if row.get("lifecycle_status") == "mature"]
    saved = sum(float(row.get("saved_loss") or 0) for row in mature)
    missed = sum(float(row.get("missed_profit") or 0) for row in mature)
    counterfactual = decision_api.evaluate_agent(
        path, strategy_id=strategy_id)
    return AgentEvaluationOut(
        samples=len(mature),
        reject_samples=sum(float(row.get("saved_loss") or 0) > 0 or float(row.get("missed_profit") or 0) > 0
                           for row in mature),
        saved_loss=round(saved, 8), missed_profit=round(missed, 8),
        incremental_ev=round(saved - missed, 8), mature_samples=len(mature),
        pending_samples=sum(row.get("lifecycle_status") == "pending" for row in rows),
        harness=decision_api.evaluate_harness(
            path, strategy_id=strategy_id),
        **counterfactual)


@router.post("/scan/daily", response_model=ScanOut, tags=["运维"],
             dependencies=[Depends(require_control)])
def scan_daily(request: Request):
    """手动触发一次全市场候选扫描（刷新 watchlist，覆盖 123 个标的）。
    耗时约 1-2 分钟；调用会阻塞等待完成。"""
    try:
        w = _runtime(request).refresh_watchlist()
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")
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
    return ScanEvolveOut(**decision_api.scan_evolution_snapshot(
        _runtime(request).db_path))


@router.post("/scan/evolve/approve", response_model=ScanEvolveOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def scan_evolve_approve(request: Request):
    """批准已通过影子验证门的扫描尺子（目前仅 REJECT_WICK_RATIO）。
    未通过验证门的提案一律拒绝。不改 config.py，覆盖写在 kv，可回滚。"""
    db = _runtime(request).db_path
    ok, msg = decision_api.approve_scan_evolution(db)
    if not ok:
        raise HTTPException(409, msg)
    out = ScanEvolveOut(**decision_api.scan_evolution_snapshot(db))
    out.message = msg
    return out


@router.post("/scan/evolve/rollback", response_model=ScanEvolveOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def scan_evolve_rollback(request: Request):
    """撤销活体影线比覆盖，回到 config.REJECT_WICK_RATIO。"""
    db = _runtime(request).db_path
    _, msg = decision_api.rollback_scan_evolution(db)
    out = ScanEvolveOut(**decision_api.scan_evolution_snapshot(db))
    out.message = msg
    return out


@router.get("/weights/evolve", response_model=dict, tags=["观测"])
def weights_evolve_status(request: Request):
    """权重进化状态：活体权重(批准后 kv 覆盖)/config 基线/待处理提案与证据。
    只读;权重永不自动改,approve 是唯一写入口。"""
    return decision_api.weight_evolution_snapshot(_runtime(request).db_path)


@router.post("/weights/evolve/propose", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_propose(request: Request):
    """按已平仓样本的逐维 IC 生成权重提案(证据达标=accepted 待批准)。
    不生效——必须再调 /weights/evolve/approve。"""
    db = _runtime(request).db_path
    status, msg, evidence = decision_api.propose_weight_evolution(db)
    out = decision_api.weight_evolution_snapshot(db)
    out.update({"status": status, "message": msg, "evidence": evidence})
    return out


@router.post("/weights/evolve/approve", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_approve(request: Request):
    """批准证据达标的权重提案 → kv 覆盖生效(评分立即用新权重)。"""
    db = _runtime(request).db_path
    ok, msg = decision_api.approve_weight_evolution(db)
    if not ok:
        raise HTTPException(409, msg)
    out = decision_api.weight_evolution_snapshot(db)
    out["message"] = msg
    return out


@router.post("/weights/evolve/rollback", response_model=dict, tags=["控制"],
             dependencies=[Depends(require_control)])
def weights_evolve_rollback(request: Request):
    """撤销活体权重覆盖,回到 config.SHADOW_WEIGHTS 基线。"""
    db = _runtime(request).db_path
    _, msg = decision_api.rollback_weight_evolution(db)
    out = decision_api.weight_evolution_snapshot(db)
    out["message"] = msg
    return out


@router.get("/models/entry", response_model=EntryModelsOut, tags=["观测"])
def entry_models(request: Request):
    """开仓概率模型版本、样本外指标、状态与预算扩张硬锁。"""
    result = decision_api.model_snapshot(_runtime(request).db_path)
    result["models"] = [model for model in result["models"]
                        if model["model_type"] == "entry_probability"]
    return EntryModelsOut(**result)


@router.post("/models/entry/rollback", response_model=EntryModelsOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def entry_model_rollback(request: Request):
    """一键回滚当前 entry 模型；只改模型状态，不下单、不改风险预算。"""
    db = _runtime(request).db_path
    ok, message = decision_api.rollback_entry_model(db)
    if not ok:
        raise HTTPException(409, message)
    result = decision_api.model_snapshot(db)
    result["models"] = [model for model in result["models"]
                        if model["model_type"] == "entry_probability"]
    return EntryModelsOut(**result)


@router.post("/cool/release", response_model=dict, tags=["控制"],
            dependencies=[Depends(require_control)])
def cool_release(request: Request):
    """手动解除连亏冷却(用户指示'解除冷却'): 清冷却计时+连亏计数归零。"""
    db = _runtime(request).db_path
    ok = decision_api.release_loss_cooling(db)
    return {"message": "冷却已解除,连亏计数归零" if ok else "解除失败"}


@router.get("/forecast/calibration", response_model=ForecastCalibrationOut, tags=["观测"])
def forecast_calibration(request: Request):
    """预测校准报告：首触 Brier + 极值分位 pinball/coverage。"""
    db = _runtime(request).db_path
    result = decision_api.forecast_calibration(db)
    result["extrema"] = {
        "models": [model for model in decision_api.model_snapshot(db)["models"]
                   if model["model_type"] == "extrema"]}
    return ForecastCalibrationOut(**result)


@router.get("/factors/trials", response_model=FactorTrialsOut, tags=["观测"])
def factor_trials(request: Request, limit: int = 50,
                  strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """最近日内因子试验、OOS 证据与拒绝原因；不触发训练。"""
    strategy_version = decision_api.research_strategy_version(strategy_id)
    rows = list_factor_trials(_runtime(request).db_path, strategy_id, limit,
                              strategy_version=strategy_version)
    return FactorTrialsOut(trials=rows)


@router.get("/research/readiness", response_model=EntryAccuracyAuditOut,
            tags=["观测"])
def entry_accuracy_readiness(
        request: Request,
        strategy_id: str = config.ENTRY_SIGNAL_STRATEGY_ID):
    """15m 开仓准确率/因子/极值/Agent 计划统计门；纯只读，不触发训练。"""
    try:
        result = decision_api.entry_accuracy_status(
            _runtime(request).db_path, strategy_id=strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return EntryAccuracyAuditOut(**result)


@router.post("/pause", response_model=ControlOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def pause(request: Request):
    """暂停方向性开仓（止损监控不暂停）。信号扫描循环内部跳过。"""
    _runtime(request).pause()
    return ControlOut(action="pause", paused=True,
                      message="已暂停开仓信号扫描；止损止盈监控继续运行")


@router.post("/resume", response_model=ControlOut, tags=["控制"],
             dependencies=[Depends(require_control)])
def resume(request: Request):
    """恢复方向性开仓信号扫描。"""
    _runtime(request).resume()
    return ControlOut(action="resume", paused=False, message="已恢复开仓信号扫描")


@router.get("/reconcile", response_model=ReconcileOut, tags=["观测"])
def reconcile(request: Request):
    """对账：journal 记账（本地） vs 交易所真实持仓（唯一事实源）。
    legacy 记录 size 是张数，按 ct_val 折算币数后对比；不一致即报告，不静默。"""
    return ReconcileOut(**_runtime(request).reconcile_snapshot())


@router.get("/analysis/latest", response_model=dict, tags=["观测"])
def analysis_latest(request: Request):
    """最近一次看账报告（报告 + 感知到的问题 + 生成的教训 id）。"""
    row = latest_analysis(_runtime(request).db_path)
    if row is None:
        return {"report": None, "issues": [], "message": "尚无分析记录"}
    return row


@router.post("/analysis/daily", response_model=dict, tags=["运维"],
             dependencies=[Depends(require_control)])
def analysis_daily(request: Request):
    """手动触发一次每日看账（分析 + 问题感知 + 教训入经验库 + 飞书反馈）。"""
    return _runtime(request).run_daily_analysis()


@router.get("/risk/events", response_model=List[RiskEventOut], tags=["观测"])
def risk_events(request: Request, limit: int = 20):
    """风控事件复盘记录：熔断/恢复，含触发时净值与持仓数快照。"""
    return [RiskEventOut(**row)
            for row in list_risk_events(_runtime(request).db_path, limit)]


@router.get("/error", response_model=dict, tags=["观测"])
def last_error(request: Request):
    """方向性引擎最近一次异常堆栈（无异常返回空串）。"""
    return {"last_error": _runtime(request).error_snapshot()}


@router.get("/readiness", response_model=dict, tags=["观测"])
def readiness(request: Request):
    """实盘就绪三盏灯(2026-08-20 用户指示)——样本/稳定/反哺,全绿才可上实盘。"""
    return decision_api.live_readiness(_runtime(request).db_path)


@router.get("/combos", response_model=dict, tags=["观测"])
def combos(request: Request, min_samples: int = 3):
    """组合试验统计(2026-08-21 用户洞察'单条不盈利,combo 可能盈利')——
    只观测;达标组合走 experiments 提案,不自动改决策。"""
    return {"combos": decision_api.experience_combo_stats(
        _runtime(request).db_path, min_samples=min_samples)}


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
