"""
因子验证门离线单测（合成数据，不触网、隔离库）：
  1. 随机噪声因子 → reject（t < 2）
  2. 强单调因子（有经济逻辑、零成本）→ promote（t ≥ 3 且净价差 > 0）
  3. 同一因子加高成本 → reject_on_cost（净价差 < 0）
  4. 与已接受因子高相关 → redundant
  5. 无经济逻辑 → hypothesis_only（GP 产物永不自证）
  6. 试验日志：evaluate 两次 → factor_trials 恰 2 行（隔离库）
运行：PYTHONPATH=lib python3 tests/test_factor_gate.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from factors.factor_gate import evaluate

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def make_synthetic(n_days=400, seed=42):
    """合成价格(随机游走) + 未来收益。返回 (dates, price_by_date, fwd)。"""
    import random
    rnd = random.Random(seed)
    closes = [100.0]
    for _ in range(n_days - 1):
        closes.append(closes[-1] * (1 + rnd.uniform(-0.02, 0.02)))
    dates = []
    base = 1700000000
    import datetime
    for i in range(n_days):
        d = datetime.datetime.fromtimestamp(
            base + i * 86400).strftime("%Y-%m-%d")
        dates.append(d)
    price_by_date = {d: (i, closes[i]) for i, d in enumerate(dates)}
    price_by_date["__series__"] = closes
    fwd = []
    for i in range(n_days - 7):
        fwd.append((closes[i + 7] - closes[i]) / closes[i])
    return dates, price_by_date, fwd


def main():
    dates, px, fwd = make_synthetic()
    tmp = tempfile.mkdtemp(prefix="tst_factor_")
    db = os.path.join(tmp, "factor.db")
    n = len(fwd)

    # 1. 随机噪声因子
    import random
    rnd = random.Random(7)
    noise = [rnd.uniform(-1, 1) for _ in range(n)]
    v = evaluate("随机噪声", dates[:n], noise, "测试", px, horizon=7,
                 fee_bps=0, db_path=db)
    check("1 随机噪声因子 → reject",
          v["status"] == "reject" and abs(v["ic_tstat"]) < 2.0,
          f"status={v['status']} t={v['ic_tstat']:.2f}")

    # 2. 强单调因子（值≈未来收益+微噪，有逻辑，零成本）
    strong = [f + rnd.uniform(-0.002, 0.002) for f in fwd]
    v = evaluate("强单调", dates[:n], strong,
                 "价格动量延续（行为偏差:反应不足）", px, horizon=7,
                 fee_bps=0, db_path=db)
    check("2 强单调因子 → promote", v["status"] == "promote",
          f"status={v['status']} t={v['ic_tstat']:.2f} net={v['net_spread']*100:.2f}%")

    # 3. 同一因子 + 高成本（20% 往返费）
    v = evaluate("强单调高成本", dates[:n], strong,
                 "价格动量延续（行为偏差:反应不足）", px, horizon=7,
                 fee_bps=2000, db_path=db)
    check("3 高成本 → reject_on_cost",
          v["status"] == "reject_on_cost" and v["net_spread"] < 0,
          f"status={v['status']} net={v['net_spread']*100:.2f}%")

    # 4. 冗余因子（与已接受因子高相关）
    v = evaluate("冗余拷贝", dates[:n],
                 [f + rnd.uniform(-0.0005, 0.0005) for f in strong],
                 "测试冗余", px, horizon=7, fee_bps=0, db_path=db,
                 accepted=[("强单调", dates[:n], strong)])
    check("4 与已接受因子高相关 → redundant",
          v["status"] == "redundant", f"status={v['status']}")

    # 5. 无经济逻辑 → hypothesis_only
    v = evaluate("无逻辑因子", dates[:n], strong, "", px, horizon=7,
                 fee_bps=0, db_path=db)
    check("5 无经济逻辑 → hypothesis_only",
          v["status"] == "hypothesis_only", f"status={v['status']}")

    # 6. 试验日志落库（隔离库恰 5 行 = 上面 5 次检验）
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM factor_trials").fetchone()[0]
    conn.close()
    check("6 试验日志：5 次检验 → factor_trials 恰 5 行", rows == 5,
          f"实际 {rows}")

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
