"""
执行层工具 — 统一"名义 USDT → 数量"换算、精度对齐、最小下单量校验。

解决的问题（审计 CR-9 / OP-9）：
  1. 数量精度/最小下单量未校验 → 静默下单失败。
  2. 各入口名义口径不一致（150 / 700 / max(700,100) 恒 700）。
  3. 低价币（DOGE/XRP）lot size 与最小下单量未校验。

依赖：exchange.base.ExchangeAdapter 接口（无 ccxt）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.models import floor_to_lot


def precision_decimals(precision):
    """精度(float) → 小数位数。0.01→2, 0.0001→4, 1e-08→8, >=1→0。"""
    if precision is None or precision >= 1:
        return 0
    s = f"{precision}".rstrip("0")
    if "e-" in s:          # 科学计数法，如 1e-08
        return int(s.split("e-")[1])
    if "." in s:
        return len(s.split(".")[1])
    return 0


def qty_for_notional(adapter, inst_id, notional_usdt, price=None):
    """名义 USDT → 数量（对齐 lotSz + 最小/最大下单量校验）。
    返回 (qty, reason)：qty 为 None 表示不可下单，reason 说明原因。
    swap 返回基础币数量（调用方直接传给 place_market_order，适配层再换算张数）。"""
    inst = adapter.instrument(inst_id)
    if price is None:
        price = adapter.fetch_ticker_last(inst_id)
    if not price:
        return None, "价格缺失"
    qty = notional_usdt / price
    lot = inst.ct_val if inst.venue == "swap" else inst.lot_sz
    qty = floor_to_lot(qty, lot)
    if qty <= 0:
        return None, "数量为 0"
    if inst.min_sz > 0:
        min_qty = inst.min_sz * (inst.ct_val if inst.venue == "swap" else 1.0)
        if qty < min_qty:
            return None, (f"数量 {qty} < 最小下单量 {min_qty}（名义 {notional_usdt} USDT "
                          f"不够，需 ≥ {min_qty * price:.0f} USDT）")
    if inst.max_mkt_sz > 0:
        max_qty = inst.max_mkt_sz * inst.ct_val * 0.9
        if qty > max_qty:
            qty = floor_to_lot(max_qty, lot)
            return qty, f"数量超上限，压至 {qty}"
    return qty, ""


if __name__ == "__main__":
    # 自测：精度转换
    cases = {0.01: 2, 0.0001: 4, 0.00000001: 8, 1: 0, None: 0, 0.1: 1}
    for p, want in cases.items():
        got = precision_decimals(p)
        assert got == want, f"precision_decimals({p}) = {got}, 期望 {want}"
        print(f"  precision {p} → {got} 位小数 ✅")
    print("execution.py 自测通过 ✅")
