"""
OKX 原生 WebSocket 实时观测 — 实时价格、资金费率、成交。
ccxt 对 OKX 的 WebSocket 未实现，故用原生 WebSocket（websocket-client）。

公共频道（无需登录）：
  tickers      实时价格（现货 instId: BTC-USDT）
  funding-rate 实时资金费率（合约 instId: BTC-USDT-SWAP）
  trades       实时成交（含方向，判断主动买卖）

用法：
  python3 realtime_okx.py    # 实时打印 BTC/ETH 价格 + 资金费率
"""
import sys
import os
import json
import threading
import time
import urllib.request
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import websocket

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
MAX_STALE_SECONDS = 120   # 数据僵死阈值：超过则判定死链，强制重连
CANDLE_KEEP = 15          # REST 预热 K线根数（仅冷启动用）
VOL_WINDOW_SECONDS = 900  # 波动率滚动窗口 15 分钟
VOL_MIN_SPAN_SECONDS = 300  # 窗口至少跨 5 分钟才计算波动率


class OKXRealtime:
    """OKX 实时行情观测器（价格 + 资金费率 + 成交）。
    内置监督线程：断线/僵死自动重连；K线按 ts 去重；冷启动 REST 预热。"""

    def __init__(self, symbols=None):
        # symbols: 如 ["BTC", "ETH", "SOL"]
        self.symbols = symbols or ["BTC", "ETH", "SOL"]
        self.subscribed = set(self.symbols)   # 动态订阅去重(2026-08-17)
        self.ws = None
        self.latest = {}  # {base: {"price":..., "funding":..., "taker_buy":...}}
        self._running = False
        self._shutdown = False
        self._supervisor_started = False
        self._restart_lock = threading.Lock()
        self.last_msg_ts = time.time()
        self._err_count = 0  # 解析错误计数（防静默吞异常）

    def _on_message(self, ws, message):
        try:
            self.last_msg_ts = time.time()
            # OKX 心跳响应是纯文本 "pong"，直接忽略
            if isinstance(message, str) and message.strip() == "pong":
                return
            d = json.loads(message)
            if "data" not in d or not d["data"]:
                if d.get("event") == "error":
                    print(f"OKX WebSocket 错误事件: {d.get('msg')} (code {d.get('code')})")
                return
            arg = d.get("arg", {})
            channel = arg.get("channel")
            inst = arg.get("instId", "")
            now = time.time()
            if channel == "tickers":
                base = inst.split("-")[0]
                d0 = d["data"][0]
                entry = self.latest.setdefault(base, {})
                if inst.endswith("-SWAP"):
                    entry["swap_price"] = float(d0["last"])
                    entry["swap_price_ts"] = now
                else:
                    entry["price"] = float(d0["last"])
                    entry["price_ts"] = now
                    # 波动率：直接用价格流滚动高低点（与 K 线等价——K 线本就是成交聚合）
                    win = entry.setdefault("price_win", deque())
                    win.append((now, entry["price"]))
                    while win and now - win[0][0] > VOL_WINDOW_SECONDS:
                        win.popleft()
                    if win and now - win[0][0] >= VOL_MIN_SPAN_SECONDS:
                        hi = max(p for _, p in win)
                        lo = min(p for _, p in win)
                        if lo > 0:
                            entry["vol_15m"] = (hi - lo) / lo
                            entry["vol_ts"] = now
            elif channel == "funding-rate":
                base = inst.split("-")[0]
                rate = float(d["data"][0].get("fundingRate", 0))
                entry = self.latest.setdefault(base, {})
                entry["funding"] = rate
                entry["funding_ts"] = now
            elif channel == "candle1m":
                # 1 分钟 K 线 → 实时波动率（日内短线用分钟级，不用 24h）
                # 注意：OKX 对进行中的K线会多次推送同一 ts → 按 ts 去重（更新末条）
                base = inst.split("-")[0]
                k = d["data"][0]  # ["ts","o","h","l","c","vol",...]
                ts = int(k[0])
                entry = self.latest.setdefault(base, {})
                candles = entry.setdefault("candles_1m", [])
                if candles and candles[-1]["ts"] == ts:
                    candles[-1] = {"high": float(k[2]), "low": float(k[3]), "ts": ts}
                else:
                    candles.append({"high": float(k[2]), "low": float(k[3]), "ts": ts})
                    if len(candles) > CANDLE_KEEP:
                        candles.pop(0)
                if len(candles) >= 5:
                    hi = max(c["high"] for c in candles)
                    lo = min(c["low"] for c in candles)
                    if lo > 0:
                        entry["vol_15m"] = (hi - lo) / lo  # 15 分钟振幅
                        entry["vol_ts"] = now
            elif channel == "trades":
                base = inst.split("-")[0]
                # 主动买卖：side 字段（buy/sell）
                buys = sum(1 for t in d["data"] if t.get("side") == "buy")
                total = len(d["data"])
                self.latest.setdefault(base, {})["taker_buy"] = buys / total if total else 0.5
        except Exception as e:
            self._err_count += 1
            if self._err_count % 50 == 1:   # 每 50 次错误打一条日志，不刷屏
                print(f"WebSocket 消息解析错误 x{self._err_count}（最近: {e}）")

    def _on_error(self, ws, error):
        print(f"WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        print(f"WebSocket 关闭: {code} {msg}（监督线程将自动重连）")
        self._running = False

    def _on_open(self, ws):
        # 订阅各标的的 tickers + funding-rate + trades
        # 额外订阅合约 ticker（算基差 basis = perp/spot - 1 用 — OP-3）
        # 波动率不需要 K线频道（OKX 公共 WS 已下线 candle 频道，且价格流与K线等价）：
        # 由现货价格流滚动窗口直接算 15 分钟振幅
        for base in self.symbols:
            args = [
                {"channel": "tickers", "instId": f"{base}-USDT"},
                {"channel": "tickers", "instId": f"{base}-USDT-SWAP"},
                {"channel": "funding-rate", "instId": f"{base}-USDT-SWAP"},
                {"channel": "trades", "instId": f"{base}-USDT"},
            ]
            sub = {"op": "subscribe", "args": args}
            ws.send(json.dumps(sub))
        print(f"已订阅 {len(self.symbols)} 个标的（现货/合约价格/费率/成交；波动率走价格流滚动窗口）")

    def subscribe(self, base):
        """下单后动态订阅(2026-08-17): 秒级价格推送覆盖交易标的。
        幂等(重复订阅去重);连接未就绪时记入清单,重连时一并订阅;线程安全。"""
        try:
            if base in self.subscribed:
                return
            self.subscribed.add(base)
            self.symbols.append(base)
            ws = self.ws
            if ws is None or not self._running:
                return
            args = [
                {"channel": "tickers", "instId": f"{base}-USDT"},
                {"channel": "tickers", "instId": f"{base}-USDT-SWAP"},
                {"channel": "funding-rate", "instId": f"{base}-USDT-SWAP"},
                {"channel": "trades", "instId": f"{base}-USDT"},
            ]
            with self._restart_lock:
                ws.send(json.dumps({"op": "subscribe", "args": args}))
            print(f"🔔 动态订阅 {base}（下单后秒级行情）")
        except Exception:
            pass

    # ---------- 断线重连：监督线程 ----------
    def _supervisor(self):
        """监督线程：每 30s 检查连接状态，断线或数据僵死 → 重启。"""
        while not self._shutdown:
            time.sleep(30)
            if self._shutdown:
                break
            if not self._running or self.stale_seconds() > MAX_STALE_SECONDS:
                if self._restart_lock.acquire(blocking=False):
                    try:
                        print(f"监督线程: 重启 WebSocket（running={self._running}, "
                              f"stale={self.stale_seconds():.0f}s）")
                        try:
                            self.ws.close()
                        except Exception:
                            pass
                        self.start()
                    finally:
                        self._restart_lock.release()

    def _refresh_candles_from_rest(self, base):
        """REST 拉某币最近 15 根 1m K线并更新 vol_15m。返回 True/False。"""
        try:
            url = ("https://www.okx.com/api/v5/market/candles"
                   f"?instId={base}-USDT&bar=1m&limit={CANDLE_KEEP}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            rows = d.get("data") or []  # 最新在前
            candles = [{"high": float(x[2]), "low": float(x[3]), "ts": int(x[0])}
                       for x in reversed(rows)]
            entry = self.latest.setdefault(base, {})
            # 与已有窗口按 ts 合并去重（保留较新 15 根）
            old = entry.get("candles_1m", [])
            merged = {c["ts"]: c for c in old}
            for c in candles:
                merged[c["ts"]] = c
            candles = [merged[ts] for ts in sorted(merged)][-CANDLE_KEEP:]
            entry["candles_1m"] = candles
            if len(candles) >= 5:
                hi = max(c["high"] for c in candles)
                lo = min(c["low"] for c in candles)
                if lo > 0:
                    entry["vol_15m"] = (hi - lo) / lo
                    entry["vol_ts"] = time.time()
            return True
        except Exception as e:
            print(f"  {base} K线REST刷新失败: {e}")
            return False

    def _seed_candles_from_rest(self):
        """冷启动预热：REST 拉最近 15 根 1m K线，让 vol_15m 启动即就绪。
        （此后由价格流滚动窗口持续更新，REST 仅在每次重连后预热一次）"""
        ok = 0
        for base in self.symbols:
            if self._refresh_candles_from_rest(base):
                ok += 1
        print(f"波动率预热完成 {ok}/{len(self.symbols)}")

    def _pinger(self):
        """应用层心跳：OKX 要求 30s 内发送纯文本 "ping"（JSON {"op":"ping"} 报 60012）。"""
        while not self._shutdown:
            time.sleep(20)
            if self._shutdown or self.ws is None:
                break
            try:
                self.ws.send("ping")
            except Exception:
                pass  # 发送失败由监督线程的重连逻辑兜底

    def start(self):
        """启动 WebSocket（后台线程）+ 监督线程 + 心跳 + K线预热。"""
        self.ws = websocket.WebSocketApp(
            WS_URL,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._running = True
        t = threading.Thread(target=self.ws.run_forever, daemon=True)
        t.start()
        if not self._supervisor_started:
            self._supervisor_started = True
            threading.Thread(target=self._supervisor, daemon=True).start()
            threading.Thread(target=self._pinger, daemon=True).start()
        threading.Thread(target=self._seed_candles_from_rest, daemon=True).start()
        return self

    def stop(self):
        """停止：关闭连接并退出监督线程。"""
        self._shutdown = True
        self._running = False
        try:
            self.ws.close()
        except Exception:
            pass

    def stale_seconds(self):
        """距最后一条推送消息的秒数（判断链路是否僵死）。"""
        return time.time() - self.last_msg_ts

    def get(self, base, max_age=None):
        """获取某标的的最新实时数据。
        max_age: 秒。给定后，超过该年龄的字段视为 stale 剔除（防止拿旧数据决策）。"""
        d = dict(self.latest.get(base, {}))
        if max_age:
            now = time.time()
            for field, ts_key in (("price", "price_ts"), ("funding", "funding_ts"),
                                  ("vol_15m", "vol_ts"), ("swap_price", "swap_price_ts")):
                ts = d.get(ts_key, 0)
                if not ts or now - ts > max_age:
                    d.pop(field, None)
        return d

    def snapshot(self):
        """获取所有标的的实时快照。"""
        return {b: self.latest.get(b, {}) for b in self.symbols}


if __name__ == "__main__":
    import time
    rt = OKXRealtime(["BTC", "ETH", "SOL"]).start()
    print("实时观测启动（15 秒演示）...")
    t0 = time.time()
    while time.time() - t0 < 15:
        time.sleep(2)
        snap = rt.snapshot()
        for base, d in snap.items():
            price = d.get("price", "?")
            fund = d.get("funding", 0)
            tb = d.get("taker_buy", 0.5)
            print(f"  {base}: 价格 {price} | 资金费率 {fund*100:+.4f}%/8h | 主动买占比 {tb*100:.0f}%")
        print("  ---")
    print("实时观测演示完成")
