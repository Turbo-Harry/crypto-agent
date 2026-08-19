"""
HTTP 响应模型 — Pydantic 类型化 schema，自动生成 OpenAPI 文档（/docs）。
AI 读代码时先看这里：每个接口的输入/输出结构一目了然。
"""
from typing import List, Optional
from pydantic import BaseModel


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


class WatchItem(BaseModel):
    base: str
    score: Optional[float]
    budget: int


class WatchlistOut(BaseModel):
    date: str
    items: List[WatchItem]


class SignalOut(BaseModel):
    base: str
    venue: Optional[str]
    signal: Optional[dict]      # {"dir","entry","stop","tp","atr"} 或 None
    message: str


class TradeItem(BaseModel):
    id: str
    symbol: str
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
    review: Optional[dict] = None     # 复盘报告（deep_review 输出，平仓后落盘）


class JournalOut(BaseModel):
    total: int
    closed: int
    win_rate: Optional[float]
    total_pnl_usdt: Optional[float] = None  # 已平仓合计实际盈亏 USDT
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


class ScanOut(BaseModel):
    """每日候选扫描结果。"""
    date: str
    fallback: bool
    candidates: List[dict]      # {base, dir, score, ...}


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
