"""
扫描尺子进化 — 只动 REJECT_WICK_RATIO 这一根尺子。

闭环（2026-08-20 用户拍板）：未触发复盘提案 → 影子记账（绝不下单）
→ DSR 验证门证明更好 → 人工批准才写 kv 覆盖。永不自动改尺子。

config.REJECT_WICK_RATIO 永远是基线/回滚值；活体覆盖只存在 kv。
放宽方向的证据来自「现役没出信号、候选影线比会出信号」之后的 1H 路径盈亏
（同根 K 线既打止盈又打止损时按止损计，影子不美化）。
"""
import json
import time
from collections import Counter

import config
from decision.experiments import propose, judge

SCAN_EVOLVE_ENABLED = config.SCAN_EVOLVE_ENABLED
SCAN_EVOLVE_KV_KEY = config.SCAN_EVOLVE_KV_KEY
SCAN_EVOLVE_WICK_STEP = config.SCAN_EVOLVE_WICK_STEP
SCAN_EVOLVE_WICK_FLOOR = config.SCAN_EVOLVE_WICK_FLOOR
SCAN_EVOLVE_PROFILE_HOURS = config.SCAN_EVOLVE_PROFILE_HOURS
SCAN_EVOLVE_SETTLE_BARS = config.SCAN_EVOLVE_SETTLE_BARS
SCAN_EVOLVE_STRATEGY = config.SCAN_EVOLVE_STRATEGY
MIN_SAMPLES = config.MIN_SAMPLES
GATE_MIN_EDGE = config.GATE_MIN_EDGE

_OPEN = ("proposed", "insufficient_data", "accepted")


def effective_wick_ratio(db_path=None):
    """活体影线比：批准过的 kv 覆盖，否则 config 基线。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key=?",
                     [SCAN_EVOLVE_KV_KEY], db_path=db_path)
        if row and row.get("value") not in (None, ""):
            v = float(row["value"])
            if SCAN_EVOLVE_WICK_FLOOR <= v <= config.REJECT_WICK_RATIO:
                return v
    except Exception:
        pass
    return config.REJECT_WICK_RATIO


def candidate_wick(incumbent=None):
    """下一档候选：现役 × STEP，夹在 [FLOOR, 现役) —— 只放宽、不收紧。"""
    inc = config.REJECT_WICK_RATIO if incumbent is None else float(incumbent)
    cand = round(inc * SCAN_EVOLVE_WICK_STEP, 2)
    cand = max(SCAN_EVOLVE_WICK_FLOOR, cand)
    return cand


def _open_row(db_path=None):
    import storage.db as sdb
    sdb.init_db(db_path)
    marks = ",".join("?" * len(_OPEN))
    return sdb.q1(
        f"SELECT * FROM experiments WHERE kind='scan_wick' "
        f"AND status IN ({marks}) ORDER BY ts DESC LIMIT 1",
        list(_OPEN), db_path=db_path)


def active_candidate(db_path=None):
    """正在影子观察的候选（accepted 已停记新影子，只等批准）。"""
    row = _open_row(db_path)
    if not row or row["status"] == "accepted":
        return None
    try:
        params = json.loads(row["params"] or "{}")
        wick = float(params["to"])
    except Exception:
        return None
    if wick >= effective_wick_ratio(db_path):
        return None
    return {"change_id": row["change_id"], "wick": wick,
            "status": row["status"], "from": params.get("from")}


def _r1_triggered(rows):
    """R1：近失率高且主瓶颈=wick。与 generate_feedback 同口径，避免 decision→tools。"""
    n = len(rows)
    if n < config.FB_MIN_PROFILES:
        return False, f"n={n}"
    bn = Counter(r["bottleneck"] for r in rows)
    top, top_n = bn.most_common(1)[0]
    near = sum(1 for r in rows if r.get("near_miss")) / n
    evidence = f"近失率 {near:.0%}, 主瓶颈 {top} {top_n / n:.0%}"
    return (near >= config.FB_NEAR_MISS_RATE and top == "wick"), evidence


def maybe_propose(db_path=None):
    """读近窗 signal_profiles，R1 触发则登记 scan_wick 试验（已有开放试验则跳过）。"""
    if not SCAN_EVOLVE_ENABLED:
        return None
    if _open_row(db_path):
        return None
    import storage.db as sdb
    sdb.init_db(db_path)
    since = time.time() - SCAN_EVOLVE_PROFILE_HOURS * 3600
    rows = sdb.q("SELECT bottleneck, near_miss FROM signal_profiles WHERE ts > ?",
                 [since], db_path=db_path)
    ok, evidence = _r1_triggered(rows)
    if not ok:
        return None
    inc = effective_wick_ratio(db_path)
    cand = candidate_wick(inc)
    if cand >= inc:
        return None
    change_id = f"scan_wick_{inc:.2f}_to_{cand:.2f}"
    dup = sdb.q1("SELECT id FROM experiments WHERE change_id=? AND status IN "
                 "('proposed','insufficient_data','accepted','applied')",
                 [change_id], db_path=db_path)
    if dup:
        return None
    params = json.dumps({"param": "REJECT_WICK_RATIO", "from": inc, "to": cand,
                         "rule": "R1", "evidence": evidence},
                        ensure_ascii=False)
    propose(change_id, "scan_wick", params, db_path=db_path)
    return change_id


def path_pnl(direction, entry, stop, tp, bars):
    """随后 K 线的假设路径盈亏。同根既止盈又止损 → 按止损（影子不美化）。
    bars: 有 high/low/close 的对象列表（升序，不含入场那根）。
    返回 (pnl, reason, done)；样本不够且未触线 → done=False。"""
    if not entry:
        return None, None, False
    n = len(bars)
    for b in bars:
        high = b.high if hasattr(b, "high") else b["high"]
        low = b.low if hasattr(b, "low") else b["low"]
        if direction == "long":
            if low <= stop:
                return (stop - entry) / entry, "stop", True
            if high >= tp:
                return (tp - entry) / entry, "tp", True
        else:
            if high >= stop:
                return (stop - entry) / entry, "stop", True
            if low <= tp:
                return (tp - entry) / entry, "tp", True
    if n >= SCAN_EVOLVE_SETTLE_BARS:
        last = bars[-1]
        close = last.close if hasattr(last, "close") else last["close"]
        if direction == "long":
            return (close - entry) / entry, "timeout", True
        return (entry - close) / entry, "timeout", True
    return None, None, False


def settle_shadows(exchange, db_path=None, inst_id_fn=None):
    """把到期的 A_wick 假想单按后续 1H 路径结算。失败不影响扫描。"""
    if not SCAN_EVOLVE_ENABLED or exchange is None:
        return 0
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q("SELECT * FROM shadow_signals WHERE strategy=? AND status=?",
                 [SCAN_EVOLVE_STRATEGY, "hypothetical"], db_path=db_path)
    n = 0
    for r in rows:
        try:
            base = r["base"]
            inst = inst_id_fn(base) if inst_id_fn else f"{base}-USDT-SWAP"
            candles = exchange.fetch_candles(
                inst, "1H", limit=SCAN_EVOLVE_SETTLE_BARS + 10)
            future = [c for c in candles if c.ts > (r["kline_ts"] or 0)]
            pnl, reason, done = path_pnl(
                r["dir"], r["entry"], r["stop"], r["tp"], future)
            if not done:
                continue
            sdb.x("UPDATE shadow_signals SET status=?, pnl=?, exit_reason=?, "
                  "settled_ts=? WHERE id=?",
                  ["settled", pnl, reason, time.time(), r["id"]],
                  db_path=db_path)
            n += 1
        except Exception:
            continue
    return n


def maybe_judge(db_path=None):
    """结算样本满 MIN_SAMPLES 后过 DSR 门。accepted ≠ 生效。"""
    row = _open_row(db_path)
    if not row or row["status"] == "accepted":
        return None
    import storage.db as sdb
    settled = sdb.q(
        "SELECT pnl FROM shadow_signals WHERE strategy=? AND status=?",
        [SCAN_EVOLVE_STRATEGY, "settled"], db_path=db_path)
    pnls = [float(x["pnl"]) for x in settled if x.get("pnl") is not None]
    if len(pnls) < MIN_SAMPLES:
        judge(row["change_id"], pnls, db_path=db_path)
        return "insufficient_data"
    mean = sum(pnls) / len(pnls)
    if mean < GATE_MIN_EDGE:
        import storage.db as sdb2
        sdb2.x("UPDATE experiments SET n_samples=?, status=?, notes=? "
               "WHERE change_id=?",
               [len(pnls), "rejected",
                f"影子均盈 {mean:+.4f} < 最小优势 {GATE_MIN_EDGE}（空仓更好）",
                row["change_id"]], db_path=db_path)
        return "rejected"
    status, _ = judge(row["change_id"], pnls, db_path=db_path, n_trials=1)
    return status


def approve(change_id=None, db_path=None):
    """人工批准：仅 accepted 可写 kv。这是尺子生效的唯一写入口。永不自动改尺子。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    if change_id:
        row = sdb.q1("SELECT * FROM experiments WHERE change_id=?",
                     [change_id], db_path=db_path)
    else:
        row = sdb.q1("SELECT * FROM experiments WHERE kind='scan_wick' "
                     "AND status='accepted' ORDER BY ts DESC LIMIT 1",
                     db_path=db_path)
    if not row or row["status"] != "accepted":
        return False, "没有通过验证门的扫描提案，不能批准"
    try:
        params = json.loads(row["params"] or "{}")
        new_wick = float(params["to"])
    except Exception:
        return False, "提案参数无法解析"
    if new_wick < SCAN_EVOLVE_WICK_FLOOR:
        return False, f"候选 {new_wick} 低于下限 {SCAN_EVOLVE_WICK_FLOOR}"
    sdb.x("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
          [SCAN_EVOLVE_KV_KEY, str(new_wick), time.time()], db_path=db_path)
    sdb.x("UPDATE experiments SET status=?, decided_by=?, notes=? WHERE change_id=?",
          ["applied", "human",
           f"人工批准 REJECT_WICK_RATIO {params.get('from')} → {new_wick}",
           row["change_id"]], db_path=db_path)
    return True, (f"已生效 REJECT_WICK_RATIO {params.get('from')} → {new_wick}"
                  "（config 基线未改，随时可回滚）")


def rollback(db_path=None):
    """撤掉 kv 覆盖，回到 config.REJECT_WICK_RATIO。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    sdb.x("DELETE FROM kv WHERE key=?", [SCAN_EVOLVE_KV_KEY], db_path=db_path)
    sdb.x("UPDATE experiments SET status=?, decided_by=?, notes=? "
          "WHERE kind='scan_wick' AND status='applied'",
          ["rolled_back", "human", "人工回滚到 config 基线"], db_path=db_path)
    return True, f"已回滚，影线比恢复 {config.REJECT_WICK_RATIO}"


def snapshot(db_path=None):
    """HTTP/看板用的只读快照。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    inc = config.REJECT_WICK_RATIO
    eff = effective_wick_ratio(db_path)
    row = _open_row(db_path)
    applied = sdb.q1("SELECT * FROM experiments WHERE kind='scan_wick' "
                     "AND status='applied' ORDER BY ts DESC LIMIT 1",
                     db_path=db_path)
    cand = None
    change_id = None
    status = None
    evidence = ""
    if row:
        change_id = row["change_id"]
        status = row["status"]
        try:
            p = json.loads(row["params"] or "{}")
            cand = float(p.get("to"))
            evidence = p.get("evidence") or ""
        except Exception:
            pass
    elif applied:
        change_id = applied["change_id"]
        status = applied["status"]
    n_open = sdb.q1("SELECT COUNT(*) c FROM shadow_signals "
                    "WHERE strategy=? AND status=?",
                    [SCAN_EVOLVE_STRATEGY, "hypothetical"], db_path=db_path)
    n_set = sdb.q1("SELECT COUNT(*) c FROM shadow_signals "
                   "WHERE strategy=? AND status=?",
                   [SCAN_EVOLVE_STRATEGY, "settled"], db_path=db_path)
    avg = sdb.q1("SELECT AVG(pnl) m FROM shadow_signals "
                 "WHERE strategy=? AND status=?",
                 [SCAN_EVOLVE_STRATEGY, "settled"], db_path=db_path)
    needs = bool(row and row["status"] == "accepted")
    if needs:
        msg = "影子验证已通过，等待你批准后才会改扫描尺子"
    elif status == "applied":
        msg = f"活体影线比 {eff}（基线 {inc}），可用回滚恢复"
    elif status in ("proposed", "insufficient_data"):
        msg = "正在影子观察，不下单、不改尺子"
    elif status == "rejected":
        msg = "影子验证未超过现役（空仓），尺子不变"
    else:
        msg = "暂无扫描尺子提案"
    return {
        "enabled": SCAN_EVOLVE_ENABLED,
        "incumbent_wick": inc,
        "effective_wick": eff,
        "candidate_wick": cand,
        "change_id": change_id,
        "status": status,
        "evidence": evidence,
        "shadow_open": int((n_open or {}).get("c") or 0),
        "shadow_settled": int((n_set or {}).get("c") or 0),
        "settled_mean_pnl": None if not avg or avg.get("m") is None
        else round(float(avg["m"]), 5),
        "needs_approval": needs,
        "message": msg,
    }


def tick(trader):
    """每轮扫描调用一次：提案 / 结算 / 验证门。任何异常不拖垮扫描。"""
    if not SCAN_EVOLVE_ENABLED or trader is None:
        return
    db = getattr(trader, "_db_path", None)
    try:
        maybe_propose(db)
    except Exception as e:
        print(f"扫描进化提案异常: {e}")
    try:
        def _inst(base):
            v = trader.exchange.venue_for(base) or "swap"
            return trader._inst_id(base, v)

        settle_shadows(trader.exchange, db, inst_id_fn=_inst)
    except Exception as e:
        print(f"扫描影子结算异常: {e}")
    try:
        before = (_open_row(db) or {}).get("status")
        st = maybe_judge(db)
        if st == "accepted" and before != "accepted":
            snap = snapshot(db)
            msg = (f"🧬 扫描尺子影子验证通过\n"
                   f"REJECT_WICK_RATIO **{snap['incumbent_wick']} → "
                   f"{snap['candidate_wick']}**\n"
                   f"影子 {snap['shadow_settled']} 笔均盈 "
                   f"{snap['settled_mean_pnl']}\n"
                   f"不会自动改尺子，请 GET /scan/evolve 确认后 "
                   f"POST /scan/evolve/approve")
            print(msg)
            notify = getattr(trader, "_notify", None)
            if callable(notify):
                notify(msg)
        elif st == "rejected":
            print("扫描尺子影子验证未通过，保持现役影线比")
    except Exception as e:
        print(f"扫描进化验证门异常: {e}")
