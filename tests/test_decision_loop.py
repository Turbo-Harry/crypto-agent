"""
决策闭环行为测试 — 补上"为了测而测"之外的真正行为验证。

每个用例对应一个真实发生过或高风险的行为：
  1. 信号分门槛（<75 拒绝）
  2. 连亏冷却 / 连亏半仓（agent 决策核心）
  3. trusted 经验干预：止损≥2 条→stop_adj；信号≥3 条→拒绝
  4. 经验闭环门控：unverified 不参与决策；3 次验证升级 trusted 后参与
  5. stop_adj/size_factor 真正生效（防 B6 死代码回归）
  6. 信号阈值卡（80 vs 自适应阈值）

全部离线（FakeAdapter + 临时 SQLite），CI 可跑。
运行：PYTHONPATH=lib python3 tests/test_decision_loop.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from decision.self_evolving_trader import SelfEvolvingTrader
from decision.experience_scoring import ScoredExperience
from execution.trade_journal import (
    TradeJournal, realized_pnl_usdt, total_realized_pnl_usdt,
)
from exchange.fake_adapter import FakeAdapter
from engines.directional_trader import DirectionalTrader, _ExpAdapter

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def _make_trader(tmp):
    """隔离环境：临时 DB 的 journal + 经验库 + 空 FakeAdapter。
    Phase0 T0.4：决策日志（scan_decisions）与阈值状态也全隔离，防污染生产库。"""
    fake = FakeAdapter(usdt_free=10_000.0)
    dt = DirectionalTrader(exchange=fake, rt=None,
                           db_path=os.path.join(tmp, "scan.db"))
    from execution.position_ownership import PositionLedger
    from decision.threshold_learning import ThresholdLearner
    dt.journal = TradeJournal(path=os.path.join(tmp, "j.json"))
    dt.exp_bank = ScoredExperience(path=os.path.join(tmp, "e.json"))
    dt.ledger = PositionLedger(path=os.path.join(tmp, "ledger.json"))
    dt.threshold_learner = ThresholdLearner(path="test_dir",
                                            db_path=os.path.join(tmp, "th.db"))
    dt.evolver.bank = _ExpAdapter(dt.exp_bank)
    return dt, fake


def _closed_trades(journal, pnls):
    """伪造 journal 里 N 笔已平仓交易（连亏检查用）。"""
    for i, p in enumerate(pnls):
        journal.trades.append({
            "id": f"fake_{i}", "symbol": "X", "status": "closed", "pnl": p,
            "entry_price": 100.0, "exit_price": 100.0 * (1 + p),
            "stop_loss": 98.0, "take_profit": 104.0, "size": 1.0,
            "size_unit": "base", "notional_usdt": 100.0, "risk_usdt": 2.0,
        })
    journal._save()


def test_realized_pnl_usdt():
    """总盈亏必须是实际 USDT，不能把各笔百分比加起来。"""
    print("== 已实现盈亏按实际 USDT ==")
    a = {"status": "closed", "pnl": 0.02, "notional_usdt": 150.0}
    b = {"status": "closed", "pnl": 0.02, "notional_usdt": 50.0}
    open_t = {"status": "open", "pnl": None, "notional_usdt": 150.0}
    loss = {"status": "closed", "pnl": -0.03, "notional_usdt": 100.0}
    check("150 名义 +2% = +3.00 USDT", realized_pnl_usdt(a) == 3.0)
    check("50 名义 +2% = +1.00 USDT", realized_pnl_usdt(b) == 1.0)
    check("未平仓无盈亏", realized_pnl_usdt(open_t) is None)
    check("缺名义时用 size×价",
          realized_pnl_usdt({"pnl": 0.01, "size": 2, "entry_price": 100}) == 2.0)
    check("两笔同 +2% 合计是 4 USDT 不是 4%",
          total_realized_pnl_usdt([a, b, open_t]) == 4.0)
    check("含亏损合计 4-3=1 USDT", total_realized_pnl_usdt([a, b, loss]) == 1.0)


def test_decision_rules(tmp):
    print("== decide() 决策规则 ==")
    t = SelfEvolvingTrader()
    t.journal = TradeJournal(path=os.path.join(tmp, "j2.json"))
    t.bank = _ExpAdapter(ScoredExperience(path=os.path.join(tmp, "e2.json")))

    # 1. 信号分门槛（统一维护于 config.DECIDE_MIN_SCORE）
    import config
    dec = t.decide("BTC", config.DECIDE_MIN_SCORE - 1, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check(f"信号分 {config.DECIDE_MIN_SCORE-1} < {config.DECIDE_MIN_SCORE} → 拒绝",
          dec["trade"] is False)
    dec = t.decide("BTC", config.DECIDE_MIN_SCORE + 10, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check(f"信号分 {config.DECIDE_MIN_SCORE+10} ≥ {config.DECIDE_MIN_SCORE} → 放行",
          dec["trade"] is True)

    # 2. 连亏 3 笔 → 冷却已移除(2026-08-17 用户指示),仍放行
    _closed_trades(t.journal, [-0.03, -0.04, -0.02])
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("连亏 3 笔 → 冷却已移除,仍放行", dec["trade"] is True)

    # 3. 连亏 2 笔 → 半仓
    t.journal.trades = t.journal.trades[:-1]   # 剩 2 笔亏损
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("连亏 2 笔 → size_factor=0.5 半仓", dec["trade"] is True
          and dec["size_factor"] == 0.5)

    # 4. trusted 经验干预：止损类 ≥2 → stop_adj=+0.2 且记录采纳 id
    t.journal.trades = []
    bank = t.bank.bank   # ScoredExperience
    ids = []
    for i in range(2):
        ids.append(bank.add("BTC", "止损", f"止损太紧 {i}", f"t{i}"))
    # 3 次盈利验证 → trusted
    for lid in ids:
        for _ in range(3):
            bank.validate(lid, +0.02)
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    # 2026-08-17 聚合口径: 两条各 3 次盈利验证的教训 → 强度 2+2=4 → +0.4 ATR
    check("trusted 止损教训×2(各验证3次) → 聚合 stop_adj=0.4", dec["stop_adj"] == 0.4)
    check("采纳的经验 id 被记录（平仓后定向验证用）",
          len(dec["adopted_lesson_ids"]) >= 2)

    # 5. trusted 信号类 ≥3 → 拒绝该信号
    for i in range(3):
        lid = bank.add("BTC", "信号", f"该信号模式失效 {i}", f"s{i}")
        for _ in range(3):
            bank.validate(lid, -0.02)
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("trusted 信号教训×3 → 拒绝下单", dec["trade"] is False)


def test_experience_gate(tmp):
    print("== 经验闭环门控（unverified 不参与，trusted 参与）==")
    bank = ScoredExperience(path=os.path.join(tmp, "e3.json"))
    adapter = _ExpAdapter(bank)
    lid = bank.add("ETH", "止损", "止损太紧", "t1")
    check("unverified 经验不参与决策", len(adapter.relevant("ETH")) == 0)
    for _ in range(3):
        bank.validate(lid, +0.02)
    check("3 次验证后升级 trusted 并参与决策", len(adapter.relevant("ETH")) == 1)
    # 亏损验证 → 降分弃用
    lid2 = bank.add("ETH", "入场时机", "追高无妨", "t2")
    for _ in range(3):
        bank.validate(lid2, -0.02)
    check("连续亏损验证 → discarded（弃用不参与）",
          all(l["id"] != lid2 for l in adapter.relevant("ETH")))


def test_stop_adj_effect(tmp):
    print("== stop_adj/size_factor 真正生效（防 B6 死代码回归）==")
    tmp = os.path.join(tmp, "stop_adj")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    sig = dt.scan_signal("BTC")
    assert sig, "前置：应出信号"
    base_stop_dist = abs(sig["entry"] - sig["stop"])
    # 开仓带 stop_adj=0.2：止损距离应变为 1.2×
    dt.open_position("BTC", sig, score=80, stop_adj=0.2, size_factor=0.5)
    t = dt.journal.trades[-1]
    widened = abs(t["entry_price"] - t["stop_loss"])
    check("stop_adj=0.2 后止损距离 = 1.2×ATR",
          abs(widened - base_stop_dist * 1.2) < 0.01)
    # size_factor=0.5：数量减半（150/110=1.36 → 0.68）
    check("size_factor=0.5 后数量减半", abs(t["size"] - 0.68) < 0.01)


def test_threshold_gate(tmp):
    print("== 信号阈值卡（SIGNAL_SCORE vs 自适应阈值）==")
    tmp = os.path.join(tmp, "thr")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    import config
    from decision.threshold_learning import ThresholdLearner
    dt.threshold_learner = ThresholdLearner(path="test",
                                            db_path=os.path.join(tmp, "th.db"),
                                            initial_threshold=85)   # 阈值 > 信号分
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
    check(f"阈值 85 > 信号分 {config.SIGNAL_SCORE} → 不开仓", len(fake.orders) == 0)
    # 阈值放低到初始值(45) → 应开仓
    dt.threshold_learner.threshold = config.THRESHOLD_INITIAL
    dt.signal_cool = {}
    dt.scan_signals()
    check(f"阈值 {config.THRESHOLD_INITIAL} < {config.SIGNAL_SCORE} → 开仓",
          len(fake.orders) >= 1)


def test_open_logged_only_on_fill(tmp):
    """开仓决策只在成交入账后记 open；下单失败不得虚增开仓数。"""
    import sqlite3
    from exchange.models import OrderResult

    def _setup(dt, fake):
        import config
        fake.candles["BTC-USDT-SWAP"] = _make_candles()
        fake.last_prices["BTC-USDT-SWAP"] = 110.0
        fake.last_prices["BTC-USDT"] = 110.0
        dt.watchlist = ["BTC"]
        dt.watch_scores = {"BTC": 0.9}
        dt._watch_date = time.strftime("%Y-%m-%d")
        dt._last_watch_refresh = time.time()
        dt._last_scan = 0
        dt.signal_cool = {}
        dt.threshold_learner.threshold = config.THRESHOLD_INITIAL

    print("== 开仓日志与台账笔数对齐 ==")
    tmp_ok = os.path.join(tmp, "fill_ok")
    os.makedirs(tmp_ok, exist_ok=True)
    dt, fake = _make_trader(tmp_ok)
    _setup(dt, fake)
    dt.scan_signals()
    conn = sqlite3.connect(os.path.join(tmp_ok, "scan.db"))
    n_open = conn.execute(
        "SELECT COUNT(*) FROM scan_decisions WHERE decision='open'").fetchone()[0]
    conn.close()
    check(f"成交后 scan open = 1（实际 {n_open}）", n_open == 1)
    check(f"成交后台账 1 笔（实际 {len(dt.journal.trades)}）",
          len(dt.journal.trades) == 1)
    check("开仓数 = 台账笔数", n_open == len(dt.journal.trades))

    tmp_fail = os.path.join(tmp, "fill_fail")
    os.makedirs(tmp_fail, exist_ok=True)
    dt, fake = _make_trader(tmp_fail)
    _setup(dt, fake)

    def _fail(*a, **kw):
        return OrderResult(ok=False, message="模拟下单失败")
    fake.place_market_order = _fail
    dt.scan_signals()
    conn = sqlite3.connect(os.path.join(tmp_fail, "scan.db"))
    n_open = conn.execute(
        "SELECT COUNT(*) FROM scan_decisions WHERE decision='open'").fetchone()[0]
    n_failed = conn.execute(
        "SELECT COUNT(*) FROM scan_decisions WHERE decision='open_failed'"
    ).fetchone()[0]
    conn.close()
    check(f"下单失败不得记 open（实际 {n_open}）", n_open == 0)
    check(f"下单失败记 open_failed（实际 {n_failed}）", n_failed >= 1)
    check(f"失败后台账 0 笔（实际 {len(dt.journal.trades)}）",
          len(dt.journal.trades) == 0)


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
                     low=last.close - body - 1.2, close=last.close - 0.05, volume=1000)
    return out


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_dec_")
    test_realized_pnl_usdt()
    test_decision_rules(tmp)
    test_experience_gate(tmp)
    test_stop_adj_effect(tmp)
    test_threshold_gate(tmp)
    test_open_logged_only_on_fill(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
