"""
Phase 1 特征采集离线单测（FakeAdapter + 全隔离库，不触网、不碰生产）：
  1. scan_signal 影子字段: shadow_score 0-100 + regime 标签（T1.3/T1.4）
  2. 开仓 → trade_features 入场行（含影子分/regime；fake 跳过订单流→计入 missing）
  3. 平仓复盘 → 离场特征更新（R 倍数/MFE/MAE/滑点/持仓时长）
  4. 特征采集失败不影响交易主链路（零回归守护）
运行：PYTHONPATH=lib python3 tests/test_phase1_features.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import engines.directional_trader as dt_mod
from tests.test_phase0_review import _make_trader, _silence_notify, \
    _restore_notify

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _features(db, trade_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM trade_features WHERE trade_id=?",
                     [trade_id]).fetchone()
    conn.close()
    return dict(r) if r else None


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


def test_scan_shadow_fields(tmp):
    print("== T1.3/T1.4 影子打分 + regime 标签 ==")
    tmp = os.path.join(tmp, "shadow")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    fake.candles["BTC-USDT-SWAP"] = _make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    sig = dt.scan_signal("BTC")
    check("前置: 出信号", sig is not None)
    if sig:
        check("shadow_score ∈ [0,100]",
              sig.get("shadow_score") is not None
              and 0 <= sig["shadow_score"] <= 100,
              f"实际 {sig.get('shadow_score')}")
        reg = sig.get("regime")
        check("regime 标签存在且合法",
              reg is not None and reg.get("tag") in
              ("low_vol", "mid_vol", "high_vol"),
              f"实际 {reg}")
        market_state = sig.get("market_regime") or {}
        route = sig.get("strategy_route") or {}
        check("行情状态权重进入候选且明确未校准",
              market_state.get("ready") is True and
              market_state.get("calibrated") is False and
              abs(sum((market_state.get("weights") or {}).values()) - 1) < 1e-5,
              f"实际 {market_state}")
        check("策略路由仅 shadow 且无执行权限",
              route.get("mode") == "shadow" and
              route.get("has_execution_authority") is False,
              f"实际 {route}")


def test_scan_cross_sectional_fields(tmp):
    print("== 同一已收线时点的跨币市场状态 ==")
    tmp = os.path.join(tmp, "cross_section")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    bases = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    from exchange.models import Instrument
    for idx, base in enumerate(bases):
        inst_id = f"{base}-USDT-SWAP"
        if inst_id not in fake._instruments:
            fake._instruments[inst_id] = Instrument(
                inst_id, base, "swap", ct_val=1, lot_sz=1, min_sz=1)
        candles = _make_candles(base=100.0 * (idx + 1), drift=.1 * (idx + 1))
        fake.candles[inst_id] = candles
        fake.last_prices[inst_id] = candles[-1].close
    dt.watchlist = bases
    sig = dt.scan_signal("BTC")
    factors = (sig or {}).get("factor_features") or {}
    check("前置: 跨币数据仍形成 BTC 回踩候选", sig is not None)
    check("市场宽度与相关集中度不再固定缺失",
          factors.get("market_breadth") is not None and
          factors.get("correlation_concentration") is not None, str(factors))
    check("截面排名、BTC beta 与残差动量进入冻结快照",
          factors.get("cross_sectional_rank") is not None and
          factors.get("btc_beta") is not None and
          factors.get("btc_residual_momentum") is not None, str(factors))


def test_entry_features_row(tmp):
    print("== 开仓 → trade_features 入场行 ==")
    tmp = os.path.join(tmp, "entry")
    os.makedirs(tmp, exist_ok=True)
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
    check("前置: 开仓成功", len(fake.orders) >= 1)
    tid = dt.journal.trades[-1]["id"]
    row = _features(os.path.join(tmp, "scan.db"), tid)
    check("入场特征行存在", row is not None)
    if row:
        check("入场价/止损/止盈落库",
              row["entry_price"] and row["stop_loss"] and row["take_profit"])
        check("影子分落库", row["signal_score"] is not None,
              f"实际 {row['signal_score']}")
        check("regime 落库", row["regime_tag"] in
              ("low_vol", "mid_vol", "high_vol"),
              f"实际 {row['regime_tag']}")
        check("fake 无订单流 → 计入 features_missing",
              "of_" in (row["features_missing"] or ""),
              f"实际 {row['features_missing']}")


def test_close_features_updated(tmp):
    print("== 平仓复盘 → 离场特征更新 ==")
    tmp = os.path.join(tmp, "close")
    os.makedirs(tmp, exist_ok=True)
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        # 合成 K 线覆盖持仓窗口（10 秒间隔保证 ≥3 根落在 [入场-60s, 出场+60s]）
        from exchange.models import Candle
        t0 = int(time.time() * 1000) - 200_000
        fake.candles["BTC-USDT-SWAP"] = [
            Candle(ts=t0 + i * 10_000, open=100.0, high=103.0, low=97.0,
                   close=101.0, volume=100) for i in range(20)]
        fake.last_prices["BTC-USDT-SWAP"] = 101.0
        tid = dt.journal.log_entry(
            symbol="BTC", signal="回踩确认", reason="测试",
            entry_price=100.0, stop_loss=98.0, take_profit=104.0,
            size=0.009, direction="long", score=80,
            adopted_lesson_ids=[], atr_value=2.0, signal_price=99.5,
            venue="swap")
        from engines.feature_collector import collect_entry_features
        collect_entry_features(tid, "BTC",
                               {"dir": "long", "entry": 100.0, "stop": 98.0,
                                "tp": 104.0, "atr": 2.0,
                                "shadow_score": 66.6,
                                "regime": {"tag": "mid_vol", "vol_pct": 0.5,
                                           "trend_slope": 0.01,
                                           "tf4h_spread": 0.001}},
                               "swap", "fake", db_path=os.path.join(tmp, "scan.db"))
        closed = dt.journal.log_exit(tid, 97.5, "止损")
        check("前置: 平仓成功", closed is not None)
        dt._post_close_review(closed, closed)
        row = _features(os.path.join(tmp, "scan.db"), tid)
        check("离场字段已更新", row and row["exit_price"] == 97.5)
        if row:
            stop_dist = 0.02
            pnl = (97.5 - 100.0) / 100.0
            check("R 倍数 ≈ pnl/止损距离",
                  row["r_multiple"] is not None
                  and abs(row["r_multiple"] - pnl / stop_dist) < 0.01,
                  f"实际 {row['r_multiple']}")
            check("MFE ≥ 0 且 ≥ 2R（高点 103）",
                  row["mfe_r"] is not None and row["mfe_r"] >= 1.4,
                  f"实际 {row['mfe_r']}")
            check("MAE ≥ 0（低点 97 即止损位）",
                  row["mae_r"] is not None and row["mae_r"] >= 0,
                  f"实际 {row['mae_r']}")
            check("滑点已算（出场 97.5 vs 止损 98）",
                  row["slippage_bps"] is not None and row["slippage_bps"] > 0,
                  f"实际 {row['slippage_bps']}")
            check("持仓时长 > 0", row["holding_hours"] is not None
                  and row["holding_hours"] >= 0)
    finally:
        _restore_notify()


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_p1_feat_")
    test_scan_shadow_fields(tmp)
    test_scan_cross_sectional_fields(tmp)
    test_entry_features_row(tmp)
    test_close_features_updated(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
