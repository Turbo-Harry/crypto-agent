"""
动态止盈净EV 门槛回归测试（2026-08-25 用户指示"打开,如果预测位盈利-手续费<0则拒绝"）:
  1. 净EV 计算: 触TP盈利×P − 触SL亏损×P − 手续费(cost_r×risk)
  2. 净EV > 0 → 放行;净EV ≤ 0 → 拒单
  3. 数据缺失 → None(不可评估 → 拒单,宁可错过)
运行: PYTHONPATH=lib python3 tests/test_dynamic_tp_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.signal_scan import dynamic_tp_net_ev, passage_gate_ok

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
    # 多单: entry 100, stop 98(risk 2), 预测TP 106(盈利6)
    # p_tp=0.6, p_sl=0.3, cost_r=0.05 → 手续费=0.1
    # 净EV = 6×0.6 − 2×0.3 − 0.1 = 3.6−0.6−0.1 = 2.9 > 0 → 放行
    sel = {"tp": 106.0, "p_hit_tp": 0.6, "p_hit_sl": 0.3, "cost_r": 0.05}
    v = dynamic_tp_net_ev(sel, 100.0, 98.0)
    check("净EV = 2.9(>0 放行)", abs(v - 2.9) < 1e-9, f"v={v}")

    # 盈利概率低: p_tp=0.1, p_sl=0.8 → 6×0.1 − 2×0.8 − 0.1 = −1.1 < 0 → 拒
    sel2 = {"tp": 106.0, "p_hit_tp": 0.1, "p_hit_sl": 0.8, "cost_r": 0.05}
    v2 = dynamic_tp_net_ev(sel2, 100.0, 98.0)
    check("净EV = −1.1(≤0 拒单)", v2 is not None and v2 <= 0, f"v={v2}")

    # 恰好 0 → 拒单(用户口径"<0 拒绝",门槛取 0 时等于0也拒)
    sel3 = {"tp": 102.0, "p_hit_tp": 0.5, "p_hit_sl": 0.5, "cost_r": 0.0}
    v3 = dynamic_tp_net_ev(sel3, 100.0, 98.0)
    check("净EV = 0(门槛0 → 拒)", v3 == 0.0, f"v={v3}")

    # 数据缺失 → None
    check("缺字段 → None", dynamic_tp_net_ev({}, 100.0, 98.0) is None)

    # ---- 触达概率门(2026-08-25 用户指示 >60% 才下单) ----
    # 强上涨漂移序列 → P(触TP) 高 → 放行
    import math
    up = [100 * math.exp(0.002 * i) for i in range(200)]
    check("强上涨漂移 → 触达门放行",
          passage_gate_ok(entry=100.0, stop=99.5, tp=100.5,
                          direction="long", klines_closes=up) is True)
    # 零漂移 1:1 → P(触TP)≈50% < 60% → 拒单
    flat = [100.0] * 200
    check("零漂移 1:1 → 触达门拒单(≈50%<60%)",
          passage_gate_ok(entry=100.0, stop=99.5, tp=100.5,
                          direction="long", klines_closes=flat) is False)
    # 数据不足 → 拒单
    check("数据不足 → 触达门拒单",
          passage_gate_ok(entry=100.0, stop=99.5, tp=100.5,
                          direction="long", klines_closes=[100.0]*10) is False)

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
