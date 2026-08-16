"""
双均线趋势策略（严谨版，多空双向）— 金叉做多、死叉做空。
严谨性：决策用 T 日收盘 EMA，成交用 T+1 日开盘价（不偷看未来）。
"""
import sys
import os
import bisect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols
from strategy.indicators import ema

FEE = config.FEE_RATE


class TrendFollow:
    def __init__(self, pool_klines, btc_klines, initial_equity=100_000):
        self.pool = pool_klines
        self.btc = btc_klines
        self.equity = initial_equity
        self.cash = initial_equity
        self.holdings = {}
        self.equity_curve = []
        self.timeline = [k["open_time"] for k in btc_klines]
        self.pool_ts = {s: [k["open_time"] for k in kl] for s, kl in pool_klines.items()}
        self.ema_map = {}
        for sym, kl in pool_klines.items():
            closes = [k["close"] for k in kl]
            self.ema_map[sym] = (ema(closes, 20), ema(closes, 50))
        self.vol_map = {}
        for sym, kl in pool_klines.items():
            vols = [k.get("quote_volume", 0) for k in kl[-30:]]
            self.vol_map[sym] = sum(vols) / len(vols) if vols else 0
        self.pending_switch = {}

    def _slippage(self, sym):
        vol = self.vol_map.get(sym, 0)
        if vol >= config.VOL_LARGE:
            return config.SLIPPAGE_LARGE
        if vol >= config.VOL_MED:
            return config.SLIPPAGE_MED
        return config.SLIPPAGE_SMALL

    def _close_at(self, sym, ts):
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < 0:
            return None
        return self.pool[sym][idx]["close"]

    def _open_at(self, sym, ts):
        idx = bisect.bisect_left(self.pool_ts[sym], ts)
        if idx < len(self.pool[sym]) and self.pool_ts[sym][idx] == ts:
            return self.pool[sym][idx]["open"]
        return None

    def _trend_at(self, sym, ts):
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < 0:
            return None
        fast, slow = self.ema_map[sym]
        return (fast[idx], slow[idx])

    def _switch_position(self, sym, ts, direction):
        px = self._open_at(sym, ts)
        if not px:
            return
        cur_qty = self.holdings.get(sym, 0)
        if cur_qty != 0:
            self.cash += cur_qty * px * (1 - FEE - self._slippage(sym))
            del self.holdings[sym]
        if direction != 0:
            alloc = self.equity / 5
            qty = alloc / px
            if direction > 0:
                cost = qty * px * (1 + FEE + self._slippage(sym))
                if cost <= self.cash:
                    self.cash -= cost
                    self.holdings[sym] = qty
            else:
                self.cash += qty * px * (1 - FEE - self._slippage(sym))
                self.holdings[sym] = -qty

    def run(self, fast=20, slow=50, max_positions=5, allow_short=True):
        n = len(self.timeline)
        for t in range(slow + 2, n):
            ts = self.timeline[t]
            for sym, direction in list(self.pending_switch.items()):
                if len(self.holdings) >= max_positions and direction != 0 and sym not in self.holdings:
                    continue
                self._switch_position(sym, ts, direction)
            self.pending_switch = {}
            for sym in self.pool:
                tr = self._trend_at(sym, ts)
                if not tr:
                    continue
                fast_v, slow_v = tr
                if fast_v > slow_v:
                    target_dir = 1
                elif fast_v < slow_v and allow_short:
                    target_dir = -1
                else:
                    target_dir = 0
                cur_qty = self.holdings.get(sym, 0)
                cur_dir = 1 if cur_qty > 0 else (-1 if cur_qty < 0 else 0)
                if target_dir != cur_dir:
                    self.pending_switch[sym] = target_dir
            self._mark(t)
            # 爆仓检查：净值归零（做空亏损超过本金），清仓停止
            if self.equity <= 0:
                self.cash = 0
                self.holdings = {}
                self.equity = 0
                break
        ts = self.timeline[-1]
        for sym, qty in list(self.holdings.items()):
            px = self._close_at(sym, ts)
            if px:
                self.cash += qty * px * (1 - FEE - self._slippage(sym))
        self.holdings = {}
        return self._stats()

    def _mark(self, t):
        ts = self.timeline[t]
        mv = self.cash
        for sym, qty in self.holdings.items():
            px = self._close_at(sym, ts)
            if px:
                mv += qty * px
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
        return {"总收益率": (final - initial) / initial, "年化收益": annual,
                "最大回撤": max_dd, "期末资金": final}


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
        ("只做多 EMA20/50", {"allow_short": False}),
        ("多空 EMA20/50", {"allow_short": True}),
        ("多空 EMA10/30", {"allow_short": True, "fast": 10, "slow": 30}),
        ("多空 EMA30/100", {"allow_short": True, "fast": 30, "slow": 100}),
    ]
    for name, ov in configs:
        bt = TrendFollow(pool, btc)
        stats = bt.run(**ov)
        print_row(name, stats)
    print("=" * 80)
