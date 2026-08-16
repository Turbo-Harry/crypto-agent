"""
纸面交易模拟器 — 不碰真钱，验证执行链路。
复用回测引擎的信号/出场逻辑，但用持久化状态从"今天"开始逐日推进。

用法：
  python3 paper_trade.py           # 初始化或推进到最新数据
  python3 paper_trade.py --reset   # 重置状态

每次运行会：
  1. 拉最新 OKX 数据
  2. 从上次位置推进到最新
  3. 处理候选（回踩确认）、出场、找新信号
  4. 保存状态并输出当前账户状态 + 建议
"""
import sys
import os
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines
from backtest.engine import Backtest, ENTRY_COST
from strategy.filters import market_gate, coin_resonance
from strategy.indicators import relative_strength

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_state.json")


class PaperTrader(Backtest):
    """继承回测引擎，用持久化状态做增量推进。"""

    def __init__(self, btc, pool, state):
        super().__init__(btc, pool, initial_equity=state["initial_equity"])
        self.cash = state["cash"]
        self.peak = state["peak_equity"]
        self.holdings = state["holdings"]
        self.pending = state["pending"]
        self.trades = state["trades"]
        self.last_idx = state["last_idx"]

    def advance(self):
        """从 last_idx+1 推进到最新，逐日处理。"""
        n = len(self.timeline)
        for t in range(self.last_idx + 1, n):
            day_key = self._date_of(self.btc[t])

            # 0. 处理候选（回踩确认入场）
            self._process_pending(t)
            # 1. 出场
            self._check_exits(t)
            # 2. 估值 + 熔断
            self.equity = self._mark_to_market(t)
            self.peak = max(self.peak, self.equity)
            dd = (self.peak - self.equity) / self.peak
            if dd >= config.MAX_DRAWDOWN_HARD:
                self._liquidate_all(t, "硬熔断")
                print(f"[{day_key}] 硬熔断触发，清仓")
                break
            # 3. 软线减仓
            if dd >= config.MAX_DRAWDOWN_SOFT:
                self._reduce_half(t, "软线减仓")
            # 4. 信号（大盘关 + 共振）
            mg_ok, mg_reason = market_gate(self.btc[: t + 1])
            if not mg_ok:
                self.last_idx = t
                continue
            if len(self.holdings) >= config.MAX_HOLDINGS:
                self.last_idx = t
                continue
            # RS 分位
            btc_close = [k["close"] for k in self.btc[: t + 1]]
            rs_map = {}
            for sym in self.pool:
                sl = self._coin_slice(sym, t)
                if len(sl) > 20:
                    rs_map[sym] = relative_strength([k["close"] for k in sl], btc_close, 20)
            if rs_map:
                sorted_syms = sorted(rs_map, key=lambda s: rs_map[s], reverse=True)
                denom = max(len(sorted_syms) - 1, 1)
                rank_map = {s: i / denom for i, s in enumerate(sorted_syms)}
            else:
                rank_map = {}
            signals = []
            for sym in self.pool:
                sl = self._coin_slice(sym, t)
                if len(sl) < config.EMA_SLOW + config.BOX_MIN_DAYS:
                    continue
                ok, _, detail = coin_resonance(sl, self.btc[: t + 1], rank_map.get(sym, 1.0))
                if ok:
                    signals.append((sym, detail))
            # 加入候选（每日最多 1 个）
            for sym, detail in signals[:1]:
                if detail and detail.get("box_high"):
                    self.pending.append({
                        "symbol": sym, "box_high": detail["box_high"],
                        "signal_idx": t,
                    })
            self.last_idx = t
        self._save_state()

    def _save_state(self):
        state = {
            "initial_equity": self.initial_equity,
            "cash": self.cash,
            "peak_equity": self.peak,
            "holdings": self.holdings,
            "pending": self.pending,
            "trades": self.trades,
            "last_idx": self.last_idx,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def report(self):
        self.equity = self._mark_to_market(self.last_idx)
        dd = (self.peak - self.equity) / self.peak if self.peak > 0 else 0
        print("\n" + "=" * 56)
        print("纸面交易账户状态")
        print("=" * 56)
        print(f"  初始资金:  {self.initial_equity:,.0f}")
        print(f"  现金:      {self.cash:,.0f}")
        print(f"  持仓市值:  {self.equity - self.cash:,.0f}")
        print(f"  总净值:    {self.equity:,.0f}")
        print(f"  总收益:    {(self.equity/self.initial_equity-1)*100:+.2f}%")
        print(f"  最大回撤:  {dd*100:.2f}%")
        print(f"  持仓数:    {len(self.holdings)}")
        print(f"  候选数:    {len(self.pending)}")
        print(f"  已平仓:    {len(self.trades)} 笔")
        if self.holdings:
            print("\n  当前持仓:")
            for h in self.holdings:
                print(f"    {h['symbol']:<14} 入场 {h['entry_price']:.4g}  止损 {h['stop_loss']:.4g}")
        if self.pending:
            print("\n  待回踩候选:")
            for p in self.pending:
                print(f"    {p['symbol']:<14} 箱体上沿 {p['box_high']:.4g}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="重置状态")
    args = parser.parse_args()

    # 先拉数据（需要知道"今天"的最新索引）
    print("拉取最新数据...")
    btc = fetch_btc_klines()
    pool_tickers = build_observe_pool(config.OBSERVE_POOL_SIZE)
    pool = {}
    for p in pool_tickers:
        try:
            k = fetch_klines(p["instId"])
            if len(k) >= 90:
                pool[p["instId"]] = k
        except Exception:
            pass

    # 首次/重置：从"今天"（最新索引）开始，只推进未来新数据
    if args.reset or not os.path.exists(STATE_FILE):
        state = {
            "initial_equity": 100_000,
            "cash": 100_000,
            "peak_equity": 100_000,
            "holdings": [],
            "pending": [],
            "trades": [],
            "last_idx": len(btc) - 1,  # 从最新开始，不回溯历史
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        print("已初始化纸面账户（10 万虚拟资金），从今天开始")
    else:
        with open(STATE_FILE) as f:
            state = json.load(f)

    pt = PaperTrader(btc, pool, state)
    pt.advance()
    pt.report()


if __name__ == "__main__":
    main()
