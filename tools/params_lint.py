#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略参数集中化审计 —— 2026-08-16 用户规则:【新增参数只能在 config.py 加】。

扫描策略层模块（engines/decision/execution/risk/service）的模块级赋值：
凡 `NAME = <数字/字符串字面量>` 且不引用 config 的 → 违规。
（factors/ 为研究层标准常量、tests/ 为测试夹具,不在扫描范围。）

允许的例外（结构/凭证/服务绑定类,非策略参数）:
  LARK / FEISHU_USER_ID / *_PATH / *_DB / *_FILE / *_SUFFIX / *_PREFIX /
  NAMES / PID_* / HEARTBEAT_* / DEFAULT_HOST / DEFAULT_PORT / OF_FIELDS
运行: cd crypto-agent && python3 tools/params_lint.py
退出码: 0=全部集中; 1=存在私藏参数（tests/test_params_centralization.py 同步断言）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = ("engines", "decision", "execution", "risk", "service")
ALLOW = re.compile(
    r"^(LARK|FEISHU_USER_ID|.*_PATH|.*_DB|.*_FILE|.*_SUFFIX|.*_PREFIX|"
    r"NAMES|PID_.*|HEARTBEAT_.*|DEFAULT_HOST|DEFAULT_PORT|OF_FIELDS|"
    r"COLLECT|UPLOAD)$")
ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$")
LITERAL = re.compile(r"[\"']|(?<![\w.])[-+]?\d")


def scan() -> list:
    """返回违规清单 [(file, line_no, name, rhs)]。"""
    problems = []
    for layer in LAYERS:
        d = os.path.join(ROOT, layer)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(d, fn)
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                m = ASSIGN.match(line.rstrip("\n"))
                if not m:
                    continue
                name, rhs = m.group(1), m.group(2).strip()
                if ALLOW.match(name):
                    continue
                if "config." in rhs:
                    continue
                if LITERAL.search(rhs):
                    problems.append((f"{layer}/{fn}", i, name, rhs[:60]))
    return problems


def main():
    problems = scan()
    if problems:
        for f, i, name, rhs in problems:
            print(f"❌ {f}:{i} {name} = {rhs} —— 策略参数必须定义在 config.py")
        print(f"\n发现 {len(problems)} 处私藏参数。修复: 移入 config.py 并在此处改为 "
              f"`{problems[0][2]} = config.{problems[0][2]}` 形式。")
        sys.exit(1)
    print("✅ 全部策略参数已集中于 config.py（无模块级私藏）")
    sys.exit(0)


if __name__ == "__main__":
    main()
