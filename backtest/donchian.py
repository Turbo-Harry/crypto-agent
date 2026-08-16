"""
Donchian 通道突破策略（严谨版，多空双向）— 择时引擎。
逻辑：收盘价突破过去 N 天最高价（上轨）做多，跌破 N 天最低价（下轨）做空。
相比固定箱体，Donchian 用滚动高低点，更动态地捕捉趋势启动时机。

严谨性：决策用 T 日收盘，成交用 T+1 日开盘价（无未来函数）。
止损：ATR(14)×1.5 动态止损。

参数：
  period：Donchian 周期（默认 20 天）
  max_positions：最大持仓数（默认 5）
  allow_short：是否允许做空（默认 True）
"""
import sys
import os
import bisect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_okx import build_observe_pool, fetch_klines, fetch_btc_klines, list_cached_symbols
from strategy.indicators import atr

FEE = config.FEE_RATE


class DonchianBacktest:
    def __init__(self, pool_klines, btc_klines, initial_equity=100_000):
        self.pool = pool_klines
        self.btc = btc_klines
        self.equity = initial_equity
        self.cash = initial_equity
        self.holdings = {}  # {symbol: {qty(可负), entry_price, stop_price}}
        self.equity_curve = []
        self.timeline = [k["open_time"] for k in btc_klines]
        self.pool_ts = {s: [k["open_time"] for k in kl] for s, kl in pool_klines.items()}
        self.pending_switch = {}  # {symbol: 1多/-1空/0平}
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
        idx = bisect.bisect_left(self.pool_ts[sym], ts)
        if idx < len(self.pool[sym]) and self.pool_ts[sym][idx] == ts:
            return self.pool[sym][idx]["open"]
        return None

    def _donchian(self, sym, ts, period):
        """返回 (上轨, 下轨)：过去 period 天【不含当天】的最高/最低价。"""
        idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
        if idx < period:
            return None
        seg = self.pool[sym][idx - period: idx]  # 不含当天
        highs = [k["high"] for k in seg]
        lows = [k["low"] for k in seg]
        return (max(highs), min(lows))

    def _switch(self, sym, ts, direction):
        """切换到 direction（1多/-1空/0平），用当天开盘价。"""
        px = self._open_at(sym, ts)
        if not px:
            return
        h = self.holdings.get(sym)
        if h:  # 平旧仓
            self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
            del self.holdings[sym]
        if direction != 0:  # 开新仓
            # ATR 动态止损 + 仓位
            idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
            sl = self.pool[sym][:idx + 1]
            a = atr(sl, 14) if len(sl) > 14 else 0
            stop_dist = config.STOP_ATR_MULT * a / px if a > 0 else config.STOP_LOSS
            risk_amount = self.equity * config.RISK_PER_TRADE
            qty = risk_amount / (px * stop_dist) if stop_dist > 0 else 0
            if qty <= 0:
                return
            if direction > 0:  # 做多
                cost = qty * px * (1 + FEE + self._slippage(sym))
                if cost <= self.cash:
                    self.cash -= cost
                    stop = px - config.STOP_ATR_MULT * a if a > 0 else px * (1 - config.STOP_LOSS)
                    self.holdings[sym] = {"qty": qty, "entry_price": px, "stop_price": stop}
            else:  # 做空
                self.cash += qty * px * (1 - FEE - self._slippage(sym))
                stop = px + config.STOP_ATR_MULT * a if a > 0 else px * (1 + config.STOP_LOSS)
                self.holdings[sym] = {"qty": -qty, "entry_price": px, "stop_price": stop}

    def _check_stops(self, t):
        ts = self.timeline[t]
        for sym in list(self.holdings.keys()):
            h = self.holdings[sym]
            idx = bisect.bisect_right(self.pool_ts[sym], ts) - 1
            if idx < 0:
                continue
            k = self.pool[sym][idx]
            if h["qty"] > 0:  # 多单止损
                if k["low"] <= h["stop_price"]:
                    px = min(h["stop_price"], k["open"])
                    self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
                    del self.holdings[sym]
            else:  # 空单止损
                if k["high"] >= h["stop_price"]:
                    px = max(h["stop_price"], k["open"])
                    self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
                    del self.holdings[sym]

    def run(self, period=20, max_positions=5, allow_short=True):
        n = len(self.timeline)
        for t in range(period + 2, n):
            ts = self.timeline[t]
            # 1. 止损
            self._check_stops(t)
            # 2. 执行待切换方向（次日开盘成交）
            for sym, direction in list(self.pending_switch.items()):
                if len(self.holdings) >= max_positions and direction != 0 and sym not in self.holdings:
                    continue
                self._switch(sym, ts, direction)
            self.pending_switch = {}
            # 3. 收盘后判断 Donchian 突破
            for sym in self.pool:
                dc = self._donchian(sym, ts, period)
                if not dc:
                    continue
                upper, lower = dc
                close = self._close_at(sym, ts)
                if close is None:
                    continue
                if close > upper:
                    target = 1
                elif close < lower and allow_short:
                    target = -1
                else:
                    target = 0
                h = self.holdings.get(sym)
                cur_dir = 0 if not h else (1 if h["qty"] > 0 else -1)
                if target != cur_dir:
                    self.pending_switch[sym] = target
            # 4. 估值 + 爆仓检查
            self._mark(t)
            if self.equity <= 0:
                self.cash = 0
                self.holdings = {}
                self.equity = 0
                break
        # 清仓
        ts = self.timeline[-1]
        for sym, h in list(self.holdings.items()):
            px = self._close_at(sym, ts)
            if px:
                self.cash += h["qty"] * px * (1 - FEE - self._slippage(sym))
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
    print(f"{name:<26} | 收益{stats['总收益率']*100:>+8.1f}% | "
          f"年化{stats['年化收益']*100:>+6.1f}% | 回撤{stats['最大回撤']*100:>5.1f}%")


if __name__ == "__main__":
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 76)
    configs = [
        ("只做多 周期20", {"period": 20, "allow_short": False}),
        ("多空 周期20", {"period": 20}),
        ("多空 周期10", {"period": 10}),
        ("多空 周期30", {"period": 30}),
        ("多空 周期55", {"period": 55}),
    ]
    for name, ov in configs:
        bt = DonchianBacktest(pool, btc)
        stats = bt.run(**ov)
        print_row(name, stats)
    print("=" * 76)
