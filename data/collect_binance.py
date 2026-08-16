"""
币安采集脚本 — 多线程采集加密币 + 美股代币的多周期 K 线到 SQLite。
与 OKX 采集共用同一个 market.db（symbol 格式不同，不冲突）。

用法：
  python3 data/collect_binance.py --bars 1m,15m,1h,4h,1d --all
  python3 data/collect_binance.py --bar 1d --stocks
  python3 data/collect_binance.py --bar 1m --top 20
"""
import sys
import os
import sqlite3
import argparse
import time
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch_binance import build_observe_pool, fetch_stock_symbols

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")
BASE = "https://data-api.binance.vision"
ALL_BARS = ["1m", "15m", "1h", "4h", "1d"]


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            inst_id TEXT NOT NULL,
            bar TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_volume REAL,
            PRIMARY KEY (inst_id, bar, open_time)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bar ON klines(bar)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_bar ON klines(inst_id, bar)")
    conn.commit()
    return conn


def fetch_candles(symbol, bar, limit=1000):
    """拉最近 limit 根 K 线，返回倒序原始数据。"""
    try:
        d = _get(f"{BASE}/api/v3/klines?symbol={symbol}&interval={bar}&limit={limit}")
        return d
    except Exception:
        return []


def save(conn, symbol, bar, candles):
    rows = []
    for c in candles:
        rows.append((
            symbol, bar, int(c[0]),
            float(c[1]), float(c[2]), float(c[3]), float(c[4]),
            float(c[5]), float(c[7]),
        ))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def build_symbols(args):
    symbols = []
    if args.inst:
        symbols = [args.inst]
    else:
        if args.crypto or args.all or (not args.stocks and not args.crypto):
            pool = build_observe_pool(config.OBSERVE_POOL_SIZE)
            if args.top:
                pool = pool[:args.top]
            symbols.extend(p["symbol"] for p in pool)
        if args.stocks or args.all:
            symbols.extend(fetch_stock_symbols())
    seen = set()
    return [s for s in symbols if not (s in seen or seen.add(s))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bar", default=None)
    parser.add_argument("--bars", default=None)
    parser.add_argument("--inst", default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--stocks", action="store_true")
    parser.add_argument("--crypto", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--threads", type=int, default=10)
    args = parser.parse_args()

    bars = ([b.strip() for b in args.bars.split(",")] if args.bars
            else [args.bar] if args.bar else ALL_BARS)
    symbols = build_symbols(args)
    conn = init_db()
    print(f"币安采集: {len(symbols)} 标的 × {len(bars)} 周期 ({'、'.join(bars)}), {args.threads}线程")

    total_new = 0
    t0 = time.time()
    tasks = [(sym, bar) for sym in symbols for bar in bars]
    done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(fetch_candles, sym, bar): (sym, bar)
                   for sym, bar in tasks}
        for fut in as_completed(futures):
            sym, bar = futures[fut]
            done += 1
            try:
                candles = fut.result()
                if candles:
                    total_new += save(conn, sym, bar, candles)
            except Exception:
                pass
            if done % 100 == 0:
                print(f"  已处理 {done}/{len(tasks)}")

    cnt = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    dt = time.time() - t0
    print(f"采集完成: {len(tasks)} 任务, 新增 {total_new} 条, 库内累计 {cnt} 条, 耗时 {dt:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
