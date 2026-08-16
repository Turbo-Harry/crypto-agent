"""
未平仓量（Open Interest）数据层 — 市场杠杆水平 + 多空比 + 爆仓量。
数据源：Gate.io（免费免 key，服务器实测可用）

OI 决策价值：
  open_interest_usd  持仓量（杠杆水平，快速上升=风险累积）
  lsr_taker          主动买卖多空比（>1 主动买多，<1 主动卖空）
  lsr_account        账户多空比（散户情绪，极端=市场拥挤）
  long/short_liq_size 多空爆仓量（清算瀑布信号）
"""
import json
import sys
import os
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://api.gateio.ws"


def fetch_oi(contract="BTC_USDT"):
    """拉单个合约的持仓量/多空比/爆仓量。"""
    url = f"{BASE}/api/v4/futures/usdt/contract_stats?contract={contract}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    if not d:
        return None
    s = d[0]
    return {
        "open_interest_usd": s.get("open_interest_usd", 0),
        "lsr_taker": s.get("lsr_taker", 0.5),       # 主动买卖多空比
        "lsr_account": s.get("lsr_account", 1.0),   # 账户多空比
        "long_liq_usd": s.get("long_liq_usd", 0),   # 多头爆仓量
        "short_liq_usd": s.get("short_liq_usd", 0), # 空头爆仓量
        "mark_price": s.get("mark_price", 0),
    }


if __name__ == "__main__":
    for c in ["BTC_USDT", "ETH_USDT"]:
        try:
            oi = fetch_oi(c)
            print(f"{c.split('_')[0]}: 持仓 {oi['open_interest_usd']/1e8:.1f}亿USD | "
                  f"主动多空比 {oi['lsr_taker']:.2f} | 账户多空比 {oi['lsr_account']:.2f} | "
                  f"多头爆仓 {oi['long_liq_usd']/1e4:.0f}万")
        except Exception as e:
            print(f"{c}: {e}")
