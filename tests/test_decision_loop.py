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
运行：PYTHONPATH=. .venv/bin/python tests/test_decision_loop.py
"""
import os
import json
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 旧 lib/ 是 Python 3.9 构建产物，不加入 Python 3.12 的测试搜索路径。

from decision.self_evolving_trader import SelfEvolvingTrader
from decision.experience_scoring import ScoredExperience
from execution.trade_journal import (
    TradeJournal, realized_pnl_usdt, total_realized_pnl_usdt,
)
from exchange.fake_adapter import FakeAdapter
from engines.directional_trader import DirectionalTrader, _ExpAdapter, _scan_slot
from engines.signal_scan import _dynamic_ofi, _microstructure_features
from decision.orderflow_entry import paper_intraday_entry_decision


def _anchored_harness_reject(prompt):
    """Return a reject tied to the exact frozen market evidence in the prompt."""
    payload = json.loads(prompt)
    evidence_id = payload["context"]["field_provenance"]["market"]
    return {
        "verdict": "reject",
        "risk_probability": 0.9,
        "confidence": 0.8,
        "reason_codes": ["extreme_market_event"],
        "evidence_ids": [evidence_id],
        "reason": "verified severe market event",
    }


def _seed_explicit_extreme_event(dt):
    """Freeze the machine-readable severe-event fact used by reject fixtures."""
    import storage.db as sdb
    sdb.x(
        "INSERT OR REPLACE INTO kv (key,value) VALUES "
        "('sentiment_latest',?)",
        [json.dumps({"ts": time.time(), "extreme_market_event": True})],
        db_path=dt._db_path)

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


def test_microstructure_snapshot():
    """盘口派生值与动态 OFI 必须真的可达，防辅助函数插入截断函数体。"""
    print("== 盘口特征与动态 OFI ==")
    first = {"bids": [[99.0, 8.0], [98.0, 2.0]],
             "asks": [[101.0, 2.0], [102.0, 1.0]]}
    features = _microstructure_features(first, depth=2)
    check("spread bps 从最佳档计算", features["spread_bps"] == 200.0)
    check("买深度更厚时 microprice 偏上", features["microprice_bps"] > 0)
    check("多档深度失衡为正", features["depth_imbalance"] > 0)
    check("逐档 VWAP 滑点包含半价差而非名义/深度比例",
          abs(features["expected_slippage_bps"] - 100.0) < 1e-9)
    asymmetric = {"bids": [[99.0, 2.0]],
                  "asks": [[101.0, 1.0], [105.0, 2.0]]}
    long_slip = _microstructure_features(
        asymmetric, depth=2, direction="long")["expected_slippage_bps"]
    short_slip = _microstructure_features(
        asymmetric, depth=2, direction="short")["expected_slippage_bps"]
    check("已知方向只模拟实际开仓侧", long_slip > short_slip)
    thin = {"bids": [[99.0, 0.1]], "asks": [[101.0, 0.1]]}
    check("150 USDT 可见深度不足显式缺失",
          _microstructure_features(
              thin, depth=2)["expected_slippage_bps"] is None)
    ofi0, state = _dynamic_ofi(first, None)
    second = {"bids": [[99.0, 10.0]], "asks": [[101.0, 1.0]]}
    ofi1, _ = _dynamic_ofi(second, state)
    check("首个快照不伪造 OFI", ofi0 is None)
    check("买量增加且卖量减少产生正 OFI", ofi1 > 0)


def test_paper_intraday_confirmation():
    """完整日内最终确认必须同时覆盖多周期、连续流、成本和波动。"""
    print("== paper 日内最终确认 ==")
    kwargs = {
        "one_minute": [{"open": 100, "close": 100.1}],
        "five_minute": [{"open": 99.8, "close": 100.1}],
        "orderflow": {"status": "ready", "ofi_event_multilevel": .2,
                      "ofi_event_cancel_imbalance": .1,
                      "ofi_event_count": 20, "ofi_event_age_ms": 100},
        "microstructure": {"spread_bps": 2, "expected_slippage_bps": 3},
        "realtime": {"taker_buy_60s": .65, "trade_flow_count_60s": 30,
                     "vol_15m": .01},
    }
    passed_gate = paper_intraday_entry_decision(
        {"dir": "long"}, **kwargs)
    check("1m/5m、OFI、成交、成本与波动同向才放行",
          passed_gate["passed"] and passed_gate["size_factor"] == 1.0)
    reduced = paper_intraday_entry_decision(
        {"dir": "long"}, **dict(kwargs,
            realtime={"taker_buy_60s": .65, "trade_flow_count_60s": 30,
                      "vol_15m": .02}))
    check("较高但未异常波动只缩仓不放大风险",
          reduced["passed"] and reduced["size_factor"] == .5)
    stale = paper_intraday_entry_decision(
        {"dir": "long"}, **dict(kwargs,
            orderflow={"status": "stale", "ofi_event_multilevel": None}))
    check("订单流缺失或陈旧失败关闭",
          not stale["passed"] and stale["reason"] == "orderflow_not_ready")
    thin_flow = paper_intraday_entry_decision(
        {"dir": "long"}, **dict(
            kwargs,
            realtime={"taker_buy_60s": .8, "trade_flow_count_60s": 2,
                      "vol_15m": .01}))
    check("少量成交不得伪装成连续成交确认",
          not thin_flow["passed"] and
          thin_flow["reason"] == "trade_flow_missing")
    volatile = paper_intraday_entry_decision(
        {"dir": "long"}, **dict(
            kwargs,
            realtime={"taker_buy_60s": .65, "trade_flow_count_60s": 30,
                      "vol_15m": .04}))
    check("异常波动必须空仓",
          not volatile["passed"] and
          volatile["reason"] == "volatility_too_high")
    short = paper_intraday_entry_decision(
        {"dir": "short"}, **dict(
            kwargs,
            one_minute=[{"open": 100, "close": 99.9}],
            five_minute=[{"open": 100.2, "close": 99.9}],
            orderflow={"status": "ready", "ofi_event_multilevel": -.2,
                       "ofi_event_cancel_imbalance": -.1},
            realtime={"taker_buy_60s": .35, "trade_flow_count_60s": 30,
                      "vol_15m": .01}))
    check("空头确认按方向镜像解释成交和订单流", short["passed"])


def test_only_closed_kline_is_sampled(tmp):
    """尾部未收线 15m bar 不得改变形态或候选 kline_ts。"""
    print("== 只消费已收线 K ==")
    import config
    work = os.path.join(tmp, "closed_kline")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    candles = _make_candles()
    closed_ts = candles[-1].ts
    from exchange.models import Candle
    current_open = int(time.time() // 900 * 900 * 1000)
    candles.append(Candle(ts=current_open, open=90.0, high=91.0,
                          low=80.0, close=81.0, volume=1.0))
    fake.candles["BTC-USDT-SWAP"] = candles
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    calls = []
    original_fetch = dt._fetch_klines_any
    def traced_fetch(base, timeframe, limit):
        calls.append(timeframe)
        return original_fetch(base, timeframe, limit)
    dt._fetch_klines_any = traced_fetch
    sig = dt.scan_signal("BTC")
    check("15m 主周期且 1H/4H 仅作环境输入",
          calls and calls[0] == "15m" and "1H" in calls and "4H" in calls)
    check("未收线尾 bar 被忽略，仍识别上一根闭合信号",
          sig is not None and sig["dir"] == "long")
    check("候选身份使用上一根闭合 K 时间",
          sig and sig["kline_ts"] == closed_ts)
    grace = config.SIGNAL_BAR_CLOSE_GRACE_SECONDS
    just_before_visible = (closed_ts + 900_000) / 1000 + grace - 0.001
    just_after_visible = (closed_ts + 900_000) / 1000 + grace + 0.001
    check("冻结时刻在收线缓冲前不得提前看到目标 K",
          dt.scan_signal("BTC", as_of_ts=just_before_visible) is None)
    frozen = dt.scan_signal("BTC", as_of_ts=just_after_visible)
    check("冻结时刻越过收线缓冲后稳定看到同一目标 K",
          frozen is not None and frozen["kline_ts"] == closed_ts)


def test_scan_slot_alignment():
    """滚动服务不得在 5m/15m 边界前启动跨周期扫描。"""
    print("== 扫描调度按收线缓冲对齐 ==")
    import config
    interval = config.SCAN_INTERVAL_MINUTES * 60
    boundary = interval * 10_000
    before = _scan_slot(boundary - 1)
    check("边界后但尚未过收线缓冲仍属于上一扫描槽",
          _scan_slot(boundary + config.SIGNAL_BAR_CLOSE_GRACE_SECONDS - 0.001)
          == before)
    check("越过收线缓冲才开启新的 UTC 对齐扫描槽",
          _scan_slot(boundary + config.SIGNAL_BAR_CLOSE_GRACE_SECONDS)
          == before + 1)


def test_no_setup_skips_slow_context(tmp):
    """无 15m 结构时只取主周期，避免全池串行 1H/4H 延迟。"""
    print("== 无主结构时跳过慢上下文 ==")
    work = os.path.join(tmp, "no_setup_fast_path")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    candles = _make_candles()
    from exchange.models import Candle
    last = candles[-1]
    candles[-1] = Candle(
        ts=last.ts, open=last.close - 0.05, high=last.close + 0.1,
        low=last.close - 0.1, close=last.close, volume=last.volume)
    fake.candles["BTC-USDT-SWAP"] = candles
    calls = []
    original_fetch = dt._fetch_klines_any

    def traced_fetch(base, timeframe, limit):
        calls.append(timeframe)
        return original_fetch(base, timeframe, limit)

    dt._fetch_klines_any = traced_fetch
    check("无回踩形态时无信号", dt.scan_signal("BTC") is None)
    check("无结构只拉 15m，不再串行等待 1H/4H", calls == ["15m"])
    calls.clear()
    check("复用策略 B 的同轮 15m 快照仍保持无信号",
          dt.scan_signal("BTC", preloaded_kl=[
              [row.ts, row.open, row.high, row.low, row.close, row.volume]
              for row in candles]) is None)
    check("A 复用同轮快照时不重复请求 15m", calls == [])


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

    # 3. 连亏半仓按实例模式隔离：paper 全仓采集，live 仍半仓
    t.journal.trades = t.journal.trades[:-1]   # 剩 2 笔亏损
    old_mode = config.CRYPTO_MODE
    config.CRYPTO_MODE = "paper"
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("paper 连亏 2 笔 → 保持 size_factor=1.0 全仓采集",
          dec["trade"] is True and dec["size_factor"] == 1.0)
    config.CRYPTO_MODE = "live"
    dec = t.decide("BTC", 80, "回踩确认", 0, 0, 0.02, 0.05, 0)
    check("live 连亏 2 笔 → size_factor=0.5 半仓",
          dec["trade"] is True and dec["size_factor"] == 0.5)
    config.CRYPTO_MODE = old_mode

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

    strict_work = os.path.join(tmp, "strict")
    os.makedirs(strict_work, exist_ok=True)
    strict_dt, strict_fake = _make_trader(strict_work)
    strict_fake.candles["BTC-USDT-SWAP"] = _make_candles()
    strict_fake.last_prices["BTC-USDT-SWAP"] = 110.0
    strict_fake.last_prices["BTC-USDT"] = 110.0
    strict_sig = strict_dt.scan_signal("BTC")
    strict_dt.require_2to1_prediction = True
    strict_dt.open_position("BTC", strict_sig, score=80, stop_adj=0.2)
    strict_trade = strict_dt.journal.trades[-1]
    actual_rr = (abs(strict_trade["take_profit"] - strict_trade["entry_price"]) /
                 abs(strict_trade["entry_price"] - strict_trade["stop_loss"]))
    check("严格预测门忽略 stop_adj，实际订单仍为 2:1",
          abs(actual_rr - 2.0) < 1e-9)


def test_four_hour_time_exit(tmp):
    """15m 策略必须在 4h timeout 经过原有安全平仓链。"""
    print("== 15m 策略 4h 时间退出 ==")
    import config
    work = os.path.join(tmp, "time_exit")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    sig = dt.scan_signal("BTC")
    assert sig, "前置：应出 15m 信号"
    dt.open_position("BTC", sig, score=80)
    trade = dt.journal.trades[-1]
    trade["entry_time"] = time.time() - config.MAX_HOLD_HOURS * 3600 - 1
    dt.journal._update_trade(trade["id"], {"entry_time": trade["entry_time"]})
    reviews = []
    dt._post_close_review = lambda closed, original: reviews.append(closed)
    dt._last_pos_fetch = 0
    dt.monitor()
    check("4h 到期即使未触 TP/SL 也平仓",
          trade["status"] == "closed" and "4h时间退出" in
          trade.get("exit_reason", ""))
    check("时间退出使用 reduce-only 且交易所持仓已归零",
          any(order.get("reduce_only") for order in fake.orders) and
          not fake.fetch_positions())
    check("时间退出撤条件单并进入复盘链",
          not fake.algos and len(reviews) == 1)
    legacy_id = dt.journal.log_entry(
        "BTC", "legacy", "migration safety", 110.0, 108.0, 112.0, 0.1,
        entry_time=time.time() - 24 * 3600, direction="long")
    dt._last_pos_fetch = 0
    dt.monitor()
    legacy = next(row for row in dt.journal.trades if row["id"] == legacy_id)
    check("旧持仓无 max_hold_hours 时不追溯强平",
          legacy["status"] == "open")


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
    # 同一根 15m K 现在是同一次真实机会，已拒绝候选不得在 5 分钟后
    # 重试；推进到下一根 K 再验证低阈值放行语义。
    next_candles = _make_candles()
    for candle in next_candles:
        candle.ts += 900_000
    fake.candles["BTC-USDT-SWAP"] = next_candles
    dt.scan_signals()
    check(f"阈值 {config.THRESHOLD_INITIAL} < {config.SIGNAL_SCORE} → 开仓",
          len(fake.orders) >= 1)


def test_strict_2to1_preopen_wiring(tmp):
    """无 active 模型拒单，但 Harness 仍须取得结构候选反事实样本。"""
    print("== 固定 2:1 开仓前预测门接线 ==")
    import config
    import storage.db as sdb
    work = os.path.join(tmp, "strict_2to1")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    _seed_explicit_extreme_event(dt)
    dt.require_2to1_prediction = True
    dt.agent_model_call = _anchored_harness_reject
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    dt.watchlist = ["BTC"]
    dt.watch_scores = {"BTC": 0.9}
    dt._watch_date = time.strftime("%Y-%m-%d")
    dt._last_watch_refresh = time.time()
    dt.signal_cool = {}
    dt.threshold_learner.threshold = config.THRESHOLD_INITIAL
    dt.scan_signals()
    row = sdb.q1(
        "SELECT features,reject_reason FROM signal_samples ORDER BY event_ts DESC LIMIT 1",
        db_path=dt._db_path)
    audit = json.loads(row["features"])["preopen_2to1"] if row else {}
    check("无 active 模型时订单为 0", len(fake.orders) == 0)
    check("候选仍保存严格 2:1 预测审计",
          audit.get("actual_reward_risk") == 2.0 and
          audit.get("reason") == "no_validated_active_model")
    check("拒绝原因明确来自 2:1 前置门",
          (row or {}).get("reject_reason", "").startswith("2to1_prediction:"))
    harness = sdb.q1("SELECT final_action FROM agent_runs", db_path=dt._db_path)
    check("2:1 门拒单前 Harness 已留下可成熟的 shadow Trace",
          harness and harness["final_action"] == "shadow_reject")


def test_paper_bootstrap_collects_real_closes(tmp):
    """首模为空时仅 paper baseline 可下单；审计不得伪装成模型通过。"""
    print("== paper 首模冷启动采集 ==")
    import config
    import storage.db as sdb
    work = os.path.join(tmp, "paper_bootstrap")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    _seed_explicit_extreme_event(dt)
    dt.require_2to1_prediction = True
    dt.paper_bootstrap_orders_enabled = True
    dt.agent_model_call = _anchored_harness_reject
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    dt.watchlist = ["BTC"]
    dt.watch_scores = {"BTC": 0.9}
    dt._watch_date = time.strftime("%Y-%m-%d")
    dt._last_watch_refresh = time.time()
    dt.signal_cool = {}
    dt.threshold_learner.threshold = config.THRESHOLD_INITIAL
    dt.scan_signals()
    row = sdb.q1(
        "SELECT features,final_decision,trade_id FROM signal_samples "
        "ORDER BY event_ts DESC LIMIT 1", db_path=dt._db_path)
    audit = json.loads(row["features"])["preopen_2to1"] if row else {}
    check("paper bootstrap 在无模型时产生模拟订单", len(fake.orders) >= 1)
    check("bootstrap 订单与候选绑定供后续真实平仓采集",
          row and row["final_decision"] == "opened" and row["trade_id"])
    check("bootstrap 保留无模型事实而不伪造验证通过",
          audit.get("passed") is False and
          audit.get("reason") == "no_validated_active_model" and
          audit.get("bootstrap_override") is True)


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


def test_extrema_shadow_wiring(tmp):
    """极值影子制品必须进入候选快照和开仓预测展示，但不能改变放行权。"""
    print("== 极值影子预测接入候选与展示 ==")
    import config
    import storage.db as sdb
    from decision.signal_identity import config_identity
    work = os.path.join(tmp, "extrema_wiring")
    os.makedirs(work, exist_ok=True)
    dt, fake = _make_trader(work)
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    dt.watchlist = ["BTC"]
    dt.watch_scores = {"BTC": 0.9}
    dt._watch_date = time.strftime("%Y-%m-%d")
    dt._last_watch_refresh = time.time()
    dt.signal_cool = {}
    dt.threshold_learner.threshold = config.THRESHOLD_INITIAL
    qmodel = lambda a, b, c: {
        "means": [0.0], "scales": [1.0], "quantiles": [0.1, 0.5, 0.9],
        "models": {"0.1": {"bias": a, "weights": [0.0]},
                   "0.5": {"bias": b, "weights": [0.0]},
                   "0.9": {"bias": c, "weights": [0.0]}}}
    artifact = {
        "version": "extrema-test-v1", "direction": "long",
        "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
        "strategy_version": config_identity(
            config.ENTRY_SIGNAL_STRATEGY_ID)[0],
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        "feature_names": ["trend"], "high_model": qmodel(0.01, 0.02, 0.03),
        "low_model": qmodel(-0.03, -0.02, -0.01),
        "baseline_high_returns": {"q10": 0.01, "q50": 0.02, "q90": 0.03},
        "baseline_low_returns": {"q10": -0.03, "q50": -0.02, "q90": -0.01},
        "high_conformal_radius": 0.001, "low_conformal_radius": 0.001}
    sdb.x(
        "INSERT INTO model_artifacts (model_id,model_type,direction,version,state,"
        "created_at,training_cutoff,data_hash,feature_names,artifact,metrics,"
        "strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ["extrema_wire", "extrema", "long", "extrema-test-v1", "shadow",
         time.time(), 0, "hash", '["trend"]', json.dumps(artifact), "{}",
         artifact["strategy_version"]],
        db_path=dt._db_path)
    dt.scan_signals()
    sample = sdb.q1("SELECT features FROM signal_samples ORDER BY event_ts DESC LIMIT 1",
                    db_path=dt._db_path)
    snapshot = json.loads(sample["features"])
    check("候选快照记录 extrema_prediction",
          snapshot["extrema_prediction"]["model_id"] == "extrema_wire")
    trade = dt.journal.trades[-1]
    trade_forecast = (json.loads(trade["forecast"])
                      if isinstance(trade.get("forecast"), str)
                      else trade.get("forecast"))
    check("开仓展示 forecast 携带概率极值区间",
          trade_forecast["extrema"]["model_id"] == "extrema_wire")
    check("shadow 极值模型不获得交易决策权",
          snapshot["extrema_prediction"]["decision_effective"] is False)


def test_harness_shadow_keeps_legacy_authority(tmp):
    """Harness 必须真实留痕，但不能夺走 legacy AI 的现役否决权。"""
    print("== Agent Harness shadow 与 legacy 权限隔离 ==")
    import config
    import decision.agent_judge as agent_judge
    import storage.db as sdb

    def setup(work):
        dt, fake = _make_trader(work)
        _seed_explicit_extreme_event(dt)
        fake.candles["BTC-USDT-SWAP"] = _make_candles()
        fake.last_prices["BTC-USDT-SWAP"] = 110.0
        fake.last_prices["BTC-USDT"] = 110.0
        dt.watchlist = ["BTC"]
        dt.watch_scores = {"BTC": 0.9}
        dt._watch_date = time.strftime("%Y-%m-%d")
        dt._last_watch_refresh = time.time()
        dt.signal_cool = {}
        dt.threshold_learner.threshold = config.THRESHOLD_INITIAL
        dt.ai_judge_enabled = True
        return dt, fake

    reject_shadow = _anchored_harness_reject
    approve_shadow = lambda prompt: {
        "verdict": "approve", "risk_probability": 0.1, "confidence": 0.8,
        "reason": "shadow approve",
    }
    original_judge = agent_judge.judge
    try:
        work = os.path.join(tmp, "harness_shadow_reject")
        os.makedirs(work, exist_ok=True)
        dt, fake = setup(work)
        dt.agent_model_call = reject_shadow
        agent_judge.judge = lambda *a, **k: ("approve", "legacy pass", None)
        dt.scan_signals()
        run = sdb.q1("SELECT final_action,input_snapshot FROM agent_runs",
                     db_path=dt._db_path)
        check("Harness reject 留下 shadow_reject Trace",
              run and run["final_action"] == "shadow_reject")
        frozen = json.loads(run["input_snapshot"])
        check("Harness 冻结账户风险、数据质量与完整候选特征",
              frozen["account"]["equity_usdt"] > 0 and
              "risk_can_trade" in frozen["health"] and
              bool(frozen["market"]["frozen_features"]))
        check("legacy approve 时 Harness reject 不得拦单", len(fake.orders) >= 1)

        work = os.path.join(tmp, "harness_active_reject")
        os.makedirs(work, exist_ok=True)
        dt, fake = setup(work)
        dt.agent_model_call = reject_shadow
        from decision import agent_lifecycle
        from storage.agent_lifecycle import transition
        version = agent_lifecycle.configured_version(
            config.ENTRY_SIGNAL_STRATEGY_ID)
        agent_lifecycle.register(version, db_path=dt._db_path)
        transition(version, "shadow", db_path=dt._db_path)
        transition(version, "validated", db_path=dt._db_path)
        agent_lifecycle.activate(version, db_path=dt._db_path)
        agent_judge.judge = lambda *a, **k: ("approve", "legacy pass", None)
        dt.scan_signals()
        run = sdb.q1("SELECT final_action FROM agent_runs",
                     db_path=dt._db_path)
        check("验证并激活的 Harness reject 进入下单前否决链",
              run and run["final_action"] == "agent_reject" and
              len(fake.orders) == 0)

        work = os.path.join(tmp, "harness_legacy_reject")
        os.makedirs(work, exist_ok=True)
        dt, fake = setup(work)
        dt.agent_model_call = approve_shadow
        agent_judge.judge = lambda *a, **k: ("reject", "legacy veto", None)
        dt.scan_signals()
        run = sdb.q1("SELECT final_action FROM agent_runs",
                     db_path=dt._db_path)
        check("Harness approve 也会留下可评价 Trace", run is not None)
        check("legacy reject 仍是唯一实际 AI 否决", len(fake.orders) == 0)
    finally:
        agent_judge.judge = original_judge


def _make_candles(n=100, base=100.0, drift=0.1):
    from exchange.models import Candle
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        close = base + i * drift
        open_ = close - drift * 0.8
        out.append(Candle(ts=ts + i * 900_000, open=open_, high=close + 0.5,
                          low=open_ - 0.5, close=close, volume=1000))
    last = out[-1]
    body = 0.4
    out[-1] = Candle(ts=last.ts, open=last.close - body, high=last.close,
                     low=last.close - body - 1.2, close=last.close - 0.05, volume=1000)
    return out


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_dec_")
    test_realized_pnl_usdt()
    test_microstructure_snapshot()
    test_paper_intraday_confirmation()
    test_only_closed_kline_is_sampled(tmp)
    test_scan_slot_alignment()
    test_no_setup_skips_slow_context(tmp)
    test_decision_rules(tmp)
    test_experience_gate(tmp)
    test_stop_adj_effect(tmp)
    test_four_hour_time_exit(tmp)
    test_threshold_gate(tmp)
    test_strict_2to1_preopen_wiring(tmp)
    test_paper_bootstrap_collects_real_closes(tmp)
    test_open_logged_only_on_fill(tmp)
    test_extrema_shadow_wiring(tmp)
    test_harness_shadow_keeps_legacy_authority(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
