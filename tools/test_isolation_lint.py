#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试隔离静态审计（收敛保证机制 M5）——把"测试必须全对象隔离"从约定变成机器检查。

DEF-8 曾两次以"冷不丁"方式暴露（test_decision_loop 阈值85 行 / test_service_api
漏传 db_path），根因是"构造带状态对象必须隔离"只靠人记。本脚本扫描 tests/*.py：
  1. DirectionalTrader(...) / ServiceTrader(...) 调用必须显式含 db_path=；
  2. TradeJournal(...) / ScoredExperience(...) / PositionLedger(...) /
     ThresholdLearner(...) 调用必须显式含 path= 或 db_path=（隔离存储）。

运行：cd crypto-agent && python3 tools/test_isolation_lint.py
退出码：0=全部隔离完整；1=存在未隔离调用（列出文件与行号）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

RULES = [
    # (构造器名, 必含的关键字参数)
    ("DirectionalTrader", ("db_path",)),
    ("ServiceTrader", ("db_path",)),
    ("TradeJournal", ("path", "db_path")),
    ("ScoredExperience", ("path", "db_path")),
    ("PositionLedger", ("path", "db_path")),
    ("ThresholdLearner", ("path", "db_path")),
]


def find_calls(src, name):
    """极简调用点扫描：找到 Name( 后配平括号取参数段。"""
    out = []
    for m in re.finditer(rf"\b{name}\s*\(", src):
        i = m.end()
        depth, start = 1, i
        while i < len(src) and depth > 0:
            if src[i] in "([{":
                depth += 1
            elif src[i] in ")]}":
                depth -= 1
            i += 1
        out.append((m.start(), src[start:i - 1]))
    return out


problems = 0
for fn in sorted(os.listdir(TESTS)):
    if not fn.endswith(".py") or fn.startswith("__"):
        continue
    path = os.path.join(TESTS, fn)
    src = open(path, encoding="utf-8").read()
    for name, required in RULES:
        for pos, args in find_calls(src, name):
            # 允许注释掉的样例（__main__ 自测段常用字符串展示）
            if any(kw + "=" in args for kw in required):
                continue
            line = src.count("\n", 0, pos) + 1
            problems += 1
            print(f"❌ {fn}:{line} {name}(...) 未显式隔离"
                  f"（缺少 {required}）")

if problems:
    print(f"\n发现 {problems} 处未隔离调用 —— 任何新测试漏隔离都会在此被拦截")
    sys.exit(1)
print("✅ 全部测试构造调用均已显式隔离")
sys.exit(0)
