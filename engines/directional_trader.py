"""
方向性交易 agent — 回踩确认信号 + 2:1 盈亏比 + 合理杠杆。

策略逻辑（做多示例，做空对称）：
  信号：1h 多头趋势（EMA20>EMA50）+ 回踩 EMA20 不破 + 拒绝K线（下影线）
  入场：回踩确认后入场（合约，2-3x 杠杆，逐仓）
  止损：入场价 - 1×ATR（单笔风险 1%）
  止盈：入场价 + 2×ATR（2:1 盈亏比）
  进化：每笔复盘，胜率/盈亏比追踪，连亏冷却

架构：只依赖 exchange.base.ExchangeAdapter 抽象接口（无 ccxt）。
交易所访问分层见 exchange/__init__.py；单测可注入 FakeAdapter。

用法：
  python3 directional_trader.py --once   扫一轮信号
  python3 directional_trader.py          常驻运行
"""
import sys
import os
import json
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from strategy.indicators import ema, atr
from decision.self_evolving_trader import SelfEvolvingTrader
MTF_ENABLED = config.MTF_ENABLED
SIGNAL_COOLDOWN_MINUTES = config.SIGNAL_COOLDOWN_MINUTES
DEFAULT_TRADE_BUDGET = config.DEFAULT_TRADE_BUDGET
from execution.trade_journal import TradeJournal
from decision.review_engine import deep_review
from exchange.base import ExchangeAdapter, ExchangeError
from exchange.models import floor_to_lot

LARK = "/Users/wuhai/Desktop/untitled folder/lark"
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"
LEVERAGE_MAP = {"BTC": 3, "ETH": 3, "SOL": 3, "XRP": 3, "DOGE": 3}
# 用户授权 3-10x；取区间下沿 3x——仓位由 1% 风险公式决定（与杠杆无关），
# 更高杠杆只缩短爆仓距离、不提高胜率；方向性策略历史回测未证明正期望，保守为上
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
RISK_PER_TRADE = 0.01  # 单笔风险 1%
RR_RATIO = 2.0         # 2:1 盈亏比
SIGNAL_SCORE = 80      # 回踩确认信号的基础分（用于阈值决策与记录）
# R2-5：止盈挂交易所侧（默认关闭——需沙盘验证通过后由用户/协调者开启）
FLAG_ENABLE_EXCHANGE_TP = False


def notify(msg):
    try:
        subprocess.run([LARK, "im", "+messages-send", "--as", "bot",
                        "--user-id", FEISHU_USER_ID, "--text", msg],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def connect() -> ExchangeAdapter:
    """构建交易所适配器（OKX 模拟盘）。策略层只见 ExchangeAdapter 接口。"""
    cfg = json.load(open("okx_config.json"))
    from exchange.okx_adapter import OKXAdapter
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"], sandbox=True)


class _ExpAdapter:
    """把 ScoredExperience 适配成 SelfEvolvingTrader 期望的 ExperienceBank 接口。
    （审计 B6：此前两套经验库并存，evolver 用旧库、交易记录用新库，闭环断裂）"""

    def __init__(self, bank):
        self.bank = bank

    def relevant(self, symbol=None, category=None):
        # R2-3：只返回 trusted（带 id，供采纳追踪）；discarded 只用于决策减分，不返回
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"]}
               for l in self.bank.trusted(symbol)]
        if category:
            out = [l for l in out if l["category"] == category]
        return out


class DirectionalTrader:
    def __init__(self, exchange: ExchangeAdapter = None, rt=None):
        self.exchange = exchange or connect()
        self.journal = TradeJournal()
        self.evolver = SelfEvolvingTrader()
        # 带评分的经验库（历史经验不一定对，用交易结果验证）
        from decision.experience_scoring import ScoredExperience
        self.exp_bank = ScoredExperience()
        # 统一经验库：evolver 的决策也走 ScoredExperience（B6）
        self.evolver.bank = _ExpAdapter(self.exp_bank)
        # 持仓所有权账本（R1-12）：组合总敞口 ≤600 + claim/release
        from execution.position_ownership import PositionLedger
        self.ledger = PositionLedger()
        # 每日候选池（用户要求：每天扫全市场挑适合下单的币；评分用于动态笔数）
        from engines.daily_scan import load_watchlist
        _watch = load_watchlist()
        self.watchlist = list(_watch.keys())
        self.watch_scores = _watch
        self._watch_date = ""
        self._last_watch_refresh = 0
        print(f"今日候选池 {len(self.watchlist)} 个: {self.watchlist}")
        for b in self.watchlist:
            print(f"  {b}: 当日评分 {self.watch_scores.get(b)} → 允许笔数 {self._trade_budget(b)}")
        # 阈值自适应（决策阈值用分数→盈亏分布校准）
        from decision.threshold_learning import ThresholdLearner
        # R1-3: 方向侧独立阈值文件。方向信号分恒 SIGNAL_SCORE=80（单点），
        # calibrate() 只在 80 单桶求均值、永远无法跨桶找盈亏平衡 → 校准恒 no-op，
        # 故方向阈值保持初始 70 固定；自适应由套利侧 threshold_state_arb.json 负责。
        self.threshold_learner = ThresholdLearner(path="threshold_state_dir.json")  # 阈值恒 70
        # 账户级风控（审计 CR-2：RiskManager 必须真正接线）
        from risk.risk_manager import RiskManager
        try:
            eq = self.exchange.fetch_balance().usdt_total
        except Exception:
            eq = 0
        self.risk = RiskManager(initial_equity=eq if eq > 0 else 4190)
        self._halt_notified = False
        # WebSocket 实时价格（tick 级止损止盈监控，替代 6 小时轮询 — OP-1）
        # 服务模式下由 service 注入共享 rt（与套利引擎共用一条 WS 连接）
        self.rt = rt
        if self.rt is None:
            try:
                from data.realtime_okx import OKXRealtime
                self.rt = OKXRealtime(SYMBOLS).start()
                print("WebSocket 实时价格已接入（止损止盈 tick 级监控）")
            except Exception as e:
                print(f"WebSocket 启动失败，止损监控退回 REST 轮询: {e}")

    # ---------- 场所/行情辅助（依赖 ExchangeAdapter 接口） ----------
    def _inst_id(self, base, venue="swap"):
        return f"{base}-USDT-SWAP" if venue == "swap" else f"{base}-USDT"

    def _fetch_klines_any(self, base, tf, limit):
        """K线获取（场所自适应）：优先合约（策略在合约腿执行），无合约回退现货。
        返回 raw OHLCV 列表（[ts,o,h,l,c,v]）或 None。"""
        venue = self.exchange.venue_for(base)
        if venue is None:
            return None
        try:
            candles = self.exchange.fetch_candles(self._inst_id(base, venue), tf, limit)
            if len(candles) >= 20:
                return [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles]
        except ExchangeError:
            return None
        return None

    def _ticker_last(self, base, prefer_swap=False):
        """最新价（场所自适应）。prefer_swap=True 时优先合约价格。"""
        venue = self.exchange.venue_for(base, prefer_swap=prefer_swap)
        if venue is None:
            return None
        try:
            return self.exchange.fetch_ticker_last(self._inst_id(base, venue))
        except ExchangeError:
            return None

    # ---------- 信号：回踩确认（1 小时线 · 真日内短线） ----------
    def scan_signal(self, base):
        """检查某币的回踩确认信号（1 小时 K 线，日内短线）。
        多周期共振过滤（MTF）：1h 信号方向必须与 4h 趋势同向——顺大势做小势，
        只抓高概率时点，不频繁交易。返回信号 dict 或 None。"""
        try:
            kl = self._fetch_klines_any(base, "1H", 100)
            if not kl:
                return None
            klines = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
                       "volume": k[5]} for k in kl]
        except Exception:
            return None
        if len(klines) < 60:
            return None
        # MTF 共振：4h 趋势方向（做多要求 4h 多头，做空要求 4h 空头）
        tf4h_trend = 0   # 1=多, -1=空, 0=未知（数据不足时视为无共振，放弃信号）
        try:
            kl4 = self._fetch_klines_any(base, "4H", 60)
            if kl4:
                c4 = [k[4] for k in kl4]
                if len(c4) >= 50:
                    e20, e50 = ema(c4, 20), ema(c4, 50)
                    tf4h_trend = 1 if e20[-1] > e50[-1] else -1
        except Exception:
            tf4h_trend = 0
        closes = [k["close"] for k in klines]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        atr_val = atr(klines, 14)

        last = klines[-1]
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        # 日内入场参考价用实时 tick 价（市价单实际成交价），
        # 趋势/ATR 仍来自 1 小时线——避免"信号收盘价 vs 市价成交"错位（RES-11）
        entry_ref = self._ticker_last(base)
        if entry_ref is None:
            entry_ref = last["close"]

        # 做多信号：多头趋势 + 回踩 EMA20 不破 + 拒绝K线（下影线）
        if ema20[-1] > ema50[-1] and last["low"] <= ema20[-1] and last["close"] > ema20[-1]:
            if lower_wick >= body * 1.5:  # 拒绝K线（下影线）
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != 1:
                    return None
                return {"dir": "long", "entry": entry_ref,
                        "stop": entry_ref - atr_val,
                        "tp": entry_ref + 2 * atr_val,
                        "atr": atr_val}
        # 做空信号：空头趋势 + 反弹 EMA20 不破 + 拒绝K线（上影线）
        upper_wick = last["high"] - max(last["open"], last["close"])
        if ema20[-1] < ema50[-1] and last["high"] >= ema20[-1] and last["close"] < ema20[-1]:
            if upper_wick >= body * 1.5:
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != -1:
                    return None
                return {"dir": "short", "entry": entry_ref,
                        "stop": entry_ref + atr_val,
                        "tp": entry_ref - 2 * atr_val,
                        "atr": atr_val}
        return None

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

        price = sig["entry"]

        # ===== 现货路径（仅现货的美股代币，仅做多，无杠杆，止损由本地监控执行） =====
        if venue == "spot":
            inst = self.exchange.instrument(inst_id)
            qty = floor_to_lot(150 / price, inst.lot_sz)
            if qty <= 0 or (inst.min_sz > 0 and qty < inst.min_sz):
                print(f"⛔ 拒绝开仓 {base}: 数量 {qty} 无效（最小 {inst.min_sz}）")
                return None
            try:
                usdt_free = self.exchange.fetch_balance().usdt_free
                if usdt_free < qty * price:
                    print(f"⛔ 拒绝开仓 {base}: USDT 可用 {usdt_free:.0f} < 所需 {qty*price:.0f}")
                    return None
            except Exception:
                pass
            ok_claim, claim_reason = self.ledger.claim(sym_ledger, "long", "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            res = self.exchange.place_market_order(inst_id, "buy", qty, venue="spot")
            if not res.ok:
                try:
                    self.ledger.release(sym_ledger, "long", "dir", qty, qty * price)
                except Exception:
                    pass
                print(f"❌ 现货开仓失败 {base}: {res.message}")
                return None
            tid = self.journal.log_entry(
                symbol=base, signal="回踩确认",
                reason=f"long {sig['atr']/price*100:.1f}%ATR(现货)",
                entry_price=price, stop_loss=sig["stop"], take_profit=sig["tp"],
                size=qty, direction="long", score=score,
                adopted_lesson_ids=adopted_ids, atr_value=sig["atr"],
                signal_price=sig["entry"], venue="spot")
            msg = (f"🎯 现货开仓 {base} long\n入场 {price:.2f} | 止损 {sig['stop']:.2f} | "
                   f"止盈 {sig['tp']:.2f}\n数量 {qty}（现货无杠杆，止损由本地监控执行）")
            print(msg)
            notify(msg)
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
        qty = min(qty, 150 / price)  # 小仓位：名义上限150USDT
        qty *= size_factor          # 连亏半仓等经验决策真正生效（B6）
        # 市场最大下单量限制（市价单，张→币）
        if inst.max_mkt_sz > 0:
            qty = min(qty, inst.max_mkt_sz * inst.ct_val * 0.9)
        # 精度对齐：向下对齐到合约面值整数倍（不超发）
        qty = floor_to_lot(qty, inst.ct_val)
        # 最小下单量校验：150 USDT 名义买不满最小张数时【拒绝】而不是放大到
        # 最小张数（放大会击穿 150 USDT 小仓位上限，例如 BTC 0.01张=630 USDT）
        min_qty = inst.min_sz * inst.ct_val
        if qty < min_qty:
            print(f"⛔ 拒绝开仓 {base}: 名义 150 USDT 只够 {qty} 币 < 最小 {min_qty} 币"
                  f"（宁可错过，不放大仓位）")
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
            ok_claim, claim_reason = self.ledger.claim(sym_ledger, sig["dir"], "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            res = self.exchange.place_market_order(inst_id, side, qty, venue="swap",
                                                   pos_side=sig["dir"])
            if not res.ok:
                try:
                    self.ledger.release(sym_ledger, sig["dir"], "dir", qty, qty * price)
                except Exception:
                    pass
                print(f"❌ 开仓失败 {base}: {res.message}")
                return None
            # 交易所侧停损单（本地进程崩溃也生效 — OP-1）
            stop_side = "sell" if sig["dir"] == "long" else "buy"
            try:
                sl_res = self.exchange.place_conditional_stop(
                    inst_id, stop_side, qty, sig["dir"], sig["stop"])
                if sl_res.ok:
                    print(f"  🛡️ 已挂交易所侧止损单（原生 slTriggerPx） @ {sig['stop']:.2f}")
                else:
                    print(f"  ⚠️ 交易所侧止损单挂单失败（本地 tick 监控兜底）: {sl_res.message}")
            except ExchangeError as e:
                print(f"  ⚠️ 交易所侧止损单挂单失败（本地 tick 监控兜底）: {e}")
            # R2-5: 止盈挂交易所侧（默认关闭；开启前须通过 tp_sandbox_verify.md 沙盘验证）
            tp_ok = True
            if FLAG_ENABLE_EXCHANGE_TP:
                tp_ok = self._place_tp(base, sig, qty)
            # 记录交易（journal）
            tid = self.journal.log_entry(
                symbol=base, signal="回踩确认", reason=f"{sig['dir']} {sig['atr']/price*100:.1f}%ATR",
                entry_price=price, stop_loss=sig["stop"], take_profit=sig["tp"],
                size=qty, direction=sig["dir"], score=score,
                adopted_lesson_ids=adopted_ids,          # R2-3：本笔实际采纳的经验
                atr_value=sig["atr"], signal_price=sig["entry"],
                venue="swap")  # 合约腿
            # R2-5: TP 挂失败 → 台账打标 tp_missing（本地 monitor 止盈兜底）
            if FLAG_ENABLE_EXCHANGE_TP and not tp_ok:
                for t in self.journal.trades:
                    if t["id"] == tid:
                        t["tp_missing"] = True
                self.journal._save()
                notify(f"⚠️ {base} TP 条件单挂失败（本地 monitor 止盈兜底）")
            msg = (f"🎯 方向性开仓 {base} {sig['dir']}\n"
                   f"入场 {price:.2f} | 止损 {sig['stop']:.2f} | 止盈 {sig['tp']:.2f}\n"
                   f"盈亏比 2:1 | 杠杆 {lev}x | 数量 {qty}")
            print(msg)
            notify(msg)
            return tid
        except Exception as e:
            # R1-12: 下单失败回滚 claim，防账本残留
            try:
                self.ledger.release(sym_ledger, sig["dir"], "dir", qty, qty * price)
            except Exception:
                pass
            print(f"❌ 开仓失败 {base}: {e}")
            return None

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
            return False
        except ExchangeError as e:
            print(f"  ⚠️ TP 挂单失败（本地 monitor 止盈兜底）: {e}")
            return False

    # ---------- 幽灵止损单清理（R1-1） ----------
    def _cancel_stop_orders(self, base, reason=""):
        """取消该 instId 全部 pending algo 单（枚举全部 ordType 查询后合并取消）。
        fail-closed：失败不中断主流程，仅告警。"""
        inst_id = self._inst_id(base, "swap")
        try:
            algo_ids = self.exchange.pending_algo_ids(inst_id)
        except Exception as e:
            notify(f"⚠️ 取消失败(查询) {base} {reason}: {e}")   # fail-closed，不中断
            return False
        if not algo_ids:
            return True
        try:
            return self.exchange.cancel_algos(inst_id, algo_ids)
        except Exception as e:
            notify(f"⚠️ 取消失败 {base} {reason} algoIds={algo_ids}: {e}")
            return False

    # ---------- 监控：止损止盈（tick 级，WebSocket 价格 + REST 兜底） ----------
    def monitor(self):
        """检查持仓，触发止损/止盈则平仓 + 复盘。每 2 秒调用一次。"""
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return
        open_trades = [t for t in self.journal.trades if t["status"] == "open"]
        if not open_trades:
            return
        for t in open_trades:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            inst_id = self._inst_id(base, venue)
            sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            # 价格：WebSocket 实时优先（stale>60s 剔除），REST 兜底
            price = None
            if self.rt is not None:
                price = self.rt.get(base, max_age=60).get("price")
            if not price:
                price = self._ticker_last(base, prefer_swap=(venue == "swap"))
                if price is None:
                    continue
            # 检查止损/止盈（按方向：空头 stop 在入场上方、tp 在下方）
            hit_exit = False
            direction = t.get("direction") or "long"
            if direction == "short":
                hit_exit = (t["stop_loss"] and price >= t["stop_loss"]) or \
                           (t["take_profit"] and price <= t["take_profit"])
            else:
                hit_exit = (t["stop_loss"] and price <= t["stop_loss"]) or \
                           (t["take_profit"] and price >= t["take_profit"])
            if hit_exit:
                # ===== 现货路径（美股代币）：按余额持有量卖出现货平仓 =====
                if venue == "spot":
                    try:
                        held = self.exchange.spot_holding(base)
                        if held >= float(t["size"]) * 0.99:
                            res = self.exchange.place_market_order(
                                inst_id, "sell", abs(float(t["size"])), venue="spot")
                            if not res.ok:
                                print(f"  现货平仓失败: {res.message}")
                                continue
                            self.ledger.release(sym_ledger, "long", "dir",
                                                float(t["size"]),
                                                float(t["size"]) * float(t.get("entry_price") or 0))
                        else:
                            # 现货已被外部卖光 → 按当前价记账平仓
                            print(f"  {base} 现货已不在账户（外部平仓），按现价记账")
                    except Exception as e:
                        print(f"  现货平仓失败: {e}")
                        continue
                    closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                    if closed:
                        self._post_close_review(closed, t)
                    continue
                # ===== 合约路径（原有） =====
                # 平仓（R1-12 最小止血：按本策略 journal 数量平 + reduceOnly，
                # 不再按交易所合并持仓全额平——防止误平同 symbol 同 posSide 的套利腿）
                pos = next((p for p in positions
                            if p.inst_id == inst_id and p.base_qty > 0), None)
                if pos:
                    try:
                        side = "sell" if pos.side == "long" else "buy"
                        close_qty = min(abs(float(t["size"])), pos.base_qty)
                        res = self.exchange.place_market_order(
                            inst_id, side, close_qty, venue="swap",
                            pos_side=pos.side, reduce_only=True)
                        if not res.ok:
                            print(f"平仓失败: {res.message}")
                            continue
                        # R1-1：平仓成功后取消交易所侧条件停损单（防幽灵单残留）
                        self._cancel_stop_orders(base, "止损/止盈平仓")
                        # R1-12：释放账本认领
                        try:
                            self.ledger.release(sym_ledger, t.get("direction") or "long", "dir",
                                                float(t["size"]),
                                                float(t["size"]) * float(t.get("entry_price") or 0))
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"平仓失败: {e}")
                        continue
                closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                if closed:
                    self._post_close_review(closed, t)

    def _post_close_review(self, closed, t):
        """平仓后复盘链（R1-1/B6/R2-3/R2-6）：deep_review → 经验库 → 采纳验证 → 阈值 → 风控净值。"""
        base = closed["symbol"]
        report = deep_review(closed,
                             atr_value=t.get("atr_value"),
                             signal_price=t.get("signal_price"))  # R2-6
        lessons = report.get("lessons", [])
        for l in lessons:
            self.exp_bank.add(base, l["category"], l["lesson"], t["id"])
        # R2-3：只 validate 本笔实际采纳的经验（替换全量 trusted validate 回声）
        for lid in t.get("adopted_lesson_ids") or []:
            self.exp_bank.validate(lid, closed["pnl"])
        # 阈值自适应：记录本次【真实】决策分数 + 结果，校准阈值
        score = t.get("score") or SIGNAL_SCORE
        self.threshold_learner.record(score, closed["pnl"])
        # 账户级风控：净值更新（平仓后）
        try:
            eq = self.exchange.fetch_balance().usdt_total
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
        except Exception:
            pass
        msg = (f"📊 平仓 {base}: 盈亏 {closed['pnl']*100:+.1f}%\n"
               f"复盘 {len(lessons)} 条新经验（待验证），"
               f"验证了 {len(t.get('adopted_lesson_ids') or [])} 条本笔采纳经验\n"
               f"当前自适应阈值: {self.threshold_learner.threshold}")
        print(msg)
        notify(msg)

    # ---------- 熔断强平（只平本策略的 journal 持仓，不动套利对冲仓） ----------
    def _liquidate_all(self, reason):
        """风控熔断时强制平掉本策略所有持仓。"""
        for t in [x for x in self.journal.trades if x["status"] == "open"]:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            inst_id = self._inst_id(base, venue)
            sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            # ===== 现货路径（美股代币）：卖出现货强平 =====
            if venue == "spot":
                try:
                    held = self.exchange.spot_holding(base)
                    if held >= float(t["size"]) * 0.99:
                        res = self.exchange.place_market_order(
                            inst_id, "sell", abs(float(t["size"])), venue="spot")
                        if res.ok:
                            self.ledger.release(sym_ledger, "long", "dir",
                                                float(t["size"]),
                                                float(t["size"]) * float(t.get("entry_price") or 0))
                    px = self._ticker_last(base)
                    if px is None:
                        print(f"强平失败 {base}: 无法获取价格")
                        continue
                    self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
                except Exception as e:
                    print(f"强平失败 {base}: {e}")
                continue
            # ===== 合约路径（原有） =====
            try:
                positions = self.exchange.fetch_positions()
                pos = next((p for p in positions
                            if p.inst_id == inst_id and p.base_qty > 0
                            and p.side == (t.get("direction") or "long")), None)
                if pos:
                    side = "sell" if pos.side == "long" else "buy"
                    # R1-12 最小止血：按 t["size"] + reduceOnly，只减本策略部分
                    close_qty = min(abs(float(t["size"])), pos.base_qty)
                    res = self.exchange.place_market_order(
                        inst_id, side, close_qty, venue="swap",
                        pos_side=pos.side, reduce_only=True)
                    if res.ok:
                        # R1-1：强平后取消交易所侧条件停损单
                        self._cancel_stop_orders(base, "熔断强平")
                        # R1-12：释放账本认领
                        try:
                            self.ledger.release(sym_ledger, t.get("direction") or "long", "dir",
                                                float(t["size"]),
                                                float(t["size"]) * float(t.get("entry_price") or 0))
                        except Exception:
                            pass
                px = self._ticker_last(base, prefer_swap=True)
                if px is None:
                    print(f"强平失败 {base}: 无法获取价格")
                    continue
                self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
            except Exception as e:
                print(f"强平失败 {base}: {e}")

    # ---------- 主循环 ----------
    def _trade_budget(self, base):
        """该币今日允许笔数：按当日扫描评分动态给（用户要求：看币动态调整笔数）。"""
        from engines.daily_scan import trades_budget
        return trades_budget(self.watch_scores.get(base))

    def scan_signals(self):
        """扫一轮候选池信号（每 15 分钟，日内短线）。
        频率约束（用户要求：看币动态调整笔数）：每个币每天的允许笔数按其当日
        评分动态给（评分越高越值得多给机会）+ 同币信号冷却 SIGNAL_COOLDOWN_MINUTES。"""
        # 每日刷新候选池（跨天自动重扫全市场）
        if time.time() - self._last_watch_refresh >= 24 * 3600 or \
                time.strftime("%Y-%m-%d") != getattr(self, "_watch_date", ""):
            try:
                from engines.daily_scan import screen_daily
                w = screen_daily()
                if w:
                    self.watchlist = [c["base"] for c in w]
                    self.watch_scores = {c["base"]: c["score"] for c in w}
                    self._watch_date = time.strftime("%Y-%m-%d")
                    self._last_watch_refresh = time.time()
                    notify(f"🔍 每日候选池刷新: {self.watchlist}")
            except Exception as e:
                print(f"候选池刷新失败，沿用旧池: {e}")
        print(f"\n=== 方向性信号扫描 [{time.strftime('%H:%M:%S')}] 候选池 {len(self.watchlist)} 个 ===")
        today = time.strftime("%Y-%m-%d")
        for base in self.watchlist:
            # 0. 动态笔数：该币今天已开几笔？按当日评分给额度（看币动态调整）
            opened_base = [t for t in self.journal.trades
                           if t.get("symbol") == base and t.get("entry_time")
                           and time.strftime("%Y-%m-%d", time.localtime(t["entry_time"])) == today]
            budget = self._trade_budget(base)
            if len(opened_base) >= budget:
                print(f"⏸️ {base}: 今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}（评分给额），跳过")
                continue
            # 1. 同币信号冷却（3 小时，1h 线 3 根K线）
            if time.time() - self.signal_cool.get(base, 0) < SIGNAL_COOLDOWN_MINUTES * 60:
                continue
            sig = self.scan_signal(base)
            if sig:
                self.signal_cool[base] = time.time()
                # 阈值决策：用自适应阈值（审计 CR-6：此前硬编码 80、与阈值无关）
                if SIGNAL_SCORE < self.threshold_learner.threshold:
                    print(f"{base}: 信号分 {SIGNAL_SCORE} < 自适应阈值 "
                          f"{self.threshold_learner.threshold}，观望")
                    continue
                # 决策（经验库，统一 ScoredExperience — B6）
                dec = self.evolver.decide(base, SIGNAL_SCORE, "回踩确认", 0, 0, 0.02, 0.05, 0)
                if dec["trade"]:
                    self.open_position(base, sig, score=SIGNAL_SCORE,
                                       stop_adj=dec.get("stop_adj", 0.0),
                                       size_factor=dec.get("size_factor", 1.0),
                                       adopted_ids=dec.get("adopted_lesson_ids", []))
                else:
                    print(f"{base}: 有信号但拒绝 - {'; '.join(dec['reason'])}")
            else:
                print(f"{base}: 无回踩确认信号")

    def run_once(self):
        self.scan_signals()
        self.monitor()

    def tick(self):
        """单拍主循环体（服务模式由 service/worker 线程调用；独立模式由 run() 调用）。
        包含：心跳、每分钟账户风控、2s 止损监控、15min 信号扫描。"""
        now = time.time()
        # R2-4: 心跳（watchdog 超时 30s 判定）
        with open("heartbeat_directional.txt", "w") as f:
            f.write(str(now))
        # 0. 账户级风控：净值喂入 + 熔断检查（每分钟 — 审计 CR-2）
        if now - self._last_risk_update >= 60:
            self._last_risk_update = now
            eq = self.exchange.fetch_balance().usdt_total
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
            if not self.risk.can_trade():
                if not self._halt_notified:
                    self._halt_notified = True
                    msg = (f"⛔ 风控熔断: {self.risk.halt_reason}\n"
                           f"正在强制平掉本策略全部持仓…")
                    print(msg)
                    notify(msg)
                    self._liquidate_all(self.risk.halt_reason)
                time.sleep(2)
                return
            elif self._halt_notified:
                self._halt_notified = False
                notify("✅ 风控解除，恢复信号扫描")
        # 1. tick 级止损止盈监控（每 2 秒 — OP-1，替代 6 小时轮询）
        self.monitor()
        # 2. 信号扫描（每 15 分钟 — 真日内短线）
        if now - self._last_scan >= 15 * 60:
            self._last_scan = now
            self.scan_signals()

    def run(self):
        notify("🎯 方向性交易 agent 启动（回踩确认 + 2:1盈亏比 + 2-3x杠杆 + tick级止损）")
        # R2-4: PID + 心跳文件（watchdog 用）
        with open("directional_trader.pid", "w") as f:
            f.write(str(os.getpid()))
        self._last_scan = 0
        self._last_risk_update = 0
        self.signal_cool = {}   # 同币信号冷却（SIGNAL_COOLDOWN_MINUTES）
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"异常: {e}")
            time.sleep(2)


if __name__ == "__main__":
    dt = DirectionalTrader()
    if "--once" in sys.argv:
        dt.run_once()
    else:
        dt.run()
