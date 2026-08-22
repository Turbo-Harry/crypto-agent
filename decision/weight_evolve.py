# -*- coding: utf-8 -*-
"""
权重进化（2026-08-23 用户问"会根据历史经验调整权重吗",后指示"不加批准,自动生效"）——
证据 → 自动生效 → 观察期 → 自动回滚:

  1. 证据: 每笔平仓已有 shadow_dims(6 维子分) + pnl。propose() 逐维算
     IC(子分与盈亏的皮尔逊相关),样本 ≥ WEIGHT_EVOLVE_MIN_SAMPLES 且
     |IC| ≥ WEIGHT_EVOLVE_MIN_IC 的维度才有资格动。
  2. 自动生效(WEIGHT_EVOLVE_AUTO_APPLY): 强维 +STEP、弱维 -STEP
     (单维变动 ≤ MAX_SHIFT),归一化到 1,直接写 kv 'shadow_weights'。
     评分时 effective_weights() 优先读 kv,否则 config.SHADOW_WEIGHTS。
  3. 观察期节流: 生效后新平仓 < OBSERVE_MIN 笔 → 暂不动(防小时级抖动)。
  4. 自动回滚: 增权维度在观察期 IC 转负(≤ ROLLBACK_IC) → 证据是噪声,
     自动回 config 基线并记 auto_rolled_back。
  5. 人工接口保留: approve()/rollback() 随时可手动覆盖。
双向共享: 权重 kv 属于策略状态,策略同步会把生效结果镜像到对端实例。
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
    """按已平仓样本算逐维 IC。返回 (status, message, evidence)。
    2026-08-23 用户指示"不加批准,自动生效": 证据达标直接写 kv 生效
    (status='auto_applied');观察期未满返回 observing;IC 转负自动回滚。"""
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
    change_id = f"w-{time.strftime('%Y%m%d%H%M%S')}"
    now = time.time()
    # ---- 2026-08-23 用户指示"不加批准,自动生效": 观察期节流 + 自动回滚 ----
    last_app = sdb.q1("SELECT * FROM experiments WHERE kind='weight_evolve' "
                      "AND status IN ('auto_applied','applied') "
                      "ORDER BY ts DESC LIMIT 1", db_path=db_path)
    if last_app:
        post_rows = sdb.q("SELECT pnl, shadow_dims, exit_time FROM trades "
                          "WHERE status='closed' AND pnl IS NOT NULL "
                          "AND shadow_dims IS NOT NULL AND shadow_dims != '' "
                          "AND exit_time > ?", [last_app["ts"]], db_path=db_path)
        # 自动回滚: 上次增权维度在观察期 IC 转负 → 证据是噪声,回基线
        try:
            _params = json.loads(last_app["params"] or "{}")
            _from, _to = _params.get("from") or {}, _params.get("to") or {}
            _boosted = [d for d in DIMS
                        if float(_to.get(d, 0)) > float(_from.get(d, 0))]
            if _boosted and len(post_rows) >= max(2, config.WEIGHT_EVOLVE_OBSERVE_MIN):
                _pnls = [float(r["pnl"]) for r in post_rows]
                _bad = []
                for d in _boosted:
                    try:
                        _xs = [float(json.loads(r["shadow_dims"] or {})
                                     .get(d, 0.5)) for r in post_rows]
                    except Exception:
                        _xs = []
                    _post_ic = _ic(_xs, _pnls[:len(_xs)])
                    if (_post_ic is not None
                            and _post_ic <= config.WEIGHT_EVOLVE_ROLLBACK_IC):
                        _bad.append((d, round(_post_ic, 4)))
                if _bad:
                    rollback(db_path=db_path)
                    sdb.x("INSERT INTO experiments (change_id, kind, params, "
                          "status, ts, decided_by, notes) VALUES (?,?,?,?,?,?,?)",
                          [f"w-{time.strftime('%Y%m%d%H%M%S')}", "weight_evolve",
                           json.dumps({"from": cand, "to": dict(config.SHADOW_WEIGHTS),
                                       "evidence": evidence}, ensure_ascii=False),
                           "auto_rolled_back", now, "auto",
                           f"观察期 IC 转负 {_bad},自动回滚基线"], db_path=db_path)
                    return ("auto_rolled_back",
                            f"增权维度观察期 IC 转负 {_bad},自动回滚基线", evidence)
        except Exception:
            pass
        # 观察期节流: 生效后新平仓不足 OBSERVE_MIN 笔,不允许下一次变动
        if len(post_rows) < config.WEIGHT_EVOLVE_OBSERVE_MIN and not force:
            return ("observing",
                    f"观察期: 上次生效后仅 {len(post_rows)}/"
                    f"{config.WEIGHT_EVOLVE_OBSERVE_MIN} 笔新平仓,暂不动", evidence)
    try:
        if getattr(config, "WEIGHT_EVOLVE_AUTO_APPLY", False):
            sdb.x("INSERT OR REPLACE INTO kv (key, value, updated_at) "
                  "VALUES (?,?,?)",
                  [config.WEIGHT_EVOLVE_KV_KEY,
                   json.dumps(cand, ensure_ascii=False), now], db_path=db_path)
            status = "auto_applied"
        else:
            status = "accepted"     # 开关关掉自动 → 回提案待批准模式
        sdb.x("INSERT INTO experiments (change_id, kind, params, status, ts, "
              "decided_by, notes) VALUES (?,?,?,?,?,?,?)",
              [change_id, "weight_evolve",
               json.dumps({"from": base, "to": cand,
                           "evidence": evidence}, ensure_ascii=False),
               status, now,
               "auto" if status == "auto_applied" else None,
               json.dumps({"strong": strong, "weak": weak},
                          ensure_ascii=False)], db_path=db_path)
    except Exception:
        pass
    if status == "auto_applied":
        msg = (f"权重自动生效: {'/'.join(strong)} 增权 · "
               f"{'/'.join(weak) or '无'} 减权 → {cand}")
    else:
        msg = f"提案 {change_id}: 证据达标待批准"
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
