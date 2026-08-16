"""
适配层 — OKXAdapter：把 OKX 原生 REST 响应翻译成 ExchangeAdapter 统一语义。

只依赖传输层 OKXTransport；策略层只依赖 base.ExchangeAdapter。
单位换算全部在这里完成：
  - swap 数量：基础币 qty ÷ ctVal → 张数，向下对齐 lotSz，校验 minSz/maxMktSz
  - spot 数量：基础币 qty 对齐 spot 的 lotSz
  - 持仓：张数 × ctVal → base_qty

已知端点要点（沙盘实测沉淀）：
  - /trade/order-algo 条件止损必须用 slTriggerPx/slTriggerPxType/slOrdPx
    （triggerPx 会报 50015）；止盈用 tpTriggerPx 系列。
  - /trade/orders-algo-pending 必须带 ordType（否则 51000）。
  - /public/instruments 的 SWAP 才有 ctVal/lotSz/minSz。
"""
import time
import uuid
from typing import Dict, List, Optional

from exchange.base import ExchangeAdapter, ExchangeError
from exchange.models import (Instrument, Candle, BalanceInfo, PositionInfo,
                             OrderResult, floor_to_lot)
from exchange.transport import OKXTransport

INSTRUMENT_CACHE_TTL = 24 * 3600   # 工具规格缓存 24h
ALGO_ORD_TYPES = ("conditional", "oco", "trigger", "move_order_stop",
                  "iceberg", "twap")


class OKXAdapter(ExchangeAdapter):
    name = "okx"

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 sandbox: bool = True):
        self.t = OKXTransport(api_key, secret, passphrase, sandbox=sandbox)
        self._instruments: Dict[str, Instrument] = {}
        self._inst_ts = 0.0

    # ---------- 工具/市场 ----------
    def _refresh_instruments(self, force: bool = False):
        if self._instruments and not force and time.time() - self._inst_ts < INSTRUMENT_CACHE_TTL:
            return
        for inst_type in ("SPOT", "SWAP"):
            resp = self.t.public("/api/v5/public/instruments",
                                 {"instType": inst_type})
            for row in resp.get("data", []):
                if row.get("state") != "live":
                    continue
                inst_id = row.get("instId", "")
                if not inst_id:
                    continue
                venue = "swap" if inst_id.endswith("-SWAP") else "spot"
                if venue == "swap":
                    base = inst_id[:-len("-USDT-SWAP")] if inst_id.endswith("-USDT-SWAP") else inst_id.split("-")[0]
                else:
                    base = inst_id[:-len("-USDT")] if inst_id.endswith("-USDT") else inst_id.split("-")[0]
                if not inst_id.endswith("-USDT") and not inst_id.endswith("-USDT-SWAP"):
                    continue   # 只收 USDT 计价
                self._instruments[inst_id] = Instrument(
                    inst_id=inst_id, base=base, venue=venue,
                    ct_val=float(row.get("ctVal") or 1),
                    lot_sz=float(row.get("lotSz") or 1e-8),
                    min_sz=float(row.get("minSz") or 0),
                    tick_sz=float(row.get("tickSz") or 0),
                    max_mkt_sz=float(row.get("maxMktSz") or 0),
                    max_lever=float(row.get("lever") or 20))
        self._inst_ts = time.time()

    def venue_for(self, base: str, prefer_swap: bool = True) -> Optional[str]:
        self._refresh_instruments()
        swap_id = f"{base}-USDT-SWAP"
        spot_id = f"{base}-USDT"
        if prefer_swap:
            if swap_id in self._instruments:
                return "swap"
            if spot_id in self._instruments:
                return "spot"
            return None
        if spot_id in self._instruments:
            return "spot"
        if swap_id in self._instruments:
            return "swap"
        return None

    def instrument(self, inst_id: str) -> Instrument:
        self._refresh_instruments()
        if inst_id not in self._instruments:
            self._refresh_instruments(force=True)
        inst = self._instruments.get(inst_id)
        if inst is None:
            raise ExchangeError(f"未知交易对: {inst_id}")
        return inst

    # ---------- 行情 ----------
    def fetch_candles(self, inst_id: str, bar: str, limit: int = 100) -> List[Candle]:
        resp = self.t.public("/api/v5/market/history-candles",
                             {"instId": inst_id, "bar": str(bar).upper(),
                              "limit": limit})
        rows = resp.get("data", [])
        out = []
        for r in reversed(rows):   # OKX 倒序（新→旧）→ 反转为升序
            out.append(Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                              low=float(r[3]), close=float(r[4]), volume=float(r[5])))
        return out

    def fetch_ticker_last(self, inst_id: str) -> float:
        resp = self.t.public("/api/v5/market/ticker", {"instId": inst_id})
        data = resp.get("data") or []
        if not data:
            raise ExchangeError(f"ticker 无数据: {inst_id}")
        return float(data[0]["last"])

    def fetch_funding_rate(self, inst_id: str) -> float:
        resp = self.t.public("/api/v5/public/funding-rate", {"instId": inst_id})
        data = resp.get("data") or []
        if not data:
            raise ExchangeError(f"funding-rate 无数据: {inst_id}")
        return float(data[0].get("fundingRate") or 0)

    # ---------- 账户 ----------
    def fetch_balance(self) -> BalanceInfo:
        resp = self.t.private_get("/api/v5/account/balance")
        data = (resp.get("data") or [{}])[0]
        by_ccy = {}
        for d in data.get("details", []):
            by_ccy[d.get("ccy")] = {
                "free": float(d.get("availBal") or 0),
                "total": float(d.get("cashBal") or d.get("eq") or 0)}
        usdt = by_ccy.get("USDT", {})
        return BalanceInfo(
            total_eq=float(data.get("totalEq") or 0),
            usdt_free=usdt.get("free", 0.0),
            usdt_total=usdt.get("total", 0.0),
            by_ccy=by_ccy)

    def fetch_positions(self, inst_id: Optional[str] = None) -> List[PositionInfo]:
        params = {"instType": "SWAP"}
        if inst_id:
            params = {"instId": inst_id}
        resp = self.t.private_get("/api/v5/account/positions", params)
        out = []
        for p in resp.get("data", []):
            contracts = float(p.get("pos") or 0)
            if contracts == 0:
                continue
            iid = p.get("instId", "")
            try:
                ct_val = self.instrument(iid).ct_val
            except ExchangeError:
                ct_val = 1.0
            out.append(PositionInfo(
                inst_id=iid,
                base=iid[:-len("-USDT-SWAP")] if iid.endswith("-USDT-SWAP") else iid.split("-")[0],
                side=(p.get("posSide") or ("long" if contracts > 0 else "short")),
                contracts=abs(contracts),
                base_qty=abs(contracts) * ct_val,
                avg_px=float(p.get("avgPx") or 0)))
        return out

    def set_leverage(self, inst_id: str, lever: int, pos_side: str,
                     mgn_mode: str = "isolated") -> None:
        self.t.private_post("/api/v5/account/set-leverage",
                            {"instId": inst_id, "lever": str(lever),
                             "mgnMode": mgn_mode, "posSide": pos_side})

    def spot_holding(self, base: str) -> float:
        try:
            resp = self.t.private_get("/api/v5/account/balance", {"ccy": base})
        except ExchangeError:
            return 0.0
        data = (resp.get("data") or [{}])[0]
        for d in data.get("details", []):
            if d.get("ccy") == base:
                return float(d.get("availBal") or 0)
        return 0.0

    # ---------- 下单 ----------
    def _swap_qty_to_contracts(self, inst: Instrument, qty: float) -> float:
        contracts = floor_to_lot(qty / inst.ct_val, inst.lot_sz)
        if inst.min_sz > 0 and contracts < inst.min_sz:
            raise ExchangeError(
                f"{inst.inst_id}: {qty} 币 = {contracts} 张 < 最小 {inst.min_sz} 张")
        if inst.max_mkt_sz > 0 and contracts > inst.max_mkt_sz:
            contracts = floor_to_lot(inst.max_mkt_sz, inst.lot_sz)
        return contracts

    def place_market_order(self, inst_id: str, side: str, qty: float,
                           venue: str = "swap", pos_side: Optional[str] = None,
                           reduce_only: bool = False,
                           cl_ord_id: Optional[str] = None) -> OrderResult:
        inst = self.instrument(inst_id)
        # 审计 C1:客户端幂等键(下单响应丢失/超时后按它反查真实状态)
        cl_ord_id = cl_ord_id or f"ca-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
        if venue == "spot":
            sz = floor_to_lot(qty, inst.lot_sz)
            if inst.min_sz > 0 and sz < inst.min_sz:
                return OrderResult(ok=False, qty=sz,
                                   message=f"{inst_id}: {sz} < 最小下单量 {inst.min_sz}")
            body = {"instId": inst_id, "tdMode": "cash", "side": side,
                    "ordType": "market", "sz": str(sz)}
        else:
            contracts = self._swap_qty_to_contracts(inst, qty)
            body = {"instId": inst_id, "tdMode": "isolated", "side": side,
                    "ordType": "market", "sz": str(contracts),
                    "posSide": pos_side or "long"}
            if reduce_only:
                body["reduceOnly"] = "true"
        body["clOrdId"] = cl_ord_id
        resp = self.t.private_post("/api/v5/trade/order", body)
        row = (resp.get("data") or [{}])[0]
        if row.get("sCode") and row.get("sCode") != "0":
            return OrderResult(ok=False, qty=qty, cl_ord_id=cl_ord_id,
                               message=f"{row.get('sCode')} {row.get('sMsg')}")
        return OrderResult(ok=True, ord_id=str(row.get("ordId") or ""),
                           cl_ord_id=cl_ord_id, qty=qty)

    def place_conditional_stop(self, inst_id: str, side: str, qty: float,
                               pos_side: str, trigger_px: float,
                               is_tp: bool = False) -> OrderResult:
        inst = self.instrument(inst_id)
        if inst.venue != "swap":
            return OrderResult(ok=False, qty=qty, message="现货不支持交易所侧条件单")
        contracts = self._swap_qty_to_contracts(inst, qty)
        body = {"instId": inst_id, "tdMode": "isolated", "side": side,
                "ordType": "conditional", "sz": str(contracts),
                "posSide": pos_side, "reduceOnly": "true"}
        if is_tp:
            body.update({"tpTriggerPx": str(trigger_px), "tpTriggerPxType": "last",
                         "tpOrdPx": "-1"})
        else:
            body.update({"slTriggerPx": str(trigger_px), "slTriggerPxType": "last",
                         "slOrdPx": "-1"})
        resp = self.t.private_post("/api/v5/trade/order-algo", body)
        row = (resp.get("data") or [{}])[0]
        if row.get("sCode") and row.get("sCode") != "0":
            return OrderResult(ok=False, qty=qty,
                               message=f"{row.get('sCode')} {row.get('sMsg')}")
        return OrderResult(ok=True, algo_id=str(row.get("algoId") or ""), qty=qty)

    def pending_algo_ids(self, inst_id: str) -> List[str]:
        ids = []
        for ot in ALGO_ORD_TYPES:
            try:
                resp = self.t.private_get("/api/v5/trade/orders-algo-pending",
                                          {"instId": inst_id, "ordType": ot})
                for r in resp.get("data", []):
                    if r.get("algoId"):
                        ids.append(str(r["algoId"]))
            except ExchangeError:
                continue
        return ids

    def cancel_algos(self, inst_id: str, algo_ids: Optional[List[str]] = None) -> bool:
        ids = algo_ids if algo_ids is not None else self.pending_algo_ids(inst_id)
        if not ids:
            return True
        body = [{"algoId": a, "instId": inst_id} for a in ids]
        resp = self.t.private_post("/api/v5/trade/cancel-algos", body)
        return resp.get("code") == "0"

    def cancel_all_spot_orders(self, inst_id: str) -> bool:
        try:
            self.t.private_post("/api/v5/trade/cancel-order",
                                {"instId": inst_id, "ordType": "market"})
        except ExchangeError:
            return False
        return True

    def fetch_order_avg_px(self, inst_id: str, ord_id: str) -> Optional[float]:
        resp = self.t.private_get("/api/v5/trade/order",
                                  {"instId": inst_id, "ordId": ord_id})
        data = resp.get("data") or []
        if not data:
            return None
        px = data[0].get("avgPx") or data[0].get("fillPx")
        return float(px) if px else None

    def fetch_order_state(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        """按 clOrdId 反查订单状态(审计 C1:超时后判断是否已成交)。"""
        resp = self.t.private_get("/api/v5/trade/order",
                                  {"instId": inst_id, "clOrdId": cl_ord_id})
        data = resp.get("data") or []
        if not data:
            return None
        r = data[0]
        px = r.get("avgPx") or r.get("fillPx")
        return {"state": str(r.get("state") or ""),
                "avg_px": float(px) if px else None,
                "ord_id": str(r.get("ordId") or "")}

    def fetch_bills(self, ccy: str = "USDT", since_ms: int = 0,
                    bill_type: str = "") -> List[dict]:
        params = {"ccy": ccy}
        if bill_type:
            params["type"] = bill_type
        if since_ms:
            params["begin"] = str(int(since_ms))
            params["end"] = str(int(time.time() * 1000))
        resp = self.t.private_get("/api/v5/account/bills", params)
        return resp.get("data", [])
