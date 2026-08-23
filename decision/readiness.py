"""
实盘就绪三盏灯（2026-08-20 用户指示）——把"什么时候上实盘"变成机器可判的灯。

  灯1 样本: 平仓样本 ≥ READY_MIN_TRADES 且 Van Tharp SQN ≥ READY_SQN_MIN
            (期望值为正且稳定性达标;SQN 用每笔 R 倍数序列计算)
  灯2 稳定: 最近 READY_CRITICAL_DAYS 天零 critical 级异常(异常中心口径)
  灯3 反哺: trusted 经验 ≥ READY_MIN_TRUSTED 且场景归纳 ≥ READY_MIN_ROLLUPS
            (自我进化层完整跑通过闭环)

三灯全绿 = 可上实盘(按既定计划: 10% 资金起步)。纯只读评估,不改任何状态。
"""
import math
import time

import config


def _r_series(db_path=None):
    """已平仓交易的 R 倍数序列(优先 trade_features.r_multiple,回退 pnl/止损距)。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    out = []
    rows = sdb.q("SELECT id, symbol, direction, entry_price, stop_loss, pnl "
                 "FROM trades WHERE status='closed'", db_path=db_path)
    feats = {r["trade_id"]: r.get("r_multiple")
             for r in sdb.q("SELECT trade_id, r_multiple FROM trade_features",
                            db_path=db_path)}
    for r in rows:
        if feats.get(r["id"]) is not None:
            out.append(feats[r["id"]])
            continue
        e, s, pnl = r["entry_price"], r["stop_loss"], r["pnl"]
        if not e or not s or pnl is None:
            continue
        sd = abs(e - s) / e
        if sd <= 0:
            continue
        out.append(pnl / sd)
    return out


def sqn(rs):
    """Van Tharp SQN = sqrt(N) * mean / std。N 过小返回 0(不做假结论)。"""
    n = len(rs)
    if n < 5:
        return 0.0
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return math.sqrt(n) * mean / std


def readiness_status(db_path=None):
    """返回三盏灯 + 明细。只读。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    now = time.time()

    # 灯1 样本
    rs = _r_series(db_path)
    s = sqn(rs)
    light1 = (len(rs) >= config.READY_MIN_TRADES
              and s >= config.READY_SQN_MIN)
    detail1 = (f"{len(rs)}/{config.READY_MIN_TRADES} 笔平仓样本, "
               f"SQN {s:.2f} (需 ≥ {config.READY_SQN_MIN})")

    # 灯2 稳定: 近 N 天零 critical 级异常 且 无未决异常(clean 状态才算稳定)
    crit = sdb.q("SELECT COUNT(*) c FROM anomalies WHERE severity='critical' "
                 "AND ts > ?", [now - config.READY_CRITICAL_DAYS * 86400],
                 db_path=db_path)[0]["c"]
    open_new = sdb.q("SELECT COUNT(*) c FROM anomalies WHERE status='new' "
                     "AND ts > ?", [now - config.READY_CRITICAL_DAYS * 86400],
                     db_path=db_path)[0]["c"]
    light2 = crit == 0 and open_new == 0
    detail2 = (f"近 {config.READY_CRITICAL_DAYS} 天 critical {crit} 条, "
               f"未决异常 {open_new} 条" + (" ✓" if light2 else "（需都为 0）"))

    # 灯3 反哺
    trusted = sdb.q("SELECT COUNT(*) c FROM lessons WHERE status='trusted'",
                    db_path=db_path)[0]["c"]
    rollups = sdb.q("SELECT COUNT(*) c FROM lesson_rollups",
                    db_path=db_path)[0]["c"]
    light3 = (trusted >= config.READY_MIN_TRUSTED
              and rollups >= config.READY_MIN_ROLLUPS)
    detail3 = (f"trusted 经验 {trusted}/{config.READY_MIN_TRUSTED}, "
               f"场景归纳 {rollups}/{config.READY_MIN_ROLLUPS}")

    all_green = light1 and light2 and light3
    return {
        "ts": round(now, 1),
        "lights": {"samples": light1, "stability": light2,
                   "feedback": light3},
        "overall": all_green,
        "details": {"samples": detail1, "stability": detail2,
                    "feedback": detail3},
    }


def render_lines(st=None):
    """给体检/报告用的三行灯文本。"""
    st = st or readiness_status()
    icons = {True: "🟢", False: "🔴"}
    names = {"samples": "灯1 样本", "stability": "灯2 稳定", "feedback": "灯3 反哺"}
    lines = []
    for k in ("samples", "stability", "feedback"):
        lines.append(f"  {icons[st['lights'][k]]} {names[k]}: {st['details'][k]}")
    lines.append(f"  → 上实盘: {'✅ 三灯全绿,按计划 10% 资金起步' if st['overall'] else '❌ 未就绪'}")
    return lines


if __name__ == "__main__":
    for ln in render_lines():
        print(ln)
