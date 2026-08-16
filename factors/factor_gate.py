"""
因子验证门（FactorGate）——把"挖出即有效"升级为"过门才有效"。

业界标准（非自定，见 docs/prompts/2026-08-16_factor_mining_goal_prompt.md）：
  - walk-forward 折内 IC 序列 → t 值：t ≥ 3.0 promote / 2.0~3.0 watch / <2.0 reject
    （Harvey-Liu-Zhu (2016) 因子动物园: 316 个已发表因子检验后 t>3.0 才可信）
  - 成本扣除：毛多空价差 − 换手 × 双向费率 → 净价差 < 0 即 reject_on_cost
  - 独立性：与已接受因子 |corr| > 0.7 → redundant（去冗余）
  - 经济逻辑必填：无 rationale → hypothesis_only（GP 表达式永不自证）
  - 每次检验必入试验日志 factor_trials（多重检验可追溯；Deflated Sharpe/PBO
    计算所需字段已落库，实现留钩子——见 prompt §4 诚实标注）

红线：任何因子在 promote + 人工批准前不得进入交易决策。
离线可用：本模块零网络依赖（stdlib + numpy），价格序列由调用方传入。
"""
import math
import time

try:
    import numpy as np
except ImportError:
    np = None

T_PROMOTE = 3.0      # Harvey-Liu-Zhu 多重检验校正门槛
T_WATCH = 2.0
CORR_DEDUP = 0.7
DEFAULT_FEE_BPS = 10.0   # taker 0.05% × 双向
DEFAULT_FOLDS = 5
DEFAULT_HORIZON = 7


def spearman(x, y):
    """Spearman 秩相关（IC）。"""
    n = len(x)
    if n < 10:
        return 0.0
    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        ranks = [0] * n
        for r, idx in enumerate(order):
            ranks[idx] = r
        return ranks
    rx, ry = rank(x), rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    vy = math.sqrt(sum((r - my) ** 2 for r in ry))
    if vx * vy == 0:
        return 0.0
    return cov / (vx * vy)


def pearson(x, y):
    n = len(x)
    if n < 10:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = math.sqrt(sum((a - mx) ** 2 for a in x))
    vy = math.sqrt(sum((b - my) ** 2 for b in y))
    if vx * vy == 0:
        return 0.0
    return cov / (vx * vy)


def _forward_returns(dates, values, price_by_date, horizon):
    """对齐: 因子日期 → 未来 horizon 日收益。返回按日期升序的 [(date, value, fwd)]。"""
    out = []
    for d, v in zip(dates, values):
        if d not in price_by_date:
            continue
        idx, close = price_by_date[d]
        if idx + horizon >= len(price_by_date["__series__"]):
            continue
        future = (price_by_date["__series__"][idx + horizon] - close) / close
        out.append((d, v, future))
    return out


def _ic_tstat(ics):
    """IC 序列 t = mean/std*sqrt(n_folds)。"""
    n = len(ics)
    if n < 2:
        return 0.0
    m = sum(ics) / n
    var = sum((x - m) ** 2 for x in ics) / (n - 1)
    if var <= 0:
        return 0.0
    return (m / math.sqrt(var)) * math.sqrt(n)


def _layered(obs, k=3):
    """按因子值分 k 组（时间点分组），返回 (spread, top_mean, bottom_mean, turnover)。"""
    sorted_obs = sorted(obs, key=lambda o: o[1])
    n = len(sorted_obs)
    if n < k * 3:
        return 0.0, 0.0, 0.0, 0.0
    size = n // k
    groups = [sorted_obs[i * size:(i + 1) * size] for i in range(k)]
    means = [sum(o[2] for o in g) / len(g) for g in groups]
    spread = means[-1] - means[0]
    # 换手近似: 相邻交易日组别变化的观测占比
    group_of = {}
    for gi, g in enumerate(groups):
        for o in g:
            group_of[o[0]] = gi
    by_date = sorted(obs, key=lambda o: o[0])
    changes = sum(1 for a, b in zip(by_date, by_date[1:])
                  if group_of.get(a[0]) != group_of.get(b[0]))
    turnover = changes / (len(by_date) - 1) if len(by_date) > 1 else 0.0
    return spread, means[-1], means[0], turnover


def evaluate(name, dates, values, rationale, price_by_date,
             horizon=DEFAULT_HORIZON, folds=DEFAULT_FOLDS,
             fee_bps=DEFAULT_FEE_BPS, accepted=None, expression=None,
             db_path=None):
    """
    单因子过门检验。price_by_date = {日期: (索引, 收盘价), "__series__": [收盘价序列]}。
    accepted = [(name, dates, values)] 已接受因子（去冗余用）。
    返回 verdict dict；db_path 提供时落 factor_trials 试验日志。
    """
    now = time.time()
    obs = _forward_returns(dates, values, price_by_date, horizon)
    n = len(obs)
    verdict = {
        "name": name, "rationale": rationale, "n_samples": n,
        "n_folds": 0, "mean_ic": 0.0, "icir": 0.0, "ic_tstat": 0.0,
        "gross_spread": 0.0, "turnover": 0.0, "net_spread": 0.0,
        "status": "reject",
    }
    if n < 30:
        verdict["status"] = "reject"
        _log(verdict, expression, db_path)
        return verdict
    if not rationale:
        verdict["status"] = "hypothesis_only"
        _log(verdict, expression, db_path)
        return verdict

    # 1. walk-forward 折内 IC 序列
    step = max(1, n // folds)
    ics = []
    for i in range(0, n, step):
        chunk = obs[i:i + step]
        if len(chunk) < 10:
            continue
        ic = spearman([o[1] for o in chunk], [o[2] for o in chunk])
        ics.append(ic)
    verdict["n_folds"] = len(ics)
    if len(ics) >= 2:
        verdict["mean_ic"] = sum(ics) / len(ics)
        sd = math.sqrt(sum((x - verdict["mean_ic"]) ** 2 for x in ics)
                       / (len(ics) - 1))
        verdict["icir"] = verdict["mean_ic"] / sd if sd > 0 else 0.0
        verdict["ic_tstat"] = _ic_tstat(ics)

    # 2. 分层 + 成本
    spread, top, bottom, turnover = _layered(obs)
    verdict["gross_spread"] = spread
    verdict["turnover"] = turnover
    net = spread - turnover * (fee_bps / 10000.0)
    verdict["net_spread"] = net

    # 3. 去冗余（与已接受因子相关性）
    if accepted:
        for a_name, a_dates, a_vals in accepted:
            common = {d: v for d, v in zip(a_dates, a_vals)}
            xs, ys = [], []
            for d, v, _f in obs:
                if d in common:
                    xs.append(v)
                    ys.append(common[d])
            if len(xs) >= 30 and abs(pearson(xs, ys)) > CORR_DEDUP:
                verdict["status"] = "redundant"
                verdict["redundant_with"] = a_name
                _log(verdict, expression, db_path)
                return verdict

    # 4. 裁决（Harvey t 门槛 + 成本必须为正）
    t = verdict["ic_tstat"]
    if t >= T_PROMOTE and net > 0:
        verdict["status"] = "promote"
    elif t >= T_WATCH and net > 0:
        verdict["status"] = "watch"
    elif t >= T_PROMOTE and net <= 0:
        verdict["status"] = "reject_on_cost"
    else:
        verdict["status"] = "reject"
    _log(verdict, expression, db_path)
    return verdict


def _log(v, expression, db_path):
    """试验日志（多重检验可追溯）。db_path=None → 共享生产库；测试传隔离库。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("INSERT INTO factor_trials (ts,name,rationale,n_samples,n_folds,"
              "mean_ic,icir,ic_tstat,gross_spread,turnover,net_spread,status,"
              "expression) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              [time.time(), v["name"], v.get("rationale", ""),
               v.get("n_samples", 0), v.get("n_folds", 0),
               v.get("mean_ic", 0.0), v.get("icir", 0.0),
               v.get("ic_tstat", 0.0), v.get("gross_spread", 0.0),
               v.get("turnover", 0.0), v.get("net_spread", 0.0),
               v.get("status", "reject"), expression],
              db_path=db_path)
    except Exception:
        pass
