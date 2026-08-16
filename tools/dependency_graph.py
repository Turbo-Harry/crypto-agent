"""
依赖图生成器 — AST 静态分析全仓 import 关系，输出层间依赖矩阵 + mermaid 图。

用途（AI 友好）：
  1. 沉淀"代码关系图"到 docs/architecture/dependency_graph.md（自动生成段落）
  2. 改代码后重跑本工具，检查是否违反分层（反向 import）

用法：
  python3 tools/dependency_graph.py --check    # 只做分层违规检查
  python3 tools/dependency_graph.py --dump     # 输出依赖矩阵（人读）
  python3 tools/dependency_graph.py --mermaid  # 输出 mermaid 层图
"""
import ast
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 目录 → 层名
LAYERS = {
    "service": "service 服务端外壳",
    "engines": "engines 交易引擎",
    "decision": "decision 决策进化",
    "execution": "execution 执行台账",
    "exchange": "exchange 交易所访问",
    "factors": "factors 因子研究",
    "tools": "tools 工具脚本",
    "data": "data 数据源",
    "strategy": "strategy 策略指标",
    "risk": "risk 风控",
    "backtest": "backtest 回测",
    "tests": "tests 测试",
    "config": "config 全局配置",
    "legacy": "legacy 废弃",
}
# 显式层级序（自上而下）：下层不得 import 上层；data/config 是底座，
# 任何上层均可引用；外围（tools/tests/factors/backtest）不受方向约束
LAYER_ORDER = [
    "service", "engines", "decision", "execution",
    "strategy", "risk", "exchange",
    "data", "config",
]
PERIPHERAL = {"tools", "tests", "factors", "backtest", "legacy"}
ALLOWED_UPWARD = PERIPHERAL | {"data", "config"}

SKIP_DIRS = {".git", "lib", "node_modules", "__pycache__", ".pycache_tmp",
             "cache_okx", "cache_binance", "cache", "cache_fng"}


def collect():
    mod_layer, mod_top = {}, {}
    files = []
    for root, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in names:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), ".")
                files.append(rel)
                top = rel.split("/")[0].replace(".py", "")
                mod_layer[rel] = LAYERS.get(top, "other")
                mod_top[rel] = top

    # 可 import 的全限定名 → 文件（如 "data.fetch" → data/fetch.py）
    importable = {}
    for rel in files:
        pkg = rel.replace("/", ".").replace(".py", "")
        importable[pkg] = rel
        # 包 __init__ 也注册为包名（如 "execution" → execution/__init__.py）
        if rel.endswith("__init__.py"):
            importable[rel.split("/")[0]] = rel

    def local_imports(path):
        out = set()
        try:
            tree = ast.parse(open(path).read())
        except Exception:
            return out
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.append(node.module)
        # 最长前缀匹配：import a.b.c 优先匹配 a.b.c，失败再试 a.b、a
        for name in names:
            parts = name.split(".")
            for i in range(len(parts), 0, -1):
                cand = ".".join(parts[:i])
                if cand in importable:
                    out.add(importable[cand])
                    break
        return out

    deps = {}
    for rel in files:
        deps[rel] = sorted(local_imports(rel))

    layer_deps = {}
    for rel, layer in mod_layer.items():
        for dep in deps[rel]:
            dl = mod_layer.get(dep, "other")
            if dl != layer:
                layer_deps.setdefault(layer, set()).add(dl)
    return mod_layer, deps, layer_deps


def main():
    mod_layer, deps, layer_deps = collect()
    cnt = Counter(mod_layer.values())

    if "--check" in sys.argv:
        idx = {top: i for i, top in enumerate(LAYER_ORDER)}
        viol = 0
        for src, outs in layer_deps.items():
            src_top = src.split()[0]
            if src_top in PERIPHERAL:
                continue
            for o in outs:
                o_top = o.split()[0]
                if o_top in ALLOWED_UPWARD or o_top not in idx or src_top not in idx:
                    continue
                # 依赖方向必须向下（target 在序中更深）；target 更浅 = 反向违规
                if idx[o_top] < idx[src_top]:
                    print(f"❌ 反向依赖: {src} → {o}")
                    viol += 1
        print(f"分层检查: {'✅ 无违规' if viol == 0 else f'{viol} 处违规'}")
        return viol

    if "--mermaid" in sys.argv:
        print("```mermaid")
        print("flowchart TD")
        for i, layer in enumerate(LAYERS.values()):
            node = layer.split()[0]
            print(f"    {node}[\"{layer}\"]")
        for src, outs in sorted(layer_deps.items()):
            s = src.split()[0]
            for o in sorted(outs):
                print(f"    {s} --> {o.split()[0]}")
        print("```")
        return 0

    # --dump（默认）
    print("== 层间依赖（跨层）==")
    for src in LAYERS.values():
        outs = sorted(layer_deps.get(src, []))
        if outs:
            print(f"{src}  →  {', '.join(o.split()[0] for o in outs)}")
    print("\n== 模块数 ==")
    for layer in LAYERS.values():
        print(f"  {layer}: {cnt.get(layer, 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
