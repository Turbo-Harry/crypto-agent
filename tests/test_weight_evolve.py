"""
权重进化回归测试 v2（2026-08-23 用户指示"不加批准,自动生效",离线隔离临时库）:
  1. 证据达标 → auto_applied,活体权重立即生效(无需 approve)
  2. 观察期节流: 生效后新平仓不足 OBSERVE_MIN → observing,权重不再变
  3. 自动回滚: 增权维度观察期 IC 转负 → auto_rolled_back,回基线
  4. 归一化: 权重和精确=1;单维变动 ≤ MAX_SHIFT
  5. 人工 rollback/approve 接口仍可用
运行: PYTHONPATH=lib python3 tests/test_weight_evolve.py
"""
import json
import os
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import config
from decision.weight_evolve import (effective_weights, propose, approve,
                                    rollback, snapshot, DIMS)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def seed(db, n, ic_dim, ic_sign, start, exit_ts):
    """n 笔平仓: ic_dim 子分与 pnl 呈 ic_sign 方向相关。"""
    conn = sqlite3.connect(db)
    for i in range(n):
        x = random.random()
        pnl = 0.01 + ic_sign * x * 0.02
        dims = {d: (x if d == ic_dim else 0.5) for d in DIMS}
        conn.execute(
            "INSERT INTO trades (id, symbol, status, pnl, shadow_dims, "
            "notional_usdt, exit_time) VALUES (?,?,?,?,?,?,?)",
            [f"t{start+i}", "TEST", "closed", round(pnl, 6),
             json.dumps(dims), 100.0, exit_ts + i])
    conn.commit(); conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="w_evolve2_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    base_w = dict(config.SHADOW_WEIGHTS)
    t0 = 1000.0

    # ---- 1. 样本不足 ----
    st, msg, ev = propose(db_path=db)
    check("样本不足 → insufficient", st == "insufficient", f"{st}")

    # ---- 2. 证据达标 → 自动生效,无需批准 ----
    random.seed(42)
    seed(db, 40, "book", +1, 0, t0)
    st, msg, ev = propose(db_path=db)
    check("证据达标 → auto_applied(自动生效)", st == "auto_applied",
          f"{st}: {msg}")
    w1 = effective_weights(db)
    check("book 自动增权且 ≤ MAX_SHIFT",
          w1["book"] > base_w["book"] and
          w1["book"] - base_w["book"] <= config.WEIGHT_EVOLVE_MAX_SHIFT + 1e-9,
          f"book {base_w['book']}→{w1['book']}")
    check("权重和精确=1", abs(sum(w1.values()) - 1.0) < 1e-9)

    # ---- 3. 观察期节流: 立即再跑 → observing ----
    st2, msg2, _ = propose(db_path=db)
    check("生效后立即再跑 → observing(观察期未满)", st2 == "observing",
          f"{st2}: {msg2}")
    check("观察期内权重未被重复改动", effective_weights(db) == w1)

    # ---- 4. 自动回滚: 观察期增权维度 IC 转负 ----
    random.seed(7)
    import time as _t
    seed(db, 15, "book", -1, 100, _t.time() + 10)
    st3, msg3, _ = propose(db_path=db)
    check("增权维度观察期 IC 转负 → auto_rolled_back", st3 == "auto_rolled_back",
          f"{st3}: {msg3}")
    check("回滚后权重=基线", effective_weights(db) == base_w)

    # ---- 5. 人工接口仍可用 ----
    ok, _ = rollback(db_path=db)
    check("人工 rollback 可用", ok)
    check("快照含 active/baseline/pending",
          set(snapshot(db)) >= {"active", "baseline", "pending"})

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
