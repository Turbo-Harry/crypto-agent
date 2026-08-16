"""
完整交易 agent — 资金费率套利 + 自进化决策 + 订单流 + 模拟盘。

架构（四层闭环）：
  信号层：资金费率（套利机会）+ 订单流（择时）→ 触发信号
  决策层：SelfEvolvingTrader 综合信号 + 经验库 → 是否开仓/选哪个/仓位
  执行层：OKX 模拟盘，现货多 + 合约空（1:1 对冲，收资金费）
  复盘层：定期检查资金费收入/费率翻转/对冲效果 → 教训入经验库

用法：
  python3 trading_agent.py scan      # 扫描资金费率套利机会
  python3 trading_agent.py run       # 完整闭环运行（扫描→决策→执行→复盘）
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.self_evolving_trader import SelfEvolvingTrader
from data.fetch_orderflow import orderflow_snapshot
from exchange.okx_adapter import OKXAdapter


def connect():
    cfg = json.load(open("okx_config.json"))
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"], sandbox=True)


LEVERAGE_MAP = {"BTC": 3, "ETH": 3, "SOL": 2, "XRP": 2, "DOGE": 2, "ADA": 2}
ANNUAL_THRESHOLD = 0.08  # 资金费率年化 > 8% 才开仓


class FundingArbAgent:
    def __init__(self):
        self.exchange = connect()
        self.trader = SelfEvolvingTrader()
        self.symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
                        "XRP/USDT:USDT", "DOGE/USDT:USDT"]

    @staticmethod
    def _swap_id(base):
        return f"{base}-USDT-SWAP"

    # ---------- 1. 信号层：资金费率 + 订单流 ----------
    def scan_signals(self):
        """扫描各币资金费率套利机会，返回信号列表。"""
        signals = []
        for sym in self.symbols:
            base = sym.split("/")[0]
            try:
                rate = self.exchange.fetch_funding_rate(self._swap_id(base))
                annual = rate * 3 * 365
                # 订单流辅助（择时）
                try:
                    of = orderflow_snapshot(f"{base}USDT")
                    taker = of["taker_buy_ratio"]
                except Exception:
                    taker = 0.5
                signals.append({
                    "symbol": sym, "base": base, "rate": rate,
                    "annual": annual, "taker_buy": taker,
                    # 信号分数：年化越高分越高，订单流方向一致加分
                    "score": min(100, abs(annual) * 500 + 50),
                })
            except Exception:
                continue
        return signals

    # ---------- 2. 决策层：信号 + 经验库 ----------
    def decide(self, sig):
        """综合信号 + 经验库 → 决策。"""
        # 资金费率年化门槛
        if abs(sig["annual"]) < ANNUAL_THRESHOLD:
            return {"trade": False, "reason": f"年化 {sig['annual']*100:.1f}% < {ANNUAL_THRESHOLD*100:.0f}%"}
        # 自进化决策（查经验库）
        dec = self.trader.decide(
            symbol=sig["base"], signal_score=sig["score"],
            signal_name="资金费率套利", signal_price=0, entry_price=0,
            stop_dist=0.02, tp_dist=0.05, atr_value=0)
        return dec

    # ---------- 3. 执行层：现货多 + 合约空 ----------
    def execute(self, sig):
        """开对冲仓（现货多 + 合约空，1:1），带 posSide 和逐仓。"""
        base = sig["base"]
        sym = sig["symbol"]
        rate = sig["rate"]
        # 配置逐仓 + 杠杆
        lev = LEVERAGE_MAP.get(base, 2)
        for side in ["long", "short"]:
            try:
                self.exchange.set_leverage(self._swap_id(base), lev, side)
            except Exception:
                pass

        # 下单量：统一 execution 工具换算（lotSz 对齐 + 最小下单量校验）
        NOTIONAL = 700  # 名义 USDT（满足 BTC 最小 0.01）
        from execution import qty_for_notional
        amount, qty_reason = qty_for_notional(self.exchange, self._swap_id(base), NOTIONAL)
        if amount is None:
            print(f"  ❌ 开仓跳过 {base}: {qty_reason}")
            return False
        if qty_reason:
            print(f"  {base} 数量调整: {qty_reason}")

        try:
            if rate > 0:
                # 正费率：现货多 + 合约空
                res1 = self.exchange.place_market_order(f"{base}-USDT", "buy", amount, venue="spot")
                if not res1.ok:
                    raise RuntimeError(res1.message)
                res2 = self.exchange.place_market_order(self._swap_id(base), "sell", amount,
                                                        venue="swap", pos_side="short")
                if not res2.ok:
                    raise RuntimeError(res2.message)
            else:
                # 负费率：现货空腿需保证金账户（未配置）→ 整体拒绝，不开单腿（R1-11）
                print(f"⛔ 负费率 {base}: 现货空腿需保证金账户（未配置），整体拒绝开仓，不开裸单腿")
                return False
            print(f"  ✅ 开仓 {base}: 现货 {'多' if rate>0 else '空'} {amount} + 合约 {'空' if rate>0 else '多'} {amount}，名义 {NOTIONAL} USDT")
            return True
        except Exception as e:
            print(f"  ❌ 开仓失败 {base}: {e}")
            return False

    # ---------- 4. 复盘层：资金费收入 + 费率翻转 ----------
    def review_positions(self):
        """复盘当前持仓：资金费收入、费率是否翻转。"""
        try:
            positions = [p for p in self.exchange.fetch_positions() if p.base_qty > 0]
            for p in positions:
                base = p.base
                rate = self.exchange.fetch_funding_rate(self._swap_id(base))
                # 复盘：费率翻转（原来正现在负）→ 该平仓/反向
                if p.side == "short" and rate < 0:
                    print(f"  ⚠️ {base} 费率翻转为负 {rate*100:.4f}%，空头套利转亏，建议平仓")
                elif p.side == "long" and rate > 0:
                    print(f"  ⚠️ {base} 费率翻转为正 {rate*100:.4f}%，多头套利转亏，建议平仓")
                else:
                    print(f"  ✅ {base} 持仓正常，当前费率 {rate*100:.4f}%/8h")
        except Exception as e:
            print(f"  复盘失败: {e}")

    # ---------- 主循环 ----------
    def run(self, once=False):
        print("=" * 60)
        print("完整交易 agent 启动（资金费率套利 + 自进化 + 订单流）")
        print("=" * 60)
        while True:
            # 扫描信号
            signals = self.scan_signals()
            print(f"\n[{time.strftime('%H:%M:%S')}] 扫描到 {len(signals)} 个标的:")
            for s in signals:
                print(f"  {s['base']:<6} 费率 {s['rate']*100:+.4f}%/8h "
                      f"年化 {s['annual']*100:+.1f}% 订单流买占比 {s['taker_buy']*100:.0f}%")
            # 决策 + 执行
            for s in signals:
                dec = self.decide(s)
                if dec["trade"]:
                    self.execute(s)
                else:
                    print(f"  {s['base']}: ❌ {'; '.join(dec['reason'])}")
            # 复盘
            self.review_positions()
            if once:
                break
            time.sleep(3600)  # 每小时一轮


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    agent = FundingArbAgent()
    if cmd == "scan":
        agent.run(once=True)
    elif cmd == "run":
        agent.run()
