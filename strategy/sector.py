"""
板块关 — 赛道分类 + 赛道相对强度过滤。
在"大盘关"通过后、"个币共振关"之前，判断资金流向哪个赛道，
只在强势赛道（赛道 RS 排名前 30%）里选币。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# 手动维护主流赛道映射（观察池币 → 赛道）
SECTOR_MAP = {
    # Layer 1
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "XRP": "L1", "BNB": "L1",
    "ADA": "L1", "AVAX": "L1", "SUI": "L1", "NEAR": "L1", "DOT": "L1",
    "ICP": "L1", "TRX": "L1", "CORE": "L1", "ONE": "L1", "XLM": "L1",
    "BCH": "L1", "LTC": "L1", "ZEC": "L1", "FIL": "L1",
    # Layer 2
    "ARB": "L2", "OP": "L2", "MOVE": "L2",
    # DeFi
    "UNI": "DeFi", "LINK": "DeFi", "CRV": "DeFi", "JTO": "DeFi",
    "ONDO": "DeFi", "ETHFI": "DeFi", "AAVE": "DeFi",
    # Meme
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme",
    "BOME": "Meme", "PUMP": "Meme", "PENGU": "Meme", "TRUMP": "Meme",
    # AI / 数据
    "WLD": "AI", "LIT": "AI", "GRAM": "AI", "KAITO": "AI",
    # 平台币 / 其他
    "OKB": "Exchange", "HYPE": "DEX", "PI": "Other",
}

SECTOR_TOP_PERCENT = 0.30  # 赛道 RS 排名前 30% 才算强势赛道


def get_sector(symbol):
    """symbol 如 'BTC-USDT'，返回赛道名（未知返回 'Unknown'）"""
    base = symbol.split("-")[0]
    return SECTOR_MAP.get(base, "Unknown")


def compute_sector_rs(rs_map):
    """
    rs_map: {symbol: rs值}（个币 20 日相对强度）
    返回 {赛道: 平均RS}，仅统计已知赛道。
    """
    sector_rs = {}
    sector_cnt = {}
    for sym, rs in rs_map.items():
        sec = get_sector(sym)
        if sec == "Unknown":
            continue
        sector_rs[sec] = sector_rs.get(sec, 0.0) + rs
        sector_cnt[sec] = sector_cnt.get(sec, 0) + 1
    return {sec: sector_rs[sec] / sector_cnt[sec] for sec in sector_rs}


def sector_gate(symbol, sector_rs):
    """
    板块关：该币所在赛道是否为强势赛道（RS 排名前 30%）。
    返回 (通过?, 原因)。
    """
    sec = get_sector(symbol)
    if sec == "Unknown":
        return False, "未知赛道"
    if not sector_rs:
        return False, "无赛道数据"
    ranked = sorted(sector_rs.items(), key=lambda x: x[1], reverse=True)
    rank_idx = [s for s, _ in ranked].index(sec)
    percentile = rank_idx / max(len(ranked) - 1, 1)
    if percentile > SECTOR_TOP_PERCENT:
        return False, f"赛道 {sec} 相对强度分位 {percentile:.0%}（> {SECTOR_TOP_PERCENT:.0%}，非强势）"
    return True, f"赛道 {sec} 强势（分位 {percentile:.0%}，RS {sector_rs[sec]:+.1f}%）"


if __name__ == "__main__":
    # 自测：模拟 rs_map
    rs_map = {
        "BTC-USDT": 2.0, "ETH-USDT": 3.0, "SOL-USDT": 8.0,
        "DOGE-USDT": 15.0, "SHIB-USDT": 12.0,
        "UNI-USDT": -1.0, "LINK-USDT": -2.0,
        "WLD-USDT": 5.0,
    }
    sec_rs = compute_sector_rs(rs_map)
    print("赛道相对强度:")
    for s, v in sorted(sec_rs.items(), key=lambda x: x[1], reverse=True):
        print(f"  {s:<10} RS {v:+.1f}%")
    print()
    for sym in rs_map:
        ok, reason = sector_gate(sym, sec_rs)
        print(f"  {sym:<12} {'✅' if ok else '❌'} {reason}")
