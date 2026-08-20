#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史数据回填(2026-08-21 用户要求'策略经过历史回测')——
market.db 只有 9 天数据,不够回测。从 OKX history-candles 分页回填
1H/4H K 线(6 个月)到 market.db,供 tools/replay_signals.py 重放策略。

只补数据,不改任何交易逻辑。INSERT OR IGNORE 幂等,可重复跑。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MARKET_DB = os.path.join(ROOT, "data", "market.db")
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX",
           "BNB", "LTC"]
BARS = {"1H": 4320, "4H": 1080}     # 6 个月
PAGE = 100                          # history-candles 每页上限


def page(inst_id, bar, after=None, before=None):
    url = ("https://www.okx.com/api/v5/market/history-candles"
           f"?instId={inst_id}&bar={bar}&limit={PAGE}")
    if after:
        url += f"&after={after}"
    if before:
        url += f"&before={before}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("data") or []


def backfill():
    conn = sqlite3.connect(MARKET_DB)
    for base in SYMBOLS:
        inst = f"{base}-USDT"
        for bar, target in BARS.items():
            have = conn.execute(
                "SELECT COUNT(*) FROM klines WHERE inst_id=? AND bar=?",
                [inst, bar]).fetchone()[0]
            if have >= target:
                print(f"{inst} {bar}: 已有 {have} 根,跳过")
                continue
            rows = []
            ts = None
            pages = 0
            while len(rows) < target and pages < 60:
                data = page(inst, bar, after=ts)
                if not data:
                    break
                rows.extend(data)
                ts = data[-1][0]
                pages += 1
                time.sleep(0.25)   # 温和节流
            # OKX 倒序(新→旧),入库前反转为升序;去重靠主键
            n = 0
            for r in reversed(rows):
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO klines (inst_id, bar, open_time,"
                        " open, high, low, close, volume, quote_volume)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        [inst, bar, int(r[0]), float(r[1]), float(r[2]),
                         float(r[3]), float(r[4]), float(r[5]),
                         float(r[6]) if len(r) > 6 and r[6] else 0])
                    n += 1
                except (TypeError, ValueError, IndexError):
                    continue
            conn.commit()
            total = conn.execute(
                "SELECT COUNT(*) FROM klines WHERE inst_id=? AND bar=?",
                [inst, bar]).fetchone()[0]
            print(f"{inst} {bar}: 本次写 {n} 根,现共 {total} 根")
    conn.close()
    print("回填完成")


if __name__ == "__main__":
    backfill()
