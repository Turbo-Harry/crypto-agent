"""
自动因子挖掘 — 纯 Python 遗传进化引擎（不依赖 gplearn）。
v2 防过拟合改造（审计 CR-10 / OP-7）：
  1. 真 GA 算子：子树交叉（随机节点切下子树互换）、真变异（算子替换/子树重生成/变量替换）
     （v1 的 swap_subtree 只是随机返回父节点之一、mutate 60% 原样返回 = 空操作）
  2. 因果归一化：vol 用扩张均值（只含过去数据），修复全样本均值泄漏
  3. walk-forward 多折：每折只在训练段进化，测试段只做一次样本外评估；
     报告 OOS IC 中位数 + 符号一致率（同号折数占比），只有稳定因子才算数
  4. 最终按"中位 OOS IC"排序（不再按单次测试 IC 选因子 = 测试集选择偏差）

用法：
  python3 factor_evolution.py          完整 walk-forward
  python3 factor_evolution.py --quick  快速模式（小种群，冒烟用）
"""
import sys
import os
import json
import random
import math
from datetime import datetime, timezone

import numpy as np
from data.fetch_okx import fetch_btc_klines
from data.fetch_fear_greed import fetch_fng

SPLIT_TS = 1704067200000

# 算子（全部向量化）
OPS = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / np.where(np.abs(b) < 1e-9, 1e-9, b),
    "min": lambda a, b: np.minimum(a, b),
    "max": lambda a, b: np.maximum(a, b),
    "abs": lambda a, b: np.abs(a),
    "neg": lambda a, b: -a,
    "log": lambda a, b: np.log(np.abs(a) + 1e-9),
    "sqrt": lambda a, b: np.sqrt(np.abs(a)),
}
BINARY = {"add", "sub", "mul", "div", "min", "max"}
UNARY = {"abs", "neg", "log", "sqrt"}


def rsi(closes, period=14):
    n = len(closes)
    out = np.full(n, 50.0)
    if n < period + 1:
        return out
    chg = np.diff(closes)
    gains = np.clip(chg, 0, None)
    losses = np.clip(-chg, 0, None)
    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    for i in range(period, n):
        out[i] = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
    return out


def ema(values, period):
    k = 2 / (period + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def build_variables(klines, fng_map):
    """基础变量矩阵。全部因果构造（只用过去+当前数据，无未来函数）。"""
    c = np.array([k["close"] for k in klines], dtype=float)
    h = np.array([k["high"] for k in klines], dtype=float)
    l = np.array([k["low"] for k in klines], dtype=float)
    v = np.array([k["volume"] for k in klines], dtype=float)
    n = len(c)
    fng = np.array([fng_map.get(
        datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"), 50)
        for k in klines], dtype=float)

    V = {}
    V["close"] = c / c[0]
    V["ret1"] = np.zeros(n); V["ret1"][1:] = np.diff(c) / c[:-1]
    V["ret7"] = np.zeros(n); V["ret7"][7:] = c[7:] / c[:-7] - 1
    V["ret30"] = np.zeros(n); V["ret30"][30:] = c[30:] / c[:-30] - 1
    e20 = ema(c, 20); e50 = ema(c, 50)
    V["dist20"] = (c - e20) / e20
    V["dist50"] = (c - e50) / e50
    V["rsi"] = rsi(c) / 100
    # 因果量比：扩张均值（只用 t 及以前的数据）——修复 v1 的全样本均值泄漏
    expanding_mean = np.cumsum(v) / np.arange(1, n + 1)
    V["vol"] = v / (expanding_mean + 1e-9)
    V["fng"] = fng / 100
    V["range"] = (h - l) / c
    return V, c


# ---------- 树结构与真 GA 算子 ----------

def random_tree(depth, var_names):
    if depth <= 0:
        return ("var", random.choice(var_names))
    if random.random() < 0.2:  # 根/内部节点 20% 概率退化（v1 的 35% 使大量树退化成单变量）
        return ("var", random.choice(var_names))
    op = random.choice(list(OPS.keys()))
    left = random_tree(depth - 1, var_names)
    right = random_tree(depth - 1, var_names)
    return (op, left, right)


def eval_tree(tree, V):
    if tree[0] == "var":
        return V[tree[1]]
    op, left, right = tree
    return OPS[op](eval_tree(left, V), eval_tree(right, V))


def tree_str(tree):
    if tree[0] == "var":
        return tree[1]
    op, l, r = tree
    return f"({tree_str(l)} {op} {tree_str(r)})"


def copy_tree(t):
    if t[0] == "var":
        return t
    op, l, r = t
    return (op, copy_tree(l), copy_tree(r))


def random_path(tree):
    """随机选一条从根到某节点的路径（1=左, 2=右）。"""
    path = []
    node = tree
    while node[0] != "var" and random.random() < 0.7:
        go = random.choice([1, 2])
        path.append(go)
        node = node[go]
    return path


def get_node(tree, path):
    node = tree
    for p in path:
        node = node[p]
    return node


def set_node(tree, path, new_node):
    """返回把 path 处节点替换为 new_node 后的新树。"""
    if not path:
        return new_node
    op, l, r = tree
    if path[0] == 1:
        return (op, set_node(l, path[1:], new_node), r)
    return (op, l, set_node(r, path[1:], new_node))


def swap_subtree(a, b):
    """真交叉：a 的随机节点替换为 b 的随机子树。
    a 是变量叶时整体换成 b 的随机子树（保证交叉有效，不返回原树）。"""
    if a[0] == "var":
        if b[0] == "var":
            return a
        return copy_tree(get_node(b, random_path(b)))
    pa = random_path(a)
    pb = random_path(b)
    sub = copy_tree(get_node(b, pb))
    return set_node(a, pa, sub)


def mutate(tree, var_names, max_depth=3):
    """真变异：35% 子树重生成 / 35% 算子或变量替换 / 30% 不变异。"""
    r = random.random()
    if r < 0.35:
        # 子树重生成
        p = random_path(tree)
        return set_node(tree, p, random_tree(2, var_names))
    if r < 0.70:
        p = random_path(tree)
        node = get_node(tree, p)
        if node[0] == "var":
            return set_node(tree, p, ("var", random.choice(var_names)))
        # 替换算子（保持子节点，深度不变）
        new_op = random.choice(list(OPS.keys()))
        return set_node(tree, p, (new_op, node[1], node[2]))
    return tree


def ic_score(values, target):
    """Spearman IC（用 numpy 快速计算）。"""
    mask = np.isfinite(values) & np.isfinite(target)
    if mask.sum() < 50:
        return 0.0
    x, y = values[mask], target[mask]
    rx = np.argsort(np.argsort(x))
    ry = np.argsort(np.argsort(y))
    if np.std(rx) == 0 or np.std(ry) == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def evolve(V, y, tr_slice, var_names, pop_size, gens, max_depth=3):
    """在一段训练数据上进化，返回最终种群（按训练 IC 排序的树列表）。"""
    pop = [random_tree(max_depth, var_names) for _ in range(pop_size)]
    y_tr = y[tr_slice]
    for gen in range(gens):
        scored = []
        for tree in pop:
            try:
                vals = eval_tree(tree, V)
                v_tr = vals[tr_slice]
                # 对齐长度：标签尾 7 天为零填充，训练 IC 允许使用
                m = min(len(v_tr), len(y_tr))
                ic_tr = ic_score(v_tr[:m], y_tr[:m])
                scored.append((ic_tr, tree))
            except Exception:
                scored.append((0.0, tree))
        scored.sort(key=lambda x: -abs(x[0]))
        survivors = [t for _, t in scored[:pop_size // 4]]
        new_pop = list(survivors)
        while len(new_pop) < pop_size:
            if random.random() < 0.7:
                a = random.choice(survivors)
                b = random.choice(survivors)
                child = swap_subtree(a, b)
            else:
                child = random_tree(max_depth, var_names)
            if random.random() < 0.3:
                child = mutate(child, var_names, max_depth)
            new_pop.append(child)
        pop = new_pop
        if gen % 2 == 0 or gen == gens - 1:
            print(f"    第{gen+1}代: 最佳训练IC {scored[0][0]:+.4f} | {tree_str(scored[0][1])[:60]}")
    return pop


def main():
    quick = "--quick" in sys.argv
    random.seed(42)
    np.random.seed(42)

    print("加载数据...")
    btc = fetch_btc_klines()
    fng = fetch_fng()
    fng_map = {f["date"]: f["value"] for f in fng}
    V, closes = build_variables(btc, fng_map)
    var_names = list(V.keys())
    n = len(closes)
    print(f"基础变量 {len(var_names)} 个: {var_names} | K线 {n} 根")

    # 标签：未来 7 天收益（尾 7 天为零填充）
    horizon = 7
    y = np.zeros(n)
    y[:n - horizon] = closes[horizon:] / closes[:-horizon] - 1

    # ---------- walk-forward 折叠：训练段进化 → 测试段只评估一次 ----------
    test_len = max(60, int(n * 0.15))
    min_train = int(n * 0.5)
    folds = []
    i = min_train
    while i + test_len <= n - horizon:
        folds.append((slice(0, i), slice(i, i + test_len)))
        i += test_len
    print(f"\nwalk-forward {len(folds)} 折（测试段各 {test_len} 根，滚动）")

    POP = 100 if quick else 500
    GENS = 3 if quick else 8
    TOP_PER_FOLD = 5   # 每折按【训练 IC】取前 N 个因子去测试段评估
    MAX_DEPTH = 3

    # 跨折聚合：{表达式: {"ics": [各折OOS IC], "tr_ics": [...], "tree": 树}}
    agg = {}
    for fi, (tr, te) in enumerate(folds, 1):
        print(f"\n--- 折 {fi}/{len(folds)}: 训练 {tr.stop - tr.start} 根 → 测试 {te.stop - te.start} 根 ---")
        pop = evolve(V, y, tr, var_names, POP, GENS, MAX_DEPTH)
        ranked = []
        for tree in pop:
            try:
                vals = eval_tree(tree, V)
                m = min(tr.stop - tr.start, len(y[tr]))
                ic_tr = ic_score(vals[tr][:m], y[tr][:m])
                ic_te = ic_score(vals[te], y[te])
                ranked.append((abs(ic_tr), ic_tr, ic_te, tree))
            except Exception:
                pass
        ranked.sort(key=lambda x: -x[0])
        kept = 0
        for _, ic_tr, ic_te, tree in ranked:
            s = tree_str(tree)
            rec = agg.get(s)
            if rec is None:
                if kept >= TOP_PER_FOLD:
                    continue   # 每折只带训练IC前N个进测试段（防选择偏差）
                rec = agg.setdefault(s, {"ics": [], "tr_ics": [], "tree": tree})
                kept += 1
            rec["ics"].append(ic_te)
            rec["tr_ics"].append(ic_tr)

    # ---------- 诚实汇总：中位 OOS IC + 符号一致率 ----------
    print("\n" + "=" * 70)
    print("walk-forward 汇总（按中位 OOS IC 排序，非单次测试 IC）:")
    print("=" * 70)
    rows = []
    for s, rec in agg.items():
        ics = np.array(rec["ics"])
        med = float(np.median(ics))
        sign_agree = float(np.mean(np.sign(ics) == np.sign(med))) if med != 0 else 0.0
        rows.append((med, sign_agree, len(ics), rec["tree"], s,
                     float(np.median(rec["tr_ics"]))))
    rows.sort(key=lambda x: -abs(x[0]))
    promoted = []
    for med, sign_agree, nf, tree, s, med_tr in rows:
        # 提升标准：≥2 折出现 + 中位 |OOS IC| ≥ 0.03 + ≥ 80% 折同号
        # （单折因子的"同号率100%"是平凡真，不作数）
        stable = nf >= 2 and abs(med) >= 0.03 and sign_agree >= 0.8
        verdict = ("✅ 稳定" if stable else
                   ("⚠️ 单折待验" if nf == 1 and abs(med) >= 0.03 else
                    ("⚠️ 边缘" if abs(med) >= 0.015 else "❌ 噪声")))
        print(f"{verdict} OOS中位IC {med:+.4f} | 同号率 {sign_agree*100:.0f}% "
              f"({nf}折) | 训练IC {med_tr:+.4f} | {s[:52]}")
        if stable:
            promoted.append({"expr": s, "median_oos_ic": med,
                             "sign_agree": sign_agree, "n_folds": nf})
    print(f"\n结论: {len(rows)} 个因子中 {len(promoted)} 个通过样本外稳定性检验（≥2折+同号≥80%+中位|IC|≥0.03）")
    print("（v1 报告的'测试IC 0.12-0.18'是单次切分+测试集选择的幸存者值，不可信）")
    print("（另注：7日重叠标签会高估 IC 显著性，本结果已用 walk-forward 缓解但仍是乐观上界）")

    # 保存通过检验的因子（供 DIR_WEIGHTS 接入）
    if promoted:
        with open("factor_top.json", "w") as f:
            json.dump(promoted, f, ensure_ascii=False, indent=2)
        print("已保存通过检验的因子 → factor_top.json")
    return promoted


if __name__ == "__main__":
    main()
