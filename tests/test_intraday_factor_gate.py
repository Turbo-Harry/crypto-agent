"""T4/T5 purged walk-forward、缺失/逻辑/DSR/PBO/稳定性验证。"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from factors.intraday_factor_gate import (EVALUATION_VERSION, evaluate_factor,
                                          purged_walk_forward_splits)
from factors.feature_registry import REGISTRY, extract_features
from engines.signal_scan import _cancellation_imbalance, _dynamic_ofi
from data.orderflow import OrderFlowAccumulator, multilevel_ofi_event
from decision.feature_transforms import (cross_sectional_snapshot,
                                         materialize_derived_features,
                                         technical_regime_features,
                                         volatility_5m_features)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def rows(n=720):
    out = []
    start = 1_700_000_000.0
    for i in range(n):
        value = ((i * 37) % 101 - 50) / 50
        pnl = 2.0 if value >= 0 else -1.0
        event_ts = start + i * 900
        out.append({"signal_id": f"s{i}", "event_ts": event_ts,
                    "label_end_ts": event_ts + 4 * 3600,
                    "symbol": ("BTC", "ETH", "SOL", "XRP")[i % 4],
                    "direction": "long" if i % 2 else "short",
                    "regime": ("low_vol", "mid_vol", "high_vol")[i % 3],
                    "month": "2026-08" if i < n // 2 else "2026-09",
                    "entry": 100.0, "stop": 99.0, "value": value,
                    "pnl_r": pnl})
    return out


def main():
    required = {"ofi_dynamic", "microprice_bps", "spread_bps",
                "open_interest_change", "basis", "btc_residual_momentum",
                "market_breadth", "realized_vol_5m", "vol_of_vol", "har_rv",
                "momentum_1h", "momentum_4h", "cross_sectional_rank",
                "funding_change", "funding_percentile", "btc_beta",
                "oi_price_interaction", "expected_slippage_bps",
                "source_latency_ms", "feature_missing_rate", "cancel_imbalance",
                "ofi_event_multilevel", "ofi_event_cancel_imbalance",
                "ofi_event_count", "ofi_event_age_ms",
                "trend_volume_confirmation", "wick_volume_absorption",
                "pullback_volume_confirmation", "trend_wick_alignment",
                "momentum_volume_confirmation", "bb_width_percentile",
                "bb_percent_b", "bb_squeeze_release", "adx",
                "directional_index_spread", "kaufman_efficiency_ratio",
                "vwap_distance_atr", "vwap_crossing_rate", "volume_zscore",
                "rv_to_har_ratio", "bb_squeeze_volume_confirmation"}
    check("首批微观/趋势/波动/拥挤/regime 因子均已注册",
          required <= set(REGISTRY), str(required - set(REGISTRY)))
    check("候选总数不超过预注册上限",
          len(REGISTRY) <= config.FACTOR_MAX_AUTO_CANDIDATES,
          f"{len(REGISTRY)}>{config.FACTOR_MAX_AUTO_CANDIDATES}")
    snapshot = __import__("json").dumps({
        "shadow_dims": {"trend": .8, "wick": .5, "depth": .75},
        "factor_features": {"volume_ratio": 1.5, "momentum_1h": -.02},
    })
    derived = extract_features({"features": snapshot})
    check("理论交互可由冻结快照统一复算",
          abs(derived["trend_volume_confirmation"] - 1.2) < 1e-9 and
          abs(derived["wick_volume_absorption"] - .75) < 1e-9 and
          abs(derived["pullback_volume_confirmation"] - 1.125) < 1e-9 and
          abs(derived["trend_wick_alignment"] - .4) < 1e-9 and
          abs(derived["momentum_volume_confirmation"] + .03) < 1e-9,
          str(derived))
    check("六维子分同步进入注册因子而非误报缺失",
          derived["trend"] == .8 and derived["wick"] == .5 and
          derived["depth"] == .75, str(derived))
    prices = [100.0]
    for idx in range(1, config.FACTOR_CROSS_SECTION_LOOKBACK_BARS):
        prices.append(prices[-1] * (1.001 + ((idx % 5) - 2) * .0001))
    cross = cross_sectional_snapshot({
        symbol: [value * scale for value in prices]
        for symbol, scale in (("BTC", 1), ("ETH", 2), ("SOL", .5),
                              ("XRP", .01), ("DOGE", .002))})
    check("跨币宽度/相关首特征值/BTC 残差可由已收线序列复算",
          cross["market_breadth"] == 1.0 and
          cross["correlation_concentration"] > .99 and
          abs(cross["by_symbol"]["BTC"]["btc_beta"] - 1.0) < 1e-9 and
          abs(cross["by_symbol"]["BTC"]["btc_residual_momentum"]) < 1e-9,
          str(cross))
    incomplete_cross = cross_sectional_snapshot({"BTC": prices})
    check("横截面不足 5 币时市场状态明确缺失",
          incomplete_cross["market_breadth"] is None and
          incomplete_cross["correlation_concentration"] is None,
          str(incomplete_cross))
    closes_5m = [100.0]
    for idx in range(config.FACTOR_5M_LOOKBACK_BARS):
        closes_5m.append(
            closes_5m[-1] * (1.0005 + ((idx % 7) - 3) * .00005))
    vol5 = volatility_5m_features(closes_5m)
    check("289 个已收线 5m close 同时产出 1h RV/vol-of-vol/HAR-RV",
          all(vol5[name] is not None for name in
              ("realized_vol_5m", "vol_of_vol", "har_rv")), str(vol5))
    technical_bars = []
    price = 100.0
    for idx in range(140):
        previous = price
        price *= 1.001 + ((idx % 9) - 4) * .00008
        technical_bars.append({
            "open": previous, "high": max(previous, price) + .15,
            "low": min(previous, price) - .12, "close": price,
            "volume": 1000.0 + (idx % 11) * 37.0})
    technical = technical_regime_features(technical_bars)
    check("布林/ADX/效率比/VWAP/量能特征由已收线 15m OHLCV 统一复算",
          all(technical[name] is not None for name in technical) and
          0 <= technical["bb_width_percentile"] <= 1 and
          0 <= technical["adx"] <= 1 and
          technical["directional_index_spread"] > 0 and
          technical["kaufman_efficiency_ratio"] > 0,
          str(technical))
    technical_derived = materialize_derived_features({
        **technical, "realized_vol_5m": .02, "har_rv": .01})
    check("RV/HAR 与 squeeze×volume 交互可复算",
          technical_derived["rv_to_har_ratio"] == 2.0 and
          technical_derived["bb_squeeze_volume_confirmation"] is not None,
          str(technical_derived))
    ofi, _ = _dynamic_ofi({"bids": [[100, 15]], "asks": [[101, 5]]},
                          (100.0, 10.0, 101.0, 10.0))
    check("动态 OFI 使用相邻盘口净变化并按深度归一", abs(ofi - 0.5) < 1e-9,
          str(ofi))
    cancel = _cancellation_imbalance(
        (100.0, 10.0, 101.0, 5.0), (100.0, 10.0, 101.0, 10.0))
    check("同价卖档撤单形成正撤单失衡", cancel == 1.0, str(cancel))
    prev = {"bids": [[100, 10], [99, 5]],
            "asks": [[101, 10], [102, 5]]}
    curr = {"bids": [[100, 15], [99, 5]],
            "asks": [[101, 5], [102, 5]]}
    event = multilevel_ofi_event(curr, prev, 2)
    check("多档事件 OFI 按 Cont 队列规则确定性复算",
          event is not None and abs(event[0] - 10.0) < 1e-9,
          str(event))
    acc = OrderFlowAccumulator(depth=2, window_seconds=60,
                               min_events=2, max_age_seconds=5)
    acc.update("BTC", prev, ts=100.0)
    acc.update("BTC", curr, ts=101.0)
    insufficient = acc.snapshot("BTC", now=101.0)
    acc.update("BTC", {"bids": [[100, 20], [99, 5]],
                       "asks": [[101, 5], [102, 5]]}, ts=102.0)
    ready = acc.snapshot("BTC", now=102.0)
    stale = acc.snapshot("BTC", now=108.0)
    check("事件数不足时 OFI 明确缺失",
          insufficient["status"] == "insufficient" and
          insufficient["ofi_event_multilevel"] is None,
          str(insufficient))
    check("事件数与新鲜度达标后才输出多档 OFI",
          ready["status"] == "ready" and ready["ofi_event_count"] == 2 and
          ready["ofi_event_multilevel"] is not None, str(ready))
    check("盘口断流后 OFI fail-closed 为 stale",
          stale["status"] == "stale" and
          stale["ofi_event_multilevel"] is None, str(stale))
    data = rows()
    splits = purged_walk_forward_splits(data)
    check("生成 5 折 walk-forward", len(splits) == 5, str(len(splits)))
    no_overlap = all(max(data[idx]["label_end_ts"] for idx in train) <=
                     min(data[idx]["event_ts"] for idx in test) -
                     config.FACTOR_EMBARGO_HOURS * 3600
                     for train, test in splits)
    check("purge+4H embargo 无标签重叠", no_overlap)
    tied = []
    for idx, row in enumerate(data):
        kline_ts = int(row["event_ts"] // 900)
        tied.append(dict(row, signal_id=f"t{idx}a", kline_ts=kline_ts))
        tied.append(dict(row, signal_id=f"t{idx}b", event_ts=row["event_ts"] + 1,
                         label_end_ts=row["label_end_ts"] + 1,
                         kline_ts=kline_ts))
    grouped_splits = purged_walk_forward_splits(tied)
    grouped_safe = all(
        {tied[idx]["kline_ts"] for idx in train}.isdisjoint(
            {tied[idx]["kline_ts"] for idx in test})
        for train, test in grouped_splits)
    check("同一 15m K 的跨币批次不会横跨训练与测试", grouped_safe)

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "factor.db")
        good = evaluate_factor(
            "synthetic_edge", "单调信息变量仅用于验证门自测", data,
            total_candidates=10, db_path=db)
        check("因子评价身份绑定隔离后的 Python 运行时",
              good["evaluation_version"] ==
              "intraday-factor-oos-v7-runtime-isolated" and
              good["evaluation_version"] == EVALUATION_VERSION,
              good["evaluation_version"])
        check("强且稳定的样本外因子通过", good["status"] == "validated", str(good))
        check("至少 4/5 折一致",
              good["fold_consistency"] >= config.FACTOR_MIN_CONSISTENT_FOLDS,
              str(good["folds"]))
        check("DSR 按概率 ≥0.95", good["dsr"] >= config.DSR_ACCEPT, str(good))
        check("PBO <0.3", good["pbo"] < config.PBO_ACCEPT, str(good))
        check("非单币贡献", good["symbol_concentration"] <=
              config.FACTOR_MAX_SYMBOL_CONCENTRATION, str(good))
        check("分方向/币种/regime/月份报告 OOS 稳定性",
              set(good["stability"]) == {"direction", "symbol", "regime", "month"},
              str(good["stability"]))
        import storage.db as sdb
        trial = sdb.q1("SELECT timeframe,horizon_hours,details FROM factor_trials "
                       "WHERE name='synthetic_edge' ORDER BY id DESC", db_path=db)
        check("因子证据绑定 15m/4h 且持久化稳定性",
              trial["timeframe"] == "15m" and trial["horizon_hours"] == 4 and
              "stability" in __import__("json").loads(trial["details"]),
              str(trial))
        b_trial = evaluate_factor(
            "synthetic_edge", "同一因子在 B 策略独立验证", data,
            total_candidates=10, db_path=db,
            strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID)
        strategy_rows = sdb.q(
            "SELECT DISTINCT strategy_id FROM factor_trials "
            "WHERE name='synthetic_edge'", db_path=db)
        check("A/B 因子试验身份与 trial_key 隔离",
              b_trial["trial_key"] != good["trial_key"] and
              {row["strategy_id"] for row in strategy_rows} ==
              {config.ENTRY_SIGNAL_STRATEGY_ID,
               config.BREAKOUT_SIGNAL_STRATEGY_ID}, str(strategy_rows))
        before = sdb.q1("SELECT COUNT(*) n FROM factor_trials", db_path=db)["n"]
        same = evaluate_factor(
            "synthetic_edge", "单调信息变量仅用于验证门自测", data,
            total_candidates=10, db_path=db)
        after_same = sdb.q1("SELECT COUNT(*) n FROM factor_trials", db_path=db)["n"]
        check("相同数据与评估版本重复运行不新增伪试验",
              same["trial_key"] == good["trial_key"] and before == after_same,
              f"before={before} after={after_same}")
        universe = evaluate_factor(
            "synthetic_edge", "单调信息变量仅用于验证门自测", data,
            total_candidates=11, db_path=db)
        after_universe = sdb.q1(
            "SELECT COUNT(*) n FROM factor_trials", db_path=db)["n"]
        check("多重检验候选宇宙变化生成独立试验身份",
              universe["trial_key"] != good["trial_key"] and
              after_universe == before + 1,
              f"before={before} after={after_universe}")
        changed_data = [dict(row) for row in data]
        changed_data[-1]["pnl_r"] += .01
        changed = evaluate_factor(
            "synthetic_edge", "单调信息变量仅用于验证门自测", changed_data,
            total_candidates=10, db_path=db)
        after_changed = sdb.q1("SELECT COUNT(*) n FROM factor_trials", db_path=db)["n"]
        check("标签变化生成新的数据身份与独立试验",
              changed["trial_key"] != good["trial_key"] and
              after_changed == before + 2,
              f"before={before} after={after_changed}")

        no_logic = evaluate_factor("gp_unknown", "", data,
                                   expression="wick*book", db_path=db)
        check("无经济逻辑只能 hypothesis_only",
              no_logic["status"] == "hypothesis_only", str(no_logic))
        missing = [dict(row, value=None if i % 2 else row["value"])
                   for i, row in enumerate(data)]
        miss_result = evaluate_factor("missing", "有逻辑", missing, db_path=db)
        check("缺失率超 10% 被拒绝", miss_result["status"] == "reject_missing",
              str(miss_result))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
