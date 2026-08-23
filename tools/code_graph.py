"""
轻量代码知识图谱 — 三层建模 + 影响面查询（纯标准库，零新依赖）。

三层：
  1. 模块层：import 依赖 → 分层违规检查（含 storage/interfaces，不再把未知层静默放过）
  2. 符号层：类/函数定义 + 调用边（含类继承、self 方法调用、跨模块解析）
  3. 数据流层：状态文件读写边（open/json.load/json.dump + 常量别名解析）→ 跨层共享状态告警

用法：
  python3 tools/code_graph.py --check             # 全量检查：分层违规 + 共享状态 + import 环
  python3 tools/code_graph.py --dump              # 人类可读汇总
  python3 tools/code_graph.py --mermaid [模块]    # mermaid 图（默认模块层；给模块=符号层）
  python3 tools/code_graph.py --json              # 完整图 JSON（知识图谱持久化/下游消费）
  python3 tools/code_graph.py --query <模式>      # 影响面查询
      file:<文件名>      谁读/写这个状态文件
      calls:<符号名>     谁调用这个函数/方法（反向调用图 = 改动影响面）
      module:<模块路径>  该模块的依赖、符号与它调用的东西
      layers             层间依赖矩阵
  python3 tools/code_graph.py --selftest          # 内嵌自测（检查器必须能抓出合成违规）

输出约定：所有输出排序确定（dict 键序 / sorted），diff 友好。
"""
import ast
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------- 分层定义 ----------
LAYERS = {
    "service": "service 服务端外壳",
    "engines": "engines 交易引擎",
    "decision": "decision 决策进化",
    "execution": "execution 执行台账",
    "storage": "storage 持久化适配",
    "interfaces": "interfaces 稳定契约",
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
# 显式层级序（自上而下）：下层不得 import 上层；data/config 是底座，任何上层可引用
LAYER_ORDER = [
    "service", "engines", "decision", "execution",
    "strategy", "risk", "storage", "exchange",
    "data", "interfaces", "config",
]
PERIPHERAL = {"tools", "tests", "factors", "backtest", "legacy"}
ALLOWED_UPWARD = PERIPHERAL | {"data", "interfaces", "config"}

SKIP_DIRS = {".git", "lib", "node_modules", "__pycache__", ".pycache_tmp",
             "cache_okx", "cache_binance", "cache", "cache_fng", "docs"}
FILE_SUFFIXES = (".json", ".db", ".txt", ".pid", ".lock", ".sqlite3")


def layer_of(rel_file):
    return LAYERS.get(rel_file.split("/")[0].replace(".py", ""), "other")


# ---------- 常量表达式求值（用于解析状态文件路径别名） ----------
def eval_const(node, const_map):
    """尽力求值字符串常量表达式：Constant / Name(查常量表) / Add(+) / os.path.join。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in const_map:
        return const_map[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = eval_const(node.left, const_map), eval_const(node.right, const_map)
        if a is not None and b is not None:
            return a + b
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "join" and \
                isinstance(f.value, ast.Name) and f.value.id == "os.path":
            parts = [eval_const(a, const_map) for a in node.args]
            if all(p is not None for p in parts):
                return os.path.join(*parts)
    return None


# ---------- 单文件解析 ----------
class _Visitor(ast.NodeVisitor):
    def __init__(self, module, const_map):
        self.module = module
        self.const_map = const_map
        self.stack = []          # ["Class", "method"] 调用方上下文
        self.defs = []           # [(symbol, kind)] kind=class/func/method
        self.calls = []          # [(caller, callee_name, is_method_attr)]
        self.file_ops = []       # [(file, mode, where)] mode=read/write
        self.assigns = []        # [(name, value)] 模块级常量

    # 符号定义
    def visit_ClassDef(self, node):
        sym = ".".join(self.stack + [node.name])
        self.defs.append((sym, "class", node.lineno))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        sym = ".".join(self.stack + [node.name])
        kind = "method" if self.stack else "func"
        self.defs.append((sym, kind, node.lineno))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    # 调用边
    def visit_Call(self, node):
        caller = self.module + "::" + ".".join(self.stack) if self.stack else self.module
        f = node.func
        if isinstance(f, ast.Name):
            self.calls.append((caller, f.id, False))
        elif isinstance(f, ast.Attribute):
            base = None
            if isinstance(f.value, ast.Name):
                base = f.value.id
            is_self = base in ("self", "cls")
            name = "self." + f.attr if is_self else (base + "." + f.attr if base else "*." + f.attr)
            self.calls.append((caller, name, True))
        # 数据流：open(...) / json.load(...) / json.dump(...)
        fn = None
        if isinstance(f, ast.Name):
            fn = f.id
        elif isinstance(f, ast.Attribute) and f.attr in ("load", "dump", "loads", "dumps") and \
                isinstance(f.value, ast.Name) and f.value.id == "json":
            fn = "json." + f.attr
        if fn == "open" and node.args:
            path = eval_const(node.args[0], self.const_map)
            if path:
                mode = "r"
                if len(node.args) > 1:
                    m = eval_const(node.args[1], self.const_map) or ""
                    mode = "w" if "w" in m or "a" in m else "r"
                self.file_ops.append((path, "write" if mode == "w" else "read",
                                      self.module + ":" + str(node.lineno)))
        elif fn in ("json.load", "json.loads") and node.args:
            path = eval_const(node.args[0], self.const_map)
            if path:
                self.file_ops.append((path, "read", self.module + ":" + str(node.lineno)))
        elif fn in ("json.dump", "json.dumps") and node.args:
            path = eval_const(node.args[0], self.const_map)
            if path:
                self.file_ops.append((path, "write", self.module + ":" + str(node.lineno)))
        self.generic_visit(node)

    # 模块级常量赋值（含文件路径别名）
    def visit_Assign(self, node):
        if isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            self.assigns.append((node.targets[0].id, node.value.value))
        self.generic_visit(node)


def parse_file(rel):
    try:
        tree = ast.parse(open(rel).read())
    except Exception:
        return None
    # 第一遍：收集模块级字符串常量（含路径别名），供 eval_const 用
    const_map = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            const_map[n.targets[0].id] = n.value.value
    v = _Visitor(rel, const_map)
    v.visit(tree)
    # 第二遍补充：所有"看起来像状态文件"的字面量（含函数默认参数里的路径，如
    # TradeJournal(path="trade_journal.json")），记为 defined（定义/默认指向处）。
    v.file_constants = sorted({
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.endswith(FILE_SUFFIXES) and " " not in n.value
        and "\n" not in n.value and not n.value.startswith("http")
    })
    return v


# ---------- 全仓建图 ----------
def build_graph():
    modules = []
    for root, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(names):
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), ".")
                modules.append(rel)
    mod_layer = {m: layer_of(m) for m in modules}

    # 可 import 全限定名 → 文件
    importable = {}
    for rel in modules:
        pkg = rel.replace("/", ".").replace(".py", "")
        importable[pkg] = rel
        if rel.endswith("__init__.py"):
            importable[rel.split("/")[0]] = rel

    def local_imports(rel):
        try:
            tree = ast.parse(open(rel).read())
        except Exception:
            return set()
        out, names = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.append(node.module)
        for name in names:
            parts = name.split(".")
            for i in range(len(parts), 0, -1):
                cand = ".".join(parts[:i])
                if cand in importable:
                    out.add(importable[cand])
                    break
        return out

    def raw_imports(rel):
        try:
            tree = ast.parse(open(rel).read())
        except Exception:
            return []
        result = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.append({"source": rel, "module": alias.name,
                                   "symbols": [], "line": node.lineno})
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                result.append({"source": rel, "module": node.module,
                               "symbols": [alias.name for alias in node.names],
                               "line": node.lineno})
        return result

    # 符号索引：符号名 → [模块]
    symbol_index = defaultdict(list)
    module_symbols = {}
    module_calls = {}
    file_ops = []
    file_constants = {}
    imports = []

    for rel in modules:
        v = parse_file(rel)
        if not v:
            continue
        module_symbols[rel] = [{"symbol": s, "kind": k, "line": l} for s, k, l in v.defs]
        module_calls[rel] = [{"caller": c, "callee": n, "attr": a} for c, n, a in v.calls]
        for s, k, _l in v.defs:
            symbol_index[s].append(rel)
            symbol_index[s.split(".")[-1]].append(rel)   # 短名索引（宽容匹配）
        file_ops.extend([{"file": f, "mode": m, "where": w} for f, m, w in v.file_ops])
        if v.file_constants:
            file_constants[rel] = v.file_constants
        imports.extend(raw_imports(rel))

    # 解析调用目标：精确 → 唯一模块；短名唯一 → 该模块；多义 → 列出候选；无 → unresolved
    resolved_calls = []
    for rel, calls in module_calls.items():
        for c in calls:
            name = c["callee"].split(".")[-1]
            cands = sorted(set(symbol_index.get(c["callee"], []) or symbol_index.get(name, [])))
            resolved_calls.append({
                "caller": c["caller"], "callee": c["callee"],
                "target": cands[0] if len(cands) == 1 else None,
                "candidates": cands,
                "resolved": len(cands) == 1,
            })

    # 层间依赖
    layer_deps = defaultdict(set)
    for rel, layer in mod_layer.items():
        for dep in local_imports(rel):
            dl = mod_layer.get(dep, "other")
            if dl != layer:
                layer_deps[layer].add(dl)

    # import 环（SCC）
    import_graph = {m: local_imports(m) for m in modules}
    cycles = find_cycles(import_graph)

    return {
        "modules": modules, "module_layer": mod_layer,
        "layer_deps": {k: sorted(v) for k, v in layer_deps.items()},
        "symbols": module_symbols, "calls": resolved_calls,
        "file_ops": file_ops, "file_constants": file_constants,
        "cycles": cycles, "imports": imports,
    }


def find_cycles(import_graph):
    """找 import 环（SCC 大小>1 或自环）。"""
    index, low, stack, on_stack, result = {}, {}, [], set(), []
    counter = [0]

    def dfs(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in import_graph.get(v, ()):
            if w not in index:
                dfs(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                result.append(sorted(comp))
    for v in import_graph:
        if v not in index:
            dfs(v)
    return sorted(result)


# ---------- 检查 ----------
def run_checks(g):
    idx = {top: i for i, top in enumerate(LAYER_ORDER)}
    problems = []

    # 1. 分层违规
    for src, outs in g["layer_deps"].items():
        src_top = src.split()[0]
        if src_top in PERIPHERAL:
            continue
        for o in outs:
            o_top = o.split()[0]
            if o_top in ALLOWED_UPWARD or o_top not in idx or src_top not in idx:
                continue
            if idx[o_top] < idx[src_top]:
                problems.append(f"反向依赖: {src} → {o}")

    # 2. 跨层共享状态（多个不同层写同一文件）
    by_file = defaultdict(set)
    for op in g["file_ops"]:
        if op["mode"] == "write":
            by_file[op["file"]].add(layer_of(op["where"].split(":")[0]))
    for f, layers in sorted(by_file.items()):
        if len(layers) > 1:
            problems.append(f"跨层共享状态文件: {f} 被 {sorted(layers)} 多层写入")

    # 3. import 环
    for comp in g["cycles"]:
        problems.append(f"import 环: {' ↔ '.join(comp)}")

    # 4. 接口边界：服务层不能绕过 query/runtime API；跨功能包不能
    # import 对方下划线私有符号。
    for item in g.get("imports", []):
        source = item["source"]
        module = item["module"]
        source_top = source.split("/", 1)[0]
        if ((source.startswith("service/") and module == "storage.db")
                or (source_top not in PERIPHERAL
                    and module.startswith("tools."))):
            problems.append(
                f"接口绕过: {source}:{item['line']} 直接 import {module}")
        target_top = module.split(".", 1)[0]
        if source_top not in PERIPHERAL and source_top != target_top:
            for symbol in item.get("symbols", []):
                if symbol.startswith("_"):
                    problems.append(
                        f"跨模块私有符号: {source}:{item['line']} import "
                        f"{module}.{symbol}")
    return problems


# ---------- 输出 ----------
def fmt_edges(edges, note=""):
    out = ["```mermaid", "flowchart TD"]
    seen = set()
    for a, b in edges:
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        a_id = a.replace(".", "_").replace("/", "_").replace("-", "_")
        b_id = b.replace(".", "_").replace("/", "_").replace("-", "_")
        out.append(f"    {a_id}[\"{a}\"]")
        out.append(f"    {b_id}[\"{b}\"]")
        out.append(f"    {a_id} --> {b_id}")
    out.append("```")
    return "\n".join(out)


def mermaid_module(g):
    edges = []
    for src, outs in sorted(g["layer_deps"].items()):
        s = src.split()[0]
        for o in outs:
            edges.append((s, o.split()[0]))
    return fmt_edges(edges)


def mermaid_symbol(g, module):
    if module not in g["symbols"]:
        print(f"未知模块: {module}", file=sys.stderr)
        return ""
    edges = []
    for c in g["calls"]:
        if c["caller"].startswith(module + "::"):
            if c["target"]:
                tgt_mod = c["target"].split("::")[0] if "::" in c["target"] else c["target"]
                label = c["target"].split("::")[-1]
                edges.append((c["caller"].split("::")[-1] or module, label))
            else:
                edges.append((c["caller"].split("::")[-1] or module, c["callee"]))
    return fmt_edges(edges[:80])


def query(g, spec):
    kind, _, arg = spec.partition(":")
    if kind == "layers":
        for src in sorted(g["layer_deps"]):
            print(f"{src}  →  {', '.join(o.split()[0] for o in g['layer_deps'][src])}")
        return
    if kind == "file":
        hits = [op for op in g["file_ops"] if op["file"].split("/")[-1] == arg]
        defined = [m for m, files in g.get("file_constants", {}).items()
                   if arg in files or any(f.split("/")[-1] == arg for f in files)]
        if not hits and not defined:
            print(f"无模块直接读写 {arg}")
            return
        for op in sorted(hits, key=lambda x: (x["mode"], x["where"])):
            print(f"  {op['mode']:<5} {op['where']}")
        writers = {op["where"].split(":")[0] for op in hits if op["mode"] == "write"}
        readers = {op["where"].split(":")[0] for op in hits if op["mode"] == "read"}
        if defined:
            print(f"  定义/默认引用: {sorted(defined)}")
        print(f"  写方: {sorted(writers)}")
        print(f"  读方: {sorted(readers)}")
        return
    if kind == "calls":
        hits = [c for c in g["calls"] if c["callee"].split(".")[-1] == arg
                or c["callee"] == arg]
        if not hits:
            print(f"无调用 {arg}")
            return
        seen = set()
        for c in sorted(hits, key=lambda x: (x["caller"], x["callee"])):
            key = (c["caller"], c["callee"])
            if key in seen:
                continue
            seen.add(key)
            print(f"  {c['caller']}  →  {c['callee']}" + (f"  (→ {c['target']})" if c["target"] else ""))
        return
    if kind == "module":
        # 支持三种写法：engines/directional_trader.py、engines.directional_trader、directional_trader
        rel = None
        if arg in g["modules"]:
            rel = arg
        else:
            dotted = arg.replace(".", "/") + ("" if arg.endswith(".py") else ".py")
            for m in g["modules"]:
                if m == dotted or m.split("/")[-1] == arg or \
                        m.split("/")[-1].replace(".py", "") == arg:
                    rel = m
                    break
        if rel is None:
            print(f"未知模块: {arg}")
            return
        print(f"层: {g['module_layer'].get(rel, '?')}")
        syms = g["symbols"].get(rel, [])
        print(f"符号({len(syms)}): " + ", ".join(f"{s['symbol']}({s['kind']})" for s in syms[:40]))
        consts = g.get("file_constants", {}).get(rel, [])
        if consts:
            print(f"状态文件常量: {', '.join(consts[:20])}")
        outs = [c for c in g["calls"] if c["caller"].startswith(rel + "::")]
        print(f"调用({len(outs)}):")
        seen = set()
        for c in sorted(outs, key=lambda x: (x["caller"], x["callee"])):
            key = (c["caller"], c["callee"])
            if key in seen:
                continue
            seen.add(key)
            print(f"  → {c['callee']}" + (f"  = {c['target']}" if c["target"] else ""))
        return
    print(f"未知查询: {spec}")


# ---------- 自测 ----------
def selftest():
    """检查器必须能抓出合成违规：反向分层 + 跨层共享状态 + import 环。"""
    g = {
        "layer_deps": {"service 服务端外壳": ["config 全局配置", "engines 交易引擎"],
                       "engines 交易引擎": ["service 服务端外壳"]},   # 反向！
        "file_ops": [{"file": "x.json", "mode": "write", "where": "engines/a.py:1"},
                     {"file": "x.json", "mode": "write", "where": "data/b.py:2"}],
        "cycles": [["a.py", "b.py"]],
        "imports": [{"source": "service/app.py", "module": "storage.db",
                     "symbols": [], "line": 1}],
    }
    probs = run_checks(g)
    assert any("反向依赖" in p for p in probs), probs
    assert any("跨层共享状态" in p for p in probs), probs
    assert any("import 环" in p for p in probs), probs
    assert any("接口绕过" in p for p in probs), probs
    # 正例：全部向下依赖 → 零问题
    g2 = {"layer_deps": {"service 服务端外壳": ["engines 交易引擎"],
                         "engines 交易引擎": ["decision 决策进化", "exchange 交易所访问"]},
          "file_ops": [], "cycles": [], "imports": []}
    assert run_checks(g2) == []
    print("selftest ✅ 检查器能抓出合成违规，正例零误报")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return 0
    if "--check" in args:
        g = build_graph()
        probs = run_checks(g)
        for p in probs:
            print(f"❌ {p}")
        print(f"知识图谱检查: {'✅ 无违规' if not probs else f'{len(probs)} 处问题'}")
        return 1 if probs else 0
    if "--mermaid" in args:
        g = build_graph()
        mod = args[args.index("--mermaid") + 1] if args.index("--mermaid") + 1 < len(args) \
            and not args[args.index("--mermaid") + 1].startswith("--") else None
        print(mermaid_symbol(g, mod) if mod else mermaid_module(g))
        return 0
    if "--json" in args:
        g = build_graph()
        print(json.dumps(g, ensure_ascii=False, indent=2))
        return 0
    if "--query" in args:
        g = build_graph()
        query(g, args[args.index("--query") + 1])
        return 0
    # 默认 --dump
    g = build_graph()
    print("== 层间依赖（跨层）==")
    for src in sorted(g["layer_deps"]):
        print(f"{src}  →  {', '.join(o.split()[0] for o in g['layer_deps'][src])}")
    from collections import Counter
    cnt = Counter(g["module_layer"].values())
    print("\n== 模块数 ==")
    for layer in LAYERS.values():
        print(f"  {layer}: {cnt.get(layer, 0)}")
    print(f"\n== 符号: {sum(len(v) for v in g['symbols'].values())} 个定义, "
          f"{len(g['calls'])} 条调用边（{sum(1 for c in g['calls'] if c['resolved'])} 已解析）==")
    print(f"== 数据流: {len(g['file_ops'])} 条直接读写, "
          f"{sum(len(v) for v in g['file_constants'].values())} 个状态文件常量 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
