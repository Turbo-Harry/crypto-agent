"""
测试替身 — FakeAdapter：内存版交易所，实现 ExchangeAdapter 全部接口。

用途：策略层单元测试 / 沙盘干跑，无需网络、无需真实资金。
行为可编程：fill_prices 决定成交价，orders/algo 记录全部动作可断言。
"""
import itertools
from typing import Dict, List, Optional

from exchange.base import ExchangeAdapter, ExchangeError
from exchange.models import (Instrument, Candle, TickerInfo, BalanceInfo,
                             PositionInfo, OrderResult)


class FakeAdapter(ExchangeAdapter):
    name = "fake"

    def __init__(self, usdt_free: float = 10_000.0,
                 instruments: Dict[str, Instrument] = None):
        self.usdt_free = usdt_free
        self._instruments = instruments or {
            "BTC-USDT-SWAP": Instrument("BTC-USDT-SWAP", "BTC", "swap",
                                        ct_val=0.01, lot_sz=1, min_sz=1),
            "BTC-USDT": Instrument("BTC-USDT", "BTC", "spot", lot_sz=1e-6, min_sz=1e-6),
            "ANTHROPIC-USDT-SWAP": Instrument("ANTHROPIC-USDT-SWAP", "ANTHROPIC",
                                              "swap", ct_val=1, lot_sz=1, min_sz=1),
        }
        self.last_prices: Dict[str, float] = {"BTC-USDT-SWAP": 63000.0,
                                              "BTC-USDT": 63000.0,
                                              "ANTHROPIC-USDT-SWAP": 180.0}
        self.funding_rates: Dict[str, float] = {}
        self.candles: Dict[str, List[Candle]] = {}
        self.positions: List[PositionInfo] = []
        self.orders: List[dict] = []          # 记录全部市价单动作
        self.algos: List[dict] = []           # 记录全部条件单动作
        self.spot_holdings: Dict[str, float] = {}
        self.bills: List[dict] = []
        self._ord_seq = itertools.count(1)
        # 测试可灌 24h 成交额；默认 0 → daily_scan 阶段1 全灭走回退池（离线安全）
        self.ticker_vol_usdt: Dict[str, float] = {}

    # ---------- 工具/市场 ----------
    def venue_for(self, base: str, prefer_swap: bool = True) -> Optional[str]:
        swap_id, spot_id = f"{base}-USDT-SWAP", f"{base}-USDT"
        if prefer_swap:
            return "swap" if swap_id in self._instruments else (
                "spot" if spot_id in self._instruments else None)
        return "spot" if spot_id in self._instruments else (
            "swap" if swap_id in self._instruments else None)

    def instrument(self, inst_id: str) -> Instrument:
        if inst_id not in self._instruments:
            raise ExchangeError(f"未知交易对: {inst_id}")
        return self._instruments[inst_id]

    # ---------- 行情 ----------
    def fetch_candles(self, inst_id: str, bar: str, limit: int = 100) -> List[Candle]:
        return self.candles.get(inst_id, [])[-limit:]

    def fetch_ticker_last(self, inst_id: str) -> float:
        if inst_id not in self.last_prices:
            raise ExchangeError(f"无价格: {inst_id}")
        return self.last_prices[inst_id]

    def fetch_funding_rate(self, inst_id: str) -> float:
        return self.funding_rates.get(inst_id, 0.0)

    def fetch_tickers(self, venue: str = "swap") -> List[TickerInfo]:
        out = []
        for inst in self._instruments.values():
            if inst.venue != venue:
                continue
            last = self.last_prices.get(inst.inst_id, 0.0)
            vol = self.ticker_vol_usdt.get(inst.inst_id, 0.0)
            out.append(TickerInfo(inst_id=inst.inst_id, base=inst.base,
                                  last=last, vol_ccy_24h=vol, vol_usdt_24h=vol))
        return out

    def new_cl_ord_id(self) -> str:
        return f"f{next(self._ord_seq)}"

    # ---------- 账户 ----------
    def fetch_balance(self) -> BalanceInfo:
        return BalanceInfo(total_eq=self.usdt_free, usdt_free=self.usdt_free,
                           usdt_total=self.usdt_free)

    def fetch_positions(self, inst_id: Optional[str] = None) -> List[PositionInfo]:
        if inst_id:
            return [p for p in self.positions if p.inst_id == inst_id]
        return list(self.positions)

    def set_leverage(self, inst_id: str, lever: int, pos_side: str,
                     mgn_mode: str = "isolated") -> None:
        pass

    def spot_holding(self, base: str) -> float:
        return self.spot_holdings.get(base, 0.0)

    # ---------- 下单 ----------
    def _fill_price(self, inst_id: str) -> float:
        return self.last_prices.get(inst_id) or self.fetch_ticker_last(inst_id)

    def place_market_order(self, inst_id: str, side: str, qty: float,
                           venue: str = "swap", pos_side: Optional[str] = None,
                           reduce_only: bool = False,
                           cl_ord_id: Optional[str] = None,
                           td_mode: Optional[str] = None) -> OrderResult:
        ord_id = f"f{next(self._ord_seq)}"
        px = self._fill_price(inst_id)
        self.orders.append({"ord_id": ord_id, "inst_id": inst_id, "side": side,
                            "qty": qty, "venue": venue, "pos_side": pos_side,
                            "reduce_only": reduce_only, "cl_ord_id": cl_ord_id,
                            "td_mode": td_mode, "price": px})
        if venue == "spot":
            if side == "buy":
                cost = qty * px
                if cost > self.usdt_free:
                    return OrderResult(ok=False, ord_id=ord_id, qty=qty,
                                       message="USDT 余额不足")
                self.usdt_free -= cost
                self.spot_holdings[inst_id.split("-")[0]] = (
                    self.spot_holdings.get(inst_id.split("-")[0], 0) + qty)
            else:
                base = inst_id.split("-")[0]
                held = self.spot_holdings.get(base, 0)
                if qty > held:
                    return OrderResult(ok=False, ord_id=ord_id, qty=qty,
                                       message="现货持仓不足")
                self.spot_holdings[base] = held - qty
                self.usdt_free += qty * px
            return OrderResult(ok=True, ord_id=ord_id, qty=qty)
        # swap：先做最小下单量校验（镜像真实适配器语义）
        inst = self._instruments[inst_id]
        contracts = qty / inst.ct_val
        if inst.min_sz > 0 and contracts < inst.min_sz:
            raise ExchangeError(
                f"{inst_id}: {qty} 币 = {contracts} 张 < 最小 {inst.min_sz} 张")
        # swap：找/建持仓腿
        pos = next((p for p in self.positions
                    if p.inst_id == inst_id and p.side == (pos_side or "long")), None)
        if reduce_only:
            if pos is None:
                return OrderResult(ok=False, ord_id=ord_id, qty=qty,
                                   message="无持仓可平")
            close_qty = min(qty, pos.base_qty)
            pos.base_qty -= close_qty
            pos.contracts = pos.base_qty / self._instruments[inst_id].ct_val
            if pos.base_qty <= 1e-12:
                self.positions.remove(pos)
            return OrderResult(ok=True, ord_id=ord_id, qty=close_qty)
        if pos is None:
            pos = PositionInfo(inst_id=inst_id, base=inst_id.split("-")[0],
                               side=pos_side or "long")
            self.positions.append(pos)
        pos.base_qty += qty
        pos.contracts = pos.base_qty / self._instruments[inst_id].ct_val
        pos.avg_px = px
        return OrderResult(ok=True, ord_id=ord_id, qty=qty)

    def place_conditional_stop(self, inst_id: str, side: str, qty: float,
                               pos_side: str, trigger_px: float,
                               is_tp: bool = False) -> OrderResult:
        algo_id = f"a{next(self._ord_seq)}"
        self.algos.append({"algo_id": algo_id, "inst_id": inst_id, "side": side,
                           "qty": qty, "pos_side": pos_side,
                           "trigger_px": trigger_px, "is_tp": is_tp})
        return OrderResult(ok=True, algo_id=algo_id, qty=qty)

    def pending_algo_ids(self, inst_id: str) -> List[str]:
        return [a["algo_id"] for a in self.algos if a["inst_id"] == inst_id]

    def cancel_algos(self, inst_id: str, algo_ids: Optional[List[str]] = None) -> bool:
        ids = set(algo_ids) if algo_ids is not None else None
        self.algos = [a for a in self.algos
                      if not (a["inst_id"] == inst_id
                              and (ids is None or a["algo_id"] in ids))]
        return True

    def cancel_all_spot_orders(self, inst_id: str) -> bool:
        return True

    def fetch_order_avg_px(self, inst_id: str, ord_id: str) -> Optional[float]:
        return self.last_prices.get(inst_id)

    def fetch_order_state(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        # 内存替身:按 clOrdId 反查(测试可预置 orders 模拟"超时已成交")
        for o in self.orders:
            if o.get("cl_ord_id") == cl_ord_id and o.get("inst_id") == inst_id:
                return {"state": "filled", "avg_px": o.get("price"),
                        "ord_id": o.get("ord_id")}
        return None

    def fetch_bills(self, ccy: str = "USDT", since_ms: int = 0,
                    bill_type: str = "") -> List[dict]:
        return list(self.bills)
