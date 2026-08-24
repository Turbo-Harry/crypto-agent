"""T2 15m 策略的 4h/1m 首触、歧义、超时与 R 标准化。"""
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import storage.db as sdb
import config
from decision.signal_outcomes import (persist_outcome, settle_barrier_grid,
                                      settle_path, settle_pending)
from factors.exit_barrier_research import evaluate_barrier_rows
from engines.signal_sampling import record_signal_sample
from exchange.fake_adapter import FakeAdapter
from exchange.models import Candle

passed = failed = 0
EVENT = 1_700_000_040.0
EVENT_MS = int(EVENT * 1000)


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def sample(direction="long", sid="sig_test"):
    return {"signal_id": sid, "direction": direction, "event_ts": EVENT,
            "entry": 100.0, "stop": 90.0 if direction == "long" else 110.0,
            "tp": 120.0 if direction == "long" else 80.0,
            "horizon_hours": 4}


def bars():
    return [Candle(EVENT_MS + i * 60_000, 100, 101, 99, 100, 1)
            for i in range(240)]


def main():
    global passed, failed
    print("== long 首触与歧义 ==")
    rows = bars()
    rows[10] = Candle(rows[10].ts, 100, 121, 99, 119, 1)
    out = settle_path(sample(), rows)
    check("long TP first", out["tp_first"] == 1 and out["sl_first"] == 0,
          str(out))
    check("long MFE 精确", math.isclose(out["mfe_r"], 2.1), str(out))
    check("long MAE 精确", math.isclose(out["mae_r"], 0.1), str(out))

    grid_rows = bars()
    grid_rows[10] = Candle(grid_rows[10].ts, 100, 121, 99, 119, 1)
    grid = settle_barrier_grid(sample() | {"atr": 10.0}, grid_rows)
    check("盈亏比与止损 ATR 尺度分离",
          grid["stop075_rr200"]["stop_atr_mult"] == 0.75 and
          grid["stop075_rr200"]["reward_risk"] == 2.0 and
          math.isclose(grid["stop075_rr200"]["target_atr_mult"], 1.5),
          str(grid))
    check("生产基线 1ATR+2比1 在研究网格保留",
          grid["stop100_rr200"]["stop"] == 90.0 and
          grid["stop100_rr200"]["target"] == 120.0 and
          grid["stop100_rr200"]["tp_first"] == 1,
          str(grid["stop100_rr200"]))
    research_rows = []
    for idx in range(360):
        event_ts = EVENT + idx * 18_000
        variants = {name: {"net_pnl_r": 0.3}
                    for name, _, _ in config.EXIT_BARRIER_RESEARCH_GRID}
        research_rows.append({"signal_id": f"barrier-{idx}",
                              "event_ts": event_ts, "kline_ts": event_ts,
                              "label_end_ts": event_ts + 14_400,
                              "barriers": variants})
    barrier_eval = evaluate_barrier_rows(research_rows)
    check("多障碍评价逐方案时间验证且不自动生产生效",
          len(barrier_eval) == len(config.EXIT_BARRIER_RESEARCH_GRID) and
          all(item["status"] == "eligible_for_model_challenge" and
              item["research_only"] for item in barrier_eval),
          str(barrier_eval))

    rows = bars()
    rows[5] = Candle(rows[5].ts, 100, 121, 89, 100, 1)
    out = settle_path(sample(sid="sig_amb"), rows)
    check("同 bar 双触按 SL", out["sl_first"] == 1 and out["tp_first"] == 0)
    check("同 bar 保留 ambiguous", out["ambiguous"] == 1)
    check("双触按 -1R 结算", math.isclose(out["pnl_r"], -1.0), str(out))

    print("== short 与 timeout ==")
    rows = bars()
    rows[8] = Candle(rows[8].ts, 100, 101, 79, 81, 1)
    out = settle_path(sample("short", "sig_short"), rows)
    check("short TP first", out["tp_first"] == 1 and out["sl_first"] == 0,
          str(out))
    check("short MFE 精确", math.isclose(out["mfe_r"], 2.1), str(out))

    rows = bars()
    rows[-1] = Candle(rows[-1].ts, 100, 106, 99, 105, 1)
    out = settle_path(sample(sid="sig_timeout"), rows)
    check("timeout 标签", out["timeout"] == 1 and not out["tp_first"] and
          not out["sl_first"], str(out))
    check("timeout 终值 0.5R", math.isclose(out["pnl_r"], 0.5), str(out))
    check("路径不足保持 pending", settle_path(sample(), rows[:100]) is None)
    gap_rows = rows[:100] + rows[101:]
    check("中间缺一分钟也保持 pending，不只看总行数",
          settle_path(sample(), gap_rows) is None)

    print("== 幂等落库与自动 sweep ==")
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "outcomes.db")
        sdb.init_db(db)
        sig = {"dir": "long", "entry": 100.0, "stop": 90.0, "tp": 120.0,
               "atr": 10.0, "kline_ts": EVENT_MS - 3_600_000,
               "shadow_dims": {name: 0.5 for name in __import__("config").SHADOW_DIMS}}
        sid, _ = record_signal_sample("BTC", sig, "swap", db_path=db,
                                      event_ts=EVENT)
        fake = FakeAdapter()
        full = bars()
        full[3] = Candle(full[3].ts, 100, 121, 99, 120, 1)
        fake.candles["BTC-USDT-SWAP"] = full
        stats = settle_pending(fake, db_path=db, now=EVENT + 4 * 3600 + 1)
        check("到期候选自动结算", stats["settled"] == 1, str(stats))
        stored = sdb.q1("SELECT * FROM signal_outcomes WHERE signal_id=?", [sid], db)
        check("结果关联 signal_id", stored and stored["tp_first"] == 1, str(stored))
        persist_outcome(dict(stored), db_path=db)
        count = sdb.q1("SELECT COUNT(*) n FROM signal_outcomes", db_path=db)["n"]
        check("重复写保持一行", count == 1, str(count))
        again = settle_pending(fake, db_path=db, now=EVENT + 8 * 3600)
        check("重复 sweep 幂等", again["scanned"] == 0, str(again))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
