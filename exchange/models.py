"""
领域模型 — 交易所无关的数据结构（dataclass）。

策略层只看到这些对象，永远不接触 HTTP 响应原文。
字段均为 float/bool/str 等标量，便于日志、记账与断言。
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Instrument:
    """交易工具规格（现货或永续合约）。"""
    inst_id: str            # 交易所原生 ID：BTC-USDT / ETH-USDT-SWAP
    base: str               # 基础币：BTC / ANTHROPIC
    venue: str              # "spot" | "swap"
    ct_val: float = 1.0     # 合约面值（swap 才 >0；spot 恒 1）
    lot_sz: float = 1e-8    # 下单步长（swap 单位为张，spot 单位为币）
    min_sz: float = 0.0     # 最小下单量（swap 最小张数，spot 最小币数）
    tick_sz: float = 0.0    # 价格步长
    max_mkt_sz: float = 0.0 # 市价单最大下单量（swap 张数；0=无限制）
    max_lever: float = 20.0 # 合约最大可用杠杆（spot 无意义）

    @property
    def amount_precision(self) -> int:
        """下单数量的小数位数（从 lot_sz 推导）。0.01→2, 1e-8→8, 1→0。"""
        if self.lot_sz >= 1:
            return 0
        s = f"{self.lot_sz}".rstrip("0")
        if "e-" in s:
            return int(s.split("e-")[1])
        return len(s.split(".")[1]) if "." in s else 0


@dataclass
class Candle:
    """一根 K 线（时间升序语义）。"""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float           # 成交量（币）


@dataclass
class BalanceInfo:
    """账户余额快照。"""
    total_eq: float = 0.0   # 账户总权益（USDT 计价）
    usdt_free: float = 0.0  # USDT 可用余额（可下单）
    usdt_total: float = 0.0 # USDT 总余额
    by_ccy: dict = field(default_factory=dict)  # ccy -> {"free":…, "total":…}


@dataclass
class PositionInfo:
    """一腿持仓（long/short 模式下按方向分开）。"""
    inst_id: str
    base: str
    side: str               # "long" | "short"
    contracts: float = 0.0  # 张数（spot 无此概念，恒 0）
    base_qty: float = 0.0   # 基础币数量（contracts × ct_val）
    avg_px: float = 0.0     # 开仓均价


@dataclass
class OrderResult:
    """下单结果。ok=False 时 message 说明原因（业务层据此拒绝/回滚）。"""
    ok: bool
    ord_id: str = ""
    algo_id: str = ""
    qty: float = 0.0        # 实际提交的数量（基础币单位，精度对齐后）
    message: str = ""


def lot_decimals(lot: float) -> int:
    """lot 步长的小数位数。0.01→2, 0.0001→4, 1e-8→8, ≥1→0。"""
    if lot is None or lot >= 1:
        return 0
    s = f"{lot}".rstrip("0")
    if "e-" in s:
        return int(s.split("e-")[1])
    return len(s.split(".")[1]) if "." in s else 0


def floor_to_lot(qty: float, lot: float) -> float:
    """向下对齐到 lot 的整数倍（字符串整数运算避免浮点误差）。
    用 int() 截断（非四舍五入）保证只向下、不超发。"""
    if lot <= 0 or qty <= 0:
        return qty
    dec = lot_decimals(lot)
    q = int(qty * (10 ** dec))
    l = int(lot * (10 ** dec))
    if l == 0:
        return qty
    return (q // l * l) / (10 ** dec)
