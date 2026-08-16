"""
数据层 — 拉取币安官方公开数据（data-api.binance.vision，不受地区限制）
所有数据缓存到本地 JSON，避免重复请求。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_24hr_tickers():
    """拉取全部交易对 24h 行情，缓存 5 分钟"""
    cache_file = os.path.join(CACHE_DIR, "tickers_24hr.json")
    if os.path.exists(cache_file) and time.time() - os.path.getmtime(cache_file) < 300:
        with open(cache_file) as f:
            return json.load(f)
    data = _get(f"{config.BASE_URL}/api/v3/ticker/24hr")
    with open(cache_file, "w") as f:
        json.dump(data, f)
    return data


def build_observe_pool(top_n=None):
    """构建观察池：排除稳定币/杠杆代币，按 24h 成交额排序取前 N"""
    top_n = top_n or config.OBSERVE_POOL_SIZE
    tickers = fetch_24hr_tickers()
    pool = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]  # 去掉 USDT
        if base in config.STABLECOINS:
            continue
        if any(sym.endswith(s) for s in config.LEVERAGED_SUFFIX):
            continue
        pool.append(t)
    pool.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return pool[:top_n]


def fetch_klines(symbol, interval=None, limit=1000):
    """
    拉取单个交易对的历史日线 K 线。
    返回按时间升序的 dict 列表。缓存 24 小时。
    """
    interval = interval or config.INTERVAL
    cache_file = os.path.join(CACHE_DIR, f"klines_{symbol}_{interval}.json")
    if os.path.exists(cache_file) and time.time() - os.path.getmtime(cache_file) < 86400:
        with open(cache_file) as f:
            return json.load(f)

    url = (f"{config.BASE_URL}/api/v3/klines?symbol={symbol}"
           f"&interval={interval}&limit={limit}")
    raw = _get(url)
    klines = []
    for row in raw:
        klines.append({
            "open_time": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": row[6],
            "quote_volume": float(row[7]),
        })
    with open(cache_file, "w") as f:
        json.dump(klines, f)
    return klines


def fetch_btc_klines():
    return fetch_klines("BTCUSDT")


if __name__ == "__main__":
    # 快速自测：拉观察池前 10 个币的日线，打印数据量
    pool = build_observe_pool(80)
    print(f"观察池 {len(pool)} 个币：")
    for i, t in enumerate(pool[:10], 1):
        print(f"  {i}. {t['symbol']}  成交额 {float(t['quoteVolume']):,.0f}")
    btc = fetch_btc_klines()
    print(f"\nBTCUSDT 日线 {len(btc)} 根，最近收盘 {btc[-1]['close']}")
