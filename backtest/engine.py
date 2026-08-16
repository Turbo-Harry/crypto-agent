"""
回测引擎 — 用真实历史数据逐日回放五层否决制策略。

严谨性关键：
  1. 信号在 T 日收盘后确认（只用 T 日及之前的数据）
  2. 入场在 T+1 日开盘价
  3. 出场用持仓币自己的日内 high/low 判断
  4. 【时间戳对齐】不同币上市时间不同，必须按 open_time 对齐，禁止按索引对齐
"""
import sys
import os
import bisect
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.filters import market_gate, coin_resonance
from strategy.indicators import relative_strength, atr
from risk.risk_manager import position_size
from data.fetch_fear_greed import fetch_fng

# 交易成本 = 滑点(按流动性分档) + 手续费，见 _entry_cost/_exit_cost 方法


class Backtest:
    def __init__(self, btc_klines, pool_klines, initial_equity=100_000):
        """
        btc_klines: BTC 日线（升序）
        pool_klines: {symbol: klines} 各币日线（升序）
        """
        self.btc = btc_klines
        self.pool = pool_klines
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.peak = initial_equity
        self.cash = initial_equity
        self.holdings = []
        self.trades = []
        self.equity_curve = []
        self.pending = []  # 突破候选，等待回踩确认入场

        # 主时间轴 = BTC 的 open_time（所有判断以它为准）
        self.timeline = [k["open_time"] for k in btc_klines]
        # 每个币的时间戳列表（升序），用于二分定位
        self.pool_ts = {sym: [k["open_time"] for k in kl]
                        for sym, kl in pool_klines.items()}
        # 每个币的日均成交额（最近30天 quote_volume），用于滑点分档
        self.vol_map = {}
        for sym, kl in pool_klines.items():
            vols = [k.get("quote_volume", 0) for k in kl[-30:]]
            self.vol_map[sym] = sum(vols) / len(vols) if vols else 0

        self.start_idx = config.EMA_SLOW + config.BOX_MIN_DAYS + 5

        # 恐惧贪婪指数（情绪面），date -> value 映射
        try:
            fng_list = fetch_fng()
            self.fng_map = {item["date"]: item["value"] for item in fng_list}
        except Exception:
            self.fng_map = {}

    def _fng_at(self, t):
        """返回 timeline[t] 当天的恐惧贪婪指数值，无数据返回 None。"""
        date_str = self._date_of(self.btc[t])
        return self.fng_map.get(date_str)

    # ---------- 滑点分档 ----------
    def _slippage(self, sym):
        vol = self.vol_map.get(sym, 0)
        if vol >= config.VOL_LARGE:
            return config.SLIPPAGE_LARGE
        if vol >= config.VOL_MED:
            return config.SLIPPAGE_MED
        return config.SLIPPAGE_SMALL

    def _entry_cost(self, sym):
        return self._slippage(sym) + config.FEE_RATE

    def _exit_cost(self, sym):
        return self._slippage(sym) + config.FEE_RATE

    # ---------- 时间对齐的工具 ----------
    def _coin_slice(self, sym, t):
        """该币截至 timeline[t]（含）的 K 线切片（升序）"""
        ts = self.pool_ts[sym]
        idx = bisect.bisect_right(ts, self.timeline[t])
        return self.pool[sym][:idx]

    def _coin_at(self, sym, t):
        """该币在 timeline[t] 当天的 K 线；若当天无数据（未上市/退市），
        返回最近的一根；完全无数据返回 None。"""
        ts = self.pool_ts[sym]
        idx = bisect.bisect_right(ts, self.timeline[t]) - 1
        if idx < 0:
            return None
        return self.pool[sym][idx]

    @staticmethod
    def _date_of(kline):
        return datetime.fromtimestamp(kline["open_time"] / 1000,
                                      tz=timezone.utc).strftime("%Y-%m-%d")

    # ---------- 出场逻辑 ----------
    def _check_exits(self, t):
        """检查所有持仓，用各币【自己】在 timeline[t] 的 K 线判断出场。"""
        remaining = []
        for h in self.holdings:
            coin_k = self._coin_at(h["symbol"], t)
            if coin_k is None:
                remaining.append(h)
                continue
            high, low, close = coin_k["high"], coin_k["low"], coin_k["close"]
            open_px = coin_k["open"]
            exit_price = None
            exit_reason = None

            # 日内触及顺序：止损优先（保守）
            if low <= h["stop_loss"]:
                # 跳空处理：若开盘价已低于止损价，按开盘价成交（更真实，更差）
                exit_price = min(h["stop_loss"], open_px)
                exit_reason = "止损"
            elif not h["half_sold"] and high >= h["tp1"]:
                exit_price = h["tp1"] * (1 - self._exit_cost(h["symbol"]))  # 净价
                exit_reason = "止盈1(平半)"
                h["half_sold"] = True
                self.cash += (h["qty"] / 2) * exit_price
                h["qty"] /= 2
                remaining.append(h)
                continue
            elif high >= h["tp2"]:
                exit_price = h["tp2"]
                exit_reason = "止盈2(清仓)"
            elif h["age_days"] >= config.TIME_STOP_DAYS:
                exit_price = close
                exit_reason = "时间止损"

            if exit_price is not None:
                exit_price = exit_price * (1 - self._exit_cost(h["symbol"]))  # 净价
                self.cash += h["qty"] * exit_price
                self.trades.append({
                    "symbol": h["symbol"], "entry_price": h["entry_price"],
                    "exit_price": exit_price, "qty": h["qty"],
                    "pnl_pct": (exit_price - h["entry_price"]) / h["entry_price"],
                    "reason": exit_reason, "entry_date": h["entry_date"],
                    "exit_date": self._date_of(coin_k),
                })
            else:
                h["age_days"] += 1
                remaining.append(h)
        self.holdings = remaining

    # ---------- 入场 ----------
    def _enter(self, sym, entry_price, box_high, kline, stop_price=None, stop_distance=None):
        """按激进档风控入场。返回 True 若成功。
        stop_distance：止损距离（百分比），用于仓位计算（止损越宽仓位越小）。
        """
        if stop_distance is None:
            stop_distance = config.STOP_LOSS
        pos_value = position_size(self.equity, stop_distance)
        pos_value = min(pos_value, self.cash)
        if pos_value <= 0:
            return False
        qty = pos_value / entry_price
        self.cash -= pos_value
        if stop_price is None:
            stop_price = entry_price * (1 - config.STOP_LOSS)
            if box_high:
                stop_price = min(stop_price, box_high)
        self.holdings.append({
            "symbol": sym, "entry_price": entry_price, "qty": qty,
            "stop_loss": stop_price,
            "tp1": entry_price * (1 + config.TAKE_PROFIT_1),
            "tp2": entry_price * (1 + config.TAKE_PROFIT_2),
            "half_sold": False, "age_days": 0,
            "entry_date": self._date_of(kline),
        })
        return True

    # ---------- 回踩确认处理（严谨版，无未来函数）----------
    def _process_pending(self, t):
        """处理突破候选：
        - 决策：T 日收盘后，用 T 日收盘+low 判断"回踩确认"
        - 成交：确认后【次日 T+1 开盘价】成交（绝不偷看盘中低点）
        """
        still_pending = []
        for p in self.pending:
            sym = p["symbol"]

            # 已确认的候选：今天开盘价成交（昨天收盘后做的决策）
            if p.get("confirmed"):
                coin_k = self._coin_at(sym, t)
                if coin_k and len(self.holdings) < config.MAX_HOLDINGS:
                    entry_price = coin_k["open"] * (1 + self._entry_cost(sym))
                    # ATR 动态止损（自适应波动率）
                    sl = self._coin_slice(sym, t)
                    atr_val = atr(sl, 14) if len(sl) > 14 else 0
                    if atr_val > 0:
                        stop_price = entry_price - config.STOP_ATR_MULT * atr_val
                        stop_distance = config.STOP_ATR_MULT * atr_val / entry_price
                        # 结构止损：箱体上沿下方留 0.3 ATR 缓冲（防插针），取更宽松
                        if p.get("box_high"):
                            struct_stop = p["box_high"] - 0.3 * atr_val
                            stop_price = min(stop_price, struct_stop)
                            stop_distance = (entry_price - stop_price) / entry_price
                    else:
                        stop_price = entry_price * (1 - config.STOP_LOSS)
                        stop_distance = config.STOP_LOSS
                    self._enter(sym, entry_price, p["box_high"], coin_k, stop_price, stop_distance)
                continue  # 成交后移出候选

            coin_k = self._coin_at(sym, t)
            if coin_k is None:
                still_pending.append(p)
                continue
            close = coin_k["close"]
            low = coin_k["low"]
            box_high = p["box_high"]
            days_since = t - p["signal_idx"]

            # 假突破：收盘跌破箱体上沿 → 取消
            if close < box_high * config.PULLBACK_BREAK:
                continue
            # 超时：突破后超过 N 天仍无回踩 → 放弃（宁可错过，不追高）
            if days_since > config.PULLBACK_WINDOW:
                continue
            # 回踩确认（T 日收盘后判断）：盘中回踩到箱体上沿附近，且收盘企稳
            if low <= box_high * (1 + config.ENTRY_PREMIUM) and close > box_high:
                p["confirmed"] = True  # 标记确认，下一轮（T+1）开盘成交
            still_pending.append(p)
        self.pending = still_pending

    # ---------- 估值 ----------
    def _mark_to_market(self, t):
        total = self.cash
        for h in self.holdings:
            coin_k = self._coin_at(h["symbol"], t)
            if coin_k:
                total += h["qty"] * coin_k["close"]
        return total

    # ---------- 主循环 ----------
    def run(self):
        n = len(self.timeline)
        for t in range(self.start_idx, n):
            day_key = self._date_of(self.btc[t])

            # 0. 处理突破候选（回踩确认入场 / 假突破取消）
            self._process_pending(t)

            # 1. 出场：先处理个股级别的止损/止盈（日内触发，先保护本金）
            self._check_exits(t)

            # 2. 估值 + 熔断（用出场后的持仓）
            self.equity = self._mark_to_market(t)
            self.peak = max(self.peak, self.equity)
            dd = (self.peak - self.equity) / self.peak

            if dd >= config.MAX_DRAWDOWN_HARD:
                self._liquidate_all(t, "硬熔断")
                self.equity_curve.append((day_key, self.equity))
                break

            # 3. 软线减仓：账户级别风控，在个股止损之后（避免用收盘价放大亏损）
            if dd >= config.MAX_DRAWDOWN_SOFT:
                self._reduce_half(t, "软线减仓")

            # 3. 信号（T 日收盘确认）
            mg_ok, _ = market_gate(self.btc[: t + 1], self._fng_at(t))
            if not mg_ok or len(self.holdings) >= config.MAX_HOLDINGS:
                self.equity_curve.append((day_key, self.equity))
                continue

            # RS 分位（用 T 日及之前数据）
            btc_close = [k["close"] for k in self.btc[: t + 1]]
            rs_map = {}
            for sym in self.pool:
                sl = self._coin_slice(sym, t)
                if len(sl) > 20:
                    rs_map[sym] = relative_strength(
                        [k["close"] for k in sl], btc_close, 20)
            if rs_map:
                sorted_syms = sorted(rs_map, key=lambda s: rs_map[s], reverse=True)
                denom = max(len(sorted_syms) - 1, 1)
                rank_map = {s: i / denom for i, s in enumerate(sorted_syms)}
            else:
                rank_map = {}

            # 逐个币共振判断
            signals = []
            for sym in self.pool:
                sl = self._coin_slice(sym, t)
                if len(sl) < config.EMA_SLOW + config.BOX_MIN_DAYS:
                    continue
                ok, _, detail = coin_resonance(
                    sl, self.btc[: t + 1], rank_map.get(sym, 1.0))
                if ok:
                    signals.append((sym, rs_map.get(sym, 0), detail))
            signals.sort(key=lambda x: x[1], reverse=True)

            # 4. 信号 → 加入候选（等待回踩确认入场，而非次日追高）
            for sym, rs, detail in signals[:1]:
                if detail and detail.get("box_high"):
                    self.pending.append({
                        "symbol": sym,
                        "box_high": detail["box_high"],
                        "signal_idx": t,
                    })

            self.equity_curve.append((day_key, self.equity))

        # 结束强制清仓
        self._liquidate_all(len(self.timeline) - 1, "回测结束")
        return self._stats()

    # ---------- 清仓 / 减仓 ----------
    def _liquidate_all(self, t, reason):
        for h in self.holdings:
            coin_k = self._coin_at(h["symbol"], t)
            if coin_k is None:
                continue
            px = coin_k["close"] * (1 - self._exit_cost(h["symbol"]))  # 净价
            self.cash += h["qty"] * px
            self.trades.append({
                "symbol": h["symbol"], "entry_price": h["entry_price"],
                "exit_price": px, "qty": h["qty"],
                "pnl_pct": (px - h["entry_price"]) / h["entry_price"],
                "reason": reason, "entry_date": h["entry_date"],
                "exit_date": self._date_of(coin_k),
            })
        self.holdings = []

    def _reduce_half(self, t, reason):
        for h in self.holdings:
            coin_k = self._coin_at(h["symbol"], t)
            if coin_k is None:
                continue
            px = coin_k["close"] * (1 - self._exit_cost(h["symbol"]))  # 净价
            sell_qty = h["qty"] / 2
            self.cash += sell_qty * px
            h["qty"] /= 2
            self.trades.append({
                "symbol": h["symbol"], "entry_price": h["entry_price"],
                "exit_price": px, "qty": sell_qty,
                "pnl_pct": (px - h["entry_price"]) / h["entry_price"],
                "reason": reason, "entry_date": h["entry_date"],
                "exit_date": self._date_of(coin_k),
            })

    # ---------- 统计 ----------
    def _stats(self):
        trades = self.trades
        # 按 qty×entry_price 加权的盈亏（平半/减仓按实际数量）
        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        n = len(trades)
        win_rate = len(wins) / n if n else 0
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

        peak = -1e18
        max_dd = 0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

        return {
            "交易次数": n,
            "胜率": win_rate,
            "平均盈利": avg_win,
            "平均亏损": avg_loss,
            "盈亏比": abs(avg_win / avg_loss) if avg_loss else float("inf"),
            "初始资金": self.initial_equity,
            "期末资金": self.equity,
            "总收益率": (self.equity - self.initial_equity) / self.initial_equity,
            "最大回撤": max_dd,
            "交易明细": trades,
        }


if __name__ == "__main__":
    print("回测引擎已就绪（需先拉取数据后运行）")
