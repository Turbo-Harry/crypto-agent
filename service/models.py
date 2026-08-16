"""
HTTP 响应模型 — Pydantic 类型化 schema，自动生成 OpenAPI 文档（/docs）。
AI 读代码时先看这里：每个接口的输入/输出结构一目了然。
"""
from typing import List, Optional
from pydantic import BaseModel


class HealthOut(BaseModel):
    """服务健康状态（两引擎）。"""
    status: str                 # "ok" | "degraded"
    adapter: str                # 交易所适配器名（okx）
    uptime_seconds: float       # 服务进程运行时长
    directional_heartbeat_age: float   # 方向性引擎心跳年龄（>30s 卡死）
    arb_heartbeat_age: float           # 套利引擎心跳年龄（>300s 卡死）
    arb_enabled: bool           # ENABLE_FUNDING_ARB 配置
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


class StatusOut(BaseModel):
    balance: BalanceOut
    positions: List[PositionOut]
    open_trades: List[OpenTradeOut]
    risk_halted: bool
    risk_reason: str
    decision_threshold: float
    today_trade_count: int


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
    status: str
    entry_time: Optional[float]
    exit_time: Optional[float]
    venue: str


class JournalOut(BaseModel):
    total: int
    closed: int
    win_rate: Optional[float]
    trades: List[TradeItem]


class ControlOut(BaseModel):
    action: str
    paused: bool
    message: str


class ArbStatusOut(BaseModel):
    """套利引擎状态。"""
    enabled: bool               # 用户决定：ENABLE_FUNDING_ARB
    positions_ledger: int       # 套利台账持仓数
    risk_halted: bool
    decision_threshold: float
    last_events: List[str]      # 最近信号事件（内存快照）


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
