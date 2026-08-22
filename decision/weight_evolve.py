# -*- coding: utf-8 -*-
"""
权重进化（2026-08-23 用户问"会根据历史经验调整权重吗"）——
证据 → 提案 → 人工批准 → 生效,与扫描尺子进化同纪律(永不自动改):

  1. 证据: 每笔平仓已有 shadow_dims(6 维子分) + pnl。propose() 逐维算
     IC(子分与盈亏的皮尔逊相关),样本 ≥ WEIGHT_EVOLVE_MIN_SAMPLES 且
     |IC| ≥ WEIGHT_EVOLVE_MIN_IC 的维度才有资格动。
  2. 提案: 强维 +STEP、弱维 -STEP(单维变动 ≤ MAX_SHIFT),归一化到 1,
     写 experiments(kind='weight_evolve'),证据达标 → status='accepted'
     (待人工批准;不达标只落 proposed 观测)。
  3. 生效唯一写入口 = approve(): 把新权重写 kv 'shadow_weights'。
     评分时 effective_weights() 优先读 kv,否则 config.SHADOW_WEIGHTS。
  4. rollback(): 删 kv 回 config 基线。
双向共享: 权重 kv 属于策略状态,经验/策略同步会把批准结果镜像到对端实例。
"""
import json
import os
import time

import config

DIMS = config.SHADOW_DIMS


def effective_weights(db_path=None):
    """活体权重: kv 批准的覆盖优先,否则 config 基线。归一化保证和=1。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key=?",
                     [config.WEIGHT_EVOLVE_KV_KEY], db_path=db_path)
        if row and row.get("value"):
            w = json.loads(row["value"])
            w = {k: float(v) for k, v in (w or {}).items() if k in DIMS}
            total = sum(w.values())
            if w and total > 0 and abs(total - 1.0) < 0.5:
                w = {k: round(w[k] / total, 4) for k in DIMS}
                return _normalize(w)
    except Exception:
        pass
    return dict(config.SHADOW_WEIGHTS)


def _ic(xs, ys):
    """皮尔逊相关(样本量<2 或零方差 → None)。"""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / ((sxx * syy) ** 0.5)


def _normalize(w):
    """归一化: 尾维补差,保证和精确=1.0(浮点四舍五入不再漂)。"""
    keys = [d for d in DIMS if d in w]
    if not keys:
        return dict(config.SHADOW_WEIGHTS)
    rest = sum(w.get(k, 0.0) for k in keys[:-1])
    w[keys[-1]] = round(1.0 - rest, 4)
    return w


def propose(db_path=None, force=False):
    """按已平仓样本算逐维 IC 并提案。返回 (status, message, evidence)。
    纯观测+写 experiments 提案,绝不改活体权重(approve 是唯一写入口)。"""
    import storage.db as sdb
    if not getattr(config, "WEIGHT_EVOLVE_ENABLED", False):
        return "disabled", "权重进化已关闭(config.WEIGHT_EVOLVE_ENABLED=False)", {}
    sdb.init_db(db_path)
    rows = sdb.q("SELECT pnl, shadow_dims FROM trades WHERE status='closed' "
                 "AND pnl IS NOT NULL AND shadow_dims IS NOT NULL "
                 "AND shadow_dims != ''", db_path=db_path)
    evidence = {d: {"n": 0, "ic": None} for d in DIMS}
    if len(rows) < max(2, config.WEIGHT_EVOLVE_MIN_SAMPLES):
        return ("insufficient",
                f"样本不足: {len(rows)}/{config.WEIGHT_EVOLVE_MIN_SAMPLES} 笔"
                f"带 6 维子分的已平仓样本", evidence)
    per_dim = {d: [] for d in DIMS}
    pnls = []
    for r in rows:
        try:
            dims = json.loads(r["shadow_dims"] or "{}")
        except Exception:
            continue
        pnls.append(float(r["pnl"]))
        for d in DIMS:
            per_dim[d].append(float(dims.get(d, 0.5)))
    for d in DIMS:
        n = len(per_dim[d])
        ic = _ic(per_dim[d], pnls[:n])
        evidence[d] = {"n": n, "ic": round(ic, 4) if ic is not None else None}
    strong = [d for d in DIMS
              if evidence[d]["ic"] is not None
              and evidence[d]["n"] >= config.WEIGHT_EVOLVE_MIN_SAMPLES
              and evidence[d]["ic"] >= config.WEIGHT_EVOLVE_MIN_IC]
    weak = [d for d in DIMS
            if evidence[d]["ic"] is not None
            and evidence[d]["n"] >= config.WEIGHT_EVOLVE_MIN_SAMPLES
            and evidence[d]["ic"] <= -config.WEIGHT_EVOLVE_MIN_IC]
    if not strong and not weak:
        return ("no_edge", "无维度 |IC| 达标,权重维持基线", evidence)
    base = effective_weights(db_path)
    cand = dict(base)
    for d in strong:
        cand[d] = min(cand[d] + config.WEIGHT_EVOLVE_STEP,
                      base[d] + config.WEIGHT_EVOLVE_MAX_SHIFT)
    for d in weak:
        cand[d] = max(cand[d] - config.WEIGHT_EVOLVE_STEP,
                      base[d] - config.WEIGHT_EVOLVE_MAX_SHIFT, 0.0)
    total = sum(cand.values())
    cand = _normalize({k: round(v / total, 4) for k, v in cand.items()})
    # 提案落 experiments: 证据达标即 accepted(待批准),否则 proposed(观测)
    gate_ok = all(evidence[d]["n"] >= config.WEIGHT_EVOLVE_MIN_SAMPLES
                  for d in (strong + weak))
    status = "accepted" if gate_ok else "proposed"
    change_id = f"w-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        old_pending = sdb.q1("SELECT change_id FROM experiments "
                             "WHERE kind='weight_evolve' AND status IN "
                             "('proposed','accepted') ORDER BY ts DESC LIMIT 1",
                             db_path=db_path)
        if old_pending and not force:
            return ("pending", f"已有待处理提案 {old_pending['change_id']},"
                    f"新证据已算但未覆盖(force=True 可强制新提案)", evidence)
        sdb.x("INSERT INTO experiments (change_id, kind, params, status, ts) "
              "VALUES (?,?,?,?,?)",
              [change_id, "weight_evolve",
               json.dumps({"from": base, "to": cand,
                           "evidence": evidence}, ensure_ascii=False),
               status, time.time()], db_path=db_path)
    except Exception:
        pass
    msg = (f"提案 {change_id}: {'/'.join(strong)} 增权 · "
           f"{'/'.join(weak) or '无'} 减权 → 待人工批准")
    return status, msg, evidence


def approve(change_id=None, db_path=None):
    """人工批准(唯一写入口): 把提案的新权重写 kv,活体生效。永不自动。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    if change_id:
        row = sdb.q1("SELECT * FROM experiments WHERE change_id=?",
                     [change_id], db_path=db_path)
    else:
        row = sdb.q1("SELECT * FROM experiments WHERE kind='weight_evolve' "
                     "AND status='accepted' ORDER BY ts DESC LIMIT 1",
                     db_path=db_path)
    if not row or row["status"] != "accepted":
        return False, "没有证据达标的权重提案,不能批准"
    try:
        params = json.loads(row["params"] or "{}")
        to_w = {k: float(v) for k, v in (params.get("to") or {}).items()
                if k in DIMS}
    except Exception:
        return False, "提案参数无法解析"
    if abs(sum(to_w.values()) - 1.0) > 0.01:
        return False, f"提案权重和不等于 1: {sum(to_w.values())}"
    sdb.x("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
          [config.WEIGHT_EVOLVE_KV_KEY,
           json.dumps(to_w, ensure_ascii=False), time.time()],
          db_path=db_path)
    sdb.x("UPDATE experiments SET status=?, decided_by=?, notes=? "
          "WHERE change_id=?",
          ["applied", "human",
           f"人工批准权重 {params.get('from')} → {to_w}",
           row["change_id"]], db_path=db_path)
    return True, f"权重已生效: {to_w}"


def rollback(db_path=None):
    """撤销活体权重覆盖,回到 config 基线。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    sdb.x("DELETE FROM kv WHERE key=?", [config.WEIGHT_EVOLVE_KV_KEY],
          db_path=db_path)
    sdb.x("UPDATE experiments SET status=?, decided_by=?, notes=? "
          "WHERE kind='weight_evolve' AND status IN ('accepted','applied')",
          ["rolled_back", "human", "人工回滚到 config.SHADOW_WEIGHTS 基线"],
          db_path=db_path)
    return True, f"已回滚基线: {dict(config.SHADOW_WEIGHTS)}"


def snapshot(db_path=None):
    """观测快照(API 用): 活体权重 + 最新提案 + 证据。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    row = sdb.q1("SELECT * FROM experiments WHERE kind='weight_evolve' "
                 "ORDER BY ts DESC LIMIT 1", db_path=db_path)
    pending = None
    if row:
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        pending = {"change_id": row["change_id"], "status": row["status"],
                   "from": params.get("from"), "to": params.get("to"),
                   "evidence": params.get("evidence")}
    return {"active": effective_weights(db_path),
            "baseline": dict(config.SHADOW_WEIGHTS),
            "pending": pending}
