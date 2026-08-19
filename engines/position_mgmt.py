"""
仓位管理层（PositionMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：开仓全链路（风控闸门/幂等/黑名单预检/数量换算/账本认领/市价单/
交易所侧止损止盈/journal 记账/特征采集/WS 订阅）、下单异常反查（clOrdId
幂等恢复）、TP 条件单、幽灵条件单清理、下单失败结构化落库。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader。
"""
import time

import config
from exchange.base import ExchangeError
from exchange.models import floor_to_lot, OrderResult

# 参数别名（统一维护于 config.py,本模块不私藏数值）
SIGNAL_SCORE = config.SIGNAL_SCORE
LEVERAGE_MAP = config.LEVERAGE_MAP
RISK_PER_TRADE = config.RISK_PER_TRADE
FLAG_ENABLE_EXCHANGE_TP = config.FLAG_ENABLE_EXCHANGE_TP


class PositionMixin:
    """仓位/订单管理功能块。"""

    # ---------- 执行：开仓（合约或现货） ----------
    def open_position(self, base, sig, score=SIGNAL_SCORE,
                      stop_adj=0.0, size_factor=1.0, adopted_ids=None):
        adopted_ids = adopted_ids or []   # R2-3 预接线：本笔实际采纳的经验 id
        # 交易场所探测：有合约 → 合约（多空皆可，杠杆+交易所侧止损）；
        # 无合约（仅现货的美股代币）→ 现货（仅做多）。
        venue = self.exchange.venue_for(base)
        if venue is None:
            print(f"⏭️ {base}: 无可用交易场所，跳过")
            return None
        # 2026-08-20 用户拍板"只做合约,不做现货": 无合约场所一律拒绝。
        # 现货路径代码保留但不可达(SWAP_ONLY=False 可逆恢复)。
        if config.SWAP_ONLY and venue != "swap":
            print(f"⏭️ {base}: 只做合约（SWAP_ONLY），无合约场所，跳过")
            self._log_scan_decision(base, True, sig["dir"], "reject_spot_only",
                                    "只做合约(用户拍板),该标的无合约场所")
            return None
        if venue == "spot" and sig["dir"] != "long":
            print(f"⏭️ {base}: 仅现货，不支持做空，跳过")
            return None
        inst_id = self._inst_id(base, venue)
        sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
        # 0. 风控闸门：熔断 + 幂等 + 余额（审计 CR-1/CR-2）
        if not self.risk.can_trade():
            print(f"⛔ 拒绝开仓 {base}: 风控熔断 {self.risk.halt_reason}")
            return None
        open_same = [t for t in self.journal.trades
                     if t["status"] == "open" and t["symbol"] == base]
        if open_same:
            print(f"⏭️ {base} 已有未平仓交易 {open_same[0]['id']}，跳过（幂等）")
            return None
        # R1-6 跨策略幂等（收窄版）：只拒【同 symbol 且同 posSide】的交易所持仓
        # （同 posSide 会合并、互顶杠杆；opposite side 是独立仓位，放行）
        dir_side = sig["dir"]
        if venue == "swap":
            try:
                same_side = [p for p in self.exchange.fetch_positions()
                             if p.inst_id == inst_id and p.base_qty > 0
                             and p.side == dir_side]
                if same_side:
                    print(f"⏭️ {base} 同方向 {dir_side} 已有 {len(same_side)} 个合约持仓"
                          f"（同 posSide 会合并，可能为套利腿），跳过")
                    return None
            except Exception:
                pass   # 查询失败退回 journal 幂等
        # 经验决策的止损修正真正生效（B6：v1 的 stop_adj/size_factor 是死代码）
        if stop_adj:
            if sig["dir"] == "long":
                sig = dict(sig, stop=sig["entry"] - (1 + stop_adj) * sig["atr"])
            else:
                sig = dict(sig, stop=sig["entry"] + (1 + stop_adj) * sig["atr"])
            print(f"  {base}: 历史止损教训 → 止损放宽 +{stop_adj:.1f}×ATR")

        # 2026-08-17: 沙盘不可交易合约预检拒绝(生产行情有、demo 51001 不存在)。
        # 来源合并: 配置静态表 + untradable_symbols 动态表(下单失败自动登记)。
        if base in config.DEMO_UNTRADABLE or self._is_auto_untradable(base):
            # 这是正常运营拒绝(无订单发出),记决策日志而非失败台账——
            # 否则黑名单符号每次出信号都产生一条 H11 假告警(2026-08-17 噪音修复)。
            self._log_scan_decision(base, True, sig["dir"], "reject_untradable",
                                    "沙盘无此合约(不可交易黑名单)")
            return None

        price = sig["entry"]

        # ===== 现货路径（仅现货的美股代币，仅做多，无杠杆，止损由本地监控执行） =====
        if venue == "spot":
            inst = self.exchange.instrument(inst_id)
            qty = floor_to_lot(config.MAX_NOTIONAL_PER_TRADE / price, inst.lot_sz)
            if qty <= 0 or (inst.min_sz > 0 and qty < inst.min_sz):
                print(f"⛔ 拒绝开仓 {base}: 数量 {qty} 无效（最小 {inst.min_sz}）")
                # 预检拒绝=正常运营(无订单发出),记决策日志,不污染失败台账
                self._log_scan_decision(base, True, sig["dir"], "reject_min_size",
                                        f"数量 {qty} 无效(最小 {inst.min_sz})")
                return None
            try:
                usdt_free = self.exchange.fetch_balance().usdt_free
                if usdt_free < qty * price:
                    print(f"⛔ 拒绝开仓 {base}: USDT 可用 {usdt_free:.0f} < 所需 {qty*price:.0f}")
                    return None
            except Exception:
                pass
            with self._mutex:
                ok_claim, claim_reason = self.ledger.claim(sym_ledger, "long", "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            cl_ord_id = self.exchange.new_cl_ord_id()
            try:
                res = self.exchange.place_market_order(inst_id, "buy", qty, venue="spot",
                                                       cl_ord_id=cl_ord_id)
            except ExchangeError as e:
                res = self._recover_order(inst_id, cl_ord_id, qty, e)
            if not res.ok:
                try:
                    self.ledger.release(sym_ledger, "long", "dir", qty, qty * price)
                except Exception:
                    pass
                print(f"❌ 现货开仓失败 {base}: {res.message}")
                self._log_order_failure(base, inst_id, "buy", qty, "open", res.message)
                return None
            with self._mutex:
                tid = self.journal.log_entry(
                    symbol=base, signal="回踩确认",
                    reason=f"long {sig['atr']/price*100:.1f}%ATR(现货)",
                entry_price=price, stop_loss=sig["stop"], take_profit=sig["tp"],
                size=qty, direction="long", score=score,
                adopted_lesson_ids=adopted_ids, atr_value=sig["atr"],
                signal_price=sig["entry"], venue="spot")
            # Phase 1: 入场特征落库（影子模式,采集失败不影响交易）
            try:
                from engines.feature_collector import collect_entry_features
                collect_entry_features(tid, base, sig, "spot",
                                       self.exchange.name, db_path=self._db_path)
            except Exception:
                pass
            # 2026-08-17: 下单后动态订阅 WS(秒级价格感知,提速止损监控)
            if self.rt is not None:
                try:
                    self.rt.subscribe(base)
                except Exception:
                    pass
            msg = (f"🎯 现货开多 {base}\n"
                   f"入场 **{price:.2f}**\n"
                   f"止损 {sig['stop']:.2f}  ·  止盈 {sig['tp']:.2f}\n"
                   f"数量 {qty}（现货无杠杆，止损由本地监控执行）")
            print(msg)
            self._notify(msg)
            return tid

        # ===== 合约路径（原有） =====
        lev = LEVERAGE_MAP.get(base, 2)
        inst = self.exchange.instrument(inst_id)
        for side in ["long", "short"]:
            try:
                self.exchange.set_leverage(inst_id, lev, side)
            except Exception:
                pass
        # 仓位：单笔风险 1%，名义金额上限 150 USDT（小仓位慢跑）
        stop_dist = abs(price - sig["stop"]) / price
        qty = (self.exchange.fetch_balance().usdt_total * RISK_PER_TRADE) / (price * stop_dist)
        qty = min(qty, config.MAX_NOTIONAL_PER_TRADE / price)  # 小仓位：名义上限(config)
        qty *= size_factor          # 连亏半仓等经验决策真正生效（B6）
        # 市场最大下单量限制（市价单，张→币）
        if inst.max_mkt_sz > 0:
            qty = min(qty, inst.max_mkt_sz * inst.ct_val * 0.9)
        # 精度对齐(2026-08-20 修正): 真实可交易增量 = lotSz × ctVal(币),
        # 只向下取整不超发。此前对齐到整张 ctVal——美股合约 ctVal=1 币
        # (NVDA/ANTHROPIC ≈180 USDT/张),150 名义上限永远凑不满 1 整币,
        # 全部被 reject_min_size 误杀;实际交易所允许 0.01 张(lotSz)。
        qty = floor_to_lot(qty, inst.lot_sz * inst.ct_val)
        # 最小下单量校验：150 USDT 名义买不满最小张数时【拒绝】而不是放大到
        # 最小张数（放大会击穿 150 USDT 小仓位上限，例如 BTC 0.01张=630 USDT）
        min_qty = inst.min_sz * inst.ct_val
        if qty < min_qty:
            print(f"⛔ 拒绝开仓 {base}: 名义 {config.MAX_NOTIONAL_PER_TRADE} USDT 只够 {qty} 币 < 最小 {min_qty} 币"
                  f"（宁可错过，不放大仓位）")
            # 预检拒绝=正常运营(无订单发出),记决策日志,不污染失败台账
            self._log_scan_decision(base, True, sig["dir"], "reject_min_size",
                                    f"名义不足最小张数(需 {min_qty} 币)")
            return None
        # 余额检查（曾发生 USDT 耗尽事故）
        try:
            usdt_free = self.exchange.fetch_balance().usdt_free
            if usdt_free < qty * price:
                print(f"⛔ 拒绝开仓 {base}: USDT 可用 {usdt_free:.0f} < 所需 {qty*price:.0f}")
                return None
        except Exception:
            pass
        try:
            side = "buy" if sig["dir"] == "long" else "sell"
            # R1-1 开仓前清理残留：取消该 instId 全部 pending algo 单（防幽灵止损单误平新仓）
            self._cancel_stop_orders(base, "开仓前清理残留")
            # R1-12 所有权账本：组合总敞口闸门 + claim
            with self._mutex:
                ok_claim, claim_reason = self.ledger.claim(sym_ledger, sig["dir"], "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            cl_ord_id = self.exchange.new_cl_ord_id()
            try:
                res = self.exchange.place_market_order(inst_id, side, qty, venue="swap",
                                                       pos_side=sig["dir"],
                                                       cl_ord_id=cl_ord_id)
            except ExchangeError as e:
                # 审计 C1:网络错误可能已成交 → 用 clOrdId 反查,已成交继续走止损/记账
                res = self._recover_order(inst_id, cl_ord_id, qty, e)
            if not res.ok:
                try:
                    with self._mutex:
                        self.ledger.release(sym_ledger, sig["dir"], "dir", qty, qty * price)
                except Exception:
                    pass
                print(f"❌ 开仓失败 {base}: {res.message}")
                self._log_order_failure(base, inst_id, side, qty, "open", res.message)
                return None
            # 审计 M5:用真实成交均价记账(响应里没有时回填;失败退回信号价)
            fill_px = None
            try:
                fill_px = self.exchange.fetch_order_avg_px(inst_id, res.ord_id) if res.ord_id else None
            except Exception:
                fill_px = None
            if fill_px:
                price = fill_px
                # 2026-08-18 止损/止盈锚定真实成交价: 此前锚定信号参考价,滑点
                # 把实际 R:R 从名义 2:1 压到 1.4~0.9(用户发现'感觉是 1:1'——
                # BNB 空单 fill 偏离 0.7 后实际 R:R 仅 0.90)。重锚后无论滑点
                # 多少,止损永远 = 成交价 ∓ (1+stop_adj)×ATR、止盈 = ∓ 2×ATR。
                stop_off = (1 + stop_adj) * config.STOP_ATR_MULT * sig["atr"]
                tp_off = config.TP_ATR_MULT * sig["atr"]
                if sig["dir"] == "long":
                    sig = dict(sig, stop=fill_px - stop_off, tp=fill_px + tp_off)
                else:
                    sig = dict(sig, stop=fill_px + stop_off, tp=fill_px - tp_off)
            # 交易所侧停损单（本地进程崩溃也生效 — OP-1）
            stop_side = "sell" if sig["dir"] == "long" else "buy"
            try:
                sl_res = self.exchange.place_conditional_stop(
                    inst_id, stop_side, qty, sig["dir"], sig["stop"])
                if sl_res.ok:
                    print(f"  🛡️ 已挂交易所侧止损单（原生 slTriggerPx） @ {sig['stop']:.2f}")
                else:
                    print(f"  ⚠️ 交易所侧止损单挂单失败（本地 tick 监控兜底）: {sl_res.message}")
                    # 只在失败时落失败台账(2026-08-17: 此前无条件落账,成功也记
                    # 一条空 error 的失败行 → H11 假告警,KAITO 首单即中招)
                    self._log_order_failure(base, inst_id, stop_side, qty, "stop_order", sl_res.message)
            except ExchangeError as e:
                print(f"  ⚠️ 交易所侧止损单挂单失败（本地 tick 监控兜底）: {e}")
                self._log_order_failure(base, inst_id, stop_side, qty, "stop_order", e)
            # R2-5: 止盈挂交易所侧（默认关闭；开启前须通过 docs/ops/tp_sandbox_verify.md 沙盘验证）
            tp_ok = True
            if FLAG_ENABLE_EXCHANGE_TP:
                tp_ok = self._place_tp(base, sig, qty)
            # 记录交易（journal）
            with self._mutex:
                tid = self.journal.log_entry(
                    symbol=base, signal="回踩确认", reason=f"{sig['dir']} {sig['atr']/price*100:.1f}%ATR",
                entry_price=price, stop_loss=sig["stop"], take_profit=sig["tp"],
                size=qty, direction=sig["dir"], score=score,
                adopted_lesson_ids=adopted_ids,          # R2-3：本笔实际采纳的经验
                atr_value=sig["atr"], signal_price=sig["entry"],
                venue="swap")  # 合约腿
            # Phase 1: 入场特征落库（影子模式,采集失败不影响交易）
            try:
                from engines.feature_collector import collect_entry_features
                collect_entry_features(tid, base, sig, "swap",
                                       self.exchange.name, db_path=self._db_path)
            except Exception:
                pass
            # 2026-08-17: 下单后动态订阅 WS(秒级价格感知,提速止损监控)
            if self.rt is not None:
                try:
                    self.rt.subscribe(base)
                except Exception:
                    pass
            # R2-5: TP 挂失败 → 台账打标 tp_missing（本地 monitor 止盈兜底）
            if FLAG_ENABLE_EXCHANGE_TP and not tp_ok:
                for t in self.journal.trades:
                    if t["id"] == tid:
                        t["tp_missing"] = True
                self.journal._save()
                self._notify(f"⚠️ {base} TP 条件单挂失败（本地 monitor 止盈兜底）")
            msg = (f"🎯 {self._dir_cn(sig['dir'])} {base}\n"
                   f"入场 **{price:.2f}**\n"
                   f"止损 {sig['stop']:.2f}  ·  止盈 {sig['tp']:.2f}\n"
                   f"盈亏比 2:1  ·  杠杆 {lev}x  ·  数量 {qty}  ·  名义 {qty * price:.0f} USDT")
            print(msg)
            self._notify(msg)
            return tid
        except Exception as e:
            # R1-12: 下单失败回滚 claim，防账本残留
            try:
                self.ledger.release(sym_ledger, sig["dir"], "dir", qty, qty * price)
            except Exception:
                pass
            print(f"❌ 开仓失败 {base}: {e}")
            self._log_order_failure(base, inst_id, side, qty, "open", e)
            return None

    def _recover_order(self, inst_id, cl_ord_id, qty, error):
        """审计 C1:下单抛 ExchangeError 后按 clOrdId 反查真实状态。
        已成交(filled)→ ok=True(继续挂止损/记账);否则 fail-closed 返回 ok=False。"""
        try:
            state = self.exchange.fetch_order_state(inst_id, cl_ord_id)
        except Exception:
            state = None
        if state and state.get("state") == "filled":
            print(f"  ⚠️ 下单响应丢失但反查已成交,继续止损/记账: {error}")
            return OrderResult(ok=True, ord_id=state.get("ord_id") or "",
                               cl_ord_id=cl_ord_id, qty=qty)
        return OrderResult(ok=False, qty=qty,
                           message=f"下单异常且反查无法确认成交: {error}")

    # ---------- R2-5：止盈挂交易所侧（独立 TP 条件单） ----------
    def _place_tp(self, base, sig, qty):
        """挂 TP 条件单（原生 tpTriggerPx 结构）。失败返回 False（上层打 tp_missing
        + 本地 monitor 兜底）。"""
        inst_id = self._inst_id(base, "swap")
        tp_side = "sell" if sig["dir"] == "long" else "buy"
        try:
            res = self.exchange.place_conditional_stop(
                inst_id, tp_side, qty, sig["dir"], sig["tp"], is_tp=True)
            if res.ok:
                print(f"  🎯 已挂交易所侧 TP 条件单（原生 tpTriggerPx） @ {sig['tp']:.2f}")
                return True
            print(f"  ⚠️ TP 挂单失败（本地 monitor 止盈兜底）: {res.message}")
            self._log_order_failure(base, inst_id, "tp", qty, "tp_order", res.message)
            return False
        except ExchangeError as e:
            print(f"  ⚠️ TP 挂单失败（本地 monitor 止盈兜底）: {e}")
            self._log_order_failure(base, inst_id, "tp", qty, "tp_order", e)
            return False

    # ---------- 幽灵止损单清理（R1-1） ----------
    def _cancel_stop_orders(self, base, reason=""):
        """取消该 instId 全部 pending algo 单（枚举全部 ordType 查询后合并取消）。
        fail-closed：失败不中断主流程，仅告警。"""
        inst_id = self._inst_id(base, "swap")
        try:
            algo_ids = self.exchange.pending_algo_ids(inst_id)
        except Exception as e:
            self._notify(f"⚠️ 取消失败(查询) {base} {reason}: {e}")   # fail-closed，不中断
            return False
        if not algo_ids:
            return True
        try:
            return self.exchange.cancel_algos(inst_id, algo_ids)
        except Exception as e:
            self._notify(f"⚠️ 取消失败 {base} {reason} algoIds={algo_ids}: {e}")
            return False

    def _log_order_failure(self, base, inst_id, side, qty, stage, error):
        """下单失败结构化落库（2026-08-16 用户问"有没有下单失败的日志"——
        此前只有 stdout 文本,无法查询/告警。每次下单/挂单/预检失败必入账）。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            sdb.x("INSERT INTO order_failures (ts, base, inst_id, side, qty, "
                  "stage, error) VALUES (?,?,?,?,?,?,?)",
                  [time.time(), base, inst_id, side, qty, stage,
                   str(error)[:300]], db_path=self._db_path)
            # 2026-08-17: 沙盘永久不可交易符号自动登记——错误码已由 transport
            # 穿透进 error 文本,解析到即入动态黑名单。覆盖永久类错误码:
            #   51001 无合约 / 51087 已退市 / 51155 本地合规限制(RE 案例)
            # 明确排除 51000(clOrdId 等参数错误——可修复,不是符号问题)。
            err = str(error)
            perm_codes = [c for c in ("51001", "51087", "51155") if c in err]
            if perm_codes and base:
                sdb.x("INSERT OR IGNORE INTO untradable_symbols (base, reason, ts) "
                      "VALUES (?,?,?)",
                      [base, perm_codes[0], time.time()], db_path=self._db_path)
        except Exception:
            pass
