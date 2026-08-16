"""
传输层 — OKX 原生 REST（无 ccxt，无第三方 SDK）。

职责（唯一）：
  1. HTTP GET/POST，HMAC-SHA256 签名（OK-ACCESS-* 头）。
  2. 模拟盘：请求头 x-simulated-trading: 1（虚拟资金下单）。
  3. 简单限速（每请求最小间隔 + 429 退避重试 1 次）。
  4. 错误归一：非 code=0 → ExchangeError(code,msg)。

不做任何业务换算——那是适配层的事。
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from exchange.base import ExchangeError

BASE_URL = "https://www.okx.com"


class OKXTransport:
    """OKX REST 直连客户端（公共 + 私有端点）。"""

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 sandbox: bool = True, base_url: str = BASE_URL,
                 min_interval: float = 0.1):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.sandbox = sandbox
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._last_ts = 0.0

    # ---------- 底层 HTTP ----------
    def _throttle(self):
        wait = self._last_ts + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.time()

    @staticmethod
    def _iso_ts() -> str:
        """OKX 要求 ISO8601 毫秒 UTC，如 2026-08-16T07:00:00.123Z。"""
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method}{path}{body}".encode()
        return base64.b64encode(
            hmac.new(self.secret.encode(), msg, hashlib.sha256).digest()).decode()

    def request(self, method: str, path: str, params: dict = None,
                body: dict = None, auth: bool = False) -> dict:
        """发起请求，返回完整 JSON（含 code/data/msg）。失败抛 ExchangeError。"""
        params = params or {}
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        if qs:
            qs = "?" + qs
        body_str = json.dumps(body) if body is not None else ""
        url = self.base_url + path + qs

        headers = {"Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        if auth:
            ts = self._iso_ts()
            headers.update({
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": self._sign(ts, method, path + qs, body_str),
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
            })
            if self.sandbox:
                headers["x-simulated-trading"] = "1"

        data = body_str.encode() if body_str else None
        for attempt in (1, 2):
            self._throttle()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if attempt == 1 and e.code == 429:      # 限频 → 退避重试一次
                    time.sleep(1.0)
                    continue
                raise ExchangeError(f"HTTP {e.code}: {e.read()[:200]}")
            except Exception as e:
                raise ExchangeError(f"网络错误: {e}")
            if resp.get("code") != "0":
                # 2026-08-17: 沙盘 code=1 "All operations failed" 会掩盖真实原因
                # (如 51000/51001/51169),必须把 data[].sCode/sMsg 穿透进异常文本,
                # 否则每次下单失败都要人工做最小对照实验(见 pitfalls.md 关键教训)。
                rows = resp.get("data") or [{}]
                row = rows[0] if isinstance(rows, list) else rows
                extra = ""
                if row.get("sCode") and row.get("sCode") != "0":
                    extra = f" | sCode={row.get('sCode')} {row.get('sMsg')}"
                raise ExchangeError(
                    f"code={resp.get('code')} {resp.get('msg')}{extra}")
            return resp

    # ---------- 便捷封装 ----------
    def public(self, path: str, params: dict = None) -> dict:
        return self.request("GET", path, params=params, auth=False)

    def private_get(self, path: str, params: dict = None) -> dict:
        return self.request("GET", path, params=params, auth=True)

    def private_post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body=body, auth=True)
