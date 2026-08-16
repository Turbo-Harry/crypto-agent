"""
动量轮动策略（严谨版）— 做多动量最强，做空动量最弱。
严谨性：决策用 T 日收盘数据，成交用 T+1 日开盘价（不偷看未来）。

参数：
  lookback：动量回顾期（默认 30 天）
  rebalance：调仓周期（默认 7 天）
  top_n：做多币数（默认 5）
  short_n：做空币数（默认 5）
  stop_loss_pct：单币止损（默认 20%）
"""
import sys
import os
import bisect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols

FEE = config.FEE_RATE


class MomentumBacktest:
    def __init__(self, pool_klines, btc_klines, initial_equity=100_000):
        self.pool = pool_klines
        self.btc = btc_klines
        self.equity = initial_equity
        self.cash = initial_equity
        self.holdings = {}  # {symbol: {qty(可负), entry_price}}
        self.equity_curve = []
        self.timeline = [k["open_time"] for k in btc_klines]
        self.pool_ts = {s: [k["open_time"] for k in kl] for s, kl in pool_klines.items()}
        self.pending_target = None  # 待执行的调仓目标 {symbol: target_qty}
        # 成交额分档
        self.vol_map = {}
        for sym, kl in pool_klines.items():
            vols = [k.get("quote_volume", 0) for k in kl[-30:]]
            self.vol_map[sym] = sum(vols) / len(vols) if vols else 0

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
        """该币在 ts 当天的开盘价；当天无数据返回 None。"""
        idx = bisect.bisect_left(self.pool_ts[sym], ts)
        if idx < len(self.pool[sym]) and self.pool_ts[sym][idx] == ts:
            return self.pool[sym][idx]["open"]
        return None

    def _kline_at(self, sym, ts):
        """该币在 ts 当天（或最近）的 K 线。"""
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < 0:
            return None
        return self.pool[sym][idx]

    def _momentum(self, sym, ts, lookback):
        c_now = self._close_at(sym, ts)
        c_past = self._close_at(sym, ts - lookback * 86400000)
        if c_now is None or c_past is None or c_past <= 0:
            return None
        return (c_now - c_past) / c_past

    # ---- 止损（挂单逻辑：用当天 high/low 判断，跳空按开盘价）----
    def _check_stops(self, t, stop_loss_pct):
        ts = self.timeline[t]
        for sym in list(self.holdings.keys()):
            h = self.holdings[sym]
            k = self._kline_at(sym, ts)
            if k is None:
                continue
            if h["qty"] > 0:  # 多单止损：跌破 stop 价
                stop = h["entry_price"] * (1 - stop_loss_pct)
                if k["low"] <= stop:
                    px = min(stop, k["open"])  # 跳空按开盘价
                    self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
                    del self.holdings[sym]
            else:  # 空单止损：涨破 stop 价
                stop = h["entry_price"] * (1 + stop_loss_pct)
                if k["high"] >= stop:
                    px = max(stop, k["open"])
                    self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
                    del self.holdings[sym]

    # ---- 执行调仓（用 T 日开盘价）----
    def _execute_target(self, t):
        ts = self.timeline[t]
        target = self.pending_target
        if target is None:
            return
        # 平掉不在目标里的仓（用 T 开盘价）
        for sym in list(self.holdings.keys()):
            if sym not in target:
                px = self._open_at(sym, ts)
                if px:
                    qty = self.holdings[sym]["qty"]
                    self.cash += qty * px * (1 - FEE - self._slippage(sym))
                    del self.holdings[sym]
        # 调整到目标仓位（用 T 开盘价）
        for sym, target_qty in target.items():
            px = self._open_at(sym, ts)
            if not px:
                continue
            cur_qty = self.holdings.get(sym, {}).get("qty", 0)
            if abs(target_qty - cur_qty) < 1e-9:
                continue
            # 平旧仓
            if cur_qty != 0:
                self.cash += cur_qty * px * (1 - FEE - self._slippage(sym))
            # 开新仓
            if target_qty > 0:
                cost = target_qty * px * (1 + FEE + self._slippage(sym))
                if cost <= self.cash:
                    self.cash -= cost
                    self.holdings[sym] = {"qty": target_qty, "entry_price": px}
            elif target_qty < 0:
                self.cash += (-target_qty) * px * (1 - FEE - self._slippage(sym))
                self.holdings[sym] = {"qty": target_qty, "entry_price": px}

    # ---- 计算调仓目标（用 T 日收盘数据）----
    def _compute_target(self, t, lookback, top_n, short_n):
        ts = self.timeline[t]
        mom = {}
        for sym in self.pool:
            m = self._momentum(sym, ts, lookback)
            if m is not None:
                mom[sym] = m
        if not mom:
            self.pending_target = {}
            return
        ranked = sorted(mom, key=lambda s: mom[s], reverse=True)
        longs = ranked[:top_n]
        shorts = ranked[-short_n:] if short_n > 0 else []
        # 目标仓位：等权
        n_pos = top_n + len(shorts)
        if n_pos == 0:
            self.pending_target = {}
            return
        target = {}
        for sym in longs:
            target[sym] = self.equity / n_pos / self._close_at(sym, ts)
        for sym in shorts:
            target[sym] = -self.equity / n_pos / self._close_at(sym, ts)
        self.pending_target = target

    def run(self, lookback=30, rebalance=7, top_n=5, short_n=5,
            stop_loss_pct=0.20):
        n = len(self.timeline)
        start = lookback + 5
        for t in range(start, n):
            # 1. 止损（挂单，用当天 high/low）
            self._check_stops(t, stop_loss_pct)
            # 2. 执行调仓（上一决策日定的 target，用今天开盘价）
            self._execute_target(t)
            # 3. 调仓决策日（每 rebalance 天，用今天收盘数据算新 target）
            if t % rebalance == 0:
                self._compute_target(t, lookback, top_n, short_n)
            # 4. 估值
            self._mark(t)
            # 爆仓检查：净值归零（做空亏损超本金），清仓停止
            if self.equity <= 0:
                self.cash = 0
                self.holdings = {}
                self.equity = 0
                break

        # 清仓
        ts = self.timeline[-1]
        for sym in list(self.holdings.keys()):
            px = self._close_at(sym, ts)
            if px:
                self.cash += self.holdings[sym]["qty"] * px * (1 - FEE - self._slippage(sym))
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
        ("只做多 前5", {"short_n": 0}),
        ("多空 前5后5", {"top_n": 5, "short_n": 5}),
        ("多空 前10后5", {"top_n": 10, "short_n": 5}),
        ("多空 前5后10", {"top_n": 5, "short_n": 10}),
        ("多空 前10后10", {"top_n": 10, "short_n": 10}),
    ]
    for name, ov in configs:
        bt = MomentumBacktest(pool, btc)
        stats = bt.run(**ov)
        print_row(name, stats)
    print("=" * 80)
