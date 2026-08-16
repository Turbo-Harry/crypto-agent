"""
数据采集脚本 — 定时任务用，多线程增量捞取 K 线到本地 SQLite。
支持全部周期（1m/15m/1H/4H/1D）和全部标的（加密币 + 美股代币）。

用法：
  python3 data/collect.py --bars 1m,15m,1H,4H,1D --all     # 全周期全标的
  python3 data/collect.py --bar 1D                         # 单周期（加密观察池）
  python3 data/collect.py --bar 1m --stocks                # 单周期（美股）
  python3 data/collect.py --bar 1m --inst BTC-USDT         # 指定单币
  python3 data/collect.py --bar 1m --top 20                # 加密前20个

存储：data/market.db (SQLite)，表 klines，主键 (inst_id, bar, open_time) 自动去重。
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
from data.fetch_okx import build_observe_pool, fetch_stock_symbols

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")
BASE = "https://www.okx.com"
ALL_BARS = ["1m", "15m", "1H", "4H", "1D"]


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
    # 索引：按周期查询、按标的+周期查询（避免大数据量全表扫描）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bar ON klines(bar)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_bar ON klines(inst_id, bar)")
    conn.commit()
    return conn


def fetch_candles(inst_id, bar, limit=100):
    """拉最近 limit 根 K 线（含最新），返回倒序原始数据。"""
    try:
        d = _get(f"{BASE}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}")
        if d.get("code") == "0":
            return d.get("data", [])
    except Exception:
        pass
    return []


def save(conn, inst_id, bar, candles):
    """增量写入，INSERT OR IGNORE 去重。返回新增条数。"""
    rows = []
    for c in candles:
        rows.append((
            inst_id, bar, int(c[0]),
            float(c[1]), float(c[2]), float(c[3]), float(c[4]),
            float(c[5]), float(c[6]),
        ))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def build_symbols(args):
    """根据参数确定要采集的标的列表。"""
    symbols = []
    if args.inst:
        symbols = [args.inst]
    else:
        if args.crypto or args.all or (not args.stocks and not args.crypto):
            pool = build_observe_pool(config.OBSERVE_POOL_SIZE)
            if args.top:
                pool = pool[:args.top]
            symbols.extend(p["instId"] for p in pool)
        if args.stocks or args.all:
            symbols.extend(fetch_stock_symbols())
    # 去重保序
    seen = set()
    return [s for s in symbols if not (s in seen or seen.add(s))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bar", default=None, help="单周期，如 1D/4H/1H/15m/1m")
    parser.add_argument("--bars", default=None, help="多周期逗号分隔，如 1m,15m,1H,4H,1D")
    parser.add_argument("--inst", default=None, help="指定标的（如 BTC-USDT）")
    parser.add_argument("--top", type=int, default=None, help="加密币只采前 N 个")
    parser.add_argument("--stocks", action="store_true", help="采集美股代币")
    parser.add_argument("--crypto", action="store_true", help="采集加密币观察池")
    parser.add_argument("--all", action="store_true", help="采集加密+美股（全部标的）")
    parser.add_argument("--threads", type=int, default=10, help="并发线程数（默认10）")
    args = parser.parse_args()

    # 确定周期列表
    if args.bars:
        bars = [b.strip() for b in args.bars.split(",") if b.strip()]
    elif args.bar:
        bars = [args.bar]
    else:
        bars = ALL_BARS

    symbols = build_symbols(args)
    conn = init_db()
    print(f"采集范围: {len(symbols)} 个标的 × {len(bars)} 个周期 "
          f"({'、'.join(bars)})，{args.threads} 线程")

    total_new = 0
    t0 = time.time()
    # 多线程并发拉取（每个任务 = 一个标的 × 一个周期）
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
    print(f"采集完成: {len(tasks)} 个任务, 新增 {total_new} 条, "
          f"库内累计 {cnt} 条, 耗时 {dt:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
