"""
完整实时交易主进程 — 9 类决策来源综合判断 + 实时观测 + 下单 + 飞书通知 + 复盘。

决策来源（9 类）：
  ① 资金费率（套利核心）② 实时价格 ③ 订单流 ④ 技术指标
  ⑤ 恐惧贪婪 ⑥ 历史经验 ⑦ 经济日历 ⑧ OI持仓量 ⑨ 链上（待接）

决策逻辑（综合判断，宁缺毋滥）：
  资金费率年化达标 AND 经济日历无事件 AND 恐惧贪婪不极端
  AND OI多空比不拥挤 AND 经验库无警示 → 开对冲仓

用法：
  python3 trading_main.py --once   跑一轮决策
  python3 trading_main.py          常驻运行
"""
import sys
import os
import json
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.fetch_open_interest import fetch_oi
from data.fetch_orderflow import orderflow_snapshot
from data.fetch_fear_greed import fetch_fng, fng_at_date
from data.economic_calendar import is_high_impact_now
from decision.self_evolving_trader import SelfEvolvingTrader
from exchange.base import ExchangeAdapter
from exchange.okx_adapter import OKXAdapter

LARK = "/Users/wuhai/Desktop/untitled folder/lark"
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"
ANNUAL_THRESHOLD = 0.08
# 对冲不需要杠杆：1x 隔离（高杠杆只降低保证金率、抬高爆仓风险 — OP-3）
LEVERAGE_MAP = {"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1}
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
ARB_STATE_FILE = "arb_positions.json"  # 套利持仓台账（自动平仓用）


def notify(msg):
    try:
        subprocess.run([LARK, "im", "+messages-send", "--as", "bot",
                        "--user-id", FEISHU_USER_ID, "--text", msg],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def connect() -> ExchangeAdapter:
    """构建 OKX 模拟盘适配器（策略层只见 ExchangeAdapter 接口）。"""
    cfg = json.load(open("okx_config.json"))
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"], sandbox=True)


def _spot_id(base):
    return f"{base}-USDT"


def _swap_id(base):
    return f"{base}-USDT-SWAP"


class TradingMain:
    def __init__(self, exchange=None, rt=None):
        self.exchange = exchange or connect()
        self.trader = SelfEvolvingTrader()
        self.fng_list = fetch_fng()
        # 带评分的经验库（历史经验不一定对，用交易结果验证）
        from decision.experience_scoring import ScoredExperience
        self.exp_bank = ScoredExperience()
        # 阈值自适应（阈值也不是真理，用分数→盈亏分布校准）
        from decision.threshold_learning import ThresholdLearner
        self.threshold_learner = ThresholdLearner(path="threshold_state_arb.json")  # R1-3: 套利侧独立阈值文件
        # WebSocket 实时监听（价格/费率/成交，毫秒级推送）
        # 服务模式下由 service 注入共享 rt（与方向性引擎共用一条 WS 连接）
        self.rt = rt
        if self.rt is None:
            from data.realtime_okx import OKXRealtime
            self.rt = OKXRealtime(SYMBOLS).start()
            print("WebSocket 实时监听已启动（价格/费率/成交）")
        # 账户级风控（审计 CR-2：RiskManager 必须真正接线）
        from risk.risk_manager import RiskManager
        try:
            eq = self.exchange.fetch_balance().usdt_total
        except Exception:
            eq = 0
        self.risk = RiskManager(initial_equity=eq if eq > 0 else 4190)
        self._halt_notified = False
        self.oi_history = {}   # {base: [(ts, oi_usd)]} 清算级联检测用（OP-6）
        # 运行时状态（服务模式 tick() 直接可用，不必先走 run()）
        self.price_history = {}
        self.alert_cool = {}
        self.signal_state = {}
        self.decision_cool = {}
        print(f"账户风控已接线（初始净值 {self.risk.initial_equity:.0f}，"
              f"单日亏损 {0.015*100:.1f}% 停手 / 回撤 {0.20*100:.0f}% 熔断）")
        # 套利持仓台账（OP-3：费率翻转/基差扩张自动平仓用）
        self.arb_positions = self._load_arb_positions()
        if self.arb_positions:
            print(f"已加载套利持仓台账 {len(self.arb_positions)} 条（自动平仓管理启用）")
        # 权重层自进化（元优化：静态权重 → 数据闭环 + 验证门）
        from decision.weight_learning import WeightLearner
        from decision.scoring import ARB_WEIGHTS
        self.weight_learner = WeightLearner(ARB_WEIGHTS)
        print(f"评分权重已接入自进化（当前 v{self.weight_learner.version}，"
              f"样本 {len(self.weight_learner.records)} 条）")

    def _load_arb_positions(self):
        import storage.db as sdb
        try:
            sdb.init_db()
            rows = sdb.q("SELECT rec FROM arb_positions")
            return [json.loads(r["rec"]) for r in rows]
        except Exception:
            return []

    def _save_arb_positions(self):
        import storage.db as sdb
        try:
            for rec in self.arb_positions:
                if isinstance(rec, dict) and rec.get("base"):
                    sdb.x("INSERT OR REPLACE INTO arb_positions (base,rec,updated_at) "
                          "VALUES (?,?,?)",
                          [rec["base"], json.dumps(rec, ensure_ascii=False), time.time()])
        except Exception:
            pass

    def gather_signals(self, base):
        """汇总某币的 9 类决策信号（WebSocket 实时优先，REST 兜底）。"""
        signals = {}
        # ①② 实时价格 + 资金费率（stale>60s 的字段剔除，强制走 REST 兜底）
        rt_data = self.rt.get(base, max_age=60)
        if rt_data.get("price"):
            signals["price"] = rt_data["price"]
        else:
            try:
                signals["price"] = self.exchange.fetch_ticker_last(_spot_id(base))
            except Exception:
                signals["price"] = 0
        if rt_data.get("funding") is not None:
            rate = rt_data["funding"]
            signals["funding_rate"] = rate
            signals["annual"] = rate * 3 * 365
        else:
            try:
                rate = self.exchange.fetch_funding_rate(_swap_id(base))
                signals["funding_rate"] = rate
                signals["annual"] = rate * 3 * 365
            except Exception:
                signals["funding_rate"] = 0
                signals["annual"] = 0
        # ③ 订单流
        try:
            of = orderflow_snapshot(f"{base}USDT")
            signals["taker_buy"] = of["taker_buy_ratio"]
            signals["imbalance"] = of["imbalance"]
        except Exception:
            signals["taker_buy"] = 0.5
            signals["imbalance"] = 0
        # ⑧ OI
        try:
            oi = fetch_oi(f"{base}_USDT")
            signals["oi_usd"] = oi["open_interest_usd"]
            signals["lsr_account"] = oi["lsr_account"]
        except Exception:
            signals["oi_usd"] = 0
            signals["lsr_account"] = 1.0
        # ② 实时波动率（15 分钟振幅，WebSocket 1m K线计算）
        # None = 数据未就绪 → score_volatility 给 45 分保守观望
        signals["volatility"] = rt_data.get("vol_15m")
        return signals

    def decide(self, base, sig):
        """统一评分体系决策：9 类信号 → 0-100 分 → 加权综合分 → 决策。
        返回 (是否开仓, 综合分, 各信号分 dict)。"""
        from decision.scoring import (score_funding_rate, score_fear_greed, score_oi,
                             score_calendar, score_experience, score_volatility,
                             ARB_WEIGHTS, composite, DECISION_THRESHOLD)

        today = time.strftime("%Y-%m-%d")
        fng = fng_at_date(self.fng_list, today)
        if fng is None:
            fng = 50

        scores = {
            "funding": score_funding_rate(sig["annual"]),
            "volatility": score_volatility(sig.get("volatility", 0)),
            "fear_greed": score_fear_greed(fng),
            "oi": score_oi(sig["lsr_account"]),
            "calendar": score_calendar(),
            "experience": score_experience(self.exp_bank, base),
        }
        # 用自进化权重（WeightLearner 验证门产出）而非静态 ARB_WEIGHTS
        total = composite(scores, self.weight_learner.weights)
        # 用自适应阈值（而非固定 70）决策
        return total >= self.threshold_learner.threshold, total, scores

    def _risk_guard(self, base):
        """开仓前风控闸门：熔断、持仓幂等、余额、持仓数、单币名义敞口。
        返回 (是否放行, 拒绝原因)。任何异常都拒绝——宁可不开，不可乱开。"""
        # 0. 账户级熔断（RiskManager）
        if not self.risk.can_trade():
            return False, f"风控熔断: {self.risk.halt_reason}"
        try:
            positions = self.exchange.fetch_positions()
            open_pos = [p for p in positions if p.base_qty > 0]
            # 1. 幂等检查：该币已有合约持仓 → 不重复开（防仓位无限堆叠）
            existing = [p for p in open_pos if p.base == base]
            if existing:
                return False, f"{base} 已有 {len(existing)} 个合约持仓，跳过（幂等）"
            # 2. 余额检查
            usdt_free = self.exchange.fetch_balance().usdt_free
            if usdt_free < 2 * 150:
                return False, f"USDT 可用 {usdt_free:.0f} < 300，余额不足"
            # 3. 持仓数上限
            if len(open_pos) >= 4:
                return False, f"合约持仓 {len(open_pos)} 个 ≥ 上限 4"
            # 4. 单币名义敞口上限
            base_notional = 0.0
            for p in open_pos:
                if p.base == base:
                    base_notional += p.base_qty * p.avg_px
            if base_notional >= 600:
                return False, f"{base} 名义敞口 {base_notional:.0f} ≥ 600 上限"
        except Exception as e:
            return False, f"风控查询失败: {e}"
        return True, ""

    def execute(self, base, sig, scores=None, composite_score=None):
        """开对冲仓（先过风控闸门 + 净年化闸门）。
        scores: 本次决策的子分数 dict，随台账记录，供权重层进化学习。"""
        # 策略总开关：资金费率套利已停用（用户决定），开仓路径直接拦截
        import config
        if not config.ENABLE_FUNDING_ARB:
            msg = f"⛔ 套利停用（ENABLE_FUNDING_ARB=False，用户决定）——{base} 本次不执行"
            print(msg)
            return False
        ok, reason = self._risk_guard(base)
        if not ok:
            msg = f"⛔ 风控拒绝 {base}: {reason}"
            print(msg)
            notify(msg)
            return False
        # 净年化闸门：毛年化扣掉往返手续费后仍为正期望才开（审计 CR-4）
        from decision.scoring import net_funding_annual
        net_annual = net_funding_annual(sig["annual"])
        if net_annual < 0.02:
            msg = (f"⛔ 套利拒绝 {base}: 毛年化 {sig['annual']*100:.1f}% → "
                   f"净年化 {net_annual*100:.1f}%（扣往返费+按14天摊销）< 2%，负期望不开")
            print(msg)
            notify(msg)
            return False
        sym = f"{base}/USDT:USDT"   # 保留台账兼容字段；下单走适配器 instId
        rate = sig["funding_rate"]
        lev = LEVERAGE_MAP.get(base, 2)
        for side in ["long", "short"]:
            try:
                self.exchange.set_leverage(_swap_id(base), lev, side)
            except Exception:
                pass
        price = sig["price"]
        # RES-15：统一走 execution.qty_for_notional（lotSz 对齐 + 最小下单量校验）
        from execution import qty_for_notional
        amount, qty_reason = qty_for_notional(self.exchange, _swap_id(base), 150.0, price=price)
        if amount is None:
            msg = f"⛔ 套利拒绝 {base}: {qty_reason}"
            print(msg)
            notify(msg)
            return False
        if qty_reason:
            print(f"  {base} 数量调整: {qty_reason}")
        spot_ok = False
        perp_ok = False
        spot_entry_px = perp_entry_px = None

        def exec_spot(side):
            res = self.exchange.place_market_order(_spot_id(base), side, amount, venue="spot")
            if not res.ok:
                raise RuntimeError(res.message)
            return res.ord_id

        def exec_swap(side, pos_side, reduce_only=False):
            res = self.exchange.place_market_order(_swap_id(base), side, amount,
                                                   venue="swap", pos_side=pos_side,
                                                   reduce_only=reduce_only)
            if not res.ok:
                raise RuntimeError(res.message)
            return res.ord_id

        try:
            if rate > 0:
                oid = exec_spot("buy")
                spot_entry_px = self._fill_price(base, "spot", oid, price)
                spot_ok = True
                oid2 = exec_swap("sell", "short")
                perp_entry_px = self._fill_price(base, "swap", oid2, price)
                perp_ok = True
                dir_txt = "现货多+合约空"
            else:
                oid = exec_spot("sell")
                spot_entry_px = self._fill_price(base, "spot", oid, price)
                spot_ok = True
                oid2 = exec_swap("buy", "long")
                perp_entry_px = self._fill_price(base, "swap", oid2, price)
                perp_ok = True
                dir_txt = "现货空+合约多"
            msg = (f"✅ 套利开仓 {base}\n方向 {dir_txt}\n"
                   f"年化 {sig['annual']*100:.1f}%（净 {net_annual*100:.1f}%）\n数量 {amount} {base}\n"
                   f"价格 {price:.2f}")
            print(msg)
            notify(msg)
            # 记入套利台账（费率翻转/基差扩张自动平仓 — OP-3）
            self.arb_positions.append({
                "base": base, "amount": amount,
                "dir": "short" if rate > 0 else "long",   # 合约腿方向
                "spot_side": "long" if rate > 0 else "short",  # 现货腿方向（R1-10）
                "entry_sign": 1 if rate > 0 else -1,
                "entry_rate": rate,
                "scores": scores if scores else {},
                "composite_score": composite_score,          # R1-2：开仓综合分快照
                "weights_version": self.weight_learner.version,  # R1-2：权重版本快照
                "spot_entry_px": spot_entry_px,              # R1-4：真实成交价
                "perp_entry_px": perp_entry_px,
                "entry_notional": amount * price,
                "opened_at": time.time(), "flip_since": None,
            })
            self._save_arb_positions()
            return True
        except Exception as e:
            # 孤儿补偿（R1-10/C）：任一腿失败 → 反手平已成交腿，不留单腿
            try:
                if spot_ok:
                    exec_spot("sell" if rate > 0 else "buy")
            except Exception:
                pass
            try:
                if perp_ok:
                    exec_swap("buy" if rate > 0 else "sell",
                              "short" if rate > 0 else "long", reduce_only=True)
            except Exception:
                pass
            msg = f"❌ 开仓失败 {base}: {e}（已尝试补偿平仓）"
            print(msg)
            notify(msg)
            return False

    # ---------- 套利持仓管理：失效自动平仓（OP-3） ----------
    def manage_arb_positions(self):
        """费率连续翻转 / 基差向不利方向扩张 → 自动平对冲仓。
        此前只在费率翻转时告警、从不平仓（审计 CR-4/OP-3）。"""
        if not self.arb_positions:
            return
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return
        for rec in list(self.arb_positions):
            base = rec["base"]
            sym = f"{base}/USDT:USDT"
            # 0. 交易所已无该仓（人工平了）→ 从台账移除
            pos = next((p for p in positions
                        if p.inst_id == _swap_id(base) and p.base_qty > 0), None)
            if pos is None:
                self.arb_positions.remove(rec)
                self._save_arb_positions()
                continue
            # 1. 当前费率
            try:
                rate = self.exchange.fetch_funding_rate(_swap_id(base))
            except Exception:
                continue
            # 2. 基差（perp/spot - 1）。RES-20：swap_price 缺失时 REST 兜底，
            # 兜底也失败 → 保守告警一次（宁告警，不让基差退出静默失效）
            rt = self.rt.get(base, max_age=60)
            spot, perp = rt.get("price"), rt.get("swap_price")
            if spot and not perp:
                try:
                    perp = self.exchange.fetch_ticker_last(_swap_id(base))
                except Exception:
                    perp = None
            basis = (perp - spot) / spot if spot and perp else 0.0
            if not (spot and perp):
                self._alert(base, "基差数据缺失（WS+REST 均无），基差退出保护暂不可用")
            exit_reason = None
            # 2a. 基差向不利方向扩张（做空合约时 perp 溢价扩大 → 亏；做多合约时折价扩大 → 亏）
            if rec["dir"] == "short" and basis > 0.005:
                exit_reason = f"基差 {basis*100:+.2f}% > 0.5%（perp 溢价扩大，空腿浮亏）"
            elif rec["dir"] == "long" and basis < -0.005:
                exit_reason = f"基差 {basis*100:+.2f}% < -0.5%（perp 折价扩大，多腿浮亏）"
            # 2b. 费率翻转持续 2 个结算周期（16h）→ 平仓
            flipped = (rec["entry_sign"] > 0 and rate < 0) or \
                      (rec["entry_sign"] < 0 and rate > 0)
            if flipped and exit_reason is None:
                if rec.get("flip_since") is None:
                    rec["flip_since"] = time.time()
                elif time.time() - rec["flip_since"] >= 16 * 3600:
                    exit_reason = (f"费率翻转持续 {int((time.time()-rec['flip_since'])/3600)}h "
                                   f"（{rate*100:+.4f}%/8h），套利方向失效")
            if not flipped:
                rec["flip_since"] = None
            self._save_arb_positions()
            if exit_reason:
                self._close_hedge(rec, exit_reason)

    # ---------- R1-4 辅助：成交价回填 + funding 账单 ----------
    def _fill_price(self, base, venue, order_id, fallback=None):
        """place/batch-orders 响应无 avgPx → fetch_order_avg_px 回填成交均价；失败返回 fallback。"""
        try:
            inst_id = _swap_id(base) if venue == "swap" else _spot_id(base)
            avg = self.exchange.fetch_order_avg_px(inst_id, order_id)
            if avg:
                return float(avg)
        except Exception:
            pass
        return fallback

    def _fetch_funding_received(self, since_ts):
        """持仓期间实际资金费收入（quote 计）。OKX bills type=8 为资金费，
        金额在 balChg 字段。沙盘/无账单返回 None（上层必须打 pnl_estimated，
        严禁静默当 0）。"""
        try:
            rows = self.exchange.fetch_bills(ccy="USDT", since_ms=int(since_ts * 1000),
                                             bill_type="8")
        except Exception:
            return None
        if not rows:
            return None
        return sum(float(r.get("balChg") or 0) for r in rows)

    def _close_hedge(self, rec, reason):
        """平对冲仓：现货腿（按 spot_side 反向）+ 合约腿。R1-10 修复：现货腿方向不再硬编码卖出。
        R1-4：平仓用真实成交价核算已实现盈亏（spot+perp+funding-fees），估算时打 pnl_estimated。"""
        base = rec["base"]
        amount = rec["amount"]
        print(f"\n🔻 自动平仓 {base}: {reason}")
        ok = True
        spot_close_px = None
        # 现货腿：spot_side 决定方向；旧台账无该字段按 entry_sign 推导；显式 None=无现货腿则跳过
        spot_side = rec.get("spot_side")
        if spot_side is None and "spot_side" not in rec:
            spot_side = "long" if rec.get("entry_sign", 1) > 0 else "short"
        if spot_side is not None:
            try:
                # 对账：按交易所现货实际持有量平（防孤儿/重复平仓）
                held = self.exchange.spot_holding(base)
                qty = min(amount, abs(held))
                if qty > 0:
                    if spot_side == "long" and held > 0:
                        res = self.exchange.place_market_order(_spot_id(base), "sell", qty,
                                                               venue="spot")
                        if not res.ok:
                            raise RuntimeError(res.message)
                        spot_close_px = self._fill_price(base, "spot", res.ord_id)
                    elif spot_side == "short" and held < 0:
                        res = self.exchange.place_market_order(_spot_id(base), "buy", qty,
                                                               venue="spot")
                        if not res.ok:
                            raise RuntimeError(res.message)
                        spot_close_px = self._fill_price(base, "spot", res.ord_id)
            except Exception as e:
                print(f"  现货腿平仓失败: {e}")
                ok = False
        perp_close_px = None
        try:
            if rec["dir"] == "short":
                res = self.exchange.place_market_order(_swap_id(base), "buy", amount,
                                                       venue="swap", pos_side="short",
                                                       reduce_only=True)
            else:
                res = self.exchange.place_market_order(_swap_id(base), "sell", amount,
                                                       venue="swap", pos_side="long",
                                                       reduce_only=True)
            if not res.ok:
                raise RuntimeError(res.message)
            perp_close_px = self._fill_price(base, "swap", res.ord_id)
        except Exception as e:
            print(f"  合约腿平仓失败: {e}")
            ok = False
        if ok:
            self.arb_positions.remove(rec)
            self._save_arb_positions()
            # R1-4：真实已实现盈亏核算
            try:
                spot_entry = rec.get("spot_entry_px")
                perp_entry = rec.get("perp_entry_px")
                notional = rec.get("entry_notional") or (amount * (spot_entry or perp_entry or 0))
                funding_received = self._fetch_funding_received(rec["opened_at"])
                pnl_estimated = (spot_close_px is None or perp_close_px is None
                                 or funding_received is None
                                 or spot_entry is None or perp_entry is None)
                if spot_entry and spot_close_px:
                    spot_pnl = (spot_close_px - spot_entry) * amount * (1 if spot_side == "long" else -1)
                else:
                    spot_pnl = 0.0
                if perp_entry and perp_close_px:
                    perp_pnl = (perp_entry - perp_close_px) * amount if rec["dir"] == "short" \
                        else (perp_close_px - perp_entry) * amount
                else:
                    perp_pnl = 0.0
                fees = 0.003 * (notional or 1.0)   # ARB_ROUNDTRIP_COST 兜底（账单缺失时）
                net_pnl = (spot_pnl + perp_pnl + (funding_received or 0.0) - fees) / (notional or 1.0)
            except Exception as e:
                # 兜底：估算标签（旧公式），强制打 pnl_estimated
                print(f"  真实盈亏核算失败，退回估算: {e}")
                days_held = (time.time() - rec["opened_at"]) / 86400.0
                net_pnl = abs(rec["entry_rate"]) * 3 * max(days_held, 0) - 0.003
                pnl_estimated = True
            # 权重层进化（估算样本打标，学习器跳过）
            if rec.get("scores"):
                try:
                    self.weight_learner.record(rec["scores"], net_pnl,
                                               pnl_estimated=pnl_estimated)
                except Exception as e:
                    print(f"  权重学习记录失败: {e}")
            # 阈值学习（R1-2）：有开仓综合分快照才喂；旧台账无快照直接跳过（不重算）
            score = rec.get("composite_score")
            if score is not None:
                try:
                    self.threshold_learner.record(float(score), float(net_pnl),
                                                  pnl_estimated=pnl_estimated)
                except Exception as e:
                    print(f"  阈值学习记录失败: {e}")
            msg = (f"🔻 套利平仓 {base}\n原因: {reason}\n数量 {amount} {base}\n"
                   f"净盈亏 {net_pnl*100:+.2f}%（{'估算' if pnl_estimated else '真实核算'}）")
        else:
            msg = f"⚠️ 套利平仓部分失败 {base}（{reason}），请手动检查"
        notify(msg)

    def run_once(self):
        print("=" * 62)
        print(f"实时决策扫描（统一评分体系）[{time.strftime('%H:%M:%S')}]")
        print("=" * 62)
        for base in SYMBOLS:
            sig = self.gather_signals(base)
            ok, total, scores = self.decide(base, sig)
            status = "✅ 开仓" if ok else "❌ 观望"
            print(f"\n{base}: 综合分 {total:.1f} → {status}")
            print(f"  费率 {scores['funding']:.0f}分 | 情绪 {scores['fear_greed']:.0f}分 | "
                  f"OI {scores['oi']:.0f}分 | 日历 {scores['calendar']:.0f}分 | "
                  f"经验 {scores['experience']:.0f}分")
            if ok:
                self.execute(base, sig, scores=scores, composite_score=total)  # R1-2：补传快照

    # ---------- 实时告警 ----------
    def check_alerts(self):
        """实时告警（每分钟）：价格异动 >2%、费率翻转。
        RES-18：用 max_age=60 过滤 stale 数据（断线时不拿旧值比较）；funding=None 防护。"""
        for base in SYMBOLS:
            data = self.rt.get(base, max_age=60)   # RES-18：stale 字段剔除
            if not data.get("price"):
                continue
            price = data["price"]
            funding = data.get("funding")          # RES-18：可能为 None（被 stale 过滤）
            prev = self.price_history.get(base)
            if prev is not None and time.time() - prev["ts"] > 30:
                # 1. 价格异动（2 分钟内 >2%）
                chg = (price - prev["price"]) / prev["price"]
                if abs(chg) >= 0.02:
                    self._alert(base, f"价格异动 {chg*100:+.1f}% (2分钟)")
                # 2. 费率翻转（RES-18：funding=None 时跳过比较，不抛 TypeError）
                if funding is not None and prev.get("funding") is not None:
                    if prev["funding"] > 0 and funding < 0:
                        self._alert(base, "⚠️ 费率翻转为负（空头套利转亏，建议平仓）")
                    elif prev["funding"] < 0 and funding > 0:
                        self._alert(base, "费率翻转为正（多头套利转亏）")
            self.price_history[base] = {"price": price, "funding": funding, "ts": time.time()}

    def _alert(self, base, msg):
        """告警（30 分钟冷却，防重复轰炸）。"""
        key = f"{base}:{msg[:10]}"
        if time.time() - self.alert_cool.get(key, 0) < 1800:
            return
        self.alert_cool[key] = time.time()
        print(f"🚨 {base}: {msg}")
        notify(f"🚨 {base}\n{msg}")

    # ---------- 信号事件检测（事件驱动，非定时） ----------
    def check_signal_event(self, base):
        """检测信号事件：费率突破阈值 / 费率翻转 / 价格异动。
        返回事件名或 None。有事件才决策，无事件安静等待。
        注意：无论是否触发事件都先更新状态——否则同一事件会重复触发。"""
        data = self.rt.get(base, max_age=60)   # RES-18：stale 字段剔除，断线不拿旧值
        if not data.get("price"):
            return None
        funding = data.get("funding")          # RES-18：可能为 None（stale 过滤后）
        if funding is None:
            funding = 0.0
        annual = funding * 3 * 365
        prev = self.signal_state.get(base, {})

        event = None
        # 1. 费率年化突破阈值（从 <8% 到 ≥8%，套利机会出现）
        #    套利停用时跳过（该事件唯一用途是触发套利开仓决策）
        import config
        prev_annual = abs(prev.get("annual", 0))
        if config.ENABLE_FUNDING_ARB and \
                abs(annual) >= ANNUAL_THRESHOLD and prev_annual < ANNUAL_THRESHOLD:
            event = f"费率年化突破阈值 {annual*100:+.1f}%"
        # 2. 费率翻转（套利风险信号）
        prev_f = prev.get("funding")
        if event is None and prev_f is not None:
            if prev_f > 0 and funding < 0:
                event = "费率翻转为负"
            elif prev_f < 0 and funding > 0:
                event = "费率翻转为正"
        # 3. 价格异动（2 分钟 >2%）
        prev_p = prev.get("price")
        if event is None and prev_p is not None and prev_p > 0:
            chg = (data["price"] - prev_p) / prev_p
            if abs(chg) >= 0.02:
                event = f"价格异动 {chg*100:+.1f}%"

        # 4. 疑似清算级联（OP-6）：价格急动 ≥3% 且 10 分钟 OI 骤降 ≥2%
        #    多头清算：价格暴跌+OI骤降；空头逼仓：价格暴涨+OI骤降
        #    可覆盖第3条普通价格异动事件（级联是更强的信号）
        if (event is None or (event and "价格异动" in event)) \
                and prev_p is not None and prev_p > 0:
            chg = (data["price"] - prev_p) / prev_p
            if abs(chg) >= 0.03:
                hist = self.oi_history.get(base, [])
                if len(hist) >= 3:
                    oi_now, oi_old = hist[-1][1], hist[0][1]
                    if oi_now and oi_old:
                        oi_chg = (oi_now - oi_old) / oi_old
                        if oi_chg <= -0.02:
                            kind = "多头清算级联" if chg < 0 else "空头逼仓级联"
                            event = (f"疑似{kind}: 价格{chg*100:+.1f}% "
                                     f"OI{oi_chg*100:+.1f}%(10min)，暂停追单")

        # 先更新状态再返回（防重复触发）
        self.signal_state[base] = {"annual": annual, "funding": funding,
                                   "price": data["price"]}
        return event

    def tick(self):
        """单拍主循环体（服务模式由 service/worker 线程调用；独立模式由 run() 调用）。
        包含：心跳、账户风控、OI 采样、实时告警、套利持仓管理、信号事件检测。"""
        # R2-4: 心跳（watchdog 超时 300s 判定）
        with open("heartbeat_arb.txt", "w") as f:
            f.write(str(time.time()))
        # 0. 账户级风控：净值喂入 + 熔断检查（审计 CR-2）
        try:
            eq = self.exchange.fetch_balance().usdt_total
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
        except Exception:
            pass
        if not self.risk.can_trade():
            if not self._halt_notified:
                self._halt_notified = True
                msg = f"⛔ 风控熔断: {self.risk.halt_reason}——停止一切开仓决策"
                print(msg)
                notify(msg)
            return
        elif self._halt_notified:
            self._halt_notified = False
            notify(f"✅ 风控解除（日界/净值恢复），恢复决策。当前阈值 {self.threshold_learner.threshold}")
        # 0.5 OI 采样（清算级联检测用 — OP-6，每分钟）
        try:
            for base in SYMBOLS:
                try:
                    oi = fetch_oi(f"{base}_USDT")
                    hist = self.oi_history.setdefault(base, [])
                    hist.append((time.time(), oi.get("open_interest_usd")))
                    self.oi_history[base] = [x for x in hist
                                             if time.time() - x[0] <= 600][-12:]
                except Exception:
                    pass
        except Exception:
            pass
        # 1. 实时告警（每分钟）
        try:
            self.check_alerts()
        except Exception as e:
            print(f"告警异常: {e}")
        # 1.5 套利持仓管理：费率翻转/基差扩张 → 自动平仓（OP-3）
        try:
            self.manage_arb_positions()
        except Exception as e:
            print(f"套利持仓管理异常: {e}")
        # 2. 信号事件检测 → 有信号才决策
        for base in SYMBOLS:
            try:
                event = self.check_signal_event(base)
                if event:
                    # 决策冷却检查（防同币频繁决策）
                    if time.time() - self.decision_cool.get(base, 0) < 1800:
                        continue
                    print(f"\n⚡ {base} 信号事件: {event}")
                    notify(f"⚡ {base}\n信号事件: {event}")
                    sig = self.gather_signals(base)
                    ok, total, scores = self.decide(base, sig)
                    print(f"  {base}: 综合分 {total:.1f} → "
                          f"{'✅ 开仓' if ok else '❌ 观望'}（阈值 {self.threshold_learner.threshold}）")
                    if ok:
                        self.execute(base, sig, scores=scores, composite_score=total)  # R1-2：快照
                    # RES-18 修正：无论开仓与否，决策后都置冷却
                    # （此前非交易事件不置位 → 每分钟重复 notify 轰炸）
                    self.decision_cool[base] = time.time()
            except Exception as e:
                print(f"信号检测异常 {base}: {e}")

    def run(self):
        notify("🚀 事件驱动交易系统启动（有信号才决策）")
        # R2-4: PID + 心跳文件（watchdog 用）
        with open("trading_main.pid", "w") as f:
            f.write(str(os.getpid()))
        self.price_history = {}
        self.alert_cool = {}
        self.signal_state = {}
        self.decision_cool = {}  # 决策冷却（同币信号决策后 30 分钟不再决策）
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    tm = TradingMain()
    if "--once" in sys.argv:
        tm.run_once()
    else:
        tm.run()
