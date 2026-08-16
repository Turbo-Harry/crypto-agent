"""
生产库污染哨兵（DEF-8 长效防线）——用"测试特征签名"检测生产表是否被测试/非生产进程写入。

背景：DEF-8 已两次以"冷不丁"的方式暴露——test_decision_loop 的阈值 85 行、
test_service_api 的 tick 行、thresholds 的临时路径 key。与其靠人工查表发现，
不如把探测器固化成测试：任何新测试漏隔离，都会在跑全量套件时被本哨兵当场抓住。

只读断言（故意连生产库，绝不写）：
  1. thresholds 不得出现临时路径 key（/var/folders、/tmp、.json 后缀的临时文件路径；
     生产方向侧唯一合法 key = threshold_state_dir.json）；
  2. scan_decisions 不得出现测试专用标的（BTC/ANTHROPIC——生产当日候选池由日志可证，
     测试只用这两个；若未来生产候选池含这些标的，需同步更新本清单）；
  3. lessons 不得出现测试符号（BTC/ANTHROPIC，生产教训符号=真实交易标的或 analyst 的 '*'）。

运行：PYTHONPATH=lib python3 tests/test_production_guard.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD = os.path.join(REPO, "crypto_agent.db")

# 测试专用签名（生产合法值见注释）
TEST_BASES = ("BTC", "ANTHROPIC")
TMP_KEY_MARKERS = ("/var/folders", "/tmp/", "tempfile")
LEGIT_KEYS = ("threshold_state_dir.json",)   # 方向侧生产 key（平仓复盘后出现）

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def run_checks(db_path):
    """对任意库执行污染签名检查，返回违反清单 [(检查名, 违例值列表)]。
    生产哨兵与变异注入自证（M6）共用此函数。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    violations = []
    try:
        rows = conn.execute("SELECT key FROM thresholds").fetchall()
        bad = [r[0] for r in rows if any(m in r[0] for m in TMP_KEY_MARKERS)
               or r[0] not in LEGIT_KEYS]
        if bad:
            violations.append(("thresholds 临时路径/非法 key", bad))
        rows = conn.execute("SELECT DISTINCT base FROM scan_decisions").fetchall()
        bad = [r[0] for r in rows if r[0] in TEST_BASES]
        if bad:
            violations.append(("scan_decisions 测试专用标的", bad))
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM lessons WHERE symbol IS NOT NULL").fetchall()
        bad = [r[0] for r in rows if r[0] in TEST_BASES]
        if bad:
            violations.append(("lessons 测试专用标的", bad))
        return violations
    finally:
        conn.close()


def main():
    if not os.path.exists(PROD):
        print("生产库不存在，跳过哨兵（无生产环境）")
        return
    violations = run_checks(PROD)
    check("thresholds 无临时路径/非法 key",
          not any(v[0].startswith("thresholds") for v in violations),
          f"发现 {[v for v in violations if v[0].startswith('thresholds')]}")
    check("scan_decisions 无测试专用标的",
          not any(v[0].startswith("scan") for v in violations),
          f"发现 {[v for v in violations if v[0].startswith('scan')]}")
    check("lessons 无测试专用标的",
          not any(v[0].startswith("lessons") for v in violations),
          f"发现 {[v for v in violations if v[0].startswith('lessons')]}")
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
