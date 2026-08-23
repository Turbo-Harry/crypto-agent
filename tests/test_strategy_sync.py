"""
策略状态共享回归测试（2026-08-23 用户指示"策略也保持一致"——阈值学习+扫描尺子
双向合并,离线隔离临时库）:
  1. 样本并集: 两库各自的决策样本合并后不重复(按 score/pnl 去重)
  2. 阈值裁决: updated_at 新者胜(模拟进化门晋升发生在某侧,另一侧跟随)
  3. 幂等: 连跑两次样本不翻倍
  4. 扫描尺子 kv 镜像: 新者胜,旧不覆盖新
运行: PYTHONPATH=lib python3 tests/test_strategy_sync.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.strategy_sync import sync_strategy

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def q1(db, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="strat_sync_")
    db_a = os.path.join(tmp, "a.db")
    db_b = os.path.join(tmp, "b.db")
    import json
    import sqlite3
    for p in (db_a, db_b):
        import storage.db as sdb
        sdb.init_db(p)

    # ---- 两侧各自记录样本(thresholds key='dir') ----
    for db, recs, thr, upd in [
        (db_a, [{"score": 60, "pnl": 0.01}, {"score": 70, "pnl": -0.02}], 45.0, 1000.0),
        (db_b, [{"score": 60, "pnl": 0.01}, {"score": 80, "pnl": 0.03}], 50.0, 2000.0),
    ]:
        conn = sqlite3.connect(db)
        conn.execute("INSERT OR REPLACE INTO thresholds (key, threshold, records, "
                     "updated_at) VALUES ('dir', ?, ?, ?)",
                     [thr, json.dumps(recs), upd])
        conn.commit(); conn.close()

    # ---- 1. B ← A 同步: 样本并集(score60/pnl0.01 重复只留一份) ----
    res = sync_strategy(db_b, db_a)
    row = q1(db_b, "SELECT threshold, records, updated_at FROM thresholds WHERE key='dir'")
    recs = json.loads(row["records"])
    check("样本并集(重复只留一份)", len(recs) == 3, f"n={len(recs)}")
    check("B 新增 1 条样本", res["records_added"] == 1, str(res))
    # threshold: B 的 2000 > A 的 1000 → 保持 B 的 50
    check("阈值裁决=updated_at 新者(B 保持 50)", row["threshold"] == 50.0,
          f"thr={row['threshold']}")
    check("阈值未翻转", res["threshold_updated"] is False)

    # ---- 2. A ← B 同步(双向): A 拿到 50 阈值 + 80 样本 ----
    res2 = sync_strategy(db_a, db_b)
    row_a = q1(db_a, "SELECT threshold, records FROM thresholds WHERE key='dir'")
    check("双向: A 阈值跟随 B(50)", row_a["threshold"] == 50.0,
          f"thr={row_a['threshold']}")
    check("双向: A 样本并集=3", len(json.loads(row_a["records"])) == 3)

    # ---- 3. 幂等: 再同步不翻倍 ----
    res3 = sync_strategy(db_a, db_b)
    row_a2 = q1(db_a, "SELECT records FROM thresholds WHERE key='dir'")
    check("幂等: 样本不翻倍", len(json.loads(row_a2["records"])) == 3)

    # ---- 4. 扫描尺子 kv: 新者胜 ----
    conn = sqlite3.connect(db_a)
    conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) "
                 "VALUES ('scan_evolve.REJECT_WICK_RATIO', '0.90', 3000)")
    conn.commit(); conn.close()
    conn = sqlite3.connect(db_b)
    conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) "
                 "VALUES ('scan_evolve.REJECT_WICK_RATIO', '0.85', 4000)")
    conn.commit(); conn.close()
    res4 = sync_strategy(db_b, db_a)   # A(3000) → B(4000 更新): 不覆盖
    v = q1(db_b, "SELECT value FROM kv WHERE key='scan_evolve.REJECT_WICK_RATIO'")
    check("尺子kv 旧不覆盖新", v["value"] == "0.85", f"v={v['value']}")
    res5 = sync_strategy(db_a, db_b)   # B(4000) → A: 更新为 0.85
    v2 = q1(db_a, "SELECT value FROM kv WHERE key='scan_evolve.REJECT_WICK_RATIO'")
    check("尺子kv 新者镜像", v2["value"] == "0.85", f"v={v2['value']}")
    check("kv 同步计数", res5["kv_synced"] == 1, str(res5))

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
