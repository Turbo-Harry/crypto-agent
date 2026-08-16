"""
参数集中化规则测试 —— 用户规则【新增参数只能在 config.py 加】的机器执行。

跑全量套件时若任何策略层模块私藏参数常量 → 本测试红（params_lint 同源逻辑）。
运行: PYTHONPATH=lib python3 tests/test_params_centralization.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.params_lint import scan

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def main():
    print("== 参数集中化规则（新增参数只能在 config.py 加）==")
    problems = scan()
    check("策略层模块零私藏参数常量", not problems,
          f"发现 {len(problems)} 处: {problems[:5]}")
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
