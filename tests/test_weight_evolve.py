"""
权重进化回归测试（2026-08-23 用户问"会根据历史经验调整权重吗",离线隔离临时库）:
  1. 有效权重: 无 kv 批准 → config 基线;approve 后 → kv 覆盖;rollback → 回基线
  2. 提案门: 样本不足 → insufficient;|IC| 不达标 → no_edge;达标 → accepted 待批准
  3. 永不自动: propose 不改活体权重(approve 是唯一写入口)
  4. 归一化: 提案权重和=1;单维变动 ≤ MAX_SHIFT
  5. pending 去重: 已有待处理提案时不重复落(force 除外)
运行: PYTHONPATH=lib python3 tests/test_weight_evolve.py
"""
import json
import os
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


def seed_trades(db, n=40, ic_dim="book", ic_strength=0.5, base_pnl=0.01):
    """造 n 笔已平仓交易: ic_dim 的子分与 pnl 强正相关,其余维度随机中性。"""
    import random
    import sqlite3
    random.seed(42)
    conn = sqlite3.connect(db)
    for i in range(n):
        x = random.random()               # 0-1
        pnl = base_pnl + ic_strength * x * 0.02
        dims = {d: (x if d == ic_dim else 0.5) for d in DIMS}
        conn.execute(
            "INSERT INTO trades (id, symbol, status, pnl, shadow_dims, "
            "notional_usdt) VALUES (?,?,?,?,?,?)",
            [f"t{i}", "TEST", "closed", round(pnl, 6),
             json.dumps(dims), 100.0])
    conn.commit(); conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="w_evolve_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)

    # ---- 1. 有效权重默认=基线 ----
    w0 = effective_weights(db)
    check("默认活体权重=config 基线", w0 == dict(config.SHADOW_WEIGHTS),
          str(w0))

    # ---- 2. 样本不足 ----
    st, msg, ev = propose(db_path=db)
    check("样本不足 → insufficient", st == "insufficient", f"{st}: {msg}")

    # ---- 3. 强 IC 维度 → 提案 accepted,但不自动生效 ----
    seed_trades(db, n=40, ic_dim="book", ic_strength=0.8)
    st, msg, ev = propose(db_path=db, force=True)
    check("证据达标 → accepted(待批准)", st == "accepted", f"{st}: {msg}")
    check("book 维 IC 显著", ev["book"]["ic"] is not None
          and ev["book"]["ic"] > config.WEIGHT_EVOLVE_MIN_IC,
          f"ic={ev['book']['ic']}")
    check("propose 后活体权重未变(永不自动)",
          effective_weights(db) == dict(config.SHADOW_WEIGHTS))

    # ---- 3.5 pending 去重(不 force 时不覆盖待处理提案) ----
    st_dup, _, _ = propose(db_path=db)
    check("已有待处理提案 → pending 不重复落", st_dup == "pending", f"{st_dup}")

    # ---- 4. 批准 → kv 覆盖 ----
    ok, msg = approve(db_path=db)
    check("批准成功", ok, msg)
    w1 = effective_weights(db)
    base = dict(config.SHADOW_WEIGHTS)
    check("book 增权且不超 MAX_SHIFT",
          w1["book"] > base["book"] and
          w1["book"] - base["book"] <= config.WEIGHT_EVOLVE_MAX_SHIFT + 1e-9,
          f"book {base['book']}→{w1['book']}")
    check("归一化和=1", abs(sum(w1.values()) - 1.0) < 1e-9,
          f"sum={sum(w1.values())}")
    check("快照 active=批准权重", snapshot(db)["active"] == w1)

    # ---- 5. 回滚 → 基线 ----
    ok, msg = rollback(db_path=db)
    check("回滚成功", ok, msg)
    check("回滚后活体=基线", effective_weights(db) == dict(config.SHADOW_WEIGHTS))

    # ---- 6. 回滚后旧提案失效,可重新生成 ----
    st3, msg3, _ = propose(db_path=db)
    check("回滚后(无待处理)可重新提案", st3 == "accepted", f"{st3}")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
