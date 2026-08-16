"""
订单流采集 — 定期拉订单簿失衡 + 主动买卖比，存 SQLite 攒历史。
订单流历史极浅（trades 只保留最近千条），必须自己持续采集积累。

用法：
  python3 data/collect_orderflow.py            # 单次快照
  python3 data/collect_orderflow.py --loop     # 常驻，每 60 秒快照
"""
import sys
import os
import time
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.fetch_orderflow import orderflow_snapshot

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderflow (
            symbol TEXT NOT NULL,
            ts INTEGER NOT NULL,
            imbalance REAL,
            taker_buy_ratio REAL,
            bid_depth REAL,
            ask_depth REAL,
            PRIMARY KEY (symbol, ts)
        )
    """)
    conn.commit()
    return conn


def snapshot(conn, symbols=SYMBOLS):
    now = int(time.time() * 1000)
    n = 0
    for sym in symbols:
        try:
            s = orderflow_snapshot(sym)
            conn.execute(
                "INSERT OR IGNORE INTO orderflow VALUES (?,?,?,?,?,?)",
                (sym, now, s["imbalance"], s["taker_buy_ratio"],
                 s["bid_depth"], s["ask_depth"]))
            n += 1
        except Exception:
            pass
    conn.commit()
    return n


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="常驻，每60秒快照")
    args = parser.parse_args()

    conn = init_db()
    if args.loop:
        print(f"订单流采集启动：{SYMBOLS}，每 60 秒，存 market.db")
        while True:
            n = snapshot(conn)
            print(f"[{time.strftime('%H:%M:%S')}] 快照 {n} 个标的")
            time.sleep(60)
    else:
        n = snapshot(conn)
        cnt = conn.execute("SELECT COUNT(*) FROM orderflow").fetchone()[0]
        print(f"单次快照 {n} 个标的，库内累计 {cnt} 条")
    conn.close()


if __name__ == "__main__":
    main()
