"""
M6 变异注入自证 —— 探测器自身有效性的验证（"检测器会抓"不是靠信,是靠测）。

方法（变异测试思想, [PIT](https://pitest.org/) / [Visdom mutation layer](
https://virtuslab.github.io/visdom-testing/reference/layers/mutation-testing/)）:
向隔离库故意注入污染签名（thresholds 临时 key / scan_decisions 测试标的 /
lessons 测试标的），断言 run_checks 必须全部抓出。若抓不出 → 哨兵是摆设。
运行: PYTHONPATH=lib python3 tests/test_mutation_selfcheck.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_production_guard import run_checks

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _make_injected_db():
    tmp = tempfile.mkdtemp(prefix="tst_mut_")
    db = os.path.join(tmp, "mut.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE thresholds (key TEXT PRIMARY KEY, threshold REAL, records TEXT,
                             updated_at REAL);
    CREATE TABLE scan_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
        base TEXT, venue TEXT, has_signal INTEGER, direction TEXT, threshold REAL,
        decision TEXT, reason TEXT);
    CREATE TABLE lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
        category TEXT, content TEXT, score REAL, adoptions INTEGER, good INTEGER,
        bad INTEGER, status TEXT, source_trade TEXT, regime TEXT, ts REAL,
        last_update REAL);
    """)
    # 注入三类污染签名
    conn.execute("INSERT INTO thresholds VALUES ('/var/folders/x/threshold_state.json', 70, '[]', 0)")
    conn.execute("INSERT INTO thresholds VALUES ('threshold_state_dir.json', 70, '[]', 0)")
    conn.execute("INSERT INTO scan_decisions (ts, base, decision) VALUES (0, 'BTC', 'open')")
    conn.execute("INSERT INTO scan_decisions (ts, base, decision) VALUES (0, 'AEON', 'no_signal')")
    conn.execute("INSERT INTO lessons (symbol, category, content, status) "
                 "VALUES ('ANTHROPIC', '止损', 'x', 'unverified')")
    conn.execute("INSERT INTO lessons (symbol, category, content, status) "
                 "VALUES ('*', '风控', 'y', 'unverified')")
    conn.commit()
    conn.close()
    return db


def main():
    print("== 变异注入自证（探测器必须抓到全部注入）==")
    db = _make_injected_db()
    violations = run_checks(db)
    names = [v[0] for v in violations]
    check("抓到 thresholds 临时 key", any("thresholds" in n for n in names),
          f"实际 {names}")
    check("抓到 scan_decisions 测试标的 BTC",
          any("scan" in n and "BTC" in str(v) for n, v in violations),
          f"实际 {violations}")
    check("抓到 lessons 测试标的 ANTHROPIC",
          any("lessons" in n and "ANTHROPIC" in str(v) for n, v in violations),
          f"实际 {violations}")
    # 合法行不得误报
    flat = " ".join(str(v) for _, v in violations)
    check("合法行（AEON/'*'/合法 key）不误报",
          "AEON" not in flat and "'*'" not in flat, f"实际 {flat}")
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
