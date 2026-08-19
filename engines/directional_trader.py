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
import uuid
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
from exchange.models import floor_to_lot, OrderResult
from exchange.okx_adapter import make_cl_ord_id

LARK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lark")
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"
# 策略参数统一维护于 config.py（2026-08-16 用户指示: 数值不再分散）——
# 本模块只保留 config 引用别名、不私藏任何参数副本。
LEVERAGE_MAP = config.LEVERAGE_MAP
SYMBOLS = config.SYMBOLS
SIGNAL_SCORE = config.SIGNAL_SCORE          # 回踩确认信号基础分
RISK_PER_TRADE = config.RISK_PER_TRADE      # 单笔风险 1%
RR_RATIO = config.TP_ATR_MULT / config.STOP_ATR_MULT  # 2:1 盈亏比
FLAG_ENABLE_EXCHANGE_TP = config.FLAG_ENABLE_EXCHANGE_TP
FLAG_USE_SHADOW_SCORE_GATE = config.FLAG_USE_SHADOW_SCORE_GATE


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


def acquire_instance_lock(timeout=300.0):
    """单实例互斥(审计:双实例事故):对项目根 engine.lock 加 flock。
    已被持有 → 等待持有者退出(热备用),超时仍未拿到 → 返回 None。
    拿到 → 返回文件句柄(进程存续期间持锁,退出自动释放)。"""
    lock_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "engine.lock")
    try:
        import fcntl
    except ImportError:
        return open(lock_path, "w")   # 平台无 flock:退化为普通文件
    deadline = time.time() + timeout
    while True:
        try:
            f = open(lock_path, "a+")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
            return f
        except OSError:
            try:
                f.close()
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(2)


def _build_trade_conditions(sig):
    """信号/交易的场景条件向量(2026-08-17): direction + vol_band + trend +
    signal_type。regime 是 compute_regime 输出的 dict(含 tag/trend_slope)。"""
    from decision.experience_scoring import build_conditions
    return build_conditions(direction=sig.get("dir"),
                            regime_dict=sig.get("regime"),
                            signal_type="pullback")


class _ExpAdapter:
    """把 ScoredExperience 适配成 SelfEvolvingTrader 期望的 ExperienceBank 接口。
    （审计 B6：此前两套经验库并存，evolver 用旧库、交易记录用新库，闭环断裂）"""

    def __init__(self, bank):
        self.bank = bank

    def relevant(self, symbol=None, category=None, conditions=None):
        # R2-3：只返回 trusted（带 id，供采纳追踪）；discarded 走 discarded() 单独查
        # Phase 4：system 级教训（symbol='*'，analyst 日度看账产出）也进入决策参考
        # 2026-08-17: 场景条件向量匹配 + 验证计数透传(聚合强度计算用)
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"], "good": l.get("good", 0),
                "bad": l.get("bad", 0), "regime": l.get("regime"),
                "conditions": l.get("conditions")}
               for l in self.bank.trusted(symbol, conditions=conditions)]
        out += [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                 "lesson": l["content"], "good": l.get("good", 0),
                 "bad": l.get("bad", 0), "regime": l.get("regime"),
                 "conditions": l.get("conditions")}
                for l in self.bank.trusted(None, conditions=conditions)
                if l.get("symbol") == "*"]
        if category:
            out = [l for l in out if l["category"] == category]
        return out

    def discarded(self, symbol=None, category=None, conditions=None):
        """被证伪的经验（3 次验证且 <40 分）。信号模式失效检查必须读这里——
        trusted 语义是'证明有用'，信号失效教训经亏损验证后只会进 discarded。"""
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"]}
               for l in self.bank.discarded(symbol, conditions=conditions)]
        if category:
            out = [l for l in out if l["category"] == category]
        return out

    def candidates(self, symbol=None, category=None):
        """待验证候选经验（Phase0 T0.2：平仓复盘一致性初筛通过、等待后续独立
        交易验证）。决策层只做低权重参考（写入理由+采纳追踪），不改变任何参数——
        这是打破 unverified 死锁的采纳通道（见设计文档 v0.2 §5.1 Q3）。"""
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"]}
               for l in self.bank.candidates(symbol)]
        if category:
            out = [l for l in out if l["category"] == category]
        return out


class DirectionalTrader:
    def __init__(self, exchange: ExchangeAdapter = None, rt=None, db_path=None):
        self.exchange = exchange or connect()
        # 2026-08-16 结构性修复: 测试/fake 适配器必须静音飞书通知——
        # 此前 test_decision_loop 等跑套件时把假开仓单真的发到了用户飞书
        # (与 DEF-8 生产库污染同类的泄漏,这次是通知通道)。
        self._notify = notify if getattr(self.exchange, "name", "") == "okx" \
            else (lambda msg: None)
        # Phase0 T0.4：审计/日志表隔离。db_path=None → 生产共享库（默认）；
        # 测试必须传隔离路径（防 scan_decisions 等污染生产表，见 pitfalls）。
        self._db_path = db_path
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
        # 2026-08-16 用户指示采集加速: 信号分门槛降到 50,阈值初始 70→45
        # (若保持 70,50<70 会让全部信号被拒)。影子分喂入后满 30 样本仍可校准。
        self.threshold_learner = ThresholdLearner(path="threshold_state_dir.json",
                                                  initial_threshold=config.THRESHOLD_INITIAL)
        # 账户级风控（审计 CR-2：RiskManager 必须真正接线）
        from risk.risk_manager import RiskManager
        try:
            eq = self.exchange.fetch_balance().total_eq
        except Exception:
            eq = 0
        self.risk = RiskManager(initial_equity=eq if eq > 0 else 4190)
        self._halt_notified = False
        # 审计 C1/H2:启动对账——交易所持仓 ∪ 未平仓 journal 为唯一事实源,
        # 幽灵 claim 物理释放;无台账的交易所持仓仅告警(不自动下单)。
        if getattr(self.exchange, "name", "") == "okx":
            self._reconcile_startup()
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

    def _reconcile_startup(self):
        """启动对账(审计 C1/H2):幽灵 claim 物理释放;无台账持仓告警。"""
        try:
            positions = self.exchange.fetch_positions()
        except Exception as e:
            print(f"启动对账:无法获取交易所持仓,跳过: {e}")
            return
        open_journal = [t for t in self.journal.trades if t.get("status") == "open"]
        active = set()
        for p in positions:
            if p.base_qty <= 0:
                continue
            active.add(f"{p.base}/USDT:USDT:{p.side}")
        for t in open_journal:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            sym = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            active.add(f"{sym}:{t.get('direction') or 'long'}")
        released = self.ledger.reconcile(active)
        for k in released:
            print(f"🧹 启动对账:释放幽灵 claim {k}")
            self._notify(f"🧹 启动对账:释放幽灵 claim {k}")
        # DEF-11:账本缺失/不完整的未平仓 journal 交易补账——journal 是本策略
        # 事实源(pitfalls),重启后账本若丢 claim,组合敞口闸门会漏计既有持仓。
        # restore() 不走 600 闸门(恢复既有事实而非授予新敞口),语义=以聚合值
        # 覆盖(幂等);同 symbol+side 的多笔交易共享同一 key,必须先聚合再补。
        pending = {}
        for t in open_journal:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            sym = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            key = f"{sym}:{t.get('direction') or 'long'}"
            qty = float(t.get("size") or 0)
            notional = float(t.get("notional_usdt")
                             or (qty * float(t.get("entry_price") or 0)))
            if qty <= 0:
                continue
            p = pending.setdefault(key, {"qty": 0.0, "notional": 0.0})
            p["qty"] += qty
            p["notional"] += notional
        for key, p in pending.items():
            sym, side = key.rsplit(":", 1)
            # 以 journal 聚合值为准:部分补账残留也会被覆盖修正
            self.ledger.restore(sym, side, "dir", p["qty"], p["notional"])
            print(f"🔧 启动对账:补账 {key} qty={p['qty']} notional={p['notional']:.0f}")
        for p in positions:
            if p.base_qty <= 0:
                continue
            has_journal = any(
                t["symbol"] == p.base and (t.get("direction") or "long") == p.side
                for t in open_journal)
            if not has_journal:
                print(f"⚠️ 启动对账:交易所存在无台账持仓 {p.inst_id} {p.side} "
                      f"qty={p.base_qty}(仅告警,人工处置)")
                self._notify(f"⚠️ 启动对账:无台账持仓 {p.inst_id} {p.side} qty={p.base_qty}")

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
        upper_wick = last["high"] - max(last["open"], last["close"])
        # 日内入场参考价用实时 tick 价（市价单实际成交价），
        # 趋势/ATR 仍来自 1 小时线——避免"信号收盘价 vs 市价成交"错位（RES-11）
        entry_ref = self._ticker_last(base)
        if entry_ref is None:
            entry_ref = last["close"]

        def _shadow(price_near_ema, wick):
            """Phase 1 T1.3 影子连续分（0-100，只记录、不进决策——阈值逻辑零改动）:
            拒绝K线强度 34% + 回踩深度适中 33% + 1h 趋势离散度 33%。
            同时采集轻量 regime 标签（T1.4:波动率分位+趋势斜率,不用 HMM）。"""
            try:
                wick_s = min(wick / body, 3.0) / 3.0 if body > 0 else 0.0
                depth_s = (max(0.0, 1.0 - abs(price_near_ema - ema20[-1]) / atr_val)
                           if atr_val and atr_val > 0 else 0.0)
                trend_s = (min(abs(ema20[-1] - ema50[-1]) / (ema50[-1] * 0.02), 1.0)
                           if ema50[-1] else 0.0)
                score = round(100 * (0.34 * wick_s + 0.33 * depth_s
                                     + 0.33 * trend_s), 1)
                reg = None
                try:
                    from engines.feature_collector import compute_regime
                    reg = compute_regime(klines, locals().get("c4"))
                except Exception:
                    reg = None
                return score, reg
            except Exception:
                return None, None

        # 做多信号：多头趋势 + 回踩 EMA20 不破 + 拒绝K线（下影线）
        if ema20[-1] > ema50[-1] and last["low"] <= ema20[-1] and last["close"] > ema20[-1]:
            if lower_wick >= body * config.REJECT_WICK_RATIO:  # 拒绝K线（下影线）
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != 1:
                    return None
                score, regime = _shadow(last["low"], lower_wick)
                return {"dir": "long", "entry": entry_ref,
                        "stop": entry_ref - config.STOP_ATR_MULT * atr_val,
                        "tp": entry_ref + config.TP_ATR_MULT * atr_val,
                        "atr": atr_val,
                        "shadow_score": score, "regime": regime}  # Phase1 影子
        # 做空信号：空头趋势 + 反弹 EMA20 不破 + 拒绝K线（上影线）
        if ema20[-1] < ema50[-1] and last["high"] >= ema20[-1] and last["close"] < ema20[-1]:
            if upper_wick >= body * config.REJECT_WICK_RATIO:
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != -1:
                    return None
                score, regime = _shadow(last["high"], upper_wick)
                return {"dir": "short", "entry": entry_ref,
                        "stop": entry_ref + config.STOP_ATR_MULT * atr_val,
                        "tp": entry_ref - config.TP_ATR_MULT * atr_val,
                        "atr": atr_val,
                        "shadow_score": score, "regime": regime}  # Phase1 影子
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
            ok_claim, claim_reason = self.ledger.claim(sym_ledger, "long", "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            cl_ord_id = make_cl_ord_id()
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
            msg = (f"🎯 现货开仓 {base} long\n入场 {price:.2f} | 止损 {sig['stop']:.2f} | "
                   f"止盈 {sig['tp']:.2f}\n数量 {qty}（现货无杠杆，止损由本地监控执行）")
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
        # 精度对齐：向下对齐到合约面值整数倍（不超发）
        qty = floor_to_lot(qty, inst.ct_val)
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
            ok_claim, claim_reason = self.ledger.claim(sym_ledger, sig["dir"], "dir", qty, qty * price)
            if not ok_claim:
                print(f"⛔ 拒绝开仓 {base}: {claim_reason}")
                return None
            cl_ord_id = make_cl_ord_id()
            try:
                res = self.exchange.place_market_order(inst_id, side, qty, venue="swap",
                                                       pos_side=sig["dir"],
                                                       cl_ord_id=cl_ord_id)
            except ExchangeError as e:
                # 审计 C1:网络错误可能已成交 → 用 clOrdId 反查,已成交继续走止损/记账
                res = self._recover_order(inst_id, cl_ord_id, qty, e)
            if not res.ok:
                try:
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
            msg = (f"🎯 方向性开仓 {base} {sig['dir']}\n"
                   f"入场 {price:.2f} | 止损 {sig['stop']:.2f} | 止盈 {sig['tp']:.2f}\n"
                   f"盈亏比 2:1 | 杠杆 {lev}x | 数量 {qty}")
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

    # ---------- 监控：止损止盈（tick 级，WebSocket 价格 + REST 兜底） ----------
    def monitor(self):
        """检查持仓，触发止损/止盈则平仓 + 复盘。每 1 秒调用一次。
        2026-08-17 提速: WS 价格检查每拍执行(秒级);交易所持仓 REST 快照
        每 2 秒刷新一次(避免 1s 节拍的 REST 频率过高);快照未刷新的拍次
        只做价格判定,平仓执行等下一拍持仓就绪(≤1s 延迟)。"""
        now = time.time()
        if now - getattr(self, "_last_pos_fetch", 0) >= 2.0:
            self._last_pos_fetch = now
            try:
                positions = self.exchange.fetch_positions()
            except Exception:
                positions = None
        else:
            positions = None
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
                                self._log_order_failure(base, inst_id, "sell",
                                                        abs(float(t["size"])),
                                                        "close", res.message)
                                continue
                        else:
                            # 现货已被外部卖光 → 按当前价记账平仓
                            print(f"  {base} 现货已不在账户（外部平仓），按现价记账")
                        # 账本认领释放与台账闭环同层(2026-08-17 同合约路径修复:
                        # 外部已平仓时跳过释放会造成 H2 对账失败)
                        self.ledger.release(sym_ledger, "long", "dir",
                                            float(t["size"]),
                                            float(t["size"]) * float(t.get("entry_price") or 0))
                    except Exception as e:
                        print(f"  现货平仓失败: {e}")
                        self._log_order_failure(base, inst_id, "sell", qty, "close", e)
                        continue
                    closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                    if closed:
                        self._post_close_review(closed, t)
                    continue
                # ===== 合约路径（原有） =====
                # 平仓（R1-12 最小止血：按本策略 journal 数量平 + reduceOnly，
                # 不再按交易所合并持仓全额平——防止误平同 symbol 同 posSide 的套利腿）
                if positions is None:
                    continue   # 持仓快照未刷新拍: 下一拍(≤1s)执行平仓
                pos = next((p for p in positions
                            if p.inst_id == inst_id and p.base_qty > 0
                            and p.side == (t.get("direction") or "long")), None)
                if pos:
                    try:
                        side = "sell" if pos.side == "long" else "buy"
                        close_qty = min(abs(float(t["size"])), pos.base_qty)
                        res = self.exchange.place_market_order(
                            inst_id, side, close_qty, venue="swap",
                            pos_side=pos.side, reduce_only=True)
                        if not res.ok:
                            print(f"平仓失败: {res.message}")
                            self._log_order_failure(base, inst_id, side, close_qty, "close", res.message)
                            continue
                        # R1-1：平仓成功后取消交易所侧条件停损单（防幽灵单残留）
                        self._cancel_stop_orders(base, "止损/止盈平仓")
                    except Exception as e:
                        print(f"平仓失败: {e}")
                        self._log_order_failure(base, inst_id, "close", 0, "close", e)
                        continue
                # R1-12：释放账本认领（2026-08-17 修复: 此前只在 `if pos` 分支内
                # 释放,交易所侧条件单已平仓时 pos=None → 跳过分支 → 认领永存 →
                # H2 对账失败(ADA/LTC 双双中招)。台账闭环就必须释放,与谁执行
                # 平仓无关——引擎平仓成功 or 交易所条件单已平,都要释放)。
                try:
                    self.ledger.release(sym_ledger, t.get("direction") or "long", "dir",
                                        float(t["size"]),
                                        float(t["size"]) * float(t.get("entry_price") or 0))
                except Exception:
                    pass
                closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                if closed:
                    self._post_close_review(closed, t)

    def _post_close_review(self, closed, t):
        """平仓后复盘链（R1-1/B6/R2-3/R2-6 + Phase0 T0.1/T0.2/T0.3）：
        deep_review → 插针反转采集 → 报告落盘 → 教训一致性初筛（candidate/dubious）
        → 经验库 → 采纳验证 → 阈值 → 风控净值。"""
        base = closed["symbol"]
        # Phase0 T0.3：止损出场后采集"是否反转"（post_exit_reverse，原死参数）。
        # 语义：long 止损后现价回到止损上方 / short 回到下方 = 被插针扫掉。
        post_rev = None
        try:
            if closed.get("exit_reason") and "止损" in closed["exit_reason"]:
                venue = t.get("venue") or "swap"
                last = self._ticker_last(base, prefer_swap=(venue == "swap"))
                stop = t.get("stop_loss")
                if last and stop:
                    direction = t.get("direction") or "long"
                    post_rev = (last > stop) if direction == "long" else (last < stop)
        except Exception:
            post_rev = None
        report = deep_review(closed,
                             atr_value=t.get("atr_value"),
                             signal_price=t.get("signal_price"),
                             post_exit_reverse=post_rev)   # Phase0 T0.3
        # 复盘报告落盘（写入 trade_journal.json 该笔记录的 review 字段，事后可查）
        self.journal.save_review(t["id"], report)
        # Phase 1: 离场特征落库（MFE/MAE 由 1m K 线高低点计算;采集失败不影响复盘链）
        try:
            kl1m = self._fetch_klines_any(base, "1m", 500)
            from engines.feature_collector import collect_close_features
            collect_close_features(t["id"], t, closed, kl1m, post_rev,
                                   db_path=self._db_path)
        except Exception:
            pass
        lessons = report.get("lessons", [])
        pnl = closed.get("pnl")
        # Phase 4: 教训带 regime 标签(取自本笔入场特征),供同环境结构化匹配
        regime_tag = None
        trend_slope = None
        try:
            import storage.db as sdb
            row = sdb.q1("SELECT regime_tag, trend_slope FROM trade_features "
                         "WHERE trade_id=?", [t["id"]], db_path=self._db_path)
            if row:
                regime_tag = row["regime_tag"]
                trend_slope = row.get("trend_slope")
        except Exception:
            regime_tag = None
        # 2026-08-17 场景条件向量: 方向+波动带+趋势+信号类型,与决策层逐维
        # 匹配(缺失维度通配)。regime 字段保留兼容旧数据。
        lesson_conditions = {
            "direction": t.get("direction") or "long",
            "vol_band": regime_tag or "",
            "trend": ("" if trend_slope is None
                      else "up" if trend_slope > 0.0005
                      else "down" if trend_slope < -0.0005 else "flat"),
            "signal_type": "pullback",
        }
        for l in lessons:
            # Phase0 T0.2 一级·一致性初筛：教训的归因方向（implies）与本笔结果一致
            # → candidate（可被决策层低权重采纳）；不一致 → dubious（不进采纳池）。
            # 注意：这只是初筛，不做评分——评分只来自后续独立交易（防循环论证，见
            # 设计文档 v0.2 §5.1 Q3 与 experience_scoring.validate）。
            implies = l.get("implies")
            if implies:
                consistent = (pnl > 0) if implies == "win" else (pnl < 0)
                status = "candidate" if consistent else "dubious"
            else:
                status = "unverified"
            self.exp_bank.add(base, l["category"], l["lesson"], t["id"],
                              status=status, regime=regime_tag,
                              conditions=lesson_conditions)
        # R2-3：只 validate 本笔实际采纳的经验（替换全量 trusted validate 回声）
        for lid in t.get("adopted_lesson_ids") or []:
            self.exp_bank.validate(lid, closed["pnl"])
        # 阈值自适应：记录本次【真实】决策分数 + 结果，校准阈值
        score = t.get("score") or SIGNAL_SCORE
        self.threshold_learner.record(score, closed["pnl"])
        # 账户级风控：净值更新（平仓后）
        try:
            eq = self.exchange.fetch_balance().total_eq
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
        except Exception:
            pass
        msg = (f"📊 平仓 {base}: 盈亏 {closed['pnl']*100:+.1f}%\n"
               f"复盘 {len(lessons)} 条新经验（待验证），"
               f"验证了 {len(t.get('adopted_lesson_ids') or [])} 条本笔采纳经验\n"
               f"当前自适应阈值: {self.threshold_learner.threshold}")
        print(msg)
        self._notify(msg)

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
                        if not res.ok:
                            print(f"现货强平失败 {base}: {res.message}（保持 open，下轮重试）")
                            continue
                        self.ledger.release(sym_ledger, "long", "dir",
                                            float(t["size"]),
                                            float(t["size"]) * float(t.get("entry_price") or 0))
                    px = self._ticker_last(base)
                    if px is None:
                        print(f"强平失败 {base}: 无法获取价格")
                        continue
                    closed = self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
                    # Phase0 T0.1：熔断强平同样走复盘链（DEF-1——此前唯一平仓
                    # 样本零复盘）。review 落盘 + 教训初筛 + 采纳验证 + 阈值。
                    if closed:
                        self._post_close_review(closed, t)
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
                    if not res.ok:
                        # 审计 H4:平仓失败不得落账"已平"——保持 open,下一轮重试
                        print(f"强平失败 {base}: {res.message}（保持 open，下轮重试）")
                        continue
                    # R1-1：强平后取消交易所侧条件停损单
                    self._cancel_stop_orders(base, "熔断强平")
                else:
                    # 交易所已无该持仓(可能被条件单平掉):闭环台账即可
                    print(f"{base}: 交易所已无持仓(可能已由条件单平仓)，仅闭环台账")
                # 账本释放：无论交易所持仓是否存在都必须执行（journal 是本策略事实源；
                # 此前放在 if pos 分支内，持仓已归零时跳过 → 账本残留，见 pitfalls）
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
                closed = self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
                # Phase0 T0.1：熔断强平同样走复盘链（DEF-1）。review 落盘 +
                # 教训初筛 + 采纳验证 + 阈值。
                if closed:
                    self._post_close_review(closed, t)
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
        # 每日刷新候选池（跨天自动重扫全市场）——screen_daily 耗时 1-2 分钟,
        # 期间心跳停更会被 watchdog 误杀:先刷一次心跳再进阻塞段(2026-08-16 事故)。
        if time.time() - self._last_watch_refresh >= 24 * 3600 or \
                time.strftime("%Y-%m-%d") != getattr(self, "_watch_date", ""):
            try:
                from execution.pidfile import write_heartbeat
                write_heartbeat("directional")
                from engines.daily_scan import screen_daily
                # 2026-08-17: 全市场筛选同样逐币插拍监控/心跳/tick——网络慢时
                # 60 币筛选会阻塞主循环数十分钟(今晚 23:08 复现: tick 卡 5 分钟
                # 停更,H9 报警),与 51 分钟盲窗同源。回调 = 每币一次。
                w = screen_daily(progress_cb=self._long_scan_progress)
                if w:
                    self.watchlist = [c["base"] for c in w]
                    self.watch_scores = {c["base"]: c["score"] for c in w}
                    self._watch_date = time.strftime("%Y-%m-%d")
                    self._last_watch_refresh = time.time()
                    self._notify(f"🔍 每日候选池刷新: {self.watchlist}")
            except Exception as e:
                print(f"候选池刷新失败，沿用旧池: {e}")
        # 2026-08-16 采集加速（用户指示）：扫描池 = 当日候选池 ∪ 回退主流池
        # （10 个主流币始终参与信号扫描,额度/冷却约束照常适用）
        scan_pool = list(dict.fromkeys(
            self.watchlist + [s for s in SYMBOLS if s not in self.watchlist]))
        print(f"\n=== 方向性信号扫描 [{time.strftime('%H:%M:%S')}] "
              f"候选池 {len(self.watchlist)} 个 + 回退池 {len(scan_pool) - len(self.watchlist)} 个 ===")
        today = time.strftime("%Y-%m-%d")
        for base in scan_pool:
            # 2026-08-16: 长扫描期间每币刷新心跳——18 币扫描需数分钟,
            # 心跳停更 >30s 会被 watchdog 误杀（exit -15 崩溃循环事故）。
            # 2026-08-17 事故: 网络黑洞让 20 币扫描 × 30s 超时阻塞主循环 51 分钟,
            # 期间 tick() 无法执行 → 止损监控失明。逐币插拍(监控+心跳+tick+
            # 60s 仓位快照): 盲窗≤单币网络超时,慢速但有进展的扫描不算卡死。
            self._long_scan_progress()
            # 0. 动态笔数：该币今天已开几笔？按当日评分给额度（看币动态调整）
            opened_base = [t for t in self.journal.trades
                           if t.get("symbol") == base and t.get("entry_time")
                           and time.strftime("%Y-%m-%d", time.localtime(t["entry_time"])) == today]
            budget = self._trade_budget(base)
            if len(opened_base) >= budget:
                print(f"⏸️ {base}: 今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}（评分给额），跳过")
                self._log_scan_decision(base, False, "", "budget",
                                        f"今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}")
                continue
            # 1. 同币信号冷却（3 小时，1h 线 3 根K线）
            if time.time() - self.signal_cool.get(base, 0) < SIGNAL_COOLDOWN_MINUTES * 60:
                self._log_scan_decision(base, False, "", "cooldown", "信号冷却中")
                continue
            sig = self.scan_signal(base)
            if sig:
                self.signal_cool[base] = time.time()
                # 阈值决策（审计 CR-6 + Phase3 T3.1）：默认用常量 SIGNAL_SCORE 卡门槛
                # （影子分未过假设 A3 检验前不得影响决策——防过拟合红线）；
                # FLAG_USE_SHADOW_SCORE_GATE=True 在 A3 通过后由人工开启。
                gate_score = SIGNAL_SCORE
                if FLAG_USE_SHADOW_SCORE_GATE:
                    gate_score = sig.get("shadow_score")
                    if gate_score is None:
                        gate_score = SIGNAL_SCORE
                if gate_score < self.threshold_learner.threshold:
                    print(f"{base}: 信号分 {gate_score} < 自适应阈值 "
                          f"{self.threshold_learner.threshold}，观望")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            f"信号分 {gate_score} < 阈值 {self.threshold_learner.threshold}")
                    continue
                # 决策（经验库，统一 ScoredExperience — B6）
                dec = self.evolver.decide(base, SIGNAL_SCORE, "回踩确认", 0, 0, 0.02, 0.05, 0,
                                          journal=self.journal,
                                          conditions=_build_trade_conditions(sig))
                if dec["trade"]:
                    self._log_scan_decision(base, True, sig["dir"], "open",
                                            "; ".join(dec.get("reason") or ["信号达标"]))
                    # Phase3 T3.1: journal 记影子分(供阈值学习喂分),门控仍由常量负责
                    self.open_position(base, sig,
                                       score=sig.get("shadow_score") or SIGNAL_SCORE,
                                       stop_adj=dec.get("stop_adj", 0.0),
                                       size_factor=dec.get("size_factor", 1.0),
                                       adopted_ids=dec.get("adopted_lesson_ids", []))
                else:
                    print(f"{base}: 有信号但拒绝 - {'; '.join(dec['reason'])}")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            "; ".join(dec["reason"]))
            else:
                print(f"{base}: 无回踩确认信号")
                self._log_scan_decision(base, False, "", "no_signal", "")
            # Phase 4 T3.3 策略 B 影子（突破/动量确认）: 只记录假设性交易、
            # 绝不下单/不发飞书/不占额度——与策略 A 真实样本分表对照。
            if config.STRATEGY_B_SHADOW_ENABLED:
                try:
                    from engines.strategy_b import breakout_signal, record_shadow
                    kl_b = self._fetch_klines_any(base, "1H", 130)
                    if kl_b:
                        sig_b = breakout_signal(kl_b)
                        if sig_b:
                            if record_shadow(base, "B_breakout", sig_b,
                                             db_path=self._db_path,
                                             klines_1h=kl_b):
                                print(f"  👻 影子信号 B_breakout {base} "
                                      f"{sig_b['dir']} @ {sig_b['entry']:.4f} "
                                      f"(score {sig_b['shadow_score']})")
                        # 2026-08-17 用户建议: 未触发信号也要复盘"为什么没触发"。
                        # 复用本轮已取的 kl_b 算四环节画像(趋势/触线/影线/量能),
                        # 零额外 API 调用;瓶颈与近失证据进 signal_profiles。
                        if sig is None:
                            from engines.strategy_b import profile_from_klines, \
                                record_profile
                            prof = profile_from_klines(kl_b)
                            if prof:
                                record_profile(base, prof,
                                               db_path=self._db_path)
                except Exception:
                    pass

    def run_once(self):
        self.scan_signals()
        self.monitor()


    def _is_auto_untradable(self, base):
        """查动态黑名单(untradable_symbols 表)——下单失败 51001/51087 自动登记,
        避免同符号每轮扫描反复下单失败。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            row = sdb.q1("SELECT 1 FROM untradable_symbols WHERE base=?",
                         [base], db_path=self._db_path)
            return bool(row)
        except Exception:
            return False

    def _long_scan_progress(self):
        """长扫描逐币进度回调(2026-08-17): 插拍止损监控 + 心跳/tick 进度 +
        60s 仓位快照——screen_daily/scan_signals 的长循环不再造成监控盲窗,
        watchdog tick 判死也看到真实进度(单调用死锁仍会被抓)。"""
        try:
            from execution.pidfile import write_heartbeat, write_tick
            write_heartbeat("directional")
            write_tick("directional")
            self.monitor()
            now = time.time()
            if now - getattr(self, "_last_snap_progress", 0) >= 60:
                self._last_snap_progress = now
                import storage.db as sdb
                sdb.init_db(self._db_path)
                for p in self.exchange.fetch_positions():
                    sdb.x("INSERT INTO position_snapshots (ts,inst_id,side,"
                          "contracts,base_qty,avg_px) VALUES (?,?,?,?,?,?)",
                          [time.time(), p.inst_id, p.side, p.contracts,
                           round(p.base_qty, 8), p.avg_px], db_path=self._db_path)
        except Exception:
            pass

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

    def _log_scan_decision(self, base, has_signal, direction, decision, reason=""):
        """信号决策过程落库（self-evolution 看账数据）：每币每轮扫都记一条。
        Phase0 T0.4：落库目标 = self._db_path（生产 None=共享库；测试传隔离路径，
        防测试进程写生产表——DEF-8 溯源：test_decision_loop 曾把 阈值85 行写进生产库）。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            sdb.x("INSERT INTO scan_decisions (ts, base, venue, has_signal, direction, "
                  "threshold, decision, reason) VALUES (?,?,?,?,?,?,?,?)",
                  [time.time(), base, self.exchange.venue_for(base), 1 if has_signal else 0,
                   direction or "", self.threshold_learner.threshold, decision, reason],
                  db_path=self._db_path)
        except Exception:
            pass

    def _log_risk_event(self, kind, reason, eq):
        """风控事件落库（复盘用）：halt/recovery 各记一条，含净值快照。"""
        try:
            import storage.db as sdb
            sdb.init_db()
            open_n = sum(1 for t in self.journal.trades if t.get("status") == "open")
            sdb.x("INSERT INTO risk_events (ts, kind, reason, equity, open_trades) "
                  "VALUES (?,?,?,?,?)",
                  [time.time(), kind, reason, round(eq, 2), open_n])
        except Exception:
            pass

    def tick(self):
        """单拍主循环体（服务模式由 service/worker 线程调用；独立模式由 run() 调用）。
        包含：心跳、每分钟账户风控、2s 止损监控、15min 信号扫描。"""
        now = time.time()
        # R2-4: 心跳（watchdog 超时 30s 判定）。写入点统一走 execution/pidfile
        # （code_graph 跨层共享状态告警修复: 文件名字面量只在 pidfile 一处）。
        from execution.pidfile import write_heartbeat
        write_heartbeat("directional")
        # 0. 账户级风控：净值喂入 + 熔断检查（每分钟 — 审计 CR-2）
        if now - self._last_risk_update >= 60:
            self._last_risk_update = now
            eq = self.exchange.fetch_balance().total_eq
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
            if not self.risk.can_trade():
                if not self._halt_notified:
                    self._halt_notified = True
                    msg = (f"⛔ 风控熔断: {self.risk.halt_reason}\n"
                           f"正在强制平掉本策略全部持仓…")
                    print(msg)
                    self._notify(msg)
                    self._log_risk_event("halt", self.risk.halt_reason, eq)
                # 审计 H4:熔断期间止损监控绝不暂停;强平失败每 30s 重试
                if now - getattr(self, "_last_liq_attempt", 0) >= 30:
                    self._last_liq_attempt = now
                    self._liquidate_all(self.risk.halt_reason)
                self.monitor()
                time.sleep(2)
                return
            elif self._halt_notified:
                self._halt_notified = False
                self._log_risk_event("recovery", "风控解除", eq)
                self._notify("✅ 风控解除，恢复信号扫描")
        # 1. tick 级止损止盈监控（每 2 秒 — OP-1，替代 6 小时轮询）
        self.monitor()
        # 2. 信号扫描（每 15 分钟 — 真日内短线）
        if now - self._last_scan >= 15 * 60:
            self._last_scan = now
            self.scan_signals()

    def run(self):
        lock = acquire_instance_lock()
        if lock is None:
            print("❌ 已有交易引擎实例在运行（engine.lock 被持有），本进程退出")
            sys.exit(1)
        self._notify("🎯 方向性交易 agent 启动（回踩确认 + 2:1盈亏比 + 2-3x杠杆 + tick级止损）")
        # R2-4: PID + 心跳文件（watchdog 用）；写入点统一走 execution/pidfile
        from execution.pidfile import write_pid
        write_pid("directional")
        self._last_scan = 0
        self._last_risk_update = 0
        self.signal_cool = {}   # 同币信号冷却（SIGNAL_COOLDOWN_MINUTES）
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"异常: {e}")
            time.sleep(1)   # 2026-08-17 提速: 与 worker 一致的 1s 止损节拍


if __name__ == "__main__":
    dt = DirectionalTrader()
    if "--once" in sys.argv:
        dt.run_once()
    else:
        dt.run()
