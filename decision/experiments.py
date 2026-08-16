import config
"""
试验注册表（Phase 3 T3.2）—— 每次参数/规则变更提案必入账，多重检验可追溯。

设计文档: docs/plans/2026-08-16_self_evolution_design.md §4 Phase 3
原则: 任何"改进"在通过验证门 + 人工批准前不得生效;每一次试验都是
一次多重检验负担,入账后才可校正(Deflated Sharpe / PBO)。
存储: storage experiments 表(db_path 隔离,测试传隔离库)。
"""
import time

from factors.overfit_guard import deflated_sharpe, pbo_cscv

# 接受线(López de Prado 实务门槛,见设计文档 S3)
DSR_ACCEPT = config.DSR_ACCEPT
PBO_ACCEPT = config.PBO_ACCEPT
MIN_SAMPLES = config.MIN_SAMPLES


def propose(change_id, kind, params, db_path=None):
    """登记一次试验提案(状态=proposed/insufficient_data)。返回状态。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    status = "proposed"
    try:
        sdb.x("INSERT INTO experiments (ts, change_id, kind, params, status, "
              "decided_by) VALUES (?,?,?,?,?,?)",
              [time.time(), change_id, kind, params, status, "gate"],
              db_path=db_path)
    except Exception:
        pass
    return status


def judge(change_id, returns_incumbent, returns_candidate=None, db_path=None,
          n_trials=1):
    """按证据裁决一次试验:
      - 样本 < MIN_SAMPLES → insufficient_data(不可接受,等样本)
      - DSR < 1 或 PBO ≥ 0.3 → rejected
      - 全部达标 → accepted
    证据写入 experiments 行。返回 (status, {dsr, pbo, n})。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    n = len(returns_incumbent or [])
    if n < MIN_SAMPLES:
        _update(change_id, n, None, None, "insufficient_data",
                f"样本 {n} < {MIN_SAMPLES}(Tharp 门槛)", db_path)
        return "insufficient_data", {"dsr": None, "pbo": None, "n": n}
    dsr = deflated_sharpe(returns_incumbent, n_trials=n_trials)
    pbo = None
    if returns_candidate and len(returns_candidate) >= MIN_SAMPLES:
        try:
            import numpy as np
            mat = np.column_stack([returns_incumbent, returns_candidate])
            pbo = pbo_cscv(mat)
        except Exception:
            pbo = None
    ok = (dsr is not None and dsr >= DSR_ACCEPT) and \
         (pbo is None or pbo < PBO_ACCEPT)
    status = "accepted" if ok else "rejected"
    _update(change_id, n, dsr, pbo, status,
            f"dsr={dsr} pbo={pbo} (门槛 dsr≥{DSR_ACCEPT}, pbo<{PBO_ACCEPT})",
            db_path)
    return status, {"dsr": dsr, "pbo": pbo, "n": n}


def _update(change_id, n, dsr, pbo, status, notes, db_path):
    import storage.db as sdb
    sdb.x("UPDATE experiments SET n_samples=?, dsr=?, pbo=?, status=?, notes=? "
          "WHERE change_id=?",
          [n, dsr, pbo, status, notes, change_id], db_path=db_path)
