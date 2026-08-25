"""
仓位管理层（PositionMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：开仓全链路（风控闸门/幂等/黑名单预检/数量换算/账本认领/市价单/
交易所侧止损止盈/journal 记账/特征采集/WS 订阅）、下单异常反查（clOrdId
幂等恢复）、TP 条件单、幽灵条件单清理、下单失败结构化落库。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader。
"""
import json
import time

import config
SIGNAL_SCORE = config.SIGNAL_SCORE
LEVERAGE_MAP = config.LEVERAGE_MAP
RISK_PER_TRADE = config.RISK_PER_TRADE
FLAG_ENABLE_EXCHANGE_TP = config.FLAG_ENABLE_EXCHANGE_TP


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global SIGNAL_SCORE
    SIGNAL_SCORE = config.SIGNAL_SCORE
    global LEVERAGE_MAP
    LEVERAGE_MAP = config.LEVERAGE_MAP
    global RISK_PER_TRADE
    RISK_PER_TRADE = config.RISK_PER_TRADE
    global FLAG_ENABLE_EXCHANGE_TP
    FLAG_ENABLE_EXCHANGE_TP = config.FLAG_ENABLE_EXCHANGE_TP



from exchange.base import ExchangeError
from exchange.models import floor_to_lot, OrderResult

# 参数别名（统一维护于 config.py,本模块不私藏数值）


def leverage_for(base, score, journal_trades):
    """B+C 杠杆分档(2026-08-20 用户拍板,纯函数可单测):
      B 信号分 ≥ config.LEVERAGE_HIGH_SCORE
      C 该币平仓样本 ≥ LEVERAGE_HIGH_MIN_TRADES 且胜率 ≥ LEVERAGE_HIGH_MIN_WINRATE
    双条件同时满足 → LEVERAGE_HIGH(5x),否则 LEVERAGE_NORMAL(3x);
    最终钳制 [LEVERAGE_MIN, LEVERAGE_MAX]。"""
    lev = LEVERAGE_MAP.get(base, config.LEVERAGE_MIN)
    recent = [t for t in (journal_trades or [])
              if t.get("symbol") == base and t.get("status") == "closed"]
    wins = [t for t in recent if (t.get("pnl") or 0) > 0]
    score_ok = (score or 0) >= config.LEVERAGE_HIGH_SCORE
    track_ok = (len(recent) >= config.LEVERAGE_HIGH_MIN_TRADES
                and len(wins) / len(recent) >= config.LEVERAGE_HIGH_MIN_WINRATE)
    if score_ok and track_ok:
        lev = config.LEVERAGE_HIGH
    return min(max(lev, config.LEVERAGE_MIN), config.LEVERAGE_MAX)


class PositionMixin:
    """仓位/订单管理功能块。"""

    # ---------- 执行：开仓（合约或现货） ----------
    def open_position(self, base, sig, score=None,
                      stop_adj=0.0, size_factor=1.0, adopted_ids=None):
        adopted_ids = adopted_ids or []   # R2-3 预接线：本笔实际采纳的经验 id
        # 交易场所探测：有合约 → 合约（多空皆可，杠杆+交易所侧止损）；
        # 无合约（仅现货的美股代币）→ 现货（仅做多）。
        venue = self.exchange.venue_for(base)
        if venue is None:
            print(f"⏭️ {base}: 无可用交易场所，跳过")
            self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                    "无可用交易场所")
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
            self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                    "仅现货不支持做空")
            return None
        inst_id = self._inst_id(base, venue)
        sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
        # 0. 风控闸门：熔断 + 幂等 + 余额（审计 CR-1/CR-2）
        if not self.risk.can_trade():
            print(f"⛔ 拒绝开仓 {base}: 风控熔断 {self.risk.halt_reason}")
            self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                    f"风控熔断 {self.risk.halt_reason}")
            return None
        open_same = [t for t in self.journal.trades
                     if t["status"] == "open" and t["symbol"] == base]
        if open_same:
            print(f"⏭️ {base} 已有未平仓交易 {open_same[0]['id']}，跳过（幂等）")
            self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                    f"已有未平仓 {open_same[0]['id']}")
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
                    self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                            f"同方向已有 {len(same_side)} 个持仓")
                    return None
            except Exception:
                pass   # 查询失败退回 journal 幂等
        # 严格 2:1 paper 的预测标签固定为 1×ATR 止损/2×ATR 止盈；若执行时
        # 再放宽 stop，模型预测的就不是实际订单。保留 legacy/Fake 调用的
        # stop_adj 能力，但真实 OKX paper 明确忽略它，使预测、标签、订单同口径。
        if getattr(self, "require_2to1_prediction", False) and stop_adj:
            print(f"  {base}: 固定 2:1 预测门启用，忽略历史 stop_adj")
            stop_adj = 0.0
        # 兼容路径的经验止损修正（B6：v1 的 stop_adj/size_factor 是死代码）
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
                    self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                            f"USDT 可用 {usdt_free:.0f} < 所需 {qty*price:.0f}")
                    return None
            except Exception:
                pass
            with self._mutex:
                ok_claim, claim_reason = self.ledger.claim(sym_ledger, "long", "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                        claim_reason)
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
                self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                        res.message)
                return None
            with self._mutex:
                tid = self.journal.log_entry(
                    symbol=base, signal="回踩确认",
                    reason=f"long {sig['atr']/price*100:.1f}%ATR(现货)",
                entry_price=price, stop_loss=sig["stop"], take_profit=sig["tp"],
                size=qty, direction="long", score=score,
                adopted_lesson_ids=adopted_ids, atr_value=sig["atr"],
                signal_price=sig["entry"],
                shadow_dims=json.dumps(sig.get("shadow_dims") or {},
                                       ensure_ascii=False),
                targets=json.dumps(sig.get("targets") or {},
                                   ensure_ascii=False),
                strategy_timeframe=config.SIGNAL_SAMPLE_TIMEFRAME,
                max_hold_hours=config.MAX_HOLD_HOURS,
                strategy_id=(sig.get("strategy_id") or
                             config.ENTRY_SIGNAL_STRATEGY_ID),
                venue=("live" if getattr(self, "live_mode", False) else "spot"))
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
        # 2026-08-20 用户指示: 合约倍数 3x~5x + B+C 分档——B 信号分 ≥
        # LEVERAGE_HIGH_SCORE 且 C 该币战绩数据验证(≥N 笔平仓且胜率≥阈值)
        # → 5x;否则 3x;最终钳制 [LEVERAGE_MIN, LEVERAGE_MAX]。
        lev = leverage_for(base, score, self.journal.trades)
        if getattr(self, "live_mode", False):
            # 2026-08-25 用户指示: 实盘全部统一 5x
            lev = config.LIVE_LEVERAGE_MAP.get(
                base, getattr(config, "LIVE_LEVERAGE_DEFAULT", 5))
        inst = self.exchange.instrument(inst_id)
        for side in ["long", "short"]:
            try:
                self.exchange.set_leverage(inst_id, lev, side)
            except Exception:
                pass
        # 仓位：单笔风险 1%，名义金额上限 150 USDT（小仓位慢跑）
        stop_dist = abs(price - sig["stop"]) / price
        # 2026-08-22 实盘模式: 单笔风险固定 LIVE_RISK_PER_TRADE USDT(预算1%),
        # 名义上限 LIVE_MAX_NOTIONAL;模拟盘维持原口径(账户1%风险)。
        if getattr(self, "live_mode", False):
            # 2026-08-25 用户确认: C 单与 A 单同口径——BTC/ETH 保留特殊名义
            # (680/230)与 1 合约兜底,其余币名义上限 LIVE_MAX_NOTIONAL
            qty = config.LIVE_RISK_PER_TRADE / (price * stop_dist)
            _notional_cap = config.LIVE_SPECIAL_NOTIONAL.get(
                base, config.LIVE_MAX_NOTIONAL)
            qty = min(qty, _notional_cap / price)
            if base in config.LIVE_SPECIAL_NOTIONAL:
                # 最小可买 = 1 合约(floor 对齐粒度),不是 min_sz×ct_val
                # (BTC 0.01 合约粒度 → 0.01 BTC;ETH 0.1 ETH)
                qty = max(qty, inst.ct_val)
        else:
            qty = (self.exchange.fetch_balance().usdt_total * RISK_PER_TRADE) / (price * stop_dist)
            qty = min(qty, config.MAX_NOTIONAL_PER_TRADE / price)  # 小仓位：名义上限(config)
        qty *= size_factor          # 连亏半仓等经验决策真正生效（B6）
        # 特殊币(BTC/ETH)半仓后会跌破 1 合约再被 floor 到 0——兜底必须在
        # size_factor 之后(2026-08-23 BTC 连续 reject_min_size 的真因)
        if getattr(self, "live_mode", False) and base in config.LIVE_SPECIAL_NOTIONAL:
            qty = max(qty, inst.ct_val)
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
        # 2026-08-23 XRP 案例: OKX 合约粒度可能非常规(如 XRP lot_sz=0.9772 张
        # ≈97.72 币),按币口径 floor 后"看起来够"但换算张数再 floor 会归零,
        # 适配器报"0.0 张 < 最小"落失败台账 → H11 每 5 分钟响一次。
        # 这里用与适配器完全相同的张数口径预检,把这种拒绝拦在下单前。
        _contracts = floor_to_lot(qty / inst.ct_val, inst.lot_sz)
        if qty < min_qty or (inst.min_sz > 0 and _contracts < inst.min_sz):
            print(f"⛔ 拒绝开仓 {base}: 名义 {config.MAX_NOTIONAL_PER_TRADE} USDT "
                  f"只够 {qty} 币({_contracts} 张) < 最小 {min_qty} 币"
                  f"（宁可错过，不放大仓位）")
            # 预检拒绝=正常运营(无订单发出),记决策日志,不污染失败台账
            self._log_scan_decision(base, True, sig["dir"], "reject_min_size",
                                    f"名义不足最小张数(需 {min_qty} 币,"
                                    f"实际可买 {_contracts} 张)")
            return None
        # 余额检查（曾发生 USDT 耗尽事故）
        # 2026-08-23 修正: 合约按【保证金】检查,不是全额名义——
        # 10x 杠杆下 BTC 0.01 合约只需 ~77 USDT 保证金,按 756 名义判拒是误杀
        try:
            usdt_free = self.exchange.fetch_balance().usdt_free
            margin_needed = qty * price / max(lev, 1)
            if usdt_free < margin_needed:
                print(f"⛔ 拒绝开仓 {base}: USDT 可用 {usdt_free:.0f} < "
                      f"所需保证金 {margin_needed:.0f} ({lev}x)")
                self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                        f"USDT 可用 {usdt_free:.0f} < 保证金 {margin_needed:.0f}")
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
                self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                        claim_reason)
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
                # 2026-08-23 XRP 案例: 适配器最小张数拒绝是【预检类】拒绝
                # (无订单发出),按 G10 语义走决策日志,不落失败台账(H11 噪声)
                if "张 < 最小" in (res.message or ""):
                    print(f"⛔ 开仓拒绝 {base}: {res.message}（预检类,不落失败台账）")
                    self._log_scan_decision(base, True, sig["dir"],
                                            "reject_min_size", res.message)
                    return None
                print(f"❌ 开仓失败 {base}: {res.message}")
                self._log_order_failure(base, inst_id, side, qty, "open", res.message)
                self._log_scan_decision(base, True, sig["dir"], "open_failed",
                                        res.message)
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
                # 多少,止损/止盈都重新锚定成交价；严格 paper 已在上游把
                # stop_adj 归零，因此最终订单仍精确为 1×ATR / 2×ATR。
                # 2026-08-25 结构位止损: stop/tp 距离以信号自带值为准
                # (结构位口径已在上游算好),不再从 ATR 重推——重推会把
                # 结构止损覆盖回纯 ATR。2:1 由上游保证(stop_adj 归零时)。
                # stop_adj 已在下单前写进 sig.stop；成交重锚只搬移距离，不能
                # 再乘一次，否则 +0.2 会从 1.2×ATR 复合放大成 1.44×ATR。
                stop_off = abs(float(sig.get("entry") or 0)
                               - float(sig.get("stop") or 0))
                tp_off = abs(float(sig.get("entry") or 0)
                             - float(sig.get("tp") or 0))
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
                shadow_dims=json.dumps(sig.get("shadow_dims") or {},
                                       ensure_ascii=False),
                targets=json.dumps(sig.get("targets") or {},
                                   ensure_ascii=False),
                forecast=json.dumps(sig.get("forecast") or {},
                                    ensure_ascii=False) if sig.get("forecast") else None,
                strategy_timeframe=config.SIGNAL_SAMPLE_TIMEFRAME,
                max_hold_hours=config.MAX_HOLD_HOURS,
                strategy_id=(sig.get("strategy_id") or
                             config.ENTRY_SIGNAL_STRATEGY_ID),
                venue=("live" if getattr(self, "live_mode", False) else "swap"))  # 合约腿；实盘标 live(2026-08-23 重新计盈亏)
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
            # 2026-08-23 目标价位带 + 历史命中率(用户问"会预测会升到哪吗")
            _tg_line = ""
            try:
                _tg = sig.get("targets") or {}
                _tg_line = (f"目标 T1 {_tg['t1']:.2f} · T2 {_tg['t2']:.2f}"
                            + (f" · T3(结构位) {_tg['t3']:.2f}"
                               if _tg.get("t3") else "")
                            + "\n")
            except Exception:
                pass
            _hit_line = ""
            try:
                from decision.target_stats import describe
                _hit_line = describe(getattr(self, "_db_path", None),
                                     sig.get("dir")) + "\n"
            except Exception:
                pass
            _fc_line = ""
            try:
                if sig.get("forecast"):
                    from decision.forecast import describe as _fc_describe
                    _fc_line = "🔮 预测 " + _fc_describe(sig["forecast"]) + "\n"
            except Exception:
                pass
            msg = (f"🎯 {self._dir_cn(sig['dir'])} {base}\n"
                   f"入场 **{price:.2f}**\n"
                   f"止损 {sig['stop']:.2f}  ·  止盈 {sig['tp']:.2f}\n"
                   f"{_tg_line}{_fc_line}{_hit_line}"
                   f"盈亏比 2:1  ·  杠杆 {lev}x  ·  数量 {qty}  ·  名义 {qty * price:.0f} USDT")
            self._log_event("open", {"tid": tid, "symbol": base,
                                     "dir": sig["dir"], "entry": price,
                                     "stop": sig["stop"], "tp": sig["tp"],
                                     "qty": qty, "lev": lev,
                                     "notional": qty * price})
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
            self._log_scan_decision(base, True, sig["dir"], "open_failed", str(e))
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
        + 本地 monitor 兜底）。
        51279(2026-08-20): TP 价已被现价越过(快涨/快跌,成交后价格瞬间越过
        fill+2ATR)→ 条件单按定义即刻触发,本地 monitor 会秒级市价止盈——
        属【预期达成】而非故障: 不落失败台账(防 H11 噪音),只记事件。"""
        inst_id = self._inst_id(base, "swap")
        tp_side = "sell" if sig["dir"] == "long" else "buy"
        try:
            res = self.exchange.place_conditional_stop(
                inst_id, tp_side, qty, sig["dir"], sig["tp"], is_tp=True)
            if res.ok:
                print(f"  🎯 已挂交易所侧 TP 条件单（原生 tpTriggerPx） @ {sig['tp']:.2f}")
                return True
            if "51279" in str(res.message):
                print(f"  ⚡ {base}: TP 价已被现价越过(51279),本地监控即刻市价止盈")
                self._log_event("tp_prepassed", {"base": base,
                                                  "tp": sig["tp"]})
                return False
            print(f"  ⚠️ TP 挂单失败（本地 monitor 止盈兜底）: {res.message}")
            self._log_order_failure(base, inst_id, "tp", qty, "tp_order", res.message)
            return False
        except ExchangeError as e:
            if "51279" in str(e):
                print(f"  ⚡ {base}: TP 价已被现价越过(51279),本地监控即刻市价止盈")
                return False
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
            # 2026-08-20 交易所故障退避: 开仓遇 50001/503(OKX 沙盘 API 全灭)
            # → 暂停开仓 EXCHANGE_OUTAGE_BACKOFF_SECONDS 秒,故障期不再刷失败行。
            # 监控/平仓(stage=close)不受影响——平仓重试每拍继续。
            err_s = str(error)
            if stage == "open" and ("50001" in err_s or "503" in err_s):
                self._open_backoff_until = time.time() + \
                    config.EXCHANGE_OUTAGE_BACKOFF_SECONDS
                print(f"⛔ 交易所下单 API 故障(50001/503),"
                      f"暂停开仓 {config.EXCHANGE_OUTAGE_BACKOFF_SECONDS}s")
            import storage.db as sdb
            sdb.init_db(self._db_path)
            sdb.x("INSERT INTO order_failures (ts, base, inst_id, side, qty, "
                  "stage, error) VALUES (?,?,?,?,?,?,?)",
                  [time.time(), base, inst_id, side, qty, stage,
                   str(error)[:300]], db_path=self._db_path)
            self._log_event("order_fail", {"base": base, "inst_id": inst_id,
                                           "side": side, "qty": qty,
                                           "stage": stage,
                                           "error": str(error)[:200]})
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
