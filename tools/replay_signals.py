#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策重放集（影子重放 —— 设计文档 v0.2 §5.1 Q1 答复的落地）。

用历史 K 线（market.db 1H/4H）重放 scan_signal 信号逻辑，生成假设性交易序列。

权限边界（红线级）:
  【证伪权】重放 SQN < 0 / 期望值为负 → 提前停调参、进入退役评估。
  【无证实权】重放结果不得用于宣称改进、不得触发任何自动变更
  （防"把回测当实盘"——多重检验高风险源，受 S3 管束）。
  - 重放集固定化: 时间截断以运行时为准,结果不随后续行情重生成（Q10）。
  - 每笔扣 10bps 往返成本。

运行: cd crypto-agent && python3 tools/replay_signals.py [--symbols BTC,ETH,SOL]
输出: data/replay.db(replay_trades 表) + 控制台摘要。
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from exchange.fake_adapter import FakeAdapter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKET_DB = os.path.join(ROOT, "data", "market.db")
REPLAY_DB = os.path.join(ROOT, "data", "replay.db")
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX", "BNB", "LTC"]
COST_BPS = 10.0          # 往返成本
MAX_HOLD_BARS = 48       # 最长持仓 48 根 1H（2 天）

SYMBOL_WHITELIST = {"BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX", "BNB", "LTC", "AEON"}


def load_klines(symbol, bar="1H"):
    conn = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT open_time, open, high, low, close, volume FROM klines "
            "WHERE inst_id=? AND bar=? ORDER BY open_time", [f"{symbol}-USDT", bar]
        ).fetchall()
        return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]
    finally:
        conn.close()


class ReplayAdapter(FakeAdapter):
    """按 bar 路由 K 线的重放适配器（fake 默认忽略 bar，无法同时喂 1H+4H）。"""

    def __init__(self):
        super().__init__(usdt_free=10000.0)
        self.candles_1h = {}
        self.candles_4h = {}

    def fetch_candles(self, inst_id, bar, limit=100):
        src = self.candles_4h if bar == "4H" else self.candles_1h
        return src.get(inst_id, [])[-limit:]


def init_replay_db():
    conn = sqlite3.connect(REPLAY_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS replay_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_ts REAL, symbol TEXT, ts INTEGER, dir TEXT,
        entry REAL, stop REAL, tp REAL, exit_px REAL,
        exit_reason TEXT, pnl_r REAL, shadow_score REAL,
        regime TEXT, bars_held INTEGER)""")
    conn.commit()
    return conn


def run_symbol(make_trader, symbol, klines, kl4, conn):
    from exchange.models import Candle
    from exchange.models import Instrument

    fake = ReplayAdapter()
    fake._instruments[f"{symbol}-USDT-SWAP"] = Instrument(
        f"{symbol}-USDT-SWAP", symbol, "swap", ct_val=1, lot_sz=1, min_sz=1)
    fake._instruments[f"{symbol}-USDT"] = Instrument(
        f"{symbol}-USDT", symbol, "spot", lot_sz=1e-6, min_sz=1e-6)
    dt = make_trader(fake)
    n = len(klines)
    trades = 0
    # 4H 索引二分定位（每步切 [<=当前1H时间] 的最近 60 根 4H）
    import bisect
    kl4_ts = [k[0] for k in kl4]
    for i in range(100, n):
        win = klines[i - 100:i]
        fake.candles_1h[f"{symbol}-USDT-SWAP"] = [
            Candle(ts=k[0], open=k[1], high=k[2], low=k[3], close=k[4],
                   volume=k[5]) for k in win]
        j = bisect.bisect_right(kl4_ts, win[-1][0])
        win4 = kl4[max(0, j - 60):j]
        if len(win4) >= 20:
            fake.candles_4h[f"{symbol}-USDT-SWAP"] = [
                Candle(ts=k[0], open=k[1], high=k[2], low=k[3], close=k[4],
                       volume=k[5]) for k in win4]
        fake.last_prices[f"{symbol}-USDT-SWAP"] = win[-1][4]
        fake.last_prices[f"{symbol}-USDT"] = win[-1][4]
        try:
            sig = dt.scan_signal(symbol)
        except Exception:
            sig = None
        if not sig:
            continue
        entry, stop, tp, direction = sig["entry"], sig["stop"], sig["tp"], sig["dir"]
        exit_px, reason, held = None, "timeout", 0
        for j in range(i + 1, min(i + MAX_HOLD_BARS + 1, n)):
            bar = klines[j]
            if direction == "long":
                if bar[3] <= stop:
                    exit_px, reason = stop, "止损"
                    break
                if bar[2] >= tp:
                    exit_px, reason = tp, "止盈"
                    break
            else:
                if bar[2] >= stop:
                    exit_px, reason = stop, "止损"
                    break
                if bar[3] <= tp:
                    exit_px, reason = tp, "止盈"
                    break
            held = j - i
        if exit_px is None:
            exit_px = klines[min(i + MAX_HOLD_BARS, n - 1)][4]
            held = MAX_HOLD_BARS
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            continue
        raw_r = ((exit_px - entry) if direction == "long"
                 else (entry - exit_px)) / stop_dist
        pnl_r = round(raw_r - COST_BPS / 10000.0 / stop_dist * entry, 4)
        conn.execute(
            "INSERT INTO replay_trades (run_ts,symbol,ts,dir,entry,stop,tp,"
            "exit_px,exit_reason,pnl_r,shadow_score,regime,bars_held) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [time.time(), symbol, klines[i][0], direction, entry, stop, tp,
             exit_px, reason, pnl_r, sig.get("shadow_score"),
             (sig.get("regime") or {}).get("tag"), held])
        trades += 1
    conn.commit()
    return trades


def summarize():
    conn = sqlite3.connect(REPLAY_DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM replay_trades")]
    conn.close()
    n = len(rows)
    print("=" * 60)
    print("决策重放集摘要（影子数据 · 只给证伪权，不给证实权）")
    print("=" * 60)
    if n == 0:
        print("无重放交易（信号在历史上极少触发——本身即证据）")
        return
    rs = [r["pnl_r"] for r in rows]
    win = sum(1 for x in rs if x > 0)
    exp_r = sum(rs) / n
    print(f"重放交易: {n} 笔 | 胜率 {win/n*100:.1f}% | 期望 {exp_r:+.3f}R")
    if n >= 30:
        m = sum(rs) / n
        sd = (sum((x - m) ** 2 for x in rs) / (n - 1)) ** 0.5
        sqn = (n ** 0.5) * m / sd if sd > 0 else 0
        print(f"SQN(重放): {sqn:.2f} "
              f"({'≥2.5 好' if sqn >= 2.5 else '<2.0 不达标' if sqn < 2.0 else '均值'})")
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r["pnl_r"])
    for s, xs in sorted(by_sym.items()):
        print(f"  {s}: {len(xs)} 笔, 期望 {sum(xs)/len(xs):+.3f}R")
    print("-" * 60)
    print("【证伪权】SQN<0 或期望为负 → 提前停调参/进入退役评估。")
    print("【无证实权】以上结果不得用于宣称改进或触发任何自动变更（S3 管束）。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    symbols = [s for s in symbols if s in SYMBOL_WHITELIST]

    from engines.directional_trader import DirectionalTrader
    from exchange.fake_adapter import FakeAdapter
    tmp_db = os.path.join(ROOT, "data", "replay_scan_decisions.db")

    def make_trader(fake):
        # rt=object() 阻止真实 WS 连接;db_path 隔离(防写生产 scan_decisions)
        return DirectionalTrader(exchange=fake, rt=object(), db_path=tmp_db)

    conn = init_replay_db()
    total = 0
    for s in symbols:
        kl = load_klines(s, "1H")
        kl4 = load_klines(s, "4H")
        if len(kl) < 105:
            print(f"{s}: 1H 数据不足({len(kl)}根),跳过")
            continue
        n = run_symbol(make_trader, s, kl, kl4, conn)
        print(f"{s}: 重放完成 {n} 笔")
        total += n
    conn.close()
    summarize()


if __name__ == "__main__":
    main()
