"""
均值回归策略 — RSI 超卖买入、超买卖出。
逻辑：价格偏离过大（RSI<30 超卖）时买入，回归（RSI>70 超买）时卖出。
在震荡市有效，趋势市容易亏损。

参数：
  rsi_period：RSI 周期（默认 14）
  oversold：超卖阈值（默认 30）
  overbought：超买阈值（默认 70）
  max_hold：最长持有天数（默认 20，防止久套）
"""
import sys
import os
import bisect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols

FEE = config.SLIPPAGE + config.FEE_RATE


def rsi(closes, period=14):
    """RSI 指标，返回与输入等长的列表。"""
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    out = [50.0] * len(closes)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    # 初始平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
        # Wilder 平滑
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
    return out


class MeanReversion:
    def __init__(self, pool_klines, btc_klines, initial_equity=100_000):
        self.pool = pool_klines
        self.btc = btc_klines
        self.equity = initial_equity
        self.cash = initial_equity
        self.holdings = {}  # {symbol: {qty, entry_price, days}}
        self.equity_curve = []
        self.timeline = [k["open_time"] for k in btc_klines]
        self.pool_ts = {s: [k["open_time"] for k in kl] for s, kl in pool_klines.items()}
        # 预计算每个币的 RSI
        self.rsi_map = {}
        for sym, kl in pool_klines.items():
            self.rsi_map[sym] = rsi([k["close"] for k in kl])

    def _close_at(self, sym, ts):
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < 0:
            return None
        return self.pool[sym][idx]["close"]

    def _rsi_at(self, sym, ts):
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < 0:
            return None
        return self.rsi_map[sym][idx]

    def run(self, rsi_period=14, oversold=30, overbought=70, max_hold=20,
            max_positions=5):
        n = len(self.timeline)
        for t in range(rsi_period + 2, n):
            ts = self.timeline[t]

            # 1. 检查持仓：超买卖出 / 超时卖出
            for sym in list(self.holdings.keys()):
                h = self.holdings[sym]
                h["days"] += 1
                r = self._rsi_at(sym, ts)
                px = self._close_at(sym, ts)
                if px is None:
                    continue
                if (r is not None and r >= overbought) or h["days"] >= max_hold:
                    self.cash += h["qty"] * px * (1 - FEE)
                    del self.holdings[sym]

            # 2. 超卖买入（RSI < oversold）
            if len(self.holdings) < max_positions:
                for sym in self.pool:
                    if len(self.holdings) >= max_positions:
                        break
                    if sym in self.holdings:
                        continue
                    r = self._rsi_at(sym, ts)
                    if r is not None and r <= oversold:
                        px = self._close_at(sym, ts)
                        if px:
                            alloc = self.equity / max_positions
                            qty = alloc / px
                            cost = qty * px * (1 + FEE)
                            if cost <= self.cash:
                                self.cash -= cost
                                self.holdings[sym] = {
                                    "qty": qty, "entry_price": px, "days": 0}

            self._mark(t)

        # 清仓
        for sym in list(self.holdings.keys()):
            px = self._close_at(sym, self.timeline[-1])
            if px:
                self.cash += self.holdings[sym]["qty"] * px * (1 - FEE)
        self.holdings = {}
        return self._stats()

    def _mark(self, t):
        ts = self.timeline[t]
        mv = self.cash
        for sym, h in self.holdings.items():
            px = self._close_at(sym, ts)
            if px:
                mv += h["qty"] * px
        self.equity = mv
        self.equity_curve.append(mv)

    def _stats(self):
        initial = 100_000
        final = self.equity_curve[-1] if self.equity_curve else initial
        peak = -1e18
        max_dd = 0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        years = len(self.equity_curve) / 365
        annual = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else 0
        return {
            "总收益率": (final - initial) / initial,
            "年化收益": annual,
            "最大回撤": max_dd,
            "期末资金": final,
        }


def load_data():
    try:
        pool = build_observe_pool(config.OBSERVE_POOL_SIZE)
        symbols = [p["instId"] for p in pool]
    except Exception:
        symbols = list_cached_symbols()
    btc = fetch_btc_klines()
    pool_klines = {}
    for sym in symbols:
        try:
            k = fetch_klines(sym)
            if len(k) >= 90:
                pool_klines[sym] = k
        except Exception:
            pass
    return btc, pool_klines


def print_row(name, stats):
    print(f"{name:<28} | 收益{stats['总收益率']*100:>+8.1f}% | "
          f"年化{stats['年化收益']*100:>+6.1f}% | 回撤{stats['最大回撤']*100:>5.1f}%")


if __name__ == "__main__":
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 80)
    configs = [
        ("RSI<30买/>70卖", {}),
        ("RSI<20买/>80卖", {"oversold": 20, "overbought": 80}),
        ("RSI<35买/>65卖", {"oversold": 35, "overbought": 65}),
        ("RSI<30买/>70卖+持仓10天", {"max_hold": 10}),
        ("RSI<30买/>70卖+持仓30天", {"max_hold": 30}),
        ("RSI<30买/>70卖+前10", {"max_positions": 10}),
    ]
    for name, ov in configs:
        bt = MeanReversion(pool, btc)
        stats = bt.run(**ov)
        print_row(name, stats)
    print("=" * 80)
