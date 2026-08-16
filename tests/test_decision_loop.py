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
from execution.trade_journal import TradeJournal
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
    """隔离环境：临时 DB 的 journal + 经验库 + 空 FakeAdapter。"""
    fake = FakeAdapter(usdt_free=10_000.0)
    dt = DirectionalTrader(exchange=fake, rt=None)
    from execution.position_ownership import PositionLedger
    dt.journal = TradeJournal(path=os.path.join(tmp, "j.json"))
    dt.exp_bank = ScoredExperience(path=os.path.join(tmp, "e.json"))
    dt.ledger = PositionLedger(path=os.path.join(tmp, "ledger.json"))
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


def test_decision_rules(tmp):
    print("== decide() 决策规则 ==")
    t = SelfEvolvingTrader()
    t.journal = TradeJournal(path=os.path.join(tmp, "j2.json"))
    t.bank = _ExpAdapter(ScoredExperience(path=os.path.join(tmp, "e2.json")))

    # 1. 信号分门槛
    dec = t.decide("BTC", 74, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("信号分 74 < 75 → 拒绝", dec["trade"] is False)
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("信号分 80 ≥ 75 → 放行", dec["trade"] is True)

    # 2. 连亏 3 笔 → 冷却拒绝
    _closed_trades(t.journal, [-0.03, -0.04, -0.02])
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("连亏 3 笔 → 冷却拒绝", dec["trade"] is False)

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
    check("trusted 止损教训×2 → stop_adj=0.2", dec["stop_adj"] == 0.2)
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
    print("== 信号阈值卡（80 vs 自适应阈值）==")
    tmp = os.path.join(tmp, "thr")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    from decision.threshold_learning import ThresholdLearner
    dt.threshold_learner = ThresholdLearner(path="test",
                                            db_path=os.path.join(tmp, "th.db"),
                                            initial_threshold=85)   # 阈值 > 80
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
    check("阈值 85 > 信号分 80 → 不开仓", len(fake.orders) == 0)
    # 阈值放低到 70 → 应开仓
    dt.threshold_learner.threshold = 70
    dt.signal_cool = {}
    dt.scan_signals()
    check("阈值 70 < 80 → 开仓", len(fake.orders) >= 1)


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
    test_decision_rules(tmp)
    test_experience_gate(tmp)
    test_stop_adj_effect(tmp)
    test_threshold_gate(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
