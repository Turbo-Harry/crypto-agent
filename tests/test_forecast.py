"""
预测机制回归测试（2026-08-23 用户要求"最好能有预测机制",纯函数离线）:
  1. bootstrap 确定性(seed): 同输入同输出
  2. 分布合理性: 分位单调 q05 ≤ 中位 ≤ q95
  3. 触达概率: 高波动收益序列下 P(触TP)+P(触SL) 显著 > 零波动序列
  4. 实证混合: blend=1 时完全采用历史概率
  5. 数据不足 → None(不硬编数字)
  6. 校准: Brier 分数与分桶命中率计算正确
运行: PYTHONPATH=lib python3 tests/test_forecast.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

from decision.forecast import (forecast, _returns, _quantile, record_outcome,
                               calibration)

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
    # ---- _returns / _quantile ----
    r = _returns([100, 110, 99, 108.9])
    check("对数收益计算", len(r) == 3 and abs(r[0] - 0.09531) < 0.01, str(r))
    check("分位: 0.5 中位", _quantile([1, 3, 2], 0.5) == 2)

    # ---- bootstrap 确定性 + 分布合理性 ----
    rets = [0.001, -0.002, 0.003, -0.001, 0.002] * 40   # 200 根
    f1 = forecast(entry=100.0, atr=1.0, direction="long",
                  stop=99.0, tp=102.0, hourly_returns=rets,
                  paths=300, seed=42)
    f2 = forecast(entry=100.0, atr=1.0, direction="long",
                  stop=99.0, tp=102.0, hourly_returns=rets,
                  paths=300, seed=42)
    check("seed 确定性: 两次结果一致", f1 == f2)
    check("分位单调: q05 ≤ 中位 ≤ q95",
          f1["q05"] <= f1["median"] <= f1["q95"], str(f1))

    # ---- 高波动 vs 零波动: 触达概率应显著更高 ----
    wild = [0.03, -0.03, 0.02, -0.02] * 40
    fw = forecast(entry=100.0, atr=1.0, direction="long",
                  stop=99.0, tp=102.0, hourly_returns=wild, paths=300, seed=1)
    flat = [0.0] * 160
    ff = forecast(entry=100.0, atr=1.0, direction="long",
                  stop=99.0, tp=102.0, hourly_returns=flat, paths=300, seed=1)
    check("高波动序列触达概率更高",
          (fw["p_hit_tp"] + fw["p_hit_sl"]) > (ff["p_hit_tp"] + ff["p_hit_sl"]),
          f"wild={fw['p_hit_tp']}/{fw['p_hit_sl']} flat={ff['p_hit_tp']}/{ff['p_hit_sl']}")

    # ---- 实证混合: blend=1 完全用历史概率 ----
    fm = forecast(entry=100.0, atr=1.0, direction="long",
                  stop=99.0, tp=102.0, hourly_returns=rets,
                  paths=300, seed=1, emp_p_tp=0.4, emp_p_sl=0.3, blend=1.0)
    check("blend=1 → 完全采用历史概率",
          fm["p_hit_tp"] == 0.4 and fm["p_hit_sl"] == 0.3, str(fm))

    # ---- 数据不足 → None ----
    check("无收益序列 → None", forecast(entry=100, atr=1, direction="long",
                                         stop=99, tp=102,
                                         hourly_returns=[]) is None)
    check("atr=0 → None", forecast(entry=100, atr=0, direction="long",
                                    stop=99, tp=102,
                                    hourly_returns=rets) is None)

    # ---- 校准落表 + Brier ----
    tmp = tempfile.mkdtemp(prefix="fc_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    for i in range(10):
        record_outcome(f"t{i}", json.dumps({"p_hit_tp": 0.5, "p_hit_sl": 0.4}),
                       {"pnl": 0.03 if i % 2 == 0 else -0.02}, db_path=db)
    cal = calibration(db, min_n=5)
    check("校准样本数=10", cal["n"] == 10, str(cal))
    check("Brier tp = 0.25(全预测0.5,命中一半)",
          abs(cal["brier_tp"] - 0.25) < 0.001, f"b={cal['brier_tp']}")
    # 5 笔 pnl=-0.02(hit_sl=1) + 5 笔 pnl=0.03(hit_sl=0):
    # Brier_sl = (5×(0.4-1)^2 + 5×(0.4-0)^2)/10 = 0.26
    check("Brier sl = 0.26(5 命中/5 未命中)",
          abs(cal["brier_sl"] - 0.26) < 0.001, f"b={cal['brier_sl']}")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
