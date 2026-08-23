"""
防过拟合守卫单测（离线合成数据）:
  1. deflated_sharpe: 纯噪声 → DSR 低(<0.5); 强信号 → DSR≈1; 试验次数爆炸 → DSR 骤降
  2. pbo_cscv: 全噪声配置矩阵 → PBO ≥ 0.4; 存在统治性配置 → PBO < 0.3
  3. experiments.judge: 样本不足→insufficient_data; 噪声→rejected; 强信号→accepted
运行: PYTHONPATH=lib python3 tests/test_overfit_guard.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import numpy as np
from factors.overfit_guard import deflated_sharpe, pbo_cscv

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def test_deflated_sharpe():
    print("== Deflated Sharpe ==")
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, 300)
    check("纯噪声 DSR < 0.5", deflated_sharpe(noise, 1) < 0.5,
          f"实际 {deflated_sharpe(noise, 1)}")
    signal = rng.normal(0.005, 0.005, 300)   # SR≈1
    d1 = deflated_sharpe(signal, 1)
    check("强信号(SR≈1) DSR ≥ 0.9", d1 is not None and d1 >= 0.9,
          f"实际 {d1}")
    weak = rng.normal(0.0025, 0.01, 60)      # SR≈0.25、n=60：可被试验次数压低
    d1w = deflated_sharpe(weak, 1)
    d2w = deflated_sharpe(weak, 100000)
    check("试验次数爆炸 → DSR 骤降（多重检验校正生效）",
          d1w is not None and d2w is not None and d2w < d1w - 0.5,
          f"{d1w} → {d2w}")


def test_pbo():
    print("== CSCV-PBO ==")
    rng = np.random.default_rng(2)
    noise_mat = rng.normal(0, 0.01, (500, 10))
    p1 = pbo_cscv(noise_mat)
    check("全噪声配置 → PBO ≥ 0.4（样本内最优不可信）",
          p1 is not None and p1 >= 0.4, f"实际 {p1}")
    dom = noise_mat.copy()
    dom[:, 0] += 0.01   # 配置 0 统治性占优
    p2 = pbo_cscv(dom)
    check("统治性配置 → PBO < 0.3（真实优势）",
          p2 is not None and p2 < 0.3, f"实际 {p2}")


def test_experiments_judge():
    print("== 试验注册表裁决 ==")
    from decision.experiments import propose, judge
    tmp = tempfile.mkdtemp(prefix="tst_exp_")
    db = os.path.join(tmp, "exp.db")
    rng = np.random.default_rng(3)
    propose("chg_1", "threshold", '{"th": 70}', db_path=db)
    st, ev = judge("chg_1", list(rng.normal(0, 0.01, 10)), db_path=db)
    check("样本<30 → insufficient_data", st == "insufficient_data",
          f"实际 {st}")
    propose("chg_2", "threshold", '{"th": 70}', db_path=db)
    st, ev = judge("chg_2", list(rng.normal(0, 0.01, 40)), db_path=db)
    check("30+ 噪声样本 → rejected（DSR<0.95）", st == "rejected",
          f"实际 {st} dsr={ev['dsr']}")
    propose("chg_3", "threshold", '{"th": 70}', db_path=db)
    st, ev = judge("chg_3", list(rng.normal(0.01, 0.005, 40)), db_path=db)
    check("30+ 强信号样本 → accepted", st == "accepted",
          f"实际 {st} dsr={ev['dsr']}")


if __name__ == "__main__":
    test_deflated_sharpe()
    test_pbo()
    test_experiments_judge()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
