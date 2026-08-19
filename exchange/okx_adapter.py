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
from exchange.models import (Instrument, Candle, TickerInfo, BalanceInfo,
                             PositionInfo, OrderResult, floor_to_lot)
from exchange.transport import OKXTransport

INSTRUMENT_CACHE_TTL = 24 * 3600   # 工具规格缓存 24h
ALGO_ORD_TYPES = ("conditional", "oco", "trigger", "move_order_stop",
                  "iceberg", "twap")


def make_cl_ord_id() -> str:
    """统一幂等键生成器(2026-08-17 根因修复收尾):
    OKX clOrdId 只允许字母数字,禁止连字符——旧格式 "ca-...-..." 触发
    51000 Parameter clOrdId error。此前引擎层自拼连字符格式并显式传入,
    覆盖适配器已修好的默认生成器,导致修复后所有开仓仍全灭(KAITO 复现)。
    全项目只允许这一处生成 clOrdId(字面量单点原则)。"""
    return f"ca{int(time.time()*1000)}{uuid.uuid4().hex[:8]}"


# 51121 自愈最多粗化次数(0.001→0.01→0.1→1 张;见 __init__._lot_eff 注释)
LOT_COARSEN_MAX = 3


class OKXAdapter(ExchangeAdapter):
    name = "okx"

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 sandbox: bool = True):
        self.t = OKXTransport(api_key, secret, passphrase, sandbox=sandbox)
        self._instruments: Dict[str, Instrument] = {}
        self._inst_ts = 0.0
        # 2026-08-20 51121 自愈: 沙盘元数据 lotSz 可能比真实撮合粒度细
        # (ANTHROPIC 实测 meta 0.001/真实 0.01)。撞 51121 时按 ×10 粗化重试,
        # 学到的有效粒度按 instId 缓存,止损/平仓单沿用同一粒度。
        self._lot_eff: Dict[str, float] = {}

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
        # 2026-08-17 双根因修复:
        #  1) bar 大小写敏感——.upper() 把 "1m" 变成 "1M"(月线),导致返回的是
        #     最近 N 个月的月K(最新一根=当月1日),平仓特征 MFE/MAE 窗口零根分钟K;
        #  2) 近期行情用 market/candles(新→旧,limit 上限 300);history-candles
        #     分页回溯仍留给 data/fetch_okx.py（研究/回测，非交易路径）。
        limit = min(int(limit), 300)
        resp = self.t.public("/api/v5/market/candles",
                             {"instId": inst_id, "bar": str(bar),
                              "limit": limit})
        rows = resp.get("data", [])
        out = []
        for r in reversed(rows):   # OKX 倒序（新→旧）→ 反转为升序
            # 2026-08-20 防御解析: OKX 偶发返回空字符串字段(新上市/退市边缘
            # 合约)→ float('') 崩溃(03:28 事故,引擎 tick 中断)。坏行直接跳过。
            try:
                out.append(Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                                  low=float(r[3]), close=float(r[4]),
                                  volume=float(r[5] or 0)))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def fetch_ticker_last(self, inst_id: str) -> float:
        resp = self.t.public("/api/v5/market/ticker", {"instId": inst_id})
        data = resp.get("data") or []
        if not data:
            raise ExchangeError(f"ticker 无数据: {inst_id}")
        # 2026-08-20 防御解析: 低活跃标的偶发 last=""(AAVE 案例,每 15 分钟
        # 扫描必崩 float('')),空值按无数据抛错——上层 _ticker_last 捕获后
        # 跳过该币,不打断整轮扫描。
        last = data[0].get("last")
        if not last:
            raise ExchangeError(f"ticker last 为空: {inst_id}")
        try:
            return float(last)
        except (TypeError, ValueError):
            raise ExchangeError(f"ticker last 非法: {inst_id}")

    def fetch_funding_rate(self, inst_id: str) -> float:
        resp = self.t.public("/api/v5/public/funding-rate", {"instId": inst_id})
        data = resp.get("data") or []
        if not data:
            raise ExchangeError(f"funding-rate 无数据: {inst_id}")
        return float(data[0].get("fundingRate") or 0)

    def fetch_tickers(self, venue: str = "swap") -> List[TickerInfo]:
        """全市场 ticker。SWAP volCcy24h 是币本位 → × last 才是 USDT
        （pitfalls 2026-08-20 ANTHROPIC 每天被误杀的根因，归一必须在适配层）。"""
        inst_type = "SWAP" if venue == "swap" else "SPOT"
        suffix = "-USDT-SWAP" if venue == "swap" else "-USDT"
        resp = self.t.public("/api/v5/market/tickers", {"instType": inst_type})
        out = []
        for t in resp.get("data", []):
            inst_id = t.get("instId") or ""
            if not inst_id.endswith(suffix):
                continue
            try:
                last = float(t.get("last") or 0)
                vol_ccy = float(t.get("volCcy24h") or 0)
            except (TypeError, ValueError):
                continue
            vol_usdt = vol_ccy * last if venue == "swap" else vol_ccy
            base = inst_id[:-len(suffix)]
            out.append(TickerInfo(inst_id=inst_id, base=base, last=last,
                                  vol_ccy_24h=vol_ccy, vol_usdt_24h=vol_usdt))
        return out

    def new_cl_ord_id(self) -> str:
        return make_cl_ord_id()

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
    def _effective_lot(self, inst: Instrument) -> float:
        """有效撮合粒度(张): 51121 自愈学到的值优先,否则用元数据 lotSz。"""
        return self._lot_eff.get(inst.inst_id, inst.lot_sz)

    def _coarsen_lot(self, inst: Instrument) -> bool:
        """51121 后把有效粒度 ×10(最多到 max(minSz,1)×100 保险丝)。"""
        new = self._effective_lot(inst) * 10
        if new > max(inst.min_sz, 1.0) * 100:
            return False
        self._lot_eff[inst.inst_id] = new
        print(f"  ⚙️ {inst.inst_id}: 51121 → 撮合粒度粗化为 {new} 张(元数据 lotSz 不可信)")
        return True

    def _swap_qty_to_contracts(self, inst: Instrument, qty: float) -> float:
        contracts = floor_to_lot(qty / inst.ct_val, self._effective_lot(inst))
        if inst.min_sz > 0 and contracts < inst.min_sz:
            raise ExchangeError(
                f"{inst.inst_id}: {qty} 币 = {contracts} 张 < 最小 {inst.min_sz} 张")
        if inst.max_mkt_sz > 0 and contracts > inst.max_mkt_sz:
            contracts = floor_to_lot(inst.max_mkt_sz, self._effective_lot(inst))
        return contracts

    def place_market_order(self, inst_id: str, side: str, qty: float,
                           venue: str = "swap", pos_side: Optional[str] = None,
                           reduce_only: bool = False,
                           cl_ord_id: Optional[str] = None) -> OrderResult:
        inst = self.instrument(inst_id)
        # 审计 C1:客户端幂等键(下单响应丢失/超时后按它反查真实状态)
        # 2026-08-17 根因修复: OKX clOrdId 只允许字母数字,禁止连字符——
        # 旧格式 "ca-...-..." 触发 51000 Parameter clOrdId error,被沙盘
        # 通用 code=1 "All operations failed" 掩盖,导致所有新订单失败。
        cl_ord_id = cl_ord_id or make_cl_ord_id()
        if venue == "spot":
            sz = floor_to_lot(qty, inst.lot_sz)
            if inst.min_sz > 0 and sz < inst.min_sz:
                return OrderResult(ok=False, qty=sz,
                                   message=f"{inst_id}: {sz} < 最小下单量 {inst.min_sz}")
            body = {"instId": inst_id, "tdMode": "cash", "side": side,
                    "ordType": "market", "sz": str(sz), "clOrdId": cl_ord_id}
            resp = self.t.private_post("/api/v5/trade/order", body)
            row = (resp.get("data") or [{}])[0]
            if row.get("sCode") and row.get("sCode") != "0":
                return OrderResult(ok=False, qty=qty, cl_ord_id=cl_ord_id,
                                   message=f"{row.get('sCode')} {row.get('sMsg')}")
            return OrderResult(ok=True, ord_id=str(row.get("ordId") or ""),
                               cl_ord_id=cl_ord_id, qty=qty)
        # ===== 合约: 51121 自愈重试(2026-08-20) =====
        # 51121 是干净的业务拒绝(未成交),粗化粒度换新 clOrdId 重试无重复成交风险;
        # 重试全灭则按原 clOrdId 抛出(引擎 _recover_order 反查语义不变)。
        attempt_cl = cl_ord_id
        for _ in range(1 + LOT_COARSEN_MAX):
            contracts = self._swap_qty_to_contracts(inst, qty)
            # 2026-08-19 根因修复: 该模拟盘账户所有持仓都在 cross 模式,
            # isolated 下单对 cross 持仓 reduce-only 报 51169'无仓位可减'——
            # ETH 突破止盈后平仓单连续 7 次失败即此因(实测 cross 同单 sCode=0)。
            body = {"instId": inst_id, "tdMode": "cross", "side": side,
                    "ordType": "market", "sz": str(contracts),
                    "posSide": pos_side or "long", "clOrdId": attempt_cl}
            if reduce_only:
                body["reduceOnly"] = "true"
            try:
                resp = self.t.private_post("/api/v5/trade/order", body)
            except ExchangeError as e:
                if "51121" in str(e) and self._coarsen_lot(inst):
                    attempt_cl = make_cl_ord_id()
                    continue
                raise
            row = (resp.get("data") or [{}])[0]
            s_code = row.get("sCode")
            if s_code == "51121" and self._coarsen_lot(inst):
                attempt_cl = make_cl_ord_id()
                continue
            if s_code and s_code != "0":
                return OrderResult(ok=False, qty=qty, cl_ord_id=attempt_cl,
                                   message=f"{s_code} {row.get('sMsg')}")
            return OrderResult(ok=True, ord_id=str(row.get("ordId") or ""),
                               cl_ord_id=attempt_cl, qty=qty)
        return OrderResult(ok=False, qty=qty, cl_ord_id=attempt_cl,
                           message="51121 粒度粗化重试用尽仍被拒")

    def place_conditional_stop(self, inst_id: str, side: str, qty: float,
                               pos_side: str, trigger_px: float,
                               is_tp: bool = False) -> OrderResult:
        inst = self.instrument(inst_id)
        if inst.venue != "swap":
            return OrderResult(ok=False, qty=qty, message="现货不支持交易所侧条件单")
        # 51121 自愈(2026-08-20): 与市价单同一有效粒度缓存,通常开仓已学到,
        # 此处兜底(条件单先于市价单撞 51121 的场景)。
        resp = None
        for _ in range(1 + LOT_COARSEN_MAX):
            contracts = self._swap_qty_to_contracts(inst, qty)
            body = {"instId": inst_id, "tdMode": "cross", "side": side,
                    "ordType": "conditional", "sz": str(contracts),
                    "posSide": pos_side, "reduceOnly": "true"}
            if is_tp:
                body.update({"tpTriggerPx": str(trigger_px), "tpTriggerPxType": "last",
                             "tpOrdPx": "-1"})
            else:
                body.update({"slTriggerPx": str(trigger_px), "slTriggerPxType": "last",
                             "slOrdPx": "-1"})
            try:
                resp = self.t.private_post("/api/v5/trade/order-algo", body)
                break
            except ExchangeError as e:
                if "51121" in str(e) and self._coarsen_lot(inst):
                    continue
                raise
        if resp is None:
            return OrderResult(ok=False, qty=qty, message="51121 粒度粗化重试用尽仍被拒")
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
