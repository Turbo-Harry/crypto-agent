"""
订单流（Order Flow）数据层 — 价格之外的微观信息。
数据源：币安现货镜像（data-api.binance.vision，免费无需 key）

两个核心因子：
  1. 订单簿失衡（Order Book Imbalance）：买盘 vs 卖盘深度，反映短期买卖压力
  2. 主动买卖比（Taker Buy Ratio）：主动买入量占比，反映谁在主动成交
    - isBuyerMaker=True  → 主动卖出（卖方吃单）
    - isBuyerMaker=False → 主动买入（买方吃单）

这些是"价格之外的信息"——不是价格数学变换，而是真实的买卖力量。
"""
import json
import sys
import os
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://data-api.binance.vision"


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_orderbook(symbol="BTCUSDT", limit=100):
    """拉订单簿，返回 {bids: [[price,qty]], asks: [[price,qty]]}。"""
    d = _get(f"{BASE}/api/v3/depth?symbol={symbol}&limit={limit}")
    return {"bids": [[float(p), float(q)] for p, q in d["bids"]],
            "asks": [[float(p), float(q)] for p, q in d["asks"]]}


def fetch_trades(symbol="BTCUSDT", limit=1000):
    """拉最近成交，返回 [{price, qty, is_buyer_maker, time}]。"""
    d = _get(f"{BASE}/api/v3/trades?symbol={symbol}&limit={limit}")
    return [{"price": float(t["price"]), "qty": float(t["qty"]),
             "is_buyer_maker": t["isBuyerMaker"], "time": t["time"]} for t in d]


def orderbook_imbalance(orderbook, depth_pct=1.0):
    """
    订单簿失衡：-1（全卖）~ +1（全买）。
    用前 N 档的买卖量计算。正值=买盘强，负值=卖盘强。
    """
    bid_vol = sum(q for _, q in orderbook["bids"])
    ask_vol = sum(q for _, q in orderbook["asks"])
    if bid_vol + ask_vol == 0:
        return 0.0
    return (bid_vol - ask_vol) / (bid_vol + ask_vol)


def taker_buy_ratio(trades):
    """主动买入占比：0~1，>0.5 主动买多，<0.5 主动卖多。"""
    buy_qty = sum(t["qty"] for t in trades if not t["is_buyer_maker"])
    total_qty = sum(t["qty"] for t in trades)
    return buy_qty / total_qty if total_qty > 0 else 0.5


def orderflow_snapshot(symbol="BTCUSDT"):
    """一次快照：返回订单流因子。"""
    ob = fetch_orderbook(symbol)
    trades = fetch_trades(symbol)
    return {
        "symbol": symbol,
        "imbalance": orderbook_imbalance(ob),
        "taker_buy_ratio": taker_buy_ratio(trades),
        "bid_depth": sum(q for _, q in ob["bids"]),
        "ask_depth": sum(q for _, q in ob["asks"]),
    }


if __name__ == "__main__":
    for sym in ["BTCUSDT", "ETHUSDT"]:
        snap = orderflow_snapshot(sym)
        print(f"{sym}: 订单簿失衡 {snap['imbalance']:+.3f} | "
              f"主动买占比 {snap['taker_buy_ratio']*100:.1f}% | "
              f"买盘 {snap['bid_depth']:.1f} vs 卖盘 {snap['ask_depth']:.1f}")
