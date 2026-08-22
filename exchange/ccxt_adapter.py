# -*- coding: utf-8 -*-
"""
CCXT 适配器（2026-08-22 用户指示"用 ccxt 那个交易库"）——
用仓库内 vendored 的 ccxt 实现 ExchangeAdapter 全接口,替代手写 OKX REST
传输层。行为对齐原 okx_adapter 的关键经验(全部在注释中标注出处):

  - clOrdId 纯字母数字(G1)
  - tdMode 按仓位自身 mgnMode(混模仓位 51169,G15)
  - 业务拒绝(sCode!=0)→ OrderResult(ok=False);网络/传输异常→抛 ExchangeError
    (引擎 _recover_order 反查语义不变)
  - 条件单 slTriggerPx/tpTriggerPx + slOrdPx/tpOrdPx=-1(市场触发)
  - 空值防御(坏 K 线跳过、ticker 空值抛错)
  - 标准错误码(2026-08-22 用户要求'同步 ccxt 标准错误码'): 用 ccxt
    标准异常层级分类——网络类(NetworkError/RequestTimeout/…)→ 抛
    ExchangeError 走重试反查;业务类(InvalidOrder/InsufficientFunds/
    OrderNotFound/… )→ OrderResult(ok=False),消息保留 OKX 原始
    sCode(引擎的黑名单 51001/51087/51155、51169 竞态、51279 预达成
    等字符串匹配语义不变)

沙盘验证过的原语: create_order(market/conditional)、fetch_open_orders(ordType
passthrough)、cancel_order(trigger)、fetch_positions(mgnMode)、fetch_balance。
"""
import time
from typing import List, Optional

from exchange.base import ExchangeAdapter, ExchangeError
from exchange.models import (BalanceInfo, Candle, Instrument, OrderResult,
                             PositionInfo, TickerInfo, floor_to_lot)


def _classify(e):
    """按 ccxt 标准异常层级分类(2026-08-22 用户要求'同步标准错误码'):
    返回 (kind, class_name)。kind: 'network' → 抛 ExchangeError 走重试/
    反查;'business' → OrderResult(ok=False)。消息保留 OKX 原始 sCode,
    引擎层字符串匹配(黑名单/51169/51279)语义不变。"""
    import ccxt
    if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout,
                      ccxt.DDoSProtection, ccxt.ExchangeNotAvailable,
                      ccxt.OnMaintenance)):
        return "network", type(e).__name__
    for cls in (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.OrderNotFound,
                ccxt.BadSymbol, ccxt.PermissionDenied, ccxt.DuplicateOrderId,
                ccxt.OrderImmediatelyFillable, ccxt.OrderNotFillable,
                ccxt.ContractUnavailable, ccxt.InvalidAddress,
                ccxt.ArgumentsRequired, ccxt.NotSupported, ccxt.BadRequest,
                ccxt.OperationFailed, ccxt.OperationRejected):
        if isinstance(e, cls):
            return "business", type(e).__name__
    # ccxt 基类 ExchangeError(OKX code=1 包装): 消息含 sCode,按业务拒绝处理
    return "business", type(e).__name__


class CCXTAdapter(ExchangeAdapter):
    name = "okx-ccxt"

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 sandbox: bool = True):
        import ccxt
        self._ccxt = ccxt.okx({
            "apiKey": api_key, "secret": secret, "password": passphrase,
            "enableRateLimit": True,
        })
        if sandbox:
            self._ccxt.set_sandbox_mode(True)
        self._markets_loaded = False
        self._inst_cache = {}

    def _load(self):
        if not self._markets_loaded:
            self._ccxt.load_markets()
            self._markets_loaded = True

    # ---------- 工具/市场 ----------
    def venue_for(self, base: str, prefer_swap: bool = True) -> Optional[str]:
        self._load()
        swap = f"{base}/USDT:USDT"
        spot = f"{base}/USDT"
        if prefer_swap and swap in self._ccxt.markets:
            return "swap"
        if spot in self._ccxt.markets:
            return "spot"
        if swap in self._ccxt.markets:
            return "swap"
        return None

    def instrument(self, inst_id: str) -> Instrument:
        self._load()
        # 引擎传 OKX 原生格式(如 KAITO-USDT-SWAP),转 ccxt 统一格式
        unified = self._to_ccxt_symbol(inst_id)
        m = self._ccxt.markets.get(unified)
        if not m:
            # 语义同 OKX 51001(沙盘无合约): 引擎 _log_order_failure 按此
            # 自动登记动态黑名单,新未知币一次失败即免疫(与 native 同语义)
            raise ExchangeError(f"51001 沙盘无合约: {inst_id}")
        ct_val = m.get("contractSize") or 1
        lot_sz = 10 ** (-m["precision"]["amount"]) if m["precision"].get("amount") else 1e-8
        base = m["base"]
        amt_limits = m.get("limits", {}).get("amount", {}) or {}
        return Instrument(inst_id=inst_id, base=base, venue=(
            "swap" if m.get("swap") else "spot"),
            ct_val=float(ct_val), lot_sz=lot_sz,
            min_sz=amt_limits.get("min") or 0,
            max_mkt_sz=amt_limits.get("max") or 0)

    @staticmethod
    def _to_ccxt_symbol(inst_id: str) -> str:
        if inst_id.endswith("-USDT-SWAP"):
            return inst_id[:-len("-SWAP")].replace("-", "/") + ":USDT"
        if inst_id.endswith("-USDT"):
            return inst_id.replace("-", "/")
        return inst_id

    # ---------- 行情 ----------
    def fetch_candles(self, inst_id: str, bar: str, limit: int = 100) -> List[Candle]:
        self._load()
        out = []
        try:
            rows = self._ccxt.fetch_ohlcv(self._to_ccxt_symbol(inst_id),
                                          bar.lower(), limit=limit)
        except Exception as e:
            raise ExchangeError(f"K线失败: {e}")
        for r in rows:
            try:
                out.append(Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                                  low=float(r[3]), close=float(r[4]),
                                  volume=float(r[5] or 0)))
            except (TypeError, ValueError, IndexError):
                continue   # 2026-08-20 防御解析: 坏行跳过(float('') 事故)
        return out

    def fetch_ticker_last(self, inst_id: str) -> float:
        self._load()
        try:
            t = self._ccxt.fetch_ticker(self._to_ccxt_symbol(inst_id))
        except Exception as e:
            raise ExchangeError(f"ticker 失败: {e}")
        last = t.get("last")
        if not last:
            raise ExchangeError(f"ticker last 为空: {inst_id}")  # G14 语义
        try:
            return float(last)
        except (TypeError, ValueError):
            raise ExchangeError(f"ticker last 非法: {inst_id}")

    def fetch_funding_rate(self, inst_id: str) -> float:
        self._load()
        try:
            f = self._ccxt.fetch_funding_rate(self._to_ccxt_symbol(inst_id))
            return float(f.get("fundingRate") or 0)
        except Exception:
            return 0.0

    def fetch_tickers(self, venue: str = "swap") -> List[TickerInfo]:
        self._load()
        try:
            raw = self._ccxt.fetch_tickers()
        except Exception as e:
            raise ExchangeError(f"tickers 失败: {e}")
        out = []
        for sym, t in raw.items():
            if venue == "swap" and not sym.endswith(":USDT"):
                continue
            if venue == "spot" and (not sym.endswith("/USDT") or ":" in sym):
                continue
            try:
                last = float(t.get("last") or 0)
            except (TypeError, ValueError):
                continue
            vol_usdt = t.get("quoteVolume")
            if not vol_usdt:   # OKX quoteVolume 常缺,按 volCcy24h×last 归一
                try:
                    vol_usdt = float(t.get("info", {}).get("volCcy24h") or 0) * last
                except (TypeError, ValueError):
                    vol_usdt = 0
            base = t.get("base")
            inst = f"{base}-USDT-SWAP" if venue == "swap" else f"{base}-USDT"
            out.append(TickerInfo(inst_id=inst, base=base, last=last,
                                  vol_ccy_24h=0.0,
                                  vol_usdt_24h=float(vol_usdt or 0)))
        return out

    def new_cl_ord_id(self) -> str:
        import uuid
        return f"ca{int(time.time()*1000)}{uuid.uuid4().hex[:8]}"  # G1 格式

    # ---------- 账户 ----------
    def fetch_balance(self) -> BalanceInfo:
        self._load()
        try:
            b = self._ccxt.fetch_balance()
        except Exception as e:
            raise ExchangeError(f"余额失败: {e}")
        usdt = b.get("USDT") or {}
        return BalanceInfo(
            total_eq=float(b.get("total", {}).get("USDT")
                           or usdt.get("total") or 0),
            usdt_free=float(usdt.get("free") or 0),
            usdt_total=float(usdt.get("total") or 0),
            by_ccy={c: {"free": float(v.get("free") or 0),
                        "total": float(v.get("total") or 0)}
                    for c, v in (b or {}).items()
                    if c not in ("info", "free", "used", "total", "timestamp",
                                 "datetime")})

    def fetch_positions(self, inst_id: Optional[str] = None) -> List[PositionInfo]:
        self._load()
        sym = self._to_ccxt_symbol(inst_id) if inst_id else None
        try:
            raw = self._ccxt.fetch_positions([sym] if sym else None)
        except Exception as e:
            raise ExchangeError(f"持仓失败: {e}")
        out = []
        for p in raw:
            contracts = float(p.get("contracts") or 0)
            if contracts == 0:
                continue
            info = p.get("info") or {}
            _base = (p.get("symbol") or "").split("/")[0]
            iid = inst_id or (f"{_base}-USDT-SWAP" if _base else "")
            ct_val = 1.0
            try:
                ct_val = self.instrument(iid).ct_val
            except ExchangeError:
                ct_val = 1.0
            out.append(PositionInfo(
                inst_id=iid,
                base=p.get("base") or iid.split("-")[0],
                side=p.get("side") or "long",
                contracts=abs(contracts),
                base_qty=abs(contracts) * ct_val,
                avg_px=float(p.get("entryPrice") or 0),
                mgn_mode=info.get("mgnMode") or "cross"))   # G15 语义
        return out

    def set_leverage(self, inst_id: str, lever: int, pos_side: str,
                     mgn_mode: str = "isolated") -> None:
        self._load()
        try:
            self._ccxt.set_leverage(lever, self._to_ccxt_symbol(inst_id),
                                    params={"mgnMode": mgn_mode,
                                            "posSide": pos_side})
        except Exception as e:
            raise ExchangeError(f"杠杆设置失败: {e}")

    def spot_holding(self, base: str) -> float:
        try:
            b = self._ccxt.fetch_balance()
            return float((b.get(base) or {}).get("total") or 0)
        except Exception:
            return 0.0

    # ---------- 下单 ----------
    def place_market_order(self, inst_id: str, side: str, qty: float,
                           venue: str = "swap", pos_side: Optional[str] = None,
                           reduce_only: bool = False,
                           cl_ord_id: Optional[str] = None,
                           td_mode: Optional[str] = None) -> OrderResult:
        self._load()
        inst = self.instrument(inst_id)
        sym = self._to_ccxt_symbol(inst_id)
        cl_ord_id = cl_ord_id or self.new_cl_ord_id()
        if venue == "spot":
            sz = floor_to_lot(qty, inst.lot_sz)
            params = {"clientOrderId": cl_ord_id}
        else:
            contracts = qty / inst.ct_val
            contracts = floor_to_lot(contracts, inst.lot_sz)
            if inst.min_sz > 0 and contracts < inst.min_sz:
                return OrderResult(ok=False, qty=qty, cl_ord_id=cl_ord_id,
                                   message=f"{inst_id}: {contracts} 张 < 最小 {inst.min_sz}")
            params = {"posSide": pos_side or "long",
                      "tdMode": td_mode or "cross",   # G15 语义
                      "clientOrderId": cl_ord_id}
            if reduce_only:
                params["reduceOnly"] = "true"
        try:
            o = self._ccxt.create_order(sym, "market", side,
                                        contracts if venue == "swap" else sz,
                                        params=params)
        except Exception as e:
            kind, cls_name = _classify(e)
            if kind == "network":
                raise ExchangeError(f"下单异常[{cls_name}]: {e}")  # 重试/反查
            return OrderResult(ok=False, qty=qty, cl_ord_id=cl_ord_id,
                               message=f"[{cls_name}] {e}")
        return OrderResult(ok=True, ord_id=str(o.get("id") or ""),
                           cl_ord_id=cl_ord_id, qty=qty)

    def place_conditional_stop(self, inst_id: str, side: str, qty: float,
                               pos_side: str, trigger_px: float,
                               is_tp: bool = False) -> OrderResult:
        self._load()
        inst = self.instrument(inst_id)
        if inst.venue != "swap":
            return OrderResult(ok=False, qty=qty,
                               message="现货不支持交易所侧条件单")
        sym = self._to_ccxt_symbol(inst_id)
        contracts = floor_to_lot(qty / inst.ct_val, inst.lot_sz)
        params = {"posSide": pos_side, "tdMode": "cross",
                  "reduceOnly": "true",
                  "clientOrderId": self.new_cl_ord_id()}
        if is_tp:
            params.update({"tpTriggerPx": str(trigger_px),
                           "tpTriggerPxType": "last", "tpOrdPx": "-1"})
        else:
            params.update({"slTriggerPx": str(trigger_px),
                           "slTriggerPxType": "last", "slOrdPx": "-1"})
        try:
            o = self._ccxt.create_order(sym, "market", side, contracts,
                                        params=params)
        except Exception as e:
            kind, cls_name = _classify(e)
            if kind == "network":
                raise ExchangeError(f"条件单异常[{cls_name}]: {e}")
            return OrderResult(ok=False, qty=qty, message=f"[{cls_name}] {e}")
        return OrderResult(ok=True, algo_id=str(o.get("id") or ""), qty=qty)

    def pending_algo_ids(self, inst_id: str) -> List[str]:
        self._load()
        sym = self._to_ccxt_symbol(inst_id)
        ids = []
        for ot in ("conditional", "oco", "trigger", "move_order_stop"):
            try:
                opens = self._ccxt.fetch_open_orders(
                    sym, params={"ordType": ot, "instType": "SWAP"})
                ids.extend(str(o.get("id")) for o in opens if o.get("id"))
            except Exception:
                pass
        return ids

    def cancel_algos(self, inst_id: str,
                     algo_ids: Optional[List[str]] = None) -> bool:
        self._load()
        sym = self._to_ccxt_symbol(inst_id)
        ids = algo_ids or self.pending_algo_ids(inst_id)
        ok_all = True
        for aid in ids:
            try:
                self._ccxt.cancel_order(aid, sym, params={"trigger": True})
            except Exception:
                ok_all = False
        return ok_all

    def cancel_all_spot_orders(self, inst_id: str) -> bool:
        self._load()
        try:
            self._ccxt.cancel_all_orders(self._to_ccxt_symbol(inst_id))
            return True
        except Exception:
            return False

    def fetch_order_avg_px(self, inst_id: str, ord_id: str) -> Optional[float]:
        self._load()
        try:
            o = self._ccxt.fetch_order(ord_id, self._to_ccxt_symbol(inst_id))
            return float(o.get("average")) if o.get("average") else None
        except Exception:
            return None

    def fetch_order_state(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        self._load()
        try:
            o = self._ccxt.fetch_order(cl_ord_id, self._to_ccxt_symbol(inst_id),
                                       params={"clientOrderId": cl_ord_id})
        except Exception:
            return None
        return {"state": o.get("status") or "",
                "avg_px": float(o.get("average")) if o.get("average") else None,
                "ord_id": str(o.get("id") or "")}

    def fetch_bills(self, ccy: str = "USDT", since_ms: int = 0,
                    bill_type: str = "") -> List[dict]:
        """账户账单(2026-08-23 改走 OKX bills 原始端点——ccxt fetch_ledger
        对 OKX 返回空)。行格式统一:
          type='fee'     → 资金费率结算(balChg=amount,负=实付)
          type='trade'   → 成交账单,手续费在 info.fee(负=实付)
        按 since_ms 过滤,只返回最近 100 条(日内短线足够)。"""
        self._load()
        try:
            params = {}
            if bill_type:
                params["type"] = bill_type
            if since_ms:
                params["begin"] = str(int(since_ms))
            resp = self._ccxt.private_get_account_bills(params)
            rows = resp.get("data") or []
        except Exception:
            return []
        out = []
        for b in rows:
            try:
                raw_type = str(b.get("type") or "")
                if raw_type == "8":       # 资金费率结算
                    out.append({"ts": int(b.get("ts") or 0),
                                "amount": float(b.get("balChg") or 0),
                                "type": "fee",
                                "info": {"instId": b.get("instId") or ""}})
                elif raw_type == "2":     # 成交
                    out.append({"ts": int(b.get("ts") or 0),
                                "amount": float(b.get("balChg") or 0),
                                "type": "trade",
                                "info": {"fee": b.get("fee"),
                                         "instId": b.get("instId") or ""}})
            except Exception:
                continue
        return out
