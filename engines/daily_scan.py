"""
每日候选扫描 — 全市场筛选"当天适合下单"的币，输出当日 watchlist。

用户要求：每天扫描一下什么币适合下单（抓最佳时机，不频繁交易）。

行情一律走 ExchangeAdapter（2026-08-20 收敛）：禁止本文件 urllib / 裸打 OKX URL。
SWAP_ONLY 下观察池直接用合约 ticker（成交额已在适配层归一成 USDT）。

筛选标准（宁可错过，不勉强）：
  1. 24h 成交额 ≥ VOL_MED（流动性门槛）
  2. 价格 ≥ 0.01 USDT（低价币精度/最小下单量风险）
  3. 1h K线 ≥ 60 根；EMA20/50 方向明确（偏离 ≥ 0.5%，震荡市不选）
  4. 1h ATR% 在甜蜜区（太静没肉、太疯危险）
  5. 4h 趋势与 1h 同向（顺大势做小势，只在明确趋势里挑）

评分：趋势强度(40%) + ATR 甜蜜度(20%) + 成交额排名(40%) → 取前 N 输出 watchlist。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.indicators import ema, atr
from exchange.base import ExchangeAdapter, ExchangeError

# 筛选参数（非拟合：区间取经验保守值）
# 2026-08-16 采集加速（用户指示）：流动性门槛 500万→200万,扩大候选池
MIN_VOL = config.MIN_VOL
MIN_PRICE = config.MIN_PRICE
MIN_TREND_DEV = config.MIN_TREND_DEV
ATR_SWEET_LOW = config.ATR_SWEET_LOW
ATR_SWEET_HIGH = config.ATR_SWEET_HIGH
WATCH_N = config.WATCH_N


def _klines_to_dicts(candles):
    return [{"open": c.open, "high": c.high, "low": c.low,
             "close": c.close, "volume": c.volume} for c in candles]


def _inst_id(base):
    """候选 instId：只做合约时一律永续。"""
    return f"{base}-USDT-SWAP" if config.SWAP_ONLY else f"{base}-USDT"


def _default_exchange() -> ExchangeAdapter:
    """CLI / HTTP 未注入适配器时的回退。不 import directional_trader（防 import 环）。"""
    from exchange.okx_adapter import OKXAdapter
    try:
        cfg = json.load(open("okx_config.json"))
        return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"],
                          sandbox=True)
    except Exception:
        return OKXAdapter("", "", "", sandbox=False)


def _swap_observe_pool(exchange: ExchangeAdapter, pool_top):
    """合约观察池：SWAP ticker 按归一成交额排序取前 N，再并入美股合约清单。

    成交额单位在适配层已归一（vol_usdt_24h）。此前 daily_scan 自己打
    /market/tickers 且用现货池再滤合约——交易路径漏出 OKX URL，且现货
    成交额排名不等于合约流动性。"""
    tickers = exchange.fetch_tickers("swap")
    by_id = {t.inst_id: t for t in tickers}
    pool = []
    for t in tickers:
        if t.base in config.STABLECOINS:
            continue
        if any(t.base.endswith(s) for s in config.LEVERAGED_SUFFIX):
            continue
        pool.append({"instId": t.inst_id, "base": t.base,
                     "vol24h": t.vol_usdt_24h,
                     "is_stock": t.base in config.STOCK_SWAP_TOKENS})
    pool.sort(key=lambda x: x["vol24h"], reverse=True)
    pool = pool[:pool_top]
    # 美股/公司代币合约并入（2026-08-20 用户拍板: 只做合约、不做现货——
    # 旧 X 前缀现货代币路径弃用,清单为 config.STOCK_SWAP_TOKENS）。
    # 已在前 N 里的只打标；沙盘缺合约的 vol=0，阶段1 会刷掉。
    seen = {p["instId"] for p in pool}
    for tok in config.STOCK_SWAP_TOKENS:
        inst = f"{tok}-USDT-SWAP"
        if inst in seen:
            continue
        t = by_id.get(inst)
        pool.append({"instId": inst, "base": tok,
                     "vol24h": t.vol_usdt_24h if t else 0.0,
                     "is_stock": True})
        seen.add(inst)
    return pool


def untradable_bases(db_path=None):
    """沙盘不可交易集合 = config.DEMO_UNTRADABLE ∪ untradable_symbols 动态表。

    静态表是人工确认的永久缺口(51001/51087);动态表由开仓失败自动登记
    (ZEC/HYPE/ALLO 等)。读动态表失败只返回静态表——漏过的币开仓层仍会
    reject_untradable,不扩大敞口,只是可能白占一席(本函数的目的就是少占席)。
    """
    blocked = set(config.DEMO_UNTRADABLE)
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        for r in sdb.q("SELECT base FROM untradable_symbols", db_path=db_path):
            b = r.get("base")
            if b:
                blocked.add(b)
    except Exception:
        pass
    return blocked


def _spot_observe_pool(exchange: ExchangeAdapter, pool_top):
    """现货观察池（SWAP_ONLY=False 时的可逆路径）。"""
    tickers = exchange.fetch_tickers("spot")
    pool = []
    for t in tickers:
        if t.base in config.STABLECOINS:
            continue
        if any(t.base.endswith(s) for s in config.LEVERAGED_SUFFIX):
            continue
        pool.append({"instId": t.inst_id, "base": t.base,
                     "vol24h": t.vol_usdt_24h, "is_stock": False})
    pool.sort(key=lambda x: x["vol24h"], reverse=True)
    return pool[:pool_top]


def screen_daily(pool_top=60, watch_n=None, progress_cb=None,
                 exchange=None, db_path=None):
    """全市场筛选（加密 + 美股代币），返回候选列表（按评分降序）。
    progress_cb(2026-08-17): 每处理一个候选调用一次——供引擎在长扫描期间
    插拍止损监控/心跳/tick 进度(网络慢时 60 币扫描可阻塞主循环数十分钟,
    与 51 分钟盲窗同源事故)。
    exchange: 注入 ExchangeAdapter（引擎/HTTP 传入同一实例；测试传 FakeAdapter）。
    db_path: 隔离 watchlist 落库（测试必须传，防污染生产库）。"""
    watch_n = watch_n or WATCH_N
    exchange = exchange or _default_exchange()
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] 每日候选扫描开始（观察池前 {pool_top} + 美股代币）…")
    if config.SWAP_ONLY:
        pool = _swap_observe_pool(exchange, pool_top)
        print(f"  只做合约观察池: {len(pool)} 个（SWAP ticker + 美股合约清单）")
    else:
        pool = _spot_observe_pool(exchange, pool_top)

    # 沙盘不可交易预过滤(2026-08-20 用户拍板): BICO/WLD/ZEC/HYPE 等生产有行情、
    # 沙盘 51001/51087/51155 下不了单,此前仍凭成交额进候选池占 12 席中数席,
    # 开仓层才 reject_untradable——名额浪费。阶段1 之前剔除,连 K 线请求都省掉。
    blocked = untradable_bases(db_path)
    removed = [p["base"] for p in pool if p["base"] in blocked]
    if removed:
        pool = [p for p in pool if p["base"] not in blocked]
        print(f"  沙盘不可交易预过滤: 剔除 {len(removed)} 个 {removed}")

    # 本账户实际没有永续合约的标的(生产 ticker 有、沙盘 instruments 无:
    # INTC/SOXL/MSTR 等)同样不占名额。venue_for 走适配器仪器缓存,沙盘
    # 账户看到的才是能下单的。探测失败 fail-open,开仓层仍会拒。
    if config.SWAP_ONLY:
        kept, no_swap = [], []
        for p in pool:
            try:
                v = exchange.venue_for(p["base"])
            except Exception:
                v = "swap"
            if v == "swap":
                kept.append(p)
            else:
                no_swap.append(p["base"])
        if no_swap:
            print(f"  本账户无永续合约剔除: {len(no_swap)} 个 {no_swap[:12]}"
                  + ("…" if len(no_swap) > 12 else ""))
        pool = kept

    # 阶段1：流动性与价格硬门槛（用 ticker 数据，无额外请求）
    # 2026-08-20: 观察池构建(全市场 ticker)+仪器探测是网络重活,此前无插拍,
    # 心跳/tick 在此段停更(03:11/03:15 两次启动均在此段无进展)。前后各插拍一次。
    if progress_cb:
        progress_cb()
    stage1 = [p for p in pool if p["vol24h"] >= MIN_VOL]
    if progress_cb:
        progress_cb()
    print(f"  阶段1 流动性/价格门槛: {len(pool)} → {len(stage1)} 个")

    # 阶段2：1h 趋势 + ATR（每个候选 1 次 K线请求）
    stage2 = []
    for p in stage1:
        if progress_cb:
            progress_cb()
        try:
            kl = exchange.fetch_candles(p["instId"], "1H", limit=120)
            if len(kl) < 60:
                continue
            last_close = kl[-1].close
            if last_close < MIN_PRICE:
                continue
            ks = _klines_to_dicts(kl)
            closes = [k["close"] for k in ks]
            e20, e50 = ema(closes, 20), ema(closes, 50)
            if not e20 or not e50 or e50[-1] == 0:
                continue
            dev = (e20[-1] - e50[-1]) / e50[-1]
            if abs(dev) < MIN_TREND_DEV:
                continue
            a = atr(ks, 14)
            atr_pct = a / last_close
            if not (ATR_SWEET_LOW <= atr_pct <= ATR_SWEET_HIGH):
                continue
            # is_stock 只信池来源标记——此前用 startswith("X") 兜底,把 XRP/XLM
            # 等 X 开头加密币误标成美股(2026-08-20 修复,看账数据曾被污染)
            stage2.append({"base": p["base"], "instId": p["instId"],
                           "vol24h": p["vol24h"], "dir": 1 if dev > 0 else -1,
                           "trend_dev": dev, "atr_pct": atr_pct,
                           "price": last_close,
                           "is_stock": p.get("is_stock", False)})
        except (ExchangeError, Exception):
            continue
    print(f"  阶段2 1h 趋势+ATR: {len(stage1)} → {len(stage2)} 个")

    # 阶段3：4h 共振（只对 1h 有趋势的候选再拉 4h）
    stage3 = []
    for c in stage2:
        if progress_cb:
            progress_cb()
        try:
            kl4 = exchange.fetch_candles(c["instId"], "4H", limit=80)
            if len(kl4) < 50:
                continue
            c4 = [k.close for k in kl4]
            e20, e50 = ema(c4, 20), ema(c4, 50)
            if not e20 or not e50:
                continue
            dir4 = 1 if e20[-1] > e50[-1] else -1
            if dir4 != c["dir"]:
                continue   # 4h 与 1h 不同向 → 放弃（顺大势）
            stage3.append(c)
        except (ExchangeError, Exception):
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
        fallback = [b for b in config.SYMBOLS[:5] if b not in blocked] or config.SYMBOLS[:5]
        watch = [{"base": b, "instId": _inst_id(b), "dir": 0, "score": 0.0,
                  "trend_dev": 0.0, "atr_pct": 0.0, "price": 0.0,
                  "is_stock": False}
                 for b in fallback]
    result = {
        "date": time.strftime("%Y-%m-%d"),
        "generated_at": time.time(),
        "fallback": bool(not stage3),
        "candidates": [{k: c[k] for k in ("base", "instId", "dir", "score",
                                           "trend_dev", "atr_pct", "price", "is_stock")}
                       for c in watch],
    }
    # 落库（storage 层 watchlist 表）。2026-08-20 修复: 此前只 INSERT OR REPLACE,
    # 同日重扫时"不再入选"的旧候选残留在当日池里(政策切换当天旧现货美股
    # 代币赖着不走)——先清当日再写,当日行 = 最新一次扫描的完整结果。
    import storage.db as sdb
    sdb.init_db(db_path)
    with sdb.tx(db_path=db_path) as conn:
        conn.execute("DELETE FROM watchlist WHERE date=?", [result["date"]])
        for c in watch:
            conn.execute(
                "INSERT OR REPLACE INTO watchlist (date,base,inst_id,dir,score,"
                "trend_dev,atr_pct,price,is_stock) VALUES (?,?,?,?,?,?,?,?,?)",
                [result["date"], c["base"], c.get("instId"), c.get("dir", 0),
                 c.get("score", 0.0), c.get("trend_dev", 0.0), c.get("atr_pct", 0.0),
                 c.get("price", 0.0), 1 if c.get("is_stock") else 0])
    print(f"  结果: 选出 {len(watch)} 个候选 → {'隔离库' if db_path else 'crypto_agent.db'}:watchlist")
    for c in watch:
        print(f"    {c['base']:<10} {'多' if c['dir']>0 else '空'}  趋势{c['trend_dev']*100:+.1f}%  "
              f"ATR{c['atr_pct']*100:.1f}%  评分{c['score']:.2f}")
    # 每天扫一次候选,顺手清过期流水(频率合适;失败不影响候选池)。
    try:
        pruned = sdb.prune_old_rows(db_path=db_path)
        n = sum(pruned.values())
        detail = ", ".join(f"{k}={v}" for k, v in pruned.items() if v)
        print(f"  库清理(保留{config.DB_RETENTION_DAYS}天): 删除 {n} 行"
              + (f" ({detail})" if detail else "（无过期）"))
    except Exception as e:
        print(f"  库清理失败(不影响候选池): {e}")
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


def load_watchlist(fallback=None, db_path=None):
    """读当日 watchlist（SQLite），返回 {base: score}（评分用于动态笔数）。
    过期/缺失回退固定池（无评分 → 默认笔数）。"""
    fallback = fallback or config.SYMBOLS[:5]
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        rows = sdb.q("SELECT base, score FROM watchlist WHERE date=?",
                     [time.strftime("%Y-%m-%d")], db_path=db_path)
        if rows:
            return {r["base"]: r["score"] for r in rows}
    except Exception:
        pass
    return {b: None for b in fallback}


if __name__ == "__main__":
    screen_daily()
