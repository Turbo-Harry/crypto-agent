"""
费率/手续费记账回归测试（2026-08-23 用户问"会计算费率和手续费吗",离线纯函数）:
  1. 手续费估算: 双边 taker = (入场名义 + 出场名义) × FEE_RATE_TAKER
  2. 净盈亏 = 毛盈亏 − 手续费 − 资金费
  3. 老数据(无 fees/funding 列) → 按 0 处理,不追溯扣
运行: PYTHONPATH=lib python3 tests/test_fee_accounting.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.trade_journal import (estimate_fees_usdt,
                                     net_realized_pnl_usdt,
                                     total_net_realized_pnl_usdt,
                                     realized_pnl_usdt)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def main():
    # 多单: 名义 100, pnl +2% → (100 + 102) × 0.0005 = 0.101
    t_long = {"pnl": 0.02, "notional_usdt": 100.0, "direction": "long"}
    f = estimate_fees_usdt(t_long)
    check("多单双边手续费估算 (100+102)*0.0005=0.101", abs(f - 0.101) < 1e-9,
          f"f={f}")

    # 空单: 出场名义不变 → (100+100)*0.0005 = 0.1
    t_short = {"pnl": 0.02, "notional_usdt": 100.0, "direction": "short"}
    f2 = estimate_fees_usdt(t_short)
    check("空单双边手续费估算 (100+100)*0.0005=0.1", abs(f2 - 0.1) < 1e-9,
          f"f={f2}")

    # 净盈亏
    t_net = {"pnl": 0.02, "notional_usdt": 100.0, "direction": "long",
             "fees_usdt": 0.101, "funding_usdt": 0.03}
    gross = realized_pnl_usdt(t_net)
    net = net_realized_pnl_usdt(t_net)
    check("净盈亏 = 毛(2.0) − 费(0.101) − 资金(0.03) = 1.869",
          abs(net - 1.869) < 1e-9, f"net={net} gross={gross}")

    # 老数据不追溯扣
    t_old = {"pnl": 0.02, "notional_usdt": 100.0}
    net_old = net_realized_pnl_usdt(t_old)
    check("老数据无费用列 → 净=毛", abs(net_old - 2.0) < 1e-9, f"net={net_old}")

    # 合计净盈亏
    total = total_net_realized_pnl_usdt([
        dict(t_net, status="closed"), dict(t_old, status="closed"),
        {"pnl": None, "status": "closed"}])
    check("合计净盈亏(含未平仓过滤) = 1.869+2.0", abs(total - 3.869) < 1e-9,
          f"total={total}")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
