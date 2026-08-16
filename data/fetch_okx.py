"""
数据层 — OKX 数据源（用户选定）。
接口：OKX REST API v5（公开接口，无需凭证）
历史 K 线可回溯至 2020 年（约 6 年，比币安 3 年更长）。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_okx")
os.makedirs(CACHE_DIR, exist_ok=True)

BASE = "https://www.okx.com"


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_instruments():
    """拉取 OKX 现货 USDT 交易对列表（live）。缓存 24h。"""
    cache = os.path.join(CACHE_DIR, "instruments.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache) as f:
            return json.load(f)
    d = _get(f"{BASE}/api/v5/public/instruments?instType=SPOT")
    insts = d.get("data", [])
    with open(cache, "w") as f:
        json.dump(insts, f)
    return insts


def build_observe_pool(top_n=None):
    """观察池：OKX 现货 USDT 交易对，排除稳定币，按 24h 成交额排序取前 N。"""
    top_n = top_n or config.OBSERVE_POOL_SIZE
    insts = fetch_instruments()
    # 拉 24h ticker 排序
    tickers = _get(f"{BASE}/api/v5/market/tickers?instType=SPOT").get("data", [])
    vol_map = {t["instId"]: float(t.get("volCcy24h", 0)) for t in tickers}

    pool = []
    for i in insts:
        inst_id = i["instId"]  # 如 BTC-USDT
        if i.get("state") != "live":
            continue
        if not inst_id.endswith("-USDT"):
            continue
        base = inst_id.split("-")[0]
        if base in config.STABLECOINS:
            continue
        if any(base.endswith(s) for s in config.LEVERAGED_SUFFIX):
            continue
        pool.append({"instId": inst_id, "base": base,
                     "vol24h": vol_map.get(inst_id, 0)})
    pool.sort(key=lambda x: x["vol24h"], reverse=True)
    return pool[:top_n]


def fetch_klines(inst_id, interval="1D", limit=2200):
    """
    拉取 OKX 历史日线 K 线（升序，dict 列表）。
    用 history-candles 分页回溯（最多 ~6 年）。缓存 24h。
    """
    cache = os.path.join(CACHE_DIR, f"klines_{inst_id}_{interval}.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache) as f:
            return json.load(f)

    bar = "1D" if interval == "1D" else interval
    raw_all = []
    after = ""
    # 分页回溯，每页 100，最多拉 limit 根
    for _ in range(limit // 100 + 5):
        url = f"{BASE}/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit=100"
        if after:
            url += f"&after={after}"
        d = _get(url)
        if d.get("code") != "0":
            break
        data = d.get("data", [])
        if not data:
            break
        raw_all.extend(data)
        after = data[-1][0]
        if len(data) < 100:
            break

    # OKX 返回倒序（新→旧），反转为升序，并转成统一 dict 结构
    klines = []
    for row in reversed(raw_all):
        klines.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),        # 成交量（币）
            "quote_volume": float(row[6]),  # 成交额（USDT）
        })
    with open(cache, "w") as f:
        json.dump(klines, f)
    return klines


def fetch_btc_klines():
    return fetch_klines("BTC-USDT")


def list_cached_symbols():
    """从 cache_okx 缓存目录列出已缓存的标的（不访问 API，用于离线/受限环境）。"""
    import glob
    files = glob.glob(os.path.join(CACHE_DIR, "klines_*_1D.json"))
    symbols = []
    for f in files:
        base = os.path.basename(f)
        name = base.replace("klines_", "").replace("_1D.json", "")
        if name:
            symbols.append(name)
    return sorted(symbols)


# 美股代币（tokenized stocks）：X 开头 USDT 计价，排除加密币/贵金属
STOCK_EXCLUDE = {"XRP", "XLM", "XCH", "XTZ", "XAUT"}


def fetch_stock_symbols():
    """获取 OKX 美股代币清单（如 XAAPL、XNVDA、XTSLA）。"""
    insts = fetch_instruments()
    stocks = []
    for i in insts:
        inst_id = i.get("instId", "")
        if i.get("state") != "live":
            continue
        if not inst_id.endswith("-USDT"):
            continue
        base = inst_id.split("-")[0]
        if base.startswith("X") and base not in STOCK_EXCLUDE:
            stocks.append(inst_id)
    return sorted(stocks)


if __name__ == "__main__":
    pool = build_observe_pool(20)
    print(f"OKX 观察池前 {len(pool)} 个：")
    for i, p in enumerate(pool[:10], 1):
        print(f"  {i}. {p['instId']}  24h成交额 {p['vol24h']:,.0f}")
    btc = fetch_btc_klines()
    print(f"\nBTC-USDT 日线 {len(btc)} 根，最近收盘 {btc[-1]['close']}")
