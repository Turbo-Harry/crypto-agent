"""
教训晋升/弃用统计显著性回归测试（2026-08-23 用户要求"补上理论依据"）:
  1. Wilson 下界: 3/3 全胜 → 显著,可 trusted;2胜2负 → 不显著,不晋升
  2. 全负 0/5 → 上界<0.5 → discarded
  3. 混合 2胜3负 → 分数<40 但上界不显著 → unverified(不误杀)
  4. trusted 之后退化(再来一负) → 显著性翻转 → 回 unverified
运行: PYTHONPATH=lib python3 tests/test_lesson_significance.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.experience_scoring import ScoredExperience, _wilson

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
    # Wilson 区间数学属性
    lb33 = _wilson(1.0, 3)
    check("3/3 全胜 Wilson 下界 > 0.5", lb33 > 0.5, f"lb={lb33:.3f}")
    lb22 = _wilson(0.5, 4)
    check("2胜2负 Wilson 下界 < 0.5", lb22 < 0.5, f"lb={lb22:.3f}")
    ub05 = _wilson(0.0, 5, upper=True)
    check("0/5 全负 Wilson 上界 < 0.5", ub05 < 0.5, f"ub={ub05:.3f}")

    tmp = tempfile.mkdtemp(prefix="lesson_sig_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)

    # ---- 3/3 全胜 → trusted ----
    bank = ScoredExperience(db)
    lid = bank.add("BTC", "入场", "回踩 EMA 后入场", "txn_a", status="candidate")
    for _ in range(3):
        bank.validate(lid, 0.01)
    got = [l for l in bank.lessons if l["id"] == lid][0]
    check("3/3 全胜 → trusted(显著)", got["status"] == "trusted",
          f"status={got['status']} score={got['score']}")

    # ---- 2胜2负 → 分数50,不晋升 ----
    bank2 = ScoredExperience(db)
    lid2 = bank2.add("ETH", "入场", "另一条", "txn_b", status="candidate")
    for p in (0.01, 0.01, -0.01, -0.01):
        bank2.validate(lid2, p)
    got2 = [l for l in bank2.lessons if l["id"] == lid2][0]
    check("2胜2负 → 不晋升(分数50 且 Wilson 不显著)",
          got2["status"] != "trusted", f"status={got2['status']} score={got2['score']}")

    # ---- 2胜3负 → 分数30,但胜率上界不显著 → unverified 不误杀 ----
    bank3 = ScoredExperience(db)
    lid3 = bank3.add("SOL", "入场", "第三条", "txn_c", status="candidate")
    for p in (0.01, 0.01, -0.01, -0.01, -0.01):
        bank3.validate(lid3, p)
    got3 = [l for l in bank3.lessons if l["id"] == lid3][0]
    check("2胜3负(分数30) → 不弃用(上界未显著 <0.5 不够)",
          got3["status"] != "discarded", f"status={got3['status']} score={got3['score']}")

    # ---- 5/5 全负 → discarded ----
    bank4 = ScoredExperience(db)
    lid4 = bank4.add("DOGE", "入场", "第四条", "txn_d", status="candidate")
    for _ in range(5):
        bank4.validate(lid4, -0.01)
    got4 = [l for l in bank4.lessons if l["id"] == lid4][0]
    check("0/5 全负 → discarded(显著)", got4["status"] == "discarded",
          f"status={got4['status']} score={got4['score']}")

    # ---- trusted 退化: 3胜后再输2次 → 显著性翻转回 unverified ----
    bank5 = ScoredExperience(db)
    lid5 = bank5.add("XRP", "入场", "第五条", "txn_e", status="candidate")
    for _ in range(3):
        bank5.validate(lid5, 0.01)
    got5 = [l for l in bank5.lessons if l["id"] == lid5][0]
    check("先 trusted", got5["status"] == "trusted", got5["status"])
    for _ in range(2):
        bank5.validate(lid5, -0.01)
    got5b = [l for l in bank5.lessons if l["id"] == lid5][0]
    check("再输2次(3胜2负,分数50) → 退出 trusted",
          got5b["status"] != "trusted", f"status={got5b['status']}")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
