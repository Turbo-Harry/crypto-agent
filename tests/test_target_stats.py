"""
目标价位带 + 历史命中率回归测试（2026-08-23 用户问"会预测会升到什么价位吗"）:
  1. compute_targets: T1=1×ATR, T2=2×ATR, T3=结构位(超出T2才列入),空头镜像
  2. hit_rates: 由 trade_features.mfe_r 算 P(+1R)/P(+2R)/中位MFE;小样本诚实返回 None
  3. describe: 样本不足时不给概率
运行: PYTHONPATH=lib python3 tests/test_target_stats.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

from engines.signal_scan import compute_targets
from decision.target_stats import hit_rates, describe

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
    # ---- compute_targets ----
    t = compute_targets(entry=100.0, atr=2.0, direction="long")
    check("多 T1=+1ATR, T2=+2ATR", t["t1"] == 102.0 and t["t2"] == 104.0, str(t))
    check("无结构位 → T3=None", t["t3"] is None)
    t2 = compute_targets(entry=100.0, atr=2.0, direction="long", swing_level=110.0)
    check("结构位 > T2 才列入 T3", t2["t3"] == 110.0)
    t3 = compute_targets(entry=100.0, atr=2.0, direction="long", swing_level=103.0)
    check("结构位 ≤ T2 不列入(不放大目标)", t3["t3"] is None)
    t4 = compute_targets(entry=100.0, atr=2.0, direction="short", swing_level=90.0)
    check("空头镜像: T1=98 T2=96 T3=90(结构位< T2)",
          t4["t1"] == 98.0 and t4["t2"] == 96.0 and t4["t3"] == 90.0, str(t4))

    # ---- hit_rates ----
    tmp = tempfile.mkdtemp(prefix="target_stats_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    h0 = hit_rates(db)
    check("无样本 → 概率 None", h0["p1r"] is None, str(h0))

    conn = sqlite3.connect(db)
    mfes = [0.5, 0.8, 1.2, 1.5, 2.1, 2.5, 3.0, 0.3, 1.1, 1.8]   # ≥1R: 7/10, ≥2R: 4/10
    for i, v in enumerate(mfes):
        conn.execute("INSERT INTO trade_features (trade_id, mfe_r, direction) "
                     "VALUES (?,?,?)", [f"t{i}", v, "long"])
    conn.commit(); conn.close()
    h = hit_rates(db, direction="long")
    check("P(+1R)=0.7", h["p1r"] == 0.7, f"p1r={h['p1r']}")
    check("P(+2R)=0.3", h["p2r"] == 0.3, f"p2r={h['p2r']}")
    check("中位 MFE=1.35R(10笔)", h["median_mfe_r"] == 1.35, f"med={h['median_mfe_r']}")
    desc = describe(db, direction="long")
    check("describe 一句话含概率", "70%" in desc and "30%" in desc, desc)

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
