"""
资金费率（Funding Rate）数据层 — 市场情绪指标。
数据源：Gate.io（免费、免 key，服务器实测可用）
API: https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract=BTC_USDT

资金费率含义：正费率=多头付空头（市场偏多/过热），负费率=空头付多头（市场偏空/超卖）。
用法：作为情绪过滤/择时（极端正费率=过热，减仓或不做多）。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://api.gateio.ws"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_funding.json")


def fetch_funding_rate(contract="BTC_USDT"):
    """拉当前资金费率（Gate.io）。返回 dict {contract, rate, ...}。"""
    url = f"{BASE}/api/v4/futures/usdt/funding_rate?contract={contract}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def fetch_all_funding_rates():
    """拉 Gate.io 全部 USDT 合约的资金费率（用于截面情绪）。"""
    url = f"{BASE}/api/v4/futures/usdt/funding_rate"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    try:
        fr = fetch_funding_rate("BTC_USDT")
        # Gate.io 返回 [{"r": rate, "t": timestamp}, ...]
        items = fr if isinstance(fr, list) else [fr]
        print(f"BTC 资金费率（最近 {len(items)} 条，每8小时）:")
        from datetime import datetime, timezone
        for item in items[:5]:
            ts = int(item.get("t", 0))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            rate = float(item.get("r", 0)) * 100
            print(f"  {dt}  {rate:.4f}%")
    except Exception as e:
        print(f"拉取失败: {e}")
