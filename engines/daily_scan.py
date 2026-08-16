"""
每日候选扫描 — 全市场筛选"当天适合下单"的币，输出当日 watchlist。

用户要求：每天扫描一下什么币适合下单（抓最佳时机，不频繁交易）。

筛选标准（宁可错过，不勉强）：
  1. 24h 成交额 ≥ VOL_MED（1000 万 USDT，流动性门槛）
  2. 价格 ≥ 0.01 USDT（低价币精度/最小下单量风险）
  3. 1h K线 ≥ 60 根；EMA20/50 方向明确（偏离 ≥ 0.5%，震荡市不选）
  4. 1h ATR% 在 0.5%~6% 甜蜜区（太静没肉、太疯危险）
  5. 4h 趋势与 1h 同向（顺大势做小势，只在明确趋势里挑）

评分：趋势强度(40%) + ATR 甜蜜度(20%) + 成交额排名(40%) → 取前 N 输出 watchlist.json。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.indicators import ema, atr
from data.fetch_okx import build_observe_pool, fetch_klines

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "watchlist.json")

# 筛选参数（非拟合：区间取经验保守值）
# 2026-08-16 采集加速（用户指示）：流动性门槛 500万→200万,扩大候选池
MIN_VOL = config.MIN_VOL
MIN_PRICE = config.MIN_PRICE
MIN_TREND_DEV = config.MIN_TREND_DEV
ATR_SWEET_LOW = config.ATR_SWEET_LOW
ATR_SWEET_HIGH = config.ATR_SWEET_HIGH
WATCH_N = config.WATCH_N


def _klines_to_dicts(kl):
    return [{"open": k["open"], "high": k["high"], "low": k["low"],
             "close": k["close"], "volume": k["volume"]} for k in kl]


def _stock_pool():
    """美股代币也在范围内（用户要求）：显式并入观察池（不参与加密币的成交额排名）。
    两类：
      1) X 前缀现货代币（XNVDA/XTSLA…，走现货 tickers 取成交额）
      2) 仅合约代币（ANTHROPIC 等，走 SWAP tickers，instId 为 XXX-USDT-SWAP）"""
    try:
        import urllib.request
        from data.fetch_okx import fetch_stock_symbols
        req = urllib.request.Request(
            "https://www.okx.com/api/v5/market/tickers?instType=SPOT",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            tickers = json.loads(r.read()).get("data", [])
        vol_map = {t["instId"]: float(t.get("volCcy24h", 0)) for t in tickers}
        out = []
        for inst in fetch_stock_symbols():
            out.append({"instId": inst, "base": inst.split("-")[0],
                        "vol24h": vol_map.get(inst, 0), "is_stock": True})
        # 仅合约的美股/公司代币：从 SWAP tickers 取成交额（volCcy24h 为 USDT 计价）
        try:
            req2 = urllib.request.Request(
                "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=30) as r:
                swap_tickers = json.loads(r.read()).get("data", [])
            swap_vol = {t["instId"]: float(t.get("volCcy24h", 0)) for t in swap_tickers}
            for tok in config.STOCK_SWAP_TOKENS:
                inst = f"{tok}-USDT-SWAP"
                out.append({"instId": inst, "base": tok,
                            "vol24h": swap_vol.get(inst, 0), "is_stock": True})
        except Exception as e:
            print(f"  仅合约美股代币清单获取失败: {e}")
        return out
    except Exception as e:
        print(f"  美股代币清单获取失败: {e}")
        return []


def screen_daily(pool_top=60, watch_n=None):
    """全市场筛选（加密 + 美股代币），返回候选列表（按评分降序）。"""
    watch_n = watch_n or WATCH_N
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] 每日候选扫描开始（观察池前 {pool_top} + 美股代币）…")
    pool = build_observe_pool(pool_top)
    # 美股代币并入（去重）
    seen = {p["instId"] for p in pool}
    for s in _stock_pool():
        if s["instId"] not in seen:
            pool.append(s)
            seen.add(s["instId"])

    # 阶段1：流动性与价格硬门槛（用 ticker 数据，无额外请求）
    stage1 = [p for p in pool if p["vol24h"] >= MIN_VOL]
    print(f"  阶段1 流动性/价格门槛: {len(pool)} → {len(stage1)} 个")

    # 阶段2：1h 趋势 + ATR（每个候选 1 次 K线请求）
    stage2 = []
    for p in stage1:
        try:
            kl = fetch_klines(p["instId"], "1H", limit=120)
            if len(kl) < 60:
                continue
            last_close = kl[-1]["close"]
            if last_close < MIN_PRICE:
                continue
            ks = _klines_to_dicts(kl)
            closes = [k["close"] for k in ks]
            e20, e50 = ema(closes, 20), ema(closes, 50)
            dev = (e20[-1] - e50[-1]) / e50[-1]
            if abs(dev) < MIN_TREND_DEV:
                continue
            a = atr(ks, 14)
            atr_pct = a / last_close
            if not (ATR_SWEET_LOW <= atr_pct <= ATR_SWEET_HIGH):
                continue
            stage2.append({"base": p["base"], "instId": p["instId"],
                           "vol24h": p["vol24h"], "dir": 1 if dev > 0 else -1,
                           "trend_dev": dev, "atr_pct": atr_pct,
                           "price": last_close,
                           "is_stock": p.get("is_stock", False) or p["base"].startswith("X")})
        except Exception:
            continue
    print(f"  阶段2 1h 趋势+ATR: {len(stage1)} → {len(stage2)} 个")

    # 阶段3：4h 共振（只对 1h 有趋势的候选再拉 4h）
    stage3 = []
    for c in stage2:
        try:
            kl4 = fetch_klines(c["instId"], "4H", limit=80)
            if len(kl4) < 50:
                continue
            c4 = [k["close"] for k in kl4]
            e20, e50 = ema(c4, 20), ema(c4, 50)
            dir4 = 1 if e20[-1] > e50[-1] else -1
            if dir4 != c["dir"]:
                continue   # 4h 与 1h 不同向 → 放弃（顺大势）
            stage3.append(c)
        except Exception:
            continue
    print(f"  阶段3 4h 共振: {len(stage2)} → {len(stage3)} 个")

    # 评分排序：成交额排名 40% + 趋势强度 40% + ATR 甜蜜度 20%
    if stage3:
        vol_sorted = sorted(stage3, key=lambda x: x["vol24h"], reverse=True)
        vol_rank = {c["instId"]: i for i, c in enumerate(vol_sorted)}
        n = len(vol_sorted)
        for c in stage3:
            c["score"] = (0.4 * (1 - vol_rank[c["instId"]] / max(n - 1, 1))
                          + 0.4 * min(abs(c["trend_dev"]) / 0.03, 1.0)
                          + 0.2 * max(0, 1 - abs(c["atr_pct"] - 0.02) / 0.03))
        stage3.sort(key=lambda x: -x["score"])

    watch = stage3[:watch_n]
    if not watch:
        # 空结果：今日无高评分候选 → 回退主流池并标注（宁可错过，但系统仍需盯盘）
        print("  今日无通过筛选的候选 → 回退主流池（BTC/ETH/SOL/XRP/DOGE）")
        watch = [{"base": b, "instId": f"{b}-USDT", "dir": 0, "score": 0.0,
                  "trend_dev": 0.0, "atr_pct": 0.0, "price": 0.0,
                  "is_stock": False}
                 for b in ("BTC", "ETH", "SOL", "XRP", "DOGE")]
    result = {
        "date": time.strftime("%Y-%m-%d"),
        "generated_at": time.time(),
        "fallback": bool(not stage3),
        "candidates": [{k: c[k] for k in ("base", "instId", "dir", "score",
                                           "trend_dev", "atr_pct", "price", "is_stock")}
                       for c in watch],
    }
    # 落库（storage 层 watchlist 表；同日重扫覆盖旧候选）
    import storage.db as sdb
    sdb.init_db()
    for c in watch:
        sdb.x("INSERT OR REPLACE INTO watchlist (date,base,inst_id,dir,score,"
              "trend_dev,atr_pct,price,is_stock) VALUES (?,?,?,?,?,?,?,?,?)",
              [result["date"], c["base"], c.get("instId"), c.get("dir", 0),
               c.get("score", 0.0), c.get("trend_dev", 0.0), c.get("atr_pct", 0.0),
               c.get("price", 0.0), 1 if c.get("is_stock") else 0])
    print(f"  结果: 选出 {len(watch)} 个候选 → crypto_agent.db:watchlist")
    for c in watch:
        print(f"    {c['base']:<10} {'多' if c['dir']>0 else '空'}  趋势{c['trend_dev']*100:+.1f}%  "
              f"ATR{c['atr_pct']*100:.1f}%  评分{c['score']:.2f}")
    return watch


def trades_budget(score):
    """按当日评分给该币允许笔数（用户要求：看币动态调整笔数）。
    评分越高（趋势强/流动性好/波动甜蜜）越值得多给机会。"""
    if score is None:
        return config.DEFAULT_TRADE_BUDGET
    for th, n in config.TRADE_BUDGET_BY_SCORE:
        if score >= th:
            return n
    return 1


def load_watchlist(fallback=None):
    """读当日 watchlist（SQLite），返回 {base: score}（评分用于动态笔数）。
    过期/缺失回退固定池（无评分 → 默认笔数）。"""
    fallback = fallback or ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    try:
        import storage.db as sdb
        sdb.init_db()
        rows = sdb.q("SELECT base, score FROM watchlist WHERE date=?",
                     [time.strftime("%Y-%m-%d")])
        if rows:
            return {r["base"]: r["score"] for r in rows}
    except Exception:
        pass
    return {b: None for b in fallback}


if __name__ == "__main__":
    screen_daily()
