# -*- coding: utf-8 -*-
"""
策略状态共享（2026-08-23 用户指示"策略也保持一致,反哺策略"）——
双实例的策略演化状态互相同步,决策用同一套反哺产物:

  1. thresholds[key='dir']: 决策阈值 + 校准样本(score→pnl 记录)。
     双向合并: 样本按 (score, round(pnl,6)) 去重取并集(每实例只记录
     自己的交易,并集 = 两实例全部证据);threshold 取 updated_at 新者
     (进化门晋升发生在哪一侧,另一侧跟随,保持同一有效阈值)。
  2. kv scan_evolve.*: 扫描尺子进化状态(REJECT_WICK_RATIO 影子/批准),
     按 updated_at 新者镜像。

与经验共享同一套节奏(启动 + 每小时,EXPERIENCE_PEER_DB 指对端库)。
幂等可反复跑;只读对端、只写本地。
"""
import json
import os

import config
MAX_RECORDS = config.STRATEGY_SYNC_MAX_RECORDS   # 与 ThresholdLearner.max_history 一致


def sync_strategy(local_db_path=None, peer_db_path=None):
    """把对端实例的策略演化状态合并进本地库。返回统计 dict。"""
    import storage.db as sdb
    res = {"records_added": 0, "threshold_updated": False, "kv_synced": 0}
    if not peer_db_path or not os.path.exists(peer_db_path):
        return res
    sdb.init_db(local_db_path)
    sdb.init_db(peer_db_path)
    try:
        peer_row = sdb.q1("SELECT threshold, records, updated_at FROM thresholds "
                          "WHERE key='dir'", db_path=peer_db_path)
        if peer_row:
            local_row = sdb.q1("SELECT threshold, records, updated_at "
                               "FROM thresholds WHERE key='dir'",
                               db_path=local_db_path)
            if local_row is None:
                sdb.x("INSERT OR REPLACE INTO thresholds (key, threshold, records, "
                      "updated_at) VALUES ('dir', ?, ?, ?)",
                      [peer_row["threshold"], peer_row["records"],
                       peer_row["updated_at"] or 0],
                      db_path=local_db_path)
                res["records_added"] = len(_parse(peer_row["records"]))
            else:
                l_rec = _parse(local_row["records"])
                p_rec = _parse(peer_row["records"])
                seen = {_rkey(r) for r in l_rec}
                added = 0
                for r in p_rec:
                    if _rkey(r) not in seen:
                        l_rec.append(r)
                        seen.add(_rkey(r))
                        added += 1
                merged = json.dumps(l_rec[-MAX_RECORDS:], ensure_ascii=False)
                # threshold: 谁新听谁(进化门晋升/回滚的时间戳裁决)
                lt, pt = (local_row["updated_at"] or 0), (peer_row["updated_at"] or 0)
                if pt > lt:
                    threshold = peer_row["threshold"]
                    updated = True
                    upd_at = pt
                else:
                    threshold = local_row["threshold"]
                    updated = False
                    upd_at = lt
                sdb.x("INSERT OR REPLACE INTO thresholds (key, threshold, records, "
                      "updated_at) VALUES ('dir', ?, ?, ?)",
                      [threshold, merged, upd_at], db_path=local_db_path)
                res["records_added"] = added
                res["threshold_updated"] = updated
        # kv: scan_evolve.* 按 updated_at 新者镜像
        for r in sdb.q("SELECT key, value, updated_at FROM kv "
                       "WHERE key LIKE 'scan_evolve.%'", db_path=peer_db_path):
            cur = sdb.q1("SELECT updated_at FROM kv WHERE key=?",
                         [r["key"]], db_path=local_db_path)
            if cur is None or (r["updated_at"] or 0) > (cur["updated_at"] or 0):
                sdb.x("INSERT OR REPLACE INTO kv (key, value, updated_at) "
                      "VALUES (?,?,?)",
                      [r["key"], r["value"], r["updated_at"] or 0],
                      db_path=local_db_path)
                res["kv_synced"] += 1
    except Exception:
        pass
    return res


def _parse(records):
    try:
        rows = json.loads(records or "[]")
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []


def _rkey(r):
    try:
        return (float(r.get("score", 0)), round(float(r.get("pnl", 0)), 6),
                bool(r.get("pnl_estimated")))
    except Exception:
        return (0.0, 0.0, False)
