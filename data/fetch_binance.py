"""
数据层 — 币安数据源（data-api.binance.vision，国内可访问）。
支持加密币 + 美股代币（tokenized stocks，{股票代码}BUSDT 格式）。
历史 K 线缓存到本地 cache_binance。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_binance")
os.makedirs(CACHE_DIR, exist_ok=True)

BASE = "https://data-api.binance.vision"

# 已知加密货币（BUSDT 结尾但 base 是币，不是美股）
CRYPTO_BASES = {
    "BNB", "ARB", "QNT", "SHIB", "B", "CK", "DG", "GS", "LTC", "TR",
    "Y", "MU", "NOK", "BCH", "XRP", "ADA", "DOGE", "SOL", "DOT", "LINK",
    "AVAX", "NEAR", "ICP", "FIL", "ETC", "XLM", "ATOM", "UNI", "AAVE",
}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_instruments():
    """拉币安全部交易对，缓存 24h。"""
    cache = os.path.join(CACHE_DIR, "instruments.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache) as f:
            return json.load(f)
    d = _get(f"{BASE}/api/v3/exchangeInfo")
    insts = d.get("symbols", [])
    with open(cache, "w") as f:
        json.dump(insts, f)
    return insts


def build_observe_pool(top_n=None):
    """加密币观察池：USDT 计价，排除稳定币和美股代币，按成交额排序。"""
    top_n = top_n or config.OBSERVE_POOL_SIZE
    insts = fetch_instruments()
    tickers = _get(f"{BASE}/api/v3/ticker/24hr")
    vol_map = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}

    pool = []
    for s in insts:
        sym = s.get("symbol", "")
        if s.get("status") != "TRADING":
            continue
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        # 排除稳定币、美股代币（BUSDT 结尾且 base 以 B 结尾）、已知加密币误判
        if base in config.STABLECOINS:
            continue
        if sym.endswith("BUSDT") and base[:-1] not in CRYPTO_BASES:
            continue  # 美股代币
        pool.append({"symbol": sym, "vol24h": vol_map.get(sym, 0)})
    pool.sort(key=lambda x: x["vol24h"], reverse=True)
    return pool[:top_n]


def fetch_stock_symbols():
    """币安美股代币清单（{股票代码}BUSDT 格式）。"""
    insts = fetch_instruments()
    stocks = []
    for s in insts:
        sym = s.get("symbol", "")
        if s.get("status") != "TRADING":
            continue
        if not sym.endswith("BUSDT"):
            continue
        base = sym[:-4]  # 如 AAPLB
        if base in CRYPTO_BASES:  # base 本身是加密货币（ARB/BNB/SHIB 等）
            continue
        if not base.endswith("B"):
            continue
        code = base[:-1]  # 如 AAPL
        # 股票代码是纯大写字母 2-5 位
        if code.isalpha() and 2 <= len(code) <= 5:
            stocks.append(sym)
    return sorted(stocks)


def fetch_klines(symbol, interval="1d", limit=1000):
    """拉币安 K 线（升序 dict 列表），缓存 24h。"""
    cache = os.path.join(CACHE_DIR, f"klines_{symbol}_{interval}.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache) as f:
            return json.load(f)

    url = f"{BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
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
            "quote_volume": float(row[7]),
        })
    with open(cache, "w") as f:
        json.dump(klines, f)
    return klines


def list_cached_symbols():
    """从缓存列出已缓存的标的（离线回退）。"""
    import glob
    files = glob.glob(os.path.join(CACHE_DIR, "klines_*_1d.json"))
    return sorted([os.path.basename(f).replace("klines_", "").replace("_1d.json", "")
                   for f in files])


if __name__ == "__main__":
    pool = build_observe_pool(20)
    stocks = fetch_stock_symbols()
    print(f"币安加密观察池前 {len(pool)} 个:")
    for p in pool[:10]:
        print(f"  {p['symbol']}  成交额 {p['vol24h']:,.0f}")
    print(f"\n币安美股代币 {len(stocks)} 个:")
    print("  " + " ".join(stocks[:20]))
