"""
HTTP 响应模型 — Pydantic 类型化 schema，自动生成 OpenAPI 文档（/docs）。
AI 读代码时先看这里：每个接口的输入/输出结构一目了然。
"""
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """服务健康状态（方向性引擎）。"""
    status: str                 # "ok" | "degraded"
    adapter: str                # 交易所适配器名（okx）
    uptime_seconds: float       # 服务进程运行时长
    directional_heartbeat_age: float   # 方向性引擎心跳年龄（>30s 卡死）
    paused: bool                # 方向性开仓是否暂停


class BalanceOut(BaseModel):
    total_equity: float
    usdt_free: float
    usdt_total: float


class PositionOut(BaseModel):
    inst_id: str
    side: str
    contracts: float
    base_qty: float
    avg_px: float


class OpenTradeOut(BaseModel):
    id: str
    symbol: str
    direction: str
    size: float
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    venue: str
    notional_usdt: Optional[float]    # 投注额（名义 USDT）
    risk_usdt: Optional[float]        # 止损风险额（USDT）


class StatusOut(BaseModel):
    balance: BalanceOut
    positions: List[PositionOut]
    open_trades: List[OpenTradeOut]
    risk_halted: bool
    risk_reason: str
    decision_threshold: float
    today_trade_count: int
    # 投注统计（显式字段，不靠调用方反推）
    total_notional_usdt: float        # 累计投注额（全部交易）
    open_notional_usdt: float         # 当前未平仓投注额
    today_notional_usdt: float        # 今日投注额
    # 实盘盈亏（2026-08-23 用户指示"重新开始计盈亏"——从实盘基线起算）
    live_realized_pnl_usdt: Optional[float] = None   # 实盘已平仓累计盈亏 USDT
    live_equity_pnl_usdt: Optional[float] = None     # 账户净值 − 实盘基线净值
    live_pnl_start_equity: Optional[float] = None    # 实盘基线净值（计盈亏起点）
    # 连亏冷却(2026-08-23 用户指示"连亏 6 笔后应主动冷却,不硬接信号")
    loss_cooling: bool = False               # 冷却中?
    loss_cooling_remaining_hours: float = 0.0   # 剩余冷却时长(小时)
    loss_streak: int = 0                     # 当前连续亏损笔数


class WatchItem(BaseModel):
    base: str
    score: Optional[float]
    budget: int
    pool: str                  # crypto | stock


class WatchlistOut(BaseModel):
    date: str
    crypto_items: List[WatchItem]
    stock_items: List[WatchItem]
    items: List[WatchItem]     # 兼容字段：两个独立池的并集


class SignalOut(BaseModel):
    base: str
    venue: Optional[str]
    signal: Optional[dict]      # {"dir","entry","stop","tp","atr"} 或 None
    message: str


class TradeItem(BaseModel):
    id: str
    symbol: str
    strategy_id: str = "A_pullback"
    direction: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    pnl_pct: Optional[float]
    pnl_usdt: Optional[float] = None   # 实际盈亏 USDT（比例 × 名义）
    status: str
    entry_time: Optional[float]
    exit_time: Optional[float]
    venue: str
    notional_usdt: Optional[float]    # 投注额（名义 USDT）
    strategy_timeframe: Optional[str] = None
    max_hold_hours: Optional[float] = None
    review: Optional[dict] = None     # 复盘报告（deep_review 输出，平仓后落盘）


class JournalOut(BaseModel):
    total: int
    closed: int
    win_rate: Optional[float]
    total_pnl_usdt: Optional[float] = None  # 已平仓合计实际盈亏 USDT（历史全量）
    live_total_pnl_usdt: Optional[float] = None  # 实盘已平仓合计盈亏 USDT（venue=live）
    trades: List[TradeItem]


class ControlOut(BaseModel):
    action: str
    paused: bool
    message: str


class RealtimeOut(BaseModel):
    """某币实时行情快照（WebSocket 数据）。"""
    base: str
    price: Optional[float]
    swap_price: Optional[float]
    funding: Optional[float]
    vol_15m: Optional[float]
    fresh: bool                 # 数据是否新鲜（<60s）
    orderflow_status: str       # missing/insufficient/stale/ready
    ofi_event_multilevel: Optional[float]
    ofi_event_cancel_imbalance: Optional[float]
    ofi_event_count: int
    ofi_event_age_ms: Optional[float]


class ScanOut(BaseModel):
    """每日候选扫描结果。"""
    date: str
    fallback: bool
    candidates: List[dict]      # {base, dir, score, ...}
    crypto_candidates: List[dict]
    stock_candidates: List[dict]


class RiskEventOut(BaseModel):
    """风控事件（熔断/恢复）复盘记录。"""
    id: int
    ts: float
    kind: str
    reason: str
    equity: float
    open_trades: int


class ReconcileOut(BaseModel):
    """journal 记账 vs 交易所真实持仓 对账结果。"""
    snapshot_ts: Optional[float]     # 本地仓位快照时间（None=尚无快照）
    journal_open: List[dict]         # journal 未平仓（含折算币数与投注额）
    exchange_positions: List[dict]   # 交易所实时持仓
    per_symbol: List[dict]           # {symbol, journal_base, exchange_base, diff}
    balanced: bool                   # 全部一致？
    notes: List[str]                 # 差异说明（如 legacy 单位换算）


class ScanEvolveOut(BaseModel):
    """扫描尺子进化状态（只动影线比；影子验证通过后须人工批准）。"""
    enabled: bool
    incumbent_wick: float
    effective_wick: float
    candidate_wick: Optional[float] = None
    change_id: Optional[str] = None
    status: Optional[str] = None
    evidence: str = ""
    shadow_open: int = 0
    shadow_settled: int = 0
    settled_mean_pnl: Optional[float] = None
    needs_approval: bool = False
    message: str = ""


class AgentStatusOut(BaseModel):
    """Agent Harness runtime health; observation only."""
    current_version: Optional[str] = None
    current_status: Optional[str] = None
    total_runs: int
    completed_runs: int
    failed_runs: int
    failure_rate: float
    shadow_enabled: bool
    veto_enabled: bool


class AgentRunsOut(BaseModel):
    runs: List[dict]


class AgentProposalsOut(BaseModel):
    """AI 主动候选的 shadow 审计；永远不含执行权限。"""
    shadow_only: bool
    execution_authority: bool
    strategy_id: str
    run_count: int
    proposal_count: int
    mature_count: int
    runs: List[dict]
    proposals: List[dict]


class AgentEvaluationOut(BaseModel):
    samples: int
    reject_samples: int
    saved_loss: float
    missed_profit: float
    incremental_ev: float
    mature_samples: int
    pending_samples: int
    status: str = "insufficient_data"
    valid_n: int = 0
    reject_n: int = 0
    blocked_loss_precision: Optional[float] = None
    opportunity_cost_r: Optional[float] = None
    avoided_loss_r: Optional[float] = None
    incremental_ev_r: Optional[float] = None
    baseline_ev_r: Optional[float] = None
    agent_policy_ev_r: Optional[float] = None
    call_status_counts: Dict = Field(default_factory=dict)
    stability: Dict = Field(default_factory=dict)
    harness: Dict = Field(default_factory=dict)


class ModelItemOut(BaseModel):
    model_id: str
    model_type: str
    strategy_id: str = "A_pullback"
    direction: Optional[str] = None
    version: str
    state: str
    created_at: float
    training_cutoff: Optional[float] = None
    data_hash: Optional[str] = None
    feature_names: List[str] = Field(default_factory=list)
    metrics: Dict = Field(default_factory=dict)
    parent_id: Optional[str] = None
    activated_at: Optional[float] = None
    history: List[Dict] = Field(default_factory=list)


class EntryModelsOut(BaseModel):
    models: List[ModelItemOut]
    budget_expansion_allowed: bool


class ForecastCalibrationOut(BaseModel):
    n: int
    status: str
    brier_tp: Optional[float] = None
    brier_sl: Optional[float] = None
    brier_multiclass: Optional[float] = None
    buckets: Dict = Field(default_factory=dict)
    extrema: Dict = Field(default_factory=dict)  # 分位 pinball/coverage/状态


class FactorTrialOut(BaseModel):
    id: int
    ts: float
    name: str
    strategy_id: str = "A_pullback"
    status: str
    n_samples: Optional[int] = None
    n_folds: Optional[int] = None
    ic_tstat: Optional[float] = None
    net_spread: Optional[float] = None
    dsr: Optional[float] = None
    pbo: Optional[float] = None
    missing_rate: Optional[float] = None
    fold_consistency: Optional[int] = None
    redundant_with: Optional[str] = None


class EntryAccuracyAuditOut(BaseModel):
    """15m 开仓准确率计划的统计完成度；只读且历史研究不冒充 paper。"""
    generated_ts: float
    db_path: str
    scope: Dict
    counts: Dict
    model_states: Dict
    agent_version: Optional[Dict] = None
    budget: Dict
    gates: Dict
    blockers: List[str]
    statistically_complete: bool
    research_only_samples_do_not_count_as_paper: bool


class FactorTrialsOut(BaseModel):
    trials: List[FactorTrialOut]
