"""真实 SWAP 历史重放的时间对齐、缺失语义与幂等回归。"""

import os
import json
import sqlite3
import tempfile
import unittest

import config
from decision.forecast import empirical_first_passage
from decision.feature_transforms import volatility_5m_features
from decision.signal_outcomes import persist_outcome
from engines.signal_sampling import record_signal_sample
from engines.signal_scan import detect_pullback_setup
from tools.replay_15m_research import (BAR_MS, MarketReader,
                                      backfill_swap_market, inventory,
                                      replay_market)
from tools.evaluate_15m_research import (
    _calibration_summary, _forecast_risk_prior_summary, _outcome_summary,
    _passive_entry_summary,
    evaluate_research,
)


def _init_market(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE klines (inst_id TEXT,bar TEXT,open_time INTEGER,"
                 "open REAL,high REAL,low REAL,close REAL,volume REAL,"
                 "quote_volume REAL,PRIMARY KEY(inst_id,bar,open_time))")
    conn.execute("CREATE TABLE funding_rates (inst_id TEXT,funding_time INTEGER,"
                 "funding_rate REAL,realized_rate REAL,"
                 "PRIMARY KEY(inst_id,funding_time))")
    return conn


class Replay15mResearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market = os.path.join(self.tmp.name, "market.db")
        self.output = os.path.join(self.tmp.name, "research.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_shape_gate_long_short_and_mtf(self):
        long = {"open": 100.5, "high": 101, "low": 99,
                "close": 100.8}
        setup = detect_pullback_setup(long, 100, 99, 1.0)
        self.assertEqual(setup["direction"], "long")
        self.assertIsNone(detect_pullback_setup(
            long, 100, 99, 1.0, tf1h_trend=-1, tf4h_trend=1,
            mtf_enabled=True))
        short = {"open": 99.5, "high": 101, "low": 99,
                 "close": 99.2}
        self.assertEqual(detect_pullback_setup(
            short, 100, 101, 1.0)["direction"], "short")

    def test_inventory_rejects_spot_proxy(self):
        conn = _init_market(self.market)
        for bar in ("1m", "15m", "1H", "4H"):
            conn.execute("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                         ["BTC-USDT", bar, 0, 1, 1, 1, 1, 1, 1])
        conn.commit()
        conn.close()
        self.assertEqual(inventory(self.market)["eligible_swap_symbols"], 0)

    def test_backfill_keeps_only_confirmed_swap_bars_and_is_idempotent(self):
        now = 1_700_100_000_000
        complete = [str(now - 60_000), "1", "2", ".5", "1.5",
                    "10", "15", "0", "1"]
        open_bar = [str(now - 120_000), "1", "2", ".5", "1.5",
                    "10", "15", "0", "0"]

        def page(_inst, _bar, after):
            return [complete, open_bar] if after is None else []

        funding = {"fundingTime": str(now - 3_600_000),
                   "fundingRate": ".0001", "realizedRate": ".0001"}

        def funding_page(_inst, after):
            return [funding] if after is None else []

        first = backfill_swap_market(
            self.market, ["BTC-USDT-SWAP"], days=1, context_days=1,
            now_ms=now, request_delay=0, page_fetch=page,
            funding_fetch=funding_page)
        self.assertEqual(first["totals"]["inserted"], 4)
        self.assertEqual(first["totals"]["funding_inserted"], 1)
        second = backfill_swap_market(
            self.market, ["BTC-USDT-SWAP"], days=1, context_days=1,
            now_ms=now, request_delay=0, page_fetch=page,
            funding_fetch=funding_page)
        self.assertEqual(second["totals"]["inserted"], 0)
        self.assertEqual(second["totals"]["funding_inserted"], 0)
        funding_only = backfill_swap_market(
            self.market, ["BTC-USDT-SWAP"], days=1, context_days=1,
            now_ms=now, request_delay=0, include_klines=False,
            page_fetch=lambda *_args: self.fail("funding-only 不应请求 K 线"),
            funding_fetch=funding_page)
        self.assertTrue(funding_only["complete"])
        self.assertEqual(funding_only["series"], {})
        db = sqlite3.connect(self.market)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM klines").fetchone()[0], 4)
        db.close()
        with self.assertRaisesRegex(ValueError, "USDT-SWAP"):
            backfill_swap_market(self.market, ["BTC-USDT"], page_fetch=page)

    def test_backfill_retries_transient_page_failure(self):
        now = 1_700_100_000_000
        row = [str(now - 60_000), "1", "2", ".5", "1.5",
               "10", "15", "0", "1"]
        calls = {}

        def flaky(_inst, bar, after):
            key = (bar, after)
            calls[key] = calls.get(key, 0) + 1
            if calls[key] == 1:
                raise OSError("transient TLS EOF")
            return [row] if after is None else []

        result = backfill_swap_market(
            self.market, ["BTC-USDT-SWAP"], days=1, context_days=1,
            now_ms=now, request_delay=0, threads=4, retries=2,
            page_fetch=flaky, funding_fetch=lambda _inst, _after: [])
        self.assertTrue(result["complete"])
        self.assertFalse(result["errors"])
        self.assertEqual(result["totals"]["inserted"], 4)

    def test_market_reader_rebuilds_causal_5m_and_cross_section(self):
        conn = _init_market(self.market)
        event_ms = (1_700_000_000_000 // BAR_MS["15m"]) * BAR_MS["15m"]
        inst = "BTC-USDT-SWAP"
        minute_rows = []
        close = 100.0
        for idx in range(1450):
            close *= 1.0001 + ((idx % 7) - 3) * .00001
            ts = event_ms - (1450 - idx) * BAR_MS["1m"]
            minute_rows.append(
                [inst, "1m", ts, close, close, close, close, 1, close])
        # event 之后的极端价格不得进入信号时点 5m 特征。
        for idx in range(5):
            ts = event_ms + idx * BAR_MS["1m"]
            minute_rows.append(
                [inst, "1m", ts, 9999, 9999, 9999, 9999, 1, 9999])
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                         minute_rows)
        universe = []
        for offset, base in enumerate(("BTC", "ETH", "SOL", "XRP", "DOGE")):
            iid = f"{base}-USDT-SWAP"
            universe.append(iid)
            prices = [100.0 * (offset + 1)]
            for idx in range(1, config.FACTOR_CROSS_SECTION_LOOKBACK_BARS):
                prices.append(prices[-1] *
                              (1.001 + ((idx % 5) - 2) * .0001))
            for idx, value in enumerate(prices):
                ts = event_ms - (len(prices) - idx) * BAR_MS["15m"]
                conn.execute("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                             [iid, "15m", ts, value, value, value, value,
                              1, value])
        conn.commit()
        conn.close()
        reader = MarketReader(self.market)
        closes_5m = reader.five_minute_closes_asof(
            inst, event_ms, config.FACTOR_5M_LOOKBACK_BARS)
        cross = reader.cross_section_asof(event_ms, universe)
        reader.close()
        self.assertEqual(len(closes_5m), config.FACTOR_5M_LOOKBACK_BARS + 1)
        self.assertLess(closes_5m[-1], 200)
        vol5 = volatility_5m_features(closes_5m)
        self.assertTrue(all(vol5[name] is not None for name in
                            ("realized_vol_5m", "vol_of_vol", "har_rv")))
        self.assertEqual(cross["market_breadth"], 1.0)
        self.assertGreater(cross["correlation_concentration"], .99)
        self.assertIsNotNone(cross["by_symbol"]["ETH"]["btc_beta"])

    def test_replay_settles_real_path_marks_missing_and_is_idempotent(self):
        conn = _init_market(self.market)
        inst = "BTC-USDT-SWAP"
        start = 1_700_000_000_000
        rows_15m = []
        for idx in range(61):
            close = 100 + idx * .1
            rows_15m.append([inst, "15m", start + idx * BAR_MS["15m"],
                             close - .05, close + .1, close - .1, close, 100, 10000])
        # 最后一根制造多头回踩拒绝 K；只用这根收线后的数据结算。
        last = rows_15m[-1]
        last[3], last[4], last[5], last[6] = 105.7, 106.2, 104.0, 106.0
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)", rows_15m)
        for bar, step, count in (("1H", BAR_MS["1H"], 60),
                                 ("4H", BAR_MS["4H"], 60)):
            rows = []
            for idx in range(count):
                close = 95 + idx * .1
                rows.append([inst, bar, start - count * step + idx * step,
                             close - .05, close + .1, close - .1, close, 100, 10000])
            conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)", rows)
        event_ms = int(last[2]) + BAR_MS["15m"]
        conn.executemany(
            "INSERT INTO funding_rates VALUES (?,?,?,?)", [
                (inst, event_ms - 16 * 3_600_000, .0002, .0002),
                (inst, event_ms - 8 * 3_600_000, .001, .001),
                # 未来费率必须被 as-of 截止排除。
                (inst, event_ms + 8 * 3_600_000, .9, .9),
            ])
        # 4h 连续 1m；第 6 根高点会穿过 2ATR 止盈。
        one_minute = []
        for idx in range(240):
            high = 110 if idx == 5 else 106.1
            one_minute.append([inst, "1m", event_ms + idx * BAR_MS["1m"],
                               106, high, 105.9, 106, 10, 1000])
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)", one_minute)
        conn.commit()
        conn.close()

        reader = MarketReader(self.market)
        self.assertEqual(reader.funding_context_asof(inst, event_ms),
                         (.001, .0008))
        reader.close()

        first = replay_market(self.market, self.output, [inst])
        self.assertEqual(first["totals"]["created"], 1)
        self.assertEqual(first["totals"]["settled"], 1)
        db = sqlite3.connect(self.output)
        db.row_factory = sqlite3.Row
        sample = db.execute("SELECT * FROM signal_samples").fetchone()
        outcome = db.execute("SELECT * FROM signal_outcomes").fetchone()
        calibration = db.execute("SELECT * FROM forecast_calibration").fetchone()
        metadata = db.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'").fetchone()
        db.close()
        self.assertEqual(sample["venue"], "swap")
        self.assertEqual(sample["timeframe"], "15m")
        missing = json.loads(sample["missing_features"])
        self.assertNotIn("funding", missing)
        self.assertIn("book", missing)
        frozen = json.loads(sample["features"])
        self.assertIsNotNone(frozen["market_regime"])
        self.assertFalse(frozen["strategy_route"]["has_execution_authority"])
        self.assertEqual(frozen["factor_features"]["funding_rate"], .001)
        self.assertAlmostEqual(
            frozen["factor_features"]["funding_change"], .0008)
        self.assertNotEqual(frozen["factor_features"]["funding_rate"], .9)
        self.assertEqual(outcome["horizon_hours"], 4)
        self.assertEqual(outcome["tp_first"], 1)
        self.assertIsNotNone(calibration)
        self.assertEqual(calibration["signal_id"], sample["signal_id"])
        self.assertIn('"research_only": true', metadata["value"])

        # 数据管线 provenance 版本变化不能重抽同一候选的 bootstrap 路径。
        alt_output = os.path.join(self.tmp.name, "research-alt.db")
        import tools.replay_15m_research as replay_module
        previous_version = replay_module.REPLAY_VERSION
        try:
            replay_module.REPLAY_VERSION = "pipeline-provenance-only-change"
            replay_module.replay_market(self.market, alt_output, [inst])
        finally:
            replay_module.REPLAY_VERSION = previous_version
        alt_db = sqlite3.connect(alt_output)
        alt_features = alt_db.execute(
            "SELECT features FROM signal_samples").fetchone()[0]
        alt_metadata = json.loads(alt_db.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'").fetchone()[0])
        alt_db.close()
        self.assertEqual(json.loads(sample["features"])["forecast"],
                         json.loads(alt_features)["forecast"])
        self.assertEqual(alt_metadata["forecast_seed_version"],
                         config.FORECAST_REPLAY_SEED_VERSION)

        second = replay_market(self.market, self.output, [inst])
        self.assertEqual(second["totals"]["created"], 0)
        db = sqlite3.connect(self.output)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_samples").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0], 1)
        db.close()

    def test_strategy_b_replay_and_evaluation_are_strictly_scoped(self):
        conn = _init_market(self.market)
        inst = "BTC-USDT-SWAP"
        start = 1_700_000_000_000
        rows_15m = []
        for idx in range(61):
            rows_15m.append([
                inst, "15m", start + idx * BAR_MS["15m"],
                100.0, 100.5, 99.5, 100.0, 100.0, 10_000.0])
        rows_15m[-1][3:] = [100.4, 102.0, 100.3, 101.8, 200.0, 20_360.0]
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                         rows_15m)
        for bar, step in (("1H", BAR_MS["1H"]), ("4H", BAR_MS["4H"])):
            context = []
            for idx in range(60):
                close = 95.0 + idx * .05
                context.append([
                    inst, bar, start - (60 - idx) * step,
                    close, close + .2, close - .2, close, 100, close * 100])
            conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                             context)
        event_ms = int(rows_15m[-1][2]) + BAR_MS["15m"]
        one_minute = []
        for idx in range(240):
            high = 105.0 if idx == 5 else 102.0
            one_minute.append([
                inst, "1m", event_ms + idx * BAR_MS["1m"],
                101.8, high, 101.7, 101.8, 10, 1_018])
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                         one_minute)
        conn.commit()
        conn.close()

        result = replay_market(
            self.market, self.output, [inst],
            strategy_ids=[config.BREAKOUT_SIGNAL_STRATEGY_ID])
        self.assertEqual(
            result["totals"]["by_strategy"][
                config.BREAKOUT_SIGNAL_STRATEGY_ID]["created"], 1)
        db = sqlite3.connect(self.output)
        db.row_factory = sqlite3.Row
        sample = db.execute("SELECT * FROM signal_samples").fetchone()
        outcome = db.execute("SELECT * FROM signal_outcomes").fetchone()
        db.close()
        self.assertEqual(sample["strategy_id"],
                         config.BREAKOUT_SIGNAL_STRATEGY_ID)
        self.assertEqual(sample["feature_schema_version"],
                         config.SIGNAL_FEATURE_SCHEMA_VERSION)
        self.assertEqual(outcome["tp_first"], 1)
        frozen = json.loads(sample["features"])
        self.assertEqual(frozen["provenance"]["strategy_id"],
                         config.BREAKOUT_SIGNAL_STRATEGY_ID)
        self.assertEqual(
            frozen["strategy_route"]["has_execution_authority"], False)
        self.assertEqual(
            frozen["factor_features"]["source_latency_ms"], 0.0)

        evaluation = evaluate_research(
            self.output, strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID)
        self.assertEqual(evaluation["scope"]["strategy_id"],
                         config.BREAKOUT_SIGNAL_STRATEGY_ID)
        self.assertEqual(evaluation["coverage"]["candidates"], 1)
        self.assertEqual(evaluation["coverage"]["outcomes"], 1)
        # 旧 replay 只有固定 ATR 路径标签，没有可审计的净 USDT v1
        # 数量/成本口径；仍计入路径覆盖，但不得进入新版入场训练。
        self.assertEqual(evaluation["models"]["entry"]["long"]["n"], 0)
        self.assertEqual(evaluation["models"]["entry"]["long"]["status"],
                         "insufficient_data")
        self.assertEqual(
            sum(item["n"] for item in
                evaluation["segments"]["market_regime"].values()), 1)
        db = sqlite3.connect(self.output)
        strategies = {row[0] for row in db.execute(
            "SELECT DISTINCT strategy_id FROM factor_trials")}
        scoped_kv = db.execute(
            "SELECT COUNT(*) FROM kv WHERE key=?",
            ("research.15m_evaluation.B_breakout.latest",)).fetchone()[0]
        db.close()
        self.assertEqual(strategies, {config.BREAKOUT_SIGNAL_STRATEGY_ID})
        self.assertEqual(scoped_kv, 1)

    def test_output_guard_rejects_runtime_database_name(self):
        conn = _init_market(self.market)
        conn.close()
        with self.assertRaisesRegex(ValueError, "运行数据库"):
            replay_market(self.market,
                          os.path.join(self.tmp.name, "crypto_agent.db"), [])

    def test_empirical_probability_respects_as_of_cutoff(self):
        ids = []
        for event_ts, kline_ts in ((1.0, 1_000), (20_000.0, 20_000_000)):
            signal_id, _ = record_signal_sample(
                "BTC", {"dir": "long", "entry": 100, "stop": 99,
                        "tp": 102, "atr": 1, "kline_ts": kline_ts,
                        "shadow_dims": {}}, "swap", db_path=self.output,
                event_ts=event_ts)
            ids.append(signal_id)
        for idx, signal_id in enumerate(ids):
            persist_outcome({
                "signal_id": signal_id, "horizon_hours": 4,
                "tp_first": 1 if idx == 0 else 0,
                "sl_first": 0 if idx == 0 else 1,
                "timeout": 0, "ambiguous": 0,
                "pnl_r": 2 if idx == 0 else -1, "mfe_r": 2, "mae_r": 1,
                "high_ret_h": .02, "low_ret_h": -.01,
                "time_to_tp_sec": 1 if idx == 0 else None,
                "time_to_sl_sec": None if idx == 0 else 1,
                "time_to_high_sec": 1, "time_to_low_sec": 1,
                "settled_at": 30_000, "bar_resolution": "1m",
                "label_version": config.SIGNAL_OUTCOME_LABEL_VERSION,
            }, db_path=self.output)
        from storage import db
        db.x(
            "INSERT INTO signal_samples (signal_id,symbol,direction,event_ts,"
            "kline_ts,timeframe,venue,strategy_version,config_hash,"
            "feature_schema_version,entry,stop,tp,atr,horizon_hours,features,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["old-forecast-scope", "ETH", "long", 2.0, 2_000,
             config.SIGNAL_SAMPLE_TIMEFRAME, "swap", "old-strategy",
             "old-config", "signal-features-v4", 100.0, 99.0, 102.0, 1.0,
             config.SIGNAL_OUTCOME_HORIZON_HOURS, "{}", 2.0, 2.0],
            db_path=self.output)
        persist_outcome({
            "signal_id": "old-forecast-scope", "horizon_hours": 4,
            "tp_first": 1, "sl_first": 0, "timeout": 0, "ambiguous": 0,
            "pnl_r": 2, "mfe_r": 2, "mae_r": .1,
            "high_ret_h": .02, "low_ret_h": -.01,
            "time_to_tp_sec": 1, "time_to_sl_sec": None,
            "time_to_high_sec": 1, "time_to_low_sec": 1,
            "settled_at": 30_000, "bar_resolution": "1m",
            "label_version": config.SIGNAL_OUTCOME_LABEL_VERSION,
        }, db_path=self.output)
        historical = empirical_first_passage(
            self.output, "long", as_of_ts=15_000)
        self.assertEqual(historical["n"], 1)
        self.assertEqual(historical["p_tp"], 1.0)

    def test_research_evaluator_requires_provenance_and_runs_full_gate(self):
        sqlite3.connect(self.output).close()
        with self.assertRaisesRegex(ValueError, "research-only"):
            evaluate_research(self.output)

        from storage import db
        db.init_db(self.output)
        metadata = {"research_only": True, "source_venue": "OKX SWAP",
                    "totals": {"signals": 0, "settled": 0}}
        db.x("INSERT INTO kv (key,value,updated_at) VALUES (?,?,?)",
             ["research.15m_replay.latest", json.dumps(metadata), 1],
             db_path=self.output)
        signal_id, _ = record_signal_sample(
            "BTC", {"dir": "long", "entry": 100, "stop": 99, "tp": 102,
                    "atr": 1, "kline_ts": 1_700_000_000_000,
                    "shadow_dims": {}}, "swap", db_path=self.output,
            event_ts=1_700_000_900)
        persist_outcome({
            "signal_id": signal_id, "horizon_hours": 4,
            "tp_first": 1, "sl_first": 0, "timeout": 0, "ambiguous": 0,
            "pnl_r": 2, "mfe_r": 2, "mae_r": 0.5,
            "high_ret_h": .02, "low_ret_h": -.005,
            "time_to_tp_sec": 60, "time_to_sl_sec": None,
            "time_to_high_sec": 60, "time_to_low_sec": 120,
            "settled_at": 1_700_015_300, "bar_resolution": "1m",
            "label_version": config.SIGNAL_OUTCOME_LABEL_VERSION,
        }, db_path=self.output)
        result = evaluate_research(self.output)
        self.assertTrue(result["research_only"])
        self.assertEqual(result["coverage"]["candidates"], 1)
        self.assertEqual(result["coverage"]["outcomes"], 1)
        self.assertEqual(result["models"]["entry"]["long"]["n"], 0)
        self.assertEqual(result["models"]["entry"]["long"]["status"],
                         "insufficient_data")
        self.assertEqual(result["models"]["extrema"]["long"]["n"], 1)
        self.assertEqual(result["decision"]["status"], "stop_no_promotion")
        self.assertFalse(result["decision"]["budget_expansion_allowed"])
        self.assertGreater(result["factors"]["tested"], 0)

    def test_research_evaluator_rejects_runtime_database_name(self):
        runtime = os.path.join(self.tmp.name, "crypto_agent.db")
        sqlite3.connect(runtime).close()
        with self.assertRaisesRegex(ValueError, "运行数据库"):
            evaluate_research(runtime)

    def test_research_evaluator_cost_and_brier_are_reproducible(self):
        outcome = _outcome_summary([
            {"direction": "long", "entry": 100, "stop": 99, "pnl_r": 2,
             "horizon_hours": 4, "funding_rate": .001,
             "tp_first": 1, "sl_first": 0, "timeout": 0},
            {"direction": "short", "entry": 100, "stop": 101, "pnl_r": -1,
             "tp_first": 0, "sl_first": 1, "timeout": 0},
        ])
        self.assertAlmostEqual(outcome["all"]["gross_ev_r"], 0.5)
        self.assertAlmostEqual(outcome["all"]["net_ev_r"], .275)
        calibration = _calibration_summary([
            {"p_hit_tp": .9, "p_hit_sl": .1, "p_timeout": 0,
             "hit_tp": 1, "hit_sl": 0, "timeout": 0},
            {"p_hit_tp": .1, "p_hit_sl": .9, "p_timeout": 0,
             "hit_tp": 0, "hit_sl": 1, "timeout": 0},
        ])
        self.assertEqual(calibration["n"], 2)
        self.assertGreater(calibration["brier_skill"]["multiclass"], 0)

    def test_passive_entry_replay_is_conservative_and_counts_unfilled(self):
        conn = _init_market(self.market)
        inst = "BTC-USDT-SWAP"
        start = 1_700_000_000_000
        bars = []
        for idx in range(600):
            ts = start + idx * BAR_MS["1m"]
            if idx == 0:
                row = [inst, "1m", ts, 100.2, 100.3, 99.7, 100.1, 1, 100]
            elif idx == 1:
                row = [inst, "1m", ts, 100.1, 102.1, 100.0, 102.0, 1, 102]
            else:
                row = [inst, "1m", ts, 101.0, 101.2, 100.5, 101.0, 1, 101]
            bars.append(row)
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)", bars)
        conn.commit()
        conn.close()
        rows = [
            {"signal_id": "fill", "event_ts": start / 1000,
             "symbol": "BTC", "direction": "long", "entry": 100,
             "stop": 99, "tp": 102, "horizon_hours": 4,
             "features": "{}", "pnl_r": 2},
            {"signal_id": "unfilled", "event_ts": (start + 5 * 3_600_000) / 1000,
             "symbol": "BTC", "direction": "long", "entry": 100,
             "stop": 99, "tp": 102, "horizon_hours": 4,
             "features": "{}", "pnl_r": -1},
        ]
        result = _passive_entry_summary(rows, self.market)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["complete"], 1)
        self.assertEqual(result["unfilled"], 1)
        self.assertEqual(result["tp_first"], 1)
        self.assertAlmostEqual(result["net_ev_r_per_fill"], 1.85)
        self.assertAlmostEqual(result["net_ev_r_per_candidate"], .925)
        self.assertEqual(result["status"], "stop_no_promotion")
        recovery = _passive_entry_summary(rows, self.market, .002)
        self.assertEqual(recovery["fills"], 1)
        self.assertEqual(recovery["tp_first"], 1)
        self.assertEqual(
            recovery["policy"],
            "roundtrip_cost_recovery_limit_one_15m_bar")
        self.assertGreater(recovery["net_ev_r_per_fill"], 1.84)

    def test_passive_entry_prefers_confirmed_v2_over_legacy_market_rows(self):
        conn = _init_market(self.market)
        conn.execute(
            "CREATE TABLE klines_v2 (source TEXT,venue TEXT,time_zone TEXT,"
            "inst_id TEXT,bar TEXT,open_time INTEGER,close_time INTEGER,"
            "open REAL,high REAL,low REAL,close REAL,volume REAL,"
            "quote_volume REAL,confirmed INTEGER,ingested_at REAL,"
            "as_of_ms INTEGER,raw_hash TEXT)")
        inst = "BTC-USDT-SWAP"
        start = 1_700_000_000_000
        legacy, confirmed = [], []
        for idx in range(300):
            ts = start + idx * BAR_MS["1m"]
            # legacy 永远不触 100 的 long limit；v2 第一分钟成交，下一分钟 TP。
            legacy.append([inst, "1m", ts, 110, 111, 109, 110, 1, 110])
            if idx == 0:
                o, high, low, close = 100.2, 100.3, 99.7, 100.1
            elif idx == 1:
                o, high, low, close = 100.1, 102.1, 100.0, 102.0
            else:
                o, high, low, close = 101.0, 101.2, 100.5, 101.0
            confirmed.append([
                "okx", "swap", "UTC", inst, "1m", ts,
                ts + BAR_MS["1m"], o, high, low, close, 1, close, 1,
                1.0, ts + BAR_MS["1m"], f"hash-{idx}"])
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?)", legacy)
        conn.executemany(
            "INSERT INTO klines_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            confirmed)
        conn.commit()
        conn.close()
        result = _passive_entry_summary([{
            "signal_id": "confirmed-fill", "event_ts": start / 1000,
            "symbol": "BTC", "direction": "long", "entry": 100,
            "stop": 99, "tp": 102, "horizon_hours": 4,
            "features": "{}", "pnl_r": 2,
        }], self.market)
        self.assertEqual(result["market_table"], "klines_v2")
        self.assertEqual(
            result["evaluation_version"],
            "passive-entry-v2-confirmed-klines")
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["tp_first"], 1)

    def test_forecast_risk_prior_uses_frozen_probability_and_full_cost(self):
        rows = []
        start = 1_700_000_000
        for index in range(100):
            rejected = index % 2 == 0
            rows.append({
                "signal_id": f"sig-{index}",
                "event_ts": start + index * 900,
                "symbol": "BTC" if index % 4 < 2 else "ETH",
                "direction": "long", "entry": 100, "stop": 99,
                "horizon_hours": 4, "funding_rate": 0,
                "features": json.dumps({
                    "forecast": {"p_hit_sl": .8 if rejected else .2}}),
                "pnl_r": -1 if rejected else 2,
                "tp_first": 0 if rejected else 1,
                "sl_first": 1 if rejected else 0,
                "timeout": 0,
            })
        result = _forecast_risk_prior_summary(rows)
        self.assertEqual(result["usable"], 100)
        self.assertEqual(result["reject_n"], 50)
        self.assertEqual(result["accepted_n"], 50)
        self.assertEqual(result["blocked_loss_precision"], 1.0)
        self.assertGreater(result["incremental_ev_r_per_candidate"], 0)
        self.assertGreater(result["accepted_net_ev_lower_95"], 0)
        self.assertGreater(result["brier_skill"], 0)
        self.assertEqual(result["positive_folds"], 5)
        self.assertEqual(result["status"], "stop_no_promotion")


if __name__ == "__main__":
    unittest.main()
