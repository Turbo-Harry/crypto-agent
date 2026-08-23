"""
接口层 — ExchangeAdapter 抽象基类（ABC）。

定义交易所无关的统一交易语义。策略/应用层只依赖此接口；
新增交易所 = 新增一个实现类，策略代码零改动。

单位约定（与 OKX 原生一致，业务层无需再换算）：
  - 数量 qty：基础币数量（币）。swap 下单时适配器负责 ÷ctVal → 张数对齐 lotSz。
  - 价格：USDT 计价。
  - K线：Candle 列表，升序（最旧→最新）。
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from exchange.models import (Instrument, Candle, TickerInfo, BalanceInfo,
                             PositionInfo, OrderResult)


class ExchangeError(Exception):
    """交易所访问异常（网络/签名/限频/业务拒绝），业务层据此 fail-closed。"""


class ExchangeAdapter(ABC):
    """交易所访问统一接口。所有实现必须保证：失败抛 ExchangeError，
    不带副作用的方法可重复调用，下单类方法返回 OrderResult（不自动抛业务拒绝）。"""

    name = "abstract"

    # ---------- 工具/市场 ----------
    @abstractmethod
    def venue_for(self, base: str, prefer_swap: bool = True) -> Optional[str]:
        """探测该币可交易的场所："swap"（有永续）或 "spot"（仅现货）；都没有 → None。"""

    @abstractmethod
    def instrument(self, inst_id: str) -> Instrument:
        """取工具规格（合约面值/lotSize/最小下单量）。未知 instId 抛 ExchangeError。"""

    # ---------- 行情 ----------
    @abstractmethod
    def fetch_candles(self, inst_id: str, bar: str, limit: int = 100) -> List[Candle]:
        """K线（升序）。bar: 1m/15m/1H/4H/1D。"""

    def fetch_candles_range(self, inst_id: str, bar: str, since_ms: int,
                            until_ms: int, max_bars: int = 1800) -> List[Candle]:
        """闭区间历史 K 线（升序），供反事实标签结算。

        默认实现适配只支持近期窗口的旧替身；原生 OKX/CCXT 会覆盖为分页
        实现。数据不完整由结算器保持 pending，绝不拿当前价伪造路径。
        """
        rows = self.fetch_candles(inst_id, bar, limit=max_bars)
        return [row for row in rows if since_ms <= row.ts <= until_ms]

    @abstractmethod
    def fetch_ticker_last(self, inst_id: str) -> float:
        """最新成交价。"""

    def fetch_order_book(self, inst_id: str, depth: int = 10) -> Optional[dict]:
        """盘口(2026-08-23 信号评分第6维): {"bids":[[价,量]...],"asks":[[价,量]...]}
        或 None(取不到)。非 abstract——旧实现可缺省返回 None(评分取中性)。"""
        return None

    def fetch_open_interest(self, inst_id: str) -> Optional[float]:
        """永续持仓量；实现不支持时返回 None，研究特征显式记缺失。"""
        return None

    def fetch_basis(self, inst_id: str) -> Optional[float]:
        """永续/现货-1；无同名现货时返回 None。"""
        return None

    @abstractmethod
    def fetch_funding_rate(self, inst_id: str) -> float:
        """当前资金费率（每 8 小时，swap）。"""

    @abstractmethod
    def fetch_tickers(self, venue: str = "swap") -> List[TickerInfo]:
        """全市场 24h ticker。venue="swap"|"spot"。
        vol_usdt_24h 已按场归一（策略层不要自己乘 last）。"""

    @abstractmethod
    def new_cl_ord_id(self) -> str:
        """客户端幂等键。超时后按此反查订单；各交易所格式由适配器保证合法。"""

    # ---------- 账户 ----------
    @abstractmethod
    def fetch_balance(self) -> BalanceInfo:
        """账户余额快照（USDT 视角）。"""

    @abstractmethod
    def fetch_positions(self, inst_id: Optional[str] = None) -> List[PositionInfo]:
        """持仓（long/short 模式按方向分腿）。"""

    @abstractmethod
    def set_leverage(self, inst_id: str, lever: int, pos_side: str,
                     mgn_mode: str = "isolated") -> None:
        """设置合约杠杆。失败抛 ExchangeError（业务层可选忽略——仅影响保证金占用）。"""

    @abstractmethod
    def spot_holding(self, base: str) -> float:
        """现货账户某币持有量（用于现货平仓判断）。"""

    # ---------- 下单 ----------
    @abstractmethod
    def place_market_order(self, inst_id: str, side: str, qty: float,
                           venue: str = "swap", pos_side: Optional[str] = None,
                           reduce_only: bool = False,
                           cl_ord_id: Optional[str] = None) -> OrderResult:
        """市价单。qty 为基础币数量；swap 自动换算张数并对齐 lotSz/minSz。
        venue="spot" 忽略 pos_side/reduce_only（现货无此概念）。
        cl_ord_id：客户端幂等键（审计 C1）；超时后用它反查订单真实状态。"""

    @abstractmethod
    def place_conditional_stop(self, inst_id: str, side: str, qty: float,
                               pos_side: str, trigger_px: float,
                               is_tp: bool = False) -> OrderResult:
        """交易所侧条件单（止损/止盈，市场价触发）。返回 OrderResult.algo_id。
        现货不支持 → 返回 ok=False。"""

    @abstractmethod
    def pending_algo_ids(self, inst_id: str) -> List[str]:
        """该工具全部待触发条件单 ID（枚举所有 ordType）。"""

    @abstractmethod
    def cancel_algos(self, inst_id: str, algo_ids: Optional[List[str]] = None) -> bool:
        """撤销条件单。algo_ids=None 时撤该工具全部。返回是否全部成功。"""

    @abstractmethod
    def cancel_all_spot_orders(self, inst_id: str) -> bool:
        """撤销现货挂单（用于下单失败后的清理兜底）。"""

    @abstractmethod
    def fetch_order_avg_px(self, inst_id: str, ord_id: str) -> Optional[float]:
        """按订单号回填成交均价（市价单响应无 avgPx 时用）。"""

    @abstractmethod
    def fetch_order_state(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        """按客户端幂等键反查订单状态（审计 C1）。
        返回 {"state": str, "avg_px": Optional[float], "ord_id": str}；查无此单 → None。"""

    @abstractmethod
    def fetch_bills(self, ccy: str = "USDT", since_ms: int = 0,
                    bill_type: str = "") -> List[dict]:
        """账户账单（资金费/手续费）。返回原始行（含 amount/type）。"""
