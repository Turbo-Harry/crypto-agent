"""R1-3 离线单测（2026-08-16 修订）：ThresholdLearner SQLite 落库 + 原子写（不触网）。

原验证为 JSON 文件原子写（.tmp + os.replace）；JSON→SQLite 迁移后状态落库由
SQLite 事务保证原子性。本测试同步更新为：
  1. ThresholdLearner.record() 后状态写入隔离库（thresholds 表），内容合法；
  2. 无 .tmp 残留；
  3. 隔离库互不影响（两个 learner 各自 db 独立）。
（WeightLearner 已随套利引擎归档 legacy/，其原子写测试一并归档。）
"""
import json
import os
import sqlite3
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.threshold_learning import ThresholdLearner

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _row(db, key):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "SELECT threshold, records FROM thresholds WHERE key=?", [key])
        r = cur.fetchone()
        return r
    finally:
        conn.close()


def test_threshold_sqlite_write():
    d = tempfile.mkdtemp()
    try:
        db = os.path.join(d, "threshold.db")
        tl = ThresholdLearner(path="dir", db_path=db, min_samples=999)
        tl.record(70, 0.01)   # < min_samples → 直接 _save()
        r = _row(db, "dir")
        check("record 后状态写入隔离库", r is not None, f"实际 {r}")
        check("阈值持久化正确", r is not None and r[0] == 70)
        records = json.loads(r[1]) if r else []
        check("样本数正确", len(records) == 1 and records[0]["score"] == 70,
              f"实际 {records}")
        tmp_files = [f for f in os.listdir(d) if f.endswith(".tmp")]
        check("无 .tmp 残留（SQLite 事务原子写）", len(tmp_files) == 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_learner_db_isolation():
    d = tempfile.mkdtemp()
    try:
        db_a, db_b = os.path.join(d, "a.db"), os.path.join(d, "b.db")
        la = ThresholdLearner(path="dir", db_path=db_a, min_samples=999)
        lb = ThresholdLearner(path="dir", db_path=db_b, min_samples=999)
        la.record(70, 0.01)
        check("A 库有记录", _row(db_a, "dir") is not None)
        check("B 库不受 A 影响（隔离）", _row(db_b, "dir") is None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_threshold_sqlite_write()
    test_learner_db_isolation()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
