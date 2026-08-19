"""扫描尺子进化（影线比）离线单测。FakeAdapter，不触网、不碰活体库。

覆盖：
  1. 未批准时活体影线比 = config 基线
  2. R1 提案 → 影子记录（不下单）→ 路径结算
  3. 样本不足不能批准；验证门通过后批准才写 kv
  4. 回滚恢复基线
  5. 同根既止盈又止损按止损计（影子不美化）

运行: PYTHONPATH=lib python3 tests/test_scan_evolve.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import config
from exchange.fake_adapter import FakeAdapter
from exchange.models import Candle
from engines.directional_trader import DirectionalTrader
from execution.trade_journal import TradeJournal
from execution.position_ownership import PositionLedger
from decision.threshold_learning import ThresholdLearner
from decision.experience_scoring import ScoredExperience
from decision import scan_evolve as se
import storage.db as sdb

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if ok:
        passed += 1
    else:
        failed += 1


def _db():
    tmp = tempfile.mkdtemp(prefix="tst_se_")
    path = os.path.join(tmp, "t.db")
    sdb.init_db(path)
    return path


def _make_trader(db, candles=None):
    fake = FakeAdapter(usdt_free=10_000.0)
    if candles:
        fake.candles["BTC-USDT-SWAP"] = candles
        fake.last_prices["BTC-USDT-SWAP"] = candles[-1].close
        fake.last_prices["BTC-USDT"] = candles[-1].close
    tmp = os.path.dirname(db)
    dt = DirectionalTrader(exchange=fake, rt=None, db_path=db)
    dt.journal = TradeJournal(path=os.path.join(tmp, "j.json"))
    dt.ledger = PositionLedger(path=os.path.join(tmp, "l.json"),
                               lock_path=os.path.join(tmp, "l.lock"))
    dt.threshold_learner = ThresholdLearner(
        path="test", db_path=os.path.join(tmp, "th.db"),
        initial_threshold=config.THRESHOLD_INITIAL)
    dt.exp_bank = ScoredExperience(path=os.path.join(tmp, "e.json"))
    dt.evolver.bank = __import__(
        "engines.directional_trader", fromlist=["_ExpAdapter"]
    )._ExpAdapter(dt.exp_bank)
    dt.watchlist = ["BTC"]
    dt.watch_scores = {"BTC": 0.9}
    dt._watch_date = time.strftime("%Y-%m-%d")
    dt._last_watch_refresh = time.time()
    dt.signal_cool = {}
    return dt, fake


def make_trend_candles(n=100, base=100.0, drift=0.1, wick_over_body=0.95):
    """缓涨 + 最后一根回踩；影线/实体 = wick_over_body（0.95 卡在 1.0 与 0.9 之间）。"""
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        close = base + i * drift
        open_ = close - drift * 0.8
        out.append(Candle(ts=ts + i * 3600_000, open=open_, high=close + 0.5,
                          low=open_ - 0.5, close=close, volume=1000))
    last = out[-1]
    body = 2.0
    wick = body * wick_over_body
    out[-1] = Candle(ts=last.ts, open=last.close - body, high=last.close + 0.1,
                     low=last.close - body - wick, close=last.close, volume=1000)
    return out


def _insert_profiles(db, n=24, bottleneck="wick", near=1):
    now = time.time()
    for _ in range(n):
        sdb.x("INSERT INTO signal_profiles (ts, base, trend_up, trend_down, "
              "touch_long, touch_short, wick_long, wick_short, vol_ratio, "
              "bottleneck, near_miss) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              [now, "BTC", 1, 0, 1, 0, 0, 0, 1.0, bottleneck, near],
              db_path=db)


def _good_pnls(n=None):
    n = n or config.MIN_SAMPLES
    return [0.012 + 0.003 * (i % 5) for i in range(n)]


def test_effective_default():
    print("== 未批准时活体影线比 = 基线 ==")
    db = _db()
    check("默认 = config.REJECT_WICK_RATIO",
          se.effective_wick_ratio(db) == config.REJECT_WICK_RATIO)
    cand = se.candidate_wick(config.REJECT_WICK_RATIO)
    check("候选 strictly 低于现役且 ≥ 下限",
          cand < config.REJECT_WICK_RATIO and cand >= config.SCAN_EVOLVE_WICK_FLOOR,
          f"cand={cand}")


def test_path_pnl_stop_first():
    print("== 路径盈亏：同根止盈+止损按止损 ==")
    class B:
        def __init__(self, h, l, c):
            self.high, self.low, self.close = h, l, c
    pnl, reason, done = se.path_pnl("long", 100, 99, 102, [B(103, 98, 101)])
    check("多头同根两边都打 → stop", done and reason == "stop" and pnl < 0,
          f"reason={reason} pnl={pnl}")
    pnl2, r2, d2 = se.path_pnl("long", 100, 99, 102, [B(103, 99.5, 102.5)])
    check("只打止盈 → tp", d2 and r2 == "tp" and pnl2 > 0, f"reason={r2}")
    few = [B(100.1, 99.9, 100.0)]
    pnl3, r3, d3 = se.path_pnl("long", 100, 90, 120, few)
    check("样本不够且未触线 → 不结算", d3 is False)


def test_propose_and_no_auto_apply():
    print("== R1 提案不改尺子；未过门不能批准 ==")
    db = _db()
    _insert_profiles(db, n=config.FB_MIN_PROFILES, bottleneck="wick", near=1)
    cid = se.maybe_propose(db)
    check("R1 触发并登记 scan_wick", cid and cid.startswith("scan_wick_"),
          f"cid={cid}")
    check("提案后活体影线比仍是基线",
          se.effective_wick_ratio(db) == config.REJECT_WICK_RATIO)
    ok, msg = se.approve(db_path=db)
    check("未过验证门 → 批准失败", ok is False, msg)
    cid2 = se.maybe_propose(db)
    check("已有开放试验不再重复提案", cid2 is None)


def test_shadow_no_order_and_settle():
    print("== 近失影线记影子、不下单、后续K线可结算 ==")
    db = _db()
    _insert_profiles(db)
    se.maybe_propose(db)
    candles = make_trend_candles(wick_over_body=0.95)
    dt, fake = _make_trader(db, candles)
    # 现役 1.0 应无信号；候选 0.9 应有影子
    sig_now = dt.scan_signal("BTC")
    check("现役影线比无信号", sig_now is None)
    cand = se.active_candidate(db)
    check("有活跃候选", cand is not None and cand["wick"] < config.REJECT_WICK_RATIO)
    dt.scan_signals()
    n_sh = sdb.q1("SELECT COUNT(*) c FROM shadow_signals WHERE strategy=?",
                  [config.SCAN_EVOLVE_STRATEGY], db_path=db)
    check("记了 A_wick 影子", (n_sh or {}).get("c", 0) >= 1)
    check("影子路径零下单", len(fake.orders) == 0)
    row = sdb.q1("SELECT * FROM shadow_signals WHERE strategy=?",
                 [config.SCAN_EVOLVE_STRATEGY], db_path=db)
    # 追加一根打止盈的 1H
    last_ts = candles[-1].ts
    entry, tp = row["entry"], row["tp"]
    fake.candles["BTC-USDT-SWAP"] = candles + [
        Candle(ts=last_ts + 3600_000, open=entry, high=tp + 1, low=entry - 0.01,
               close=tp, volume=1000)]
    n = se.settle_shadows(fake, db, inst_id_fn=lambda b: "BTC-USDT-SWAP")
    settled = sdb.q1("SELECT pnl, exit_reason, status FROM shadow_signals "
                     "WHERE id=?", [row["id"]], db_path=db)
    check("结算 1 笔", n >= 1)
    check("止盈路径 pnl>0", settled and settled["status"] == "settled"
          and settled["exit_reason"] == "tp" and settled["pnl"] > 0,
          f"实际 {settled}")


def test_judge_approve_rollback():
    print("== 验证门通过后批准生效；回滚恢复基线 ==")
    db = _db()
    _insert_profiles(db)
    cid = se.maybe_propose(db)
    # 灌满达标影子样本（有方差、均值为正）
    now = time.time()
    for i, pnl in enumerate(_good_pnls()):
        sdb.x("INSERT INTO shadow_signals (ts, base, strategy, dir, entry, stop, "
              "tp, atr, signal_score, kline_ts, status, pnl, exit_reason, settled_ts) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              [now, "BTC", config.SCAN_EVOLVE_STRATEGY, "long", 100, 99, 102, 1,
               50, 1, "settled", pnl, "tp", now], db_path=db)
    st = se.maybe_judge(db)
    check("DSR 达标 → accepted", st == "accepted", f"st={st}")
    check("accepted 仍不改活体影线比",
          se.effective_wick_ratio(db) == config.REJECT_WICK_RATIO)
    snap = se.snapshot(db)
    check("快照 needs_approval", snap["needs_approval"] is True)
    ok, msg = se.approve(change_id=cid, db_path=db)
    check("批准成功", ok is True, msg)
    eff = se.effective_wick_ratio(db)
    check("批准后活体影线比 = 候选", abs(eff - se.candidate_wick()) < 1e-9,
          f"eff={eff}")
    se.rollback(db)
    check("回滚后回到基线",
          se.effective_wick_ratio(db) == config.REJECT_WICK_RATIO)


def test_reject_negative_mean():
    print("== 影子均盈为负 → 拒绝，尺子不变 ==")
    db = _db()
    _insert_profiles(db)
    se.maybe_propose(db)
    now = time.time()
    for i in range(config.MIN_SAMPLES):
        sdb.x("INSERT INTO shadow_signals (ts, base, strategy, dir, entry, stop, "
              "tp, status, pnl, settled_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
              [now, "BTC", config.SCAN_EVOLVE_STRATEGY, "long", 100, 99, 102,
               "settled", -0.02, now], db_path=db)
    st = se.maybe_judge(db)
    check("负期望 → rejected", st == "rejected", f"st={st}")
    ok, _ = se.approve(db_path=db)
    check("被拒提案不能批准", ok is False)
    check("尺子仍是基线", se.effective_wick_ratio(db) == config.REJECT_WICK_RATIO)


if __name__ == "__main__":
    test_effective_default()
    test_path_pnl_stop_first()
    test_propose_and_no_auto_apply()
    test_shadow_no_order_and_settle()
    test_judge_approve_rollback()
    test_reject_negative_mean()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
