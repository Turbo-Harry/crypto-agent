#!/usr/bin/env python3
"""
OKX 多周期 K 线 + 资金费率 → PostgreSQL 采集入库。

覆盖：
  1. 现货加密（USDT 交易对，按 24h 成交额取前 N）
  2. 美股代币（X 开头现货，全部）
  3. 永续合约（USDT 本位，按 24h 成交额取前 M）
  4. 永续合约资金费率历史（funding-rate-history）

周期: 1m / 15m / 1H / 4H / 1D

用法:
  python3 okx_pg_ingest.py --dry-run
  python3 okx_pg_ingest.py --bars 1D,4H,1H,15m,1m --crypto-top 60 --swap-top 80 --funding
"""
import argparse
import json
import os
import time
import urllib.request

BASE = "https://www.okx.com"

# 各周期建议深度（根数）：1m 取 7 天，其余按 OKX 可用历史
BAR_LIMITS = {"1m": 10080, "15m": 2976, "1H": 2160, "4H": 2190, "1D": 2200}

STABLECOINS = {
    "USDC", "USDT", "TUSD", "FDUSD", "BUSD", "DAI", "USD1", "RLUSD", "U",
    "EURI", "XAUT", "EUR", "GBP", "AEUR", "USDY", "PYUSD", "USTC",
    "PAXG", "WBTC", "WBETH", "USDE", "USDP", "TRIBE", "SUSD", "USDX",
}
LEVERAGED_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")
STOCK_EXCLUDE = {"XRP", "XLM", "XCH", "XTZ", "XAUT"}


def http_get(url, timeout=30, retries=4):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = 2 * (i + 1)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise last_err


def _ticker_vol(inst_type):
    tickers = http_get(f"{BASE}/api/v5/market/tickers?instType={inst_type}").get("data", [])
    return {t["instId"]: float(t.get("volCcy24h", 0)) for t in tickers}


def get_spot_symbols(crypto_top=60):
    """现货：加密(前 N) + 美股代币(全部)。返回 [(inst_id, 类别)]。"""
    insts = http_get(f"{BASE}/api/v5/public/instruments?instType=SPOT").get("data", [])
    vol = _ticker_vol("SPOT")
    crypto, stocks = [], []
    for i in insts:
        inst = i.get("instId", "")
        if i.get("state") != "live" or not inst.endswith("-USDT"):
            continue
        base = inst.split("-")[0]
        if base in STABLECOINS or any(base.endswith(s) for s in LEVERAGED_SUFFIX):
            continue
        if base.startswith("X"):
            if base in STOCK_EXCLUDE:
                continue
            stocks.append(inst)
        else:
            crypto.append((inst, vol.get(inst, 0)))
    crypto.sort(key=lambda x: -x[1])
    crypto = [c[0] for c in crypto[:crypto_top]]
    return [(s, "crypto") for s in crypto] + [(s, "stock") for s in sorted(stocks)]


def get_swap_symbols(swap_top=80):
    """永续合约：USDT 本位，按 24h 成交额取前 N。返回 inst_id 列表。"""
    insts = http_get(f"{BASE}/api/v5/public/instruments?instType=SWAP").get("data", [])
    vol = _ticker_vol("SWAP")
    swaps = []
    for i in insts:
        inst = i.get("instId", "")
        if i.get("state") != "live" or i.get("settleCcy") != "USDT":
            continue
        if not inst.endswith("-USDT-SWAP"):
            continue
        swaps.append((inst, vol.get(inst, 0)))
    swaps.sort(key=lambda x: -x[1])
    return [s[0] for s in swaps[:swap_top]]


def fetch_candles(inst_id, bar, max_candles=30000, sleep=0.08):
    """分页拉取 history-candles，返回升序 dict 列表。"""
    max_candles = min(max_candles, BAR_LIMITS.get(bar, max_candles))
    rows = []
    after = ""
    while len(rows) < max_candles:
        url = (f"{BASE}/api/v5/market/history-candles"
               f"?instId={inst_id}&bar={bar}&limit=100")
        if after:
            url += f"&after={after}"
        try:
            d = http_get(url)
        except Exception as e:
            print(f"  [warn] {inst_id} {bar} 拉取失败: {e}", flush=True)
            break
        if d.get("code") != "0":
            print(f"  [warn] {inst_id} {bar} 错误 {d.get('code')}: {d.get('msg')}", flush=True)
            break
        data = d.get("data", [])
        if not data:
            break
        rows.extend(data)
        after = data[-1][0]
        if len(data) < 100:
            break
        time.sleep(sleep)
    out = []
    for row in reversed(rows):
        out.append({
            "inst_id": inst_id, "bar": bar, "open_time": int(row[0]),
            "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
            "close": float(row[4]), "volume": float(row[5]),
            "quote_volume": float(row[6]),
        })
    return out


def fetch_funding_history(inst_id, max_records=10000, sleep=0.12):
    """拉取资金费率历史，返回升序 dict 列表。"""
    records = []
    after = ""
    while len(records) < max_records:
        url = (f"{BASE}/api/v5/public/funding-rate-history"
               f"?instId={inst_id}&limit=100")
        if after:
            url += f"&after={after}"
        try:
            d = http_get(url)
        except Exception as e:
            print(f"  [warn] {inst_id} funding 拉取失败: {e}", flush=True)
            break
        if d.get("code") != "0":
            break
        data = d.get("data", [])
        if not data:
            break
        records.extend(data)
        after = data[-1].get("fundingTime", "")
        if len(data) < 100:
            break
        time.sleep(sleep)
    out = []
    for r in reversed(records):
        out.append({
            "inst_id": inst_id,
            "funding_time": int(r["fundingTime"]),
            "funding_rate": float(r.get("fundingRate", 0) or 0),
            "realized_rate": float(r.get("realizedRate", 0) or 0),
        })
    return out


def load_pg_config():
    cfg = {"host": "127.0.0.1", "port": 5432, "user": "crypto",
           "password": "", "dbname": "crypto"}
    path = os.environ.get("PG_ENV", "/root/.crypto-pg.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            kmap = {"POSTGRES_HOST": "host", "POSTGRES_PORT": "port",
                    "POSTGRES_USER": "user", "POSTGRES_PASSWORD": "password",
                    "POSTGRES_DB": "dbname"}
            if k in kmap:
                cfg[kmap[k]] = v
    cfg["port"] = int(cfg["port"])
    return cfg


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS klines (
                inst_id TEXT NOT NULL,
                bar TEXT NOT NULL,
                open_time BIGINT NOT NULL,
                open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                close DOUBLE PRECISION, volume DOUBLE PRECISION, quote_volume DOUBLE PRECISION,
                PRIMARY KEY (inst_id, bar, open_time)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_klines_bar ON klines(bar);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_klines_inst_bar ON klines(inst_id, bar);")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS funding_rates (
                inst_id TEXT NOT NULL,
                funding_time BIGINT NOT NULL,
                funding_rate DOUBLE PRECISION,
                realized_rate DOUBLE PRECISION,
                PRIMARY KEY (inst_id, funding_time)
            );
        """)
    conn.commit()


def upsert_klines(conn, rows):
    if not rows:
        return 0
    import psycopg2.extras
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO klines (inst_id, bar, open_time, open, high, low, close, volume, quote_volume) "
            "VALUES %s ON CONFLICT (inst_id, bar, open_time) DO NOTHING",
            [(r["inst_id"], r["bar"], r["open_time"], r["open"], r["high"],
              r["low"], r["close"], r["volume"], r["quote_volume"]) for r in rows],
            page_size=1000,
        )
    conn.commit()
    return len(rows)


def upsert_funding(conn, rows):
    if not rows:
        return 0
    import psycopg2.extras
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO funding_rates (inst_id, funding_time, funding_rate, realized_rate) "
            "VALUES %s ON CONFLICT (inst_id, funding_time) DO NOTHING",
            [(r["inst_id"], r["funding_time"], r["funding_rate"], r["realized_rate"])
             for r in rows],
            page_size=1000,
        )
    conn.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", default="1m,15m,1H,4H,1D")
    ap.add_argument("--crypto-top", type=int, default=60)
    ap.add_argument("--swap-top", type=int, default=80)
    ap.add_argument("--no-stocks", action="store_true")
    ap.add_argument("--no-swap", action="store_true")
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--sleep", type=float, default=0.08)
    ap.add_argument("--recent", type=int, default=0,
                    help="增量模式：每周期只拉最近 N 根（0=全量）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bars = [b.strip() for b in args.bars.split(",") if b.strip()]

    spot = [] if args.no_stocks and args.crypto_top == 0 else get_spot_symbols(args.crypto_top)
    if args.no_stocks:
        spot = [(s, k) for s, k in spot if k == "crypto"]
    swap = [] if args.no_swap else get_swap_symbols(args.swap_top)

    print(f"标的: 现货 {len(spot)} 个 + 合约 {len(swap)} 个，周期 {bars}")
    if args.dry_run:
        print("--- 现货 ---")
        for s, k in spot:
            print(f"  [{k}] {s}")
        print("--- 合约 ---")
        for s in swap:
            print(f"  [swap] {s}")
        return

    import psycopg2
    conn = psycopg2.connect(**load_pg_config())
    ensure_tables(conn)

    lim = args.recent if args.recent > 0 else args.limit
    total_k = 0
    # 现货 K 线
    for s, kind in spot:
        for bar in bars:
            rows = fetch_candles(s, bar, lim, args.sleep)
            n = upsert_klines(conn, rows)
            total_k += n
            if rows:
                print(f"[{kind}] {s} {bar}: +{n} 根", flush=True)
    # 合约 K 线
    for s in swap:
        for bar in bars:
            rows = fetch_candles(s, bar, lim, args.sleep)
            n = upsert_klines(conn, rows)
            total_k += n
            if rows:
                print(f"[swap] {s} {bar}: +{n} 根", flush=True)
    # 合约资金费率
    total_f = 0
    if not args.no_funding:
        for s in swap:
            rows = fetch_funding_history(s)
            n = upsert_funding(conn, rows)
            total_f += n
            if rows:
                print(f"[funding] {s}: +{n} 条", flush=True)

    conn.close()
    print(f"\n完成：K 线 {total_k} 根，资金费率 {total_f} 条")


if __name__ == "__main__":
    main()
