# -*- coding: utf-8 -*-
"""
ccxt.pro 实时行情（2026-08-23 用户指示"换"用 ccxt 实时监听接口）——
替换自写 WebSocket 模块(realtime_okx.py,保留可回滚),同接口:

  OKXRealtime(symbols, fetch_candles).start()
  .subscribe(base) / .get(base, max_age) / .snapshot() / .stale_seconds()

实现: 后台 asyncio 循环跑 ccxt.pro watch_ticker(每个订阅币一个 task,
subscribe 线程安全新增订阅),价格进共享 dict;冷启动用 REST 1m K 线预热
波动率(与旧模块'波动率预热 N/10'同语义)。watch_ticker 断线自动重连
由 ccxt.pro 处理,外层 task 循环容错。凭证解析与 connect() 一致:
LIVE_MODE → ~/.crypto_live/okx_live.json(实盘),否则 okx_config.json。
"""
import os
import json
import asyncio
import threading
import time
from collections import deque

import config
from data.orderflow import OrderFlowAccumulator

CANDLE_KEEP = 15   # 波动率预热用 1m K 线根数（与旧模块一致）


class OKXRealtime:
    def __init__(self, symbols=None, fetch_candles=None,
                 api_key=None, secret=None, password=None, sandbox=None):
        self.symbols = list(symbols or ["BTC", "ETH", "SOL"])
        self._fetch_candles = fetch_candles
        self._cred = (api_key, secret, password)
        self._sandbox = sandbox  # None → 按 config.LIVE_MODE 自动判定
        self.subscribed = set(self.symbols)
        self.latest = {}     # {base: {"price":..., "price_ts":...,
                             #         "vol_15m":..., "vol_ts":...}}
        self._running = False
        self._shutdown = False
        self._last_msg_ts = time.time()
        self._loop = None
        self._thread = None
        self._orderflow = OrderFlowAccumulator(
            config.ORDERFLOW_BOOK_DEPTH, config.ORDERFLOW_WINDOW_SECONDS,
            config.ORDERFLOW_MIN_EVENTS, config.ORDERFLOW_MAX_AGE_SECONDS)
        self._trade_flow = {base: deque() for base in self.symbols}

    def _resolve_cred(self):
        """凭证解析: LIVE_MODE → 实盘凭证;否则 okx_config.json(与 connect() 一致)。
        watch_ticker 是公共频道,凭证缺失也能跑(拿不到只影响私有频道)。"""
        if self._cred[0]:
            sandbox = (not self._sandbox) if self._sandbox is None else self._sandbox
            return self._cred, sandbox
        import config
        live = bool(getattr(config, "LIVE_MODE", False))
        path = os.path.expanduser(
            config.LIVE_CRED_FILE if live else "okx_config.json")
        try:
            cfg = json.load(open(path))
            cred = (cfg.get("apiKey", ""), cfg.get("secret", ""),
                    cfg.get("password", ""))
        except Exception:
            cred = ("", "", "")
        sandbox = (not live) if self._sandbox is None else self._sandbox
        return cred, sandbox

    # ---------- 生命周期 ----------
    def start(self):
        if self._running:
            return self
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                        name="ccxtpro-rt", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._shutdown = True
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        import warnings
        warnings.filterwarnings("ignore")
        import ccxt.pro as ccxtpro
        cred, sandbox = self._resolve_cred()
        ex = ccxtpro.okx({
            "apiKey": cred[0] or None, "secret": cred[1] or None,
            "password": cred[2] or None, "enableRateLimit": True})
        if sandbox is not None:
            ex.set_sandbox_mode(sandbox)
        try:
            await ex.load_markets()
        except Exception:
            pass
        self._seed_candles_from_rest()
        tasks = {}
        f_tasks = {}
        book_tasks = {}
        trade_tasks = {}
        while not self._shutdown:
            for base in list(self.subscribed):
                if base in tasks and not tasks[base].done():
                    continue
                tasks[base] = asyncio.ensure_future(self._watch_one(ex, base))
                if base not in f_tasks or f_tasks[base].done():
                    f_tasks[base] = asyncio.ensure_future(
                        self._watch_funding(ex, base))
                if base not in book_tasks or book_tasks[base].done():
                    book_tasks[base] = asyncio.ensure_future(
                        self._watch_book(ex, base))
                if base not in trade_tasks or trade_tasks[base].done():
                    trade_tasks[base] = asyncio.ensure_future(
                        self._watch_trades(ex, base))
            await asyncio.sleep(1)
        for t in (list(tasks.values()) + list(f_tasks.values()) +
                  list(book_tasks.values()) + list(trade_tasks.values())):
            t.cancel()

    async def _watch_one(self, ex, base):
        sym = f"{base}/USDT:USDT"
        while not self._shutdown:
            try:
                t = await ex.watch_ticker(sym)
                last = t.get("last")
                if last:
                    now = time.time()
                    entry = self.latest.setdefault(base, {})
                    price = float(last)
                    entry["price"] = price
                    entry["price_ts"] = now
                    win = entry.setdefault("price_win", deque())
                    win.append((now, price))
                    while win and now - win[0][0] > 900:
                        win.popleft()
                    if len(win) >= 2:
                        prices = [point[1] for point in win]
                        lo = min(prices)
                        if lo > 0:
                            entry["vol_15m"] = (max(prices) - lo) / lo
                            entry["vol_ts"] = now
                    self._last_msg_ts = now
            except Exception:
                await asyncio.sleep(2)   # 单币任务容错,不影响其它币

    async def _watch_funding(self, ex, base):
        """资金费率频道(2026-08-23 用户问'会计算费率吗'): 费率进 latest,
        /realtime 可见。首帧即返回当前值,后续随结算/变更推送。"""
        sym = f"{base}/USDT:USDT"
        while not self._shutdown:
            try:
                fr = await ex.watch_funding_rate(sym)
                v = fr.get("fundingRate")
                if v is not None:
                    self.latest.setdefault(base, {})
                    self.latest[base]["funding"] = float(v)
                    self.latest[base]["funding_ts"] = time.time()
            except Exception:
                await asyncio.sleep(10)

    async def _watch_book(self, ex, base):
        """Continuous L2 events for shadow OFI; never used to place orders."""
        sym = f"{base}/USDT:USDT"
        while not self._shutdown:
            try:
                book = await ex.watch_order_book(
                    sym, limit=config.ORDERFLOW_BOOK_DEPTH)
                now = time.time()
                self._orderflow.update(base, book, ts=now)
                self._last_msg_ts = now
            except Exception:
                await asyncio.sleep(2)

    async def _watch_trades(self, ex, base):
        """连续合约成交方向，供 paper 最终确认；不参与 live 决策。"""
        sym = f"{base}/USDT:USDT"
        while not self._shutdown:
            try:
                trades = await ex.watch_trades(sym)
                now = time.time()
                flow = self._trade_flow.setdefault(base, deque())
                for trade in trades or []:
                    flow.append((now, trade.get("side") == "buy"))
                while flow and now - flow[0][0] > config.ORDERFLOW_WINDOW_SECONDS:
                    flow.popleft()
                buys = sum(int(is_buy) for _, is_buy in flow)
                entry = self.latest.setdefault(base, {})
                entry["taker_buy_60s"] = buys / len(flow) if flow else None
                entry["trade_flow_count_60s"] = len(flow)
                self._last_msg_ts = now
            except Exception:
                await asyncio.sleep(2)

    def _seed_candles_from_rest(self):
        """冷启动预热(与旧模块同语义): REST 拉最近 15 根 1m K 线算 vol_15m。"""
        if not self._fetch_candles:
            return
        ok = 0
        for base in self.symbols:
            try:
                rows = self._fetch_candles(f"{base}-USDT", "1m", CANDLE_KEEP)
                if len(rows) >= 5:
                    hi = max(c.high for c in rows)
                    lo = min(c.low for c in rows)
                    if lo > 0:
                        entry = self.latest.setdefault(base, {})
                        entry["vol_15m"] = round((hi - lo) / lo, 6)
                        entry["vol_ts"] = time.time()
                        now = time.time()
                        win = entry.setdefault("price_win", deque())
                        for candle in rows:
                            ts = float(candle.ts) / 1000.0
                            if now - 900 <= ts <= now:
                                win.append((ts, float(candle.close)))
                        ok += 1
            except Exception:
                continue
        print(f"波动率预热完成 {ok}/{len(self.symbols)}")

    # ---------- 接口(与旧模块对齐) ----------
    def subscribe(self, base):
        """下单后动态订阅(2026-08-17 语义): 幂等,线程安全。
        新订阅由 _main 循环自动接管(订阅集是共享 set,循环每 1s 扫一遍)。"""
        if base in self.subscribed:
            return
        self.subscribed.add(base)
        self.symbols.append(base)
        print(f"🔔 动态订阅 {base}（ccxt.pro watch_ticker）")

    def get(self, base, max_age=None):
        """获取某标的的最新实时数据。max_age 秒内无更新则剔除价格字段。"""
        d = dict(self.latest.get(base, {}))
        if max_age:
            now = time.time()
            for field, ts_key in (("price", "price_ts"), ("vol_15m", "vol_ts"),
                                  ("funding", "funding_ts")):
                ts = d.get(ts_key, 0)
                if not ts or now - ts > max_age:
                    d.pop(field, None)
        return d

    def stale_seconds(self):
        """距最后一条推送消息的秒数(判断链路是否僵死)。"""
        return time.time() - self._last_msg_ts

    def get_orderflow(self, base):
        """Return freshness-gated multilevel event OFI shadow features."""
        return self._orderflow.snapshot(base)

    def snapshot(self):
        """获取所有标的的实时快照。"""
        return {b: self.latest.get(b, {}) for b in self.symbols}


if __name__ == "__main__":
    rt = OKXRealtime(["BTC", "ETH"]).start()
    print("ccxt.pro 实时行情演示（15 秒）...")
    t0 = time.time()
    while time.time() - t0 < 15:
        time.sleep(2)
        for base in ["BTC", "ETH"]:
            d = rt.get(base, max_age=5)
            print(f"  {base}: 价格 {d.get('price')} | vol_15m {d.get('vol_15m')}")
    rt.stop()
    print("done")
