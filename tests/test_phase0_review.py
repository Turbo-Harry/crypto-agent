"""
Phase 0 复盘链断点修复回归测试（离线，FakeAdapter + 全隔离临时库，不碰生产状态）：
  1. T0.1 熔断强平走复盘链：_liquidate_all → review 落盘 + 教训入经验库（DEF-1）
  2. T0.2 死锁打破：candidate 可被采纳→独立验证→晋升；dubious 不进采纳池（DEF-2）
  3. T0.3 post_exit_reverse 传参：止损后反转 → 插针教训触发（DEF-3）
  4. T0.4 scan_decisions 生产表隔离：scan_signals 只写隔离库（DEF-8）
运行：PYTHONPATH=lib python3 tests/test_phase0_review.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

from exchange.fake_adapter import FakeAdapter
from exchange.models import PositionInfo
from engines.directional_trader import DirectionalTrader, _ExpAdapter
import engines.directional_trader as dt_mod
from decision.self_evolving_trader import SelfEvolvingTrader
from decision.experience_scoring import ScoredExperience, rollup_lessons
from execution.trade_journal import TradeJournal
from execution.position_ownership import PositionLedger
from decision.threshold_learning import ThresholdLearner

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _make_trader(tmp):
    """隔离环境：journal/经验库/账本/阈值/决策日志全部指向临时库。"""
    fake = FakeAdapter(usdt_free=10_000.0)
    dt = DirectionalTrader(exchange=fake, rt=None,
                           db_path=os.path.join(tmp, "scan.db"))  # T0.4
    dt.journal = TradeJournal(path=os.path.join(tmp, "j.json"))
    dt.exp_bank = ScoredExperience(path=os.path.join(tmp, "e.json"))
    dt.ledger = PositionLedger(path=os.path.join(tmp, "ledger.json"))
    dt.threshold_learner = ThresholdLearner(path="test_dir",
                                            db_path=os.path.join(tmp, "th.db"),
                                            initial_threshold=config.THRESHOLD_INITIAL)
    dt.evolver.bank = _ExpAdapter(dt.exp_bank)
    return dt, fake


def _silence_notify():
    dt_mod.notify = lambda msg: None


def _restore_notify():
    dt_mod.notify = _orig_notify


_orig_notify = dt_mod.notify


def test_liquidate_runs_review(tmp):
    print("== T0.1 熔断强平走复盘链（DEF-1）==")
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        # ANTHROPIC-USDT-SWAP: ct_val=1 → 1 张=1 币，名义 180 ≤ 600 敞口上限
        fake.last_prices["ANTHROPIC-USDT-SWAP"] = 170.0
        tid = dt.journal.log_entry(
            symbol="ANTHROPIC", signal="回踩确认", reason="测试",
            entry_price=180.0, stop_loss=175.0, take_profit=190.0,
            size=1.0, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=5.0, signal_price=178.0,
            venue="swap")
        fake.positions = [PositionInfo(inst_id="ANTHROPIC-USDT-SWAP",
                                       base="ANTHROPIC", side="long",
                                       base_qty=1.0, avg_px=180.0)]
        ok, reason = dt.ledger.claim("ANTHROPIC/USDT:USDT", "long", "dir",
                                     1.0, 180.0)
        check("前置：账本 claim 成功", ok, reason)
        dt._liquidate_all("测试熔断")
        t = dt.journal.trades[-1]
        check("T0.1 熔断强平后状态=closed", t["status"] == "closed")
        check("T0.1 复盘报告已落盘（review 非空）", bool(t.get("review")),
              f"review={t.get('review')}")
        check("T0.1 复盘链产生教训入经验库", len(dt.exp_bank.lessons) > 0,
              f"实际 {len(dt.exp_bank.lessons)}")
        cands = [l for l in dt.exp_bank.lessons if l["status"] == "candidate"]
        check("T0.2 亏损交易 loss 归因教训 → candidate", len(cands) > 0,
              f"实际 {[l['status'] for l in dt.exp_bank.lessons]}")
    finally:
        _restore_notify()


def test_deadlock_broken(tmp):
    print("== T0.2 unverified 死锁打破（DEF-2）==")
    bank = ScoredExperience(path=os.path.join(tmp, "e2.json"))
    t = SelfEvolvingTrader()
    t.bank = _ExpAdapter(bank)
    # 2026-08-17: decide 必须用隔离 journal——evolver 自建 TradeJournal 读生产库
    # (隔离泄漏: 生产有 3 笔连亏时会触发冷却误拒,与 test_decision_loop 同源)
    tj = TradeJournal(path=os.path.join(tmp, "j2.json"))
    lid = bank.add("BTC", "止损", "止损太紧", "t1", status="candidate")
    lid_d = bank.add("BTC", "出场", "止盈太早", "t2", status="dubious")
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0, journal=tj)
    check("T0.2 决策允许交易（候选不影响参数）", dec["trade"] is True)
    check("T0.2 候选教训纳入采纳追踪", lid in dec["adopted_lesson_ids"])
    check("T0.2 dubious 不进采纳池", lid_d not in dec["adopted_lesson_ids"])
    check("T0.2 决策理由标注候选参考",
          any("候选" in r for r in dec["reason"]), dec["reason"])
    for _ in range(3):
        bank.validate(lid, -0.02)
    got = next((l for l in bank.lessons if l["id"] == lid), None)
    check("T0.2 3 次独立亏损验证 → discarded（晋升通道贯通）",
          got is not None and got["status"] == "discarded",
          f"实际 {got and got['status']}")


def test_post_exit_reverse(tmp):
    print("== T0.3 post_exit_reverse 传参（DEF-3）==")
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        fake.last_prices["BTC-USDT-SWAP"] = 62500.0   # 高于止损 62000 → 反转
        tid = dt.journal.log_entry(
            symbol="BTC", signal="回踩确认", reason="测试",
            entry_price=63000.0, stop_loss=62000.0, take_profit=66000.0,
            size=0.01, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=300.0, signal_price=62500.0,
            venue="swap")
        closed = dt.journal.log_exit(tid, 61990.0, "止损")
        check("前置：平仓成功", closed is not None)
        dt._post_close_review(closed, closed)
        check("T0.3 止损后反转 → 插针教训入经验库",
              any("插针" in l["content"] for l in dt.exp_bank.lessons),
              f"实际 {[l['category'] for l in dt.exp_bank.lessons]}")
    finally:
        _restore_notify()


def _make_candles(n=100, base=100.0, drift=0.1):
    from exchange.models import Candle
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        close = base + i * drift
        open_ = close - drift * 0.8
        out.append(Candle(ts=ts + i * 3600_000, open=open_, high=close + 0.5,
                          low=open_ - 0.5, close=close, volume=1000))
    last = out[-1]
    body = 0.4
    out[-1] = Candle(ts=last.ts, open=last.close - body, high=last.close,
                     low=last.close - body - 1.2, close=last.close - 0.05,
                     volume=1000)
    return out


def test_scan_decision_isolation(tmp):
    print("== T0.4 scan_decisions 生产表隔离（DEF-8）==")
    _silence_notify()
    try:
        prod = os.path.join(REPO, "crypto_agent.db")
        before = None
        if os.path.exists(prod):
            conn = sqlite3.connect(f"file:{prod}?mode=ro", uri=True)
            before = conn.execute(
                "SELECT COUNT(*) FROM scan_decisions").fetchone()[0]
            conn.close()
        dt, fake = _make_trader(tmp)
        fake.candles["BTC-USDT-SWAP"] = _make_candles()
        fake.last_prices["BTC-USDT-SWAP"] = 110.0
        fake.last_prices["BTC-USDT"] = 110.0
        dt.watchlist = ["BTC"]
        dt.watch_scores = {"BTC": 0.9}
        dt._watch_date = time.strftime("%Y-%m-%d")
        dt._last_watch_refresh = time.time()
        dt._last_scan = 0
        dt.signal_cool = {}
        dt.scan_signals()
        conn = sqlite3.connect(os.path.join(tmp, "scan.db"))
        iso = conn.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()[0]
        conn.close()
        check("T0.4 scan_decisions 写入隔离库", iso > 0, f"实际 {iso}")
        if before is not None:
            conn = sqlite3.connect(f"file:{prod}?mode=ro", uri=True)
            after = conn.execute(
                "SELECT COUNT(*) FROM scan_decisions").fetchone()[0]
            conn.close()
            check("T0.4 生产库零新增（DEF-8 修复）", after == before,
                  f"{before} → {after}")
    finally:
        _restore_notify()


def test_startup_reclaims_journal(tmp):
    print("== DEF-11 启动对账补回 journal 持仓 claim ==")
    tmp = os.path.join(tmp, "def11")
    os.makedirs(tmp, exist_ok=True)
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        # 预置:journal 两笔同币 open 交易(同 key 聚合:100+60=160 USDT),账本为空
        dt.journal.log_entry(
            symbol="ETH", signal="回踩确认", reason="测试",
            entry_price=2000.0, stop_loss=1950.0, take_profit=2100.0,
            size=0.05, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=20.0, signal_price=1990.0,
            venue="swap")
        dt.journal.log_entry(
            symbol="ETH", signal="回踩确认", reason="测试2",
            entry_price=2000.0, stop_loss=1950.0, take_profit=2100.0,
            size=0.03, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=20.0, signal_price=1990.0,
            venue="swap")
        check("前置:账本为空", dt.ledger.total_notional() == 0.0,
              f"实际 {dt.ledger.total_notional()}")
        dt._reconcile_startup()
        check("DEF-11 同 key 多笔聚合补账（闸门不漏计既有持仓）",
              155 < dt.ledger.total_notional() < 165,
              f"实际 {dt.ledger.total_notional()}")
        dt._reconcile_startup()
        check("DEF-11 幂等（重复对账不累加）",
              155 < dt.ledger.total_notional() < 165,
              f"实际 {dt.ledger.total_notional()}")
    finally:
        _restore_notify()


def test_ws_subscribe_dedup():
    print("== 2026-08-17 动态订阅(去重/离线安全) ==")
    from data.realtime_okx import OKXRealtime
    rt = OKXRealtime(["BTC"])
    rt.subscribe("ETH")
    check("新币入订阅清单", "ETH" in rt.symbols)
    rt.subscribe("ETH")
    check("重复订阅去重", rt.symbols.count("ETH") == 1,
          f"实际 {rt.symbols}")
    rt.subscribe("BTC")
    check("已订阅币幂等跳过", rt.symbols.count("BTC") == 1)


def test_monitor_pos_throttle(tmp):
    print("== 2026-08-17 持仓快照 2s 节流(不误平/不误报) ==")
    tmp = os.path.join(tmp, "thr2")
    os.makedirs(tmp, exist_ok=True)
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        dt.journal.log_entry(
            symbol="BTC", signal="回踩确认", reason="测试",
            entry_price=100.0, stop_loss=98.0, take_profit=104.0,
            size=0.01, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=2.0, signal_price=99.5,
            venue="swap")
        fake.last_prices["BTC-USDT-SWAP"] = 97.0   # 已破止损
        dt.rt = None   # 测试隔离: 引擎默认会接真实 WS(真实 BTC 价 63k 会盖过假价)
        dt._last_pos_fetch = time.time()           # 本拍不刷持仓快照
        dt.monitor()
        t = dt.journal.trades[-1]
        check("持仓快照未刷新拍: 不执行平仓(等下一拍)", t["status"] == "open")
        check("无订单发出", len(fake.orders) == 0)
        dt._last_pos_fetch = 0                     # 下一拍刷新持仓
        from exchange.models import PositionInfo
        fake.positions = [PositionInfo(inst_id="BTC-USDT-SWAP", base="BTC",
                                       side="long", base_qty=0.01, avg_px=100.0)]
        dt.monitor()
        check("持仓就绪拍: 止损平仓执行", t["status"] == "closed",
              f"实际 {t['status']}")
    finally:
        _restore_notify()


def test_aggregation_and_regime(tmp):
    print("== 2026-08-17 教训聚合生效 + 场景条件向量匹配 ==")
    tmp = os.path.join(tmp, "agg")
    os.makedirs(tmp, exist_ok=True)
    bank = ScoredExperience(path=os.path.join(tmp, "e3.json"))
    t = SelfEvolvingTrader()
    t.bank = _ExpAdapter(bank)
    tj = TradeJournal(path=os.path.join(tmp, "j3.json"))

    def _trusted_lesson(category, conds, good, bad):
        lid = bank.add("BTC", category, f"{category}教训",
                       f"t{category}{good}{bad}{conds.get('direction','')}",
                       status="candidate", conditions=conds)
        for _ in range(good):
            bank.validate(lid, 0.02)
        for _ in range(bad):
            bank.validate(lid, -0.02)
        return lid

    # 两条同类别教训,不同波动带: good=3 各验证 3 次 → trusted
    lid_hi = _trusted_lesson("止损",
                             {"direction": "long", "vol_band": "high_vol"}, 3, 0)
    lid_lo = _trusted_lesson("止损",
                             {"direction": "long", "vol_band": "low_vol"}, 3, 0)
    # 场景匹配: high_vol 只聚合 high_vol 教训 → 强度 2 → +0.2 ATR
    cond_hi = {"direction": "long", "vol_band": "high_vol"}
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                   journal=tj, conditions=cond_hi)
    check("同场景单条强教训 → +0.2 ATR", dec["stop_adj"] == 0.2,
          f"实际 {dec['stop_adj']}")
    check("只采纳同场景教训", lid_hi in dec["adopted_lesson_ids"]
          and lid_lo not in dec["adopted_lesson_ids"])
    # 方向维度: 做空场景不匹配做多教训 → 强度 0
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                   journal=tj, conditions={"direction": "short",
                                           "vol_band": "high_vol"})
    check("方向不匹配 → 教训不生效", dec["stop_adj"] == 0,
          f"实际 {dec['stop_adj']}")
    # 两条同场景教训聚合: 强度 2+2=4 → +0.4 ATR
    _trusted_lesson("止损",
                    {"direction": "long", "vol_band": "high_vol"}, 3, 0)
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                   journal=tj, conditions=cond_hi)
    check("两条强教训聚合 → +0.4 ATR", dec["stop_adj"] == 0.4,
          f"实际 {dec['stop_adj']}")
    # 坏验证抵消: good=3 bad=3 → 净 0 权重
    bank2 = ScoredExperience(path=os.path.join(tmp, "e4.json"))
    t2 = SelfEvolvingTrader()
    t2.bank = _ExpAdapter(bank2)
    lid = bank2.add("BTC", "止损", "被抵消", "tx", status="candidate",
                    conditions={"direction": "long", "vol_band": "high_vol"})
    for _ in range(3):
        bank2.validate(lid, 0.02)
    for _ in range(3):
        bank2.validate(lid, -0.02)
    dec = t2.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                    journal=tj, conditions=cond_hi)
    check("净验证为 0 的教训不产生效果", dec["stop_adj"] == 0,
          f"实际 {dec['stop_adj']}")
    # 无 conditions 的旧教训(仅 regime 标签) → vol_band 迁移匹配,视为通配其余维度
    bank3 = ScoredExperience(path=os.path.join(tmp, "e5.json"))
    t3 = SelfEvolvingTrader()
    t3.bank = _ExpAdapter(bank3)
    lid_w = bank3.add("BTC", "止损", "通配", "tw", status="candidate",
                      regime="high_vol")
    for _ in range(3):
        bank3.validate(lid_w, 0.02)
    dec = t3.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                    journal=tj, conditions=cond_hi)
    check("旧教训(regime→vol_band)仍匹配 → +0.2 ATR", dec["stop_adj"] == 0.2,
          f"实际 {dec['stop_adj']}")


def test_rollup(tmp):
    print("== 2026-08-17 场景归纳教训(多维经验总结层) ==")
    tmp = os.path.join(tmp, "ru")
    os.makedirs(tmp, exist_ok=True)
    bank = ScoredExperience(path=os.path.join(tmp, "e6.json"))
    t = SelfEvolvingTrader()
    t.bank = _ExpAdapter(bank)
    tj = TradeJournal(path=os.path.join(tmp, "j6.json"))
    cond = {"direction": "long", "vol_band": "high_vol"}

    def _add(good):
        lid = bank.add("BTC", "止损", "插针", f"r{good}", status="candidate",
                       conditions=cond)
        for _ in range(good):
            bank.validate(lid, 0.02)
        return lid

    # 2 条成员 → 不足 ROLLUP_MIN_MEMBERS,不归纳
    _add(3)
    _add(3)
    ru = rollup_lessons(bank, db_path=os.path.join(tmp, "e6.json"))
    check("成员不足 3 条不归纳", len(ru) == 0, f"实际 {len(ru)}")
    # 第 3 条成员 → 归纳成立,强度 2+2+2=6
    _add(3)
    ru = rollup_lessons(bank, db_path=os.path.join(tmp, "e6.json"))
    check("3 条同场景 trusted → 1 条归纳", len(ru) == 1 and ru[0]["member_count"] == 3,
          f"实际 {ru}")
    check("归纳强度 = 成员权重和", ru and ru[0]["strength"] == 6,
          f"实际 {ru and ru[0]['strength']}")
    # 决策层理由带归纳审计注释
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0,
                   journal=tj, conditions=cond)
    check("决策理由含场景归纳注释",
          any("场景归纳经验" in r for r in dec["reason"]), dec["reason"])


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_p0_review_")
    test_liquidate_runs_review(tmp)
    test_deadlock_broken(tmp)
    test_post_exit_reverse(tmp)
    test_scan_decision_isolation(tmp)
    test_startup_reclaims_journal(tmp)
    test_ws_subscribe_dedup()
    test_monitor_pos_throttle(os.path.join(tmp, "thr2"))
    test_aggregation_and_regime(tmp)
    test_rollup(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
