#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试隔离静态审计（收敛保证机制 M5）——把"测试必须全对象隔离"从约定变成机器检查。

DEF-8 曾两次以"冷不丁"方式暴露（test_decision_loop 阈值85 行 / test_service_api
漏传 db_path），根因是"构造带状态对象必须隔离"只靠人记。本脚本扫描 tests/*.py：
  1. DirectionalTrader(...) / ServiceTrader(...) 调用必须显式含 db_path=；
  2. TradeJournal(...) / ScoredExperience(...) / PositionLedger(...) /
     ThresholdLearner(...) 调用必须显式含 path= 或 db_path=（隔离存储）。
  3. CI 全量脚本必须同时隔离 DB、事件 JSONL 与 PID/心跳/tick 运行目录。

运行：cd crypto-agent && python3 tools/test_isolation_lint.py
退出码：0=全部隔离完整；1=存在未隔离调用（列出文件与行号）。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

RULES = [
    # (构造器名, 隔离关键字, db/path 的位置参数索引)
    ("DirectionalTrader", ("db_path",), 2),
    ("ServiceTrader", ("db_path",), 2),
    ("TradeJournal", ("path", "db_path"), 0),
    ("ScoredExperience", ("path", "db_path"), 0),
    ("PositionLedger", ("path", "db_path"), 0),
    ("ThresholdLearner", ("path", "db_path"), 0),
]


def find_calls(src, name):
    """用 AST 找构造调用，正确识别关键字和位置参数。"""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = fn.id if isinstance(fn, ast.Name) else \
            fn.attr if isinstance(fn, ast.Attribute) else None
        if called == name:
            out.append(node)
    return out


problems = 0
for fn in sorted(os.listdir(TESTS)):
    if not fn.endswith(".py") or fn.startswith("__"):
        continue
    path = os.path.join(TESTS, fn)
    src = open(path, encoding="utf-8").read()
    for name, required, positional_index in RULES:
        for call in find_calls(src, name):
            if any(kw.arg in required for kw in call.keywords if kw.arg):
                continue
            if positional_index is not None and len(call.args) > positional_index:
                continue
            line = call.lineno
            problems += 1
            print(f"❌ {fn}:{line} {name}(...) 未显式隔离"
                  f"（缺少 {required} 或位置参数 {positional_index + 1}）")

if problems:
    print(f"\n发现 {problems} 处未隔离调用 —— 任何新测试漏隔离都会在此被拦截")
    sys.exit(1)
workflow = os.path.join(ROOT, ".github", "workflows", "ci.yml")
try:
    ci_source = open(workflow, encoding="utf-8").read()
except OSError:
    ci_source = ""
for env_name in ("CRYPTO_AGENT_DB", "CRYPTO_AGENT_EVENTS_FILE",
                 "CRYPTO_AGENT_RUNTIME_DIR"):
    if env_name not in ci_source:
        problems += 1
        print(f"❌ CI 全量测试缺少 {env_name} 隔离")
if problems:
    print(f"\n发现 {problems} 处未隔离调用/通道")
    sys.exit(1)
print("✅ 全部测试构造调用均已显式隔离")
sys.exit(0)
