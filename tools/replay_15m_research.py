#!/usr/bin/env python3
"""把真实 OKX SWAP 历史行情重放成独立的 15m/4h 研究数据集。

输入库只读，必须包含 ``*-USDT-SWAP`` 的 1m/15m/1H/4H K 线；输出必须是
独立研究库，明确拒绝生产/模拟盘运行库。默认仅盘点，``--apply`` 才写入。
历史重放不含信号时点盘口与 OI；资金费使用信号时点之前最近一次已结算费率，
绝不读取未来结算。盘口/OI 仍保持缺失，不计作“六维完整平仓”或 Agent 判断样本。
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.feature_transforms import (cross_sectional_snapshot,
                                         materialize_derived_features,
                                         technical_regime_features,
                                         volatility_5m_features)
from decision.market_regime import classify_market_regime
from decision.signal_outcomes import persist_outcome, settle_path
from decision.strategy_router import route_strategy
from engines.feature_collector import compute_regime
from engines.signal_sampling import (merge_sample_features, record_signal_sample,
                                     update_signal_decision)
from engines.signal_scan import (compute_shadow_score, compute_targets,
                                 detect_pullback_setup)
from engines.strategy_b import breakout_signal, enrich_shadow_signal
from factors.feature_registry import REGISTRY
from strategy.indicators import atr, ema

REPLAY_VERSION = "swap-15m-replay-v4"
BAR_MS = {"1m": 60_000, "15m": 900_000, "1H": 3_600_000,
          "4H": 14_400_000}
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}
OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_FUNDING_HISTORY_URL = "https://www.okx.com/api/v5/public/funding-rate-history"


class MarketReader:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        # Current collector writes strict confirmed SWAP rows to klines_v2.
        # Legacy/replay fixtures keep using klines; once v2 exists we never
        # silently fall back to the known-unfinalized legacy snapshots.
        has_v2 = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='klines_v2'").fetchone()
        self._kline_table = "klines_v2" if has_v2 else "klines"
        self._series_cache: dict[tuple[str, str], list[list[float]]] = {}
        self._times_cache: dict[tuple[str, str], list[int]] = {}
        self._cross_cache: dict[tuple[int, tuple[str, ...]], dict] = {}

    def close(self) -> None:
        self.conn.close()

    def symbols(self) -> list[str]:
        rows = self.conn.execute(
            f"SELECT inst_id FROM {self._kline_table} "
            "WHERE inst_id LIKE '%-USDT-SWAP' "
            "AND bar IN ('1m','15m','1H','4H') GROUP BY inst_id "
            "HAVING COUNT(DISTINCT bar)=4 ORDER BY inst_id").fetchall()
        return [str(row[0]) for row in rows]

    def series(self, inst_id: str, bar: str) -> list[list[float]]:
        key = (inst_id, bar)
        if key in self._series_cache:
            return self._series_cache[key]
        rows = self.conn.execute(
            "SELECT open_time,open,high,low,close,volume,quote_volume "
            f"FROM {self._kline_table} WHERE inst_id=? AND bar=? "
            "ORDER BY open_time",
            [inst_id, bar]).fetchall()
        result = [[int(row[0]), *[float(value or 0) for value in row[1:]]]
                  for row in rows]
        self._series_cache[key] = result
        self._times_cache[key] = [int(row[0]) for row in result]
        return result

    def closed_closes_asof(self, inst_id: str, bar: str, event_ms: int,
                           limit: int) -> list[float]:
        rows = self.series(inst_id, bar)
        times = self._times_cache[(inst_id, bar)]
        end = bisect.bisect_right(times, int(event_ms) - BAR_MS[bar])
        return [float(row[4]) for row in rows[max(0, end - limit):end]
                if float(row[4]) > 0]

    def five_minute_closes_asof(self, inst_id: str, event_ms: int,
                                returns_required: int) -> list[float]:
        """从完整 1m bar 因果聚合 5m close；遇缺口只保留尾部连续段。"""
        rows = self.series(inst_id, "1m")
        times = self._times_cache[(inst_id, "1m")]
        end = bisect.bisect_right(times, int(event_ms) - BAR_MS["1m"])
        needed_minutes = (int(returns_required) + 1) * 5
        prefix = rows[max(0, end - needed_minutes - 10):end]
        buckets: dict[int, dict[str, Any]] = {}
        for row in prefix:
            bucket = int(row[0]) // 300_000 * 300_000
            item = buckets.setdefault(bucket, {"count": 0, "close": None})
            item["count"] += 1
            item["close"] = float(row[4])
        complete = [(bucket, item["close"]) for bucket, item in buckets.items()
                    if item["count"] == 5 and item["close"] is not None]
        complete.sort()
        trailing = []
        for bucket, close in reversed(complete):
            if trailing and trailing[-1][0] - bucket != 300_000:
                break
            trailing.append((bucket, close))
        trailing.reverse()
        return [float(close) for _, close in trailing[-returns_required - 1:]]

    def cross_section_asof(self, event_ms: int,
                           universe: list[str]) -> dict:
        key = (int(event_ms), tuple(sorted(universe)))
        if key in self._cross_cache:
            return self._cross_cache[key]
        closes = {}
        for inst_id in universe:
            base = inst_id.removesuffix("-USDT-SWAP")
            values = self.closed_closes_asof(
                inst_id, "15m", event_ms,
                config.FACTOR_CROSS_SECTION_LOOKBACK_BARS)
            if values:
                closes[base] = values
        result = cross_sectional_snapshot(closes)
        self._cross_cache[key] = result
        return result

    def funding_asof(self, inst_id: str, event_ms: int) -> float | None:
        """Latest completed funding rate at or before the signal timestamp."""
        try:
            row = self.conn.execute(
                "SELECT funding_rate FROM funding_rates WHERE inst_id=? "
                "AND funding_time<=? ORDER BY funding_time DESC LIMIT 1",
                [inst_id, int(event_ms)]).fetchone()
        except sqlite3.Error:
            return None
        return float(row[0]) if row and row[0] is not None else None

    def funding_context_asof(self, inst_id: str, event_ms: int) -> tuple[
            float | None, float | None]:
        """Return latest rate and causal change from the previous settlement."""
        try:
            rows = self.conn.execute(
                "SELECT funding_rate FROM funding_rates WHERE inst_id=? "
                "AND funding_time<=? ORDER BY funding_time DESC LIMIT 2",
                [inst_id, int(event_ms)]).fetchall()
        except sqlite3.Error:
            return None, None
        current = float(rows[0][0]) if rows and rows[0][0] is not None else None
        previous = (float(rows[1][0]) if len(rows) > 1 and
                    rows[1][0] is not None else None)
        change = current - previous if current is not None and previous is not None else None
        return current, change

    def funding_percentile_asof(self, inst_id: str, event_ms: int,
                                universe: list[str]) -> float | None:
        current = self.funding_asof(inst_id, event_ms)
        if current is None:
            return None
        values = [value for value in
                  (self.funding_asof(symbol, event_ms) for symbol in universe)
                  if value is not None]
        if len(values) < 3:
            return None
        return sum(value <= current for value in values) / len(values)


def _init_market_db(path: str) -> sqlite3.Connection:
    if os.path.basename(os.path.realpath(path)) in RUNTIME_DB_NAMES:
        raise ValueError("拒绝把历史行情写入运行数据库")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS klines (inst_id TEXT NOT NULL,bar TEXT NOT NULL,"
        "open_time INTEGER NOT NULL,open REAL,high REAL,low REAL,close REAL,"
        "volume REAL,quote_volume REAL,PRIMARY KEY(inst_id,bar,open_time))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_klines_inst_bar "
                 "ON klines(inst_id,bar,open_time)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS funding_rates (inst_id TEXT NOT NULL,"
        "funding_time INTEGER NOT NULL,funding_rate REAL,realized_rate REAL,"
        "PRIMARY KEY(inst_id,funding_time))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_funding_inst_time "
                 "ON funding_rates(inst_id,funding_time)")
    conn.commit()
    return conn


def _fetch_history_page(inst_id: str, bar: str, after: int | None = None,
                        timeout: float = 20.0) -> list[list[str]]:
    params = {"instId": inst_id, "bar": bar, "limit": "100"}
    if after is not None:
        params["after"] = str(after)
    url = OKX_HISTORY_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX history-candles error: {payload.get('code')}")
    return payload.get("data") or []


def _fetch_funding_page(inst_id: str, after: int | None = None,
                        timeout: float = 20.0) -> list[dict[str, Any]]:
    params = {"instId": inst_id, "limit": "100"}
    if after is not None:
        params["after"] = str(after)
    url = OKX_FUNDING_HISTORY_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX funding-rate-history error: {payload.get('code')}")
    return payload.get("data") or []


def backfill_swap_market(market_db: str, symbols: list[str], *, days: int = 8,
                         context_days: int = 30, now_ms: int | None = None,
                         request_delay: float = 0.12,
                         threads: int = 1,
                         retries: int = 4,
                         page_fetch=None,
                         funding_fetch=None,
                         include_klines: bool = True,
                         include_funding: bool = True,
                         progress: bool = False) -> dict[str, Any]:
    """分页获取真实 SWAP K 线/资金费；仅写独立行情库，重复运行幂等。"""
    if not symbols or any(not item.endswith("-USDT-SWAP") for item in symbols):
        raise ValueError("--symbols 必须全部是完整的 *-USDT-SWAP")
    conn = _init_market_db(market_db)
    fetch = page_fetch or _fetch_history_page
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    totals = {"requests": 0, "received": 0, "inserted": 0,
              "funding_requests": 0, "funding_received": 0,
              "funding_inserted": 0}
    per_series = {}
    per_funding = {}
    errors = []
    rate_lock = threading.Lock()
    next_request_at = [0.0]

    def limited_fetch(inst_id, bar, cursor):
        last_error = None
        for attempt in range(max(1, int(retries))):
            if request_delay > 0:
                with rate_lock:
                    now_monotonic = time.monotonic()
                    wait = max(0.0, next_request_at[0] - now_monotonic)
                    if wait:
                        time.sleep(wait)
                    next_request_at[0] = time.monotonic() + request_delay
            try:
                return fetch(inst_id, bar, cursor)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, int(retries)):
                    time.sleep(min(2.0, .25 * (2 ** attempt)))
        raise last_error

    def download_series(inst_id, bar):
        duration_days = context_days if bar in ("1H", "4H") else days
        since = now - int(duration_days) * 86_400_000
        target_rows = math.ceil((now - since) / BAR_MS[bar])
        max_pages = math.ceil(target_rows / 100) + 5
        cursor = None
        received = requests = 0
        unique = {}
        for _ in range(max_pages):
            page = limited_fetch(inst_id, bar, cursor)
            requests += 1
            if progress and requests % 50 == 0:
                print(f"[backfill] {inst_id}:{bar} pages={requests} "
                      f"rows={len(unique)}", file=sys.stderr, flush=True)
            if not page:
                break
            for row in page:
                try:
                    open_time = int(row[0])
                    if open_time < since or open_time >= now:
                        continue
                    if len(row) > 8 and str(row[8]) != "1":
                        continue
                    unique[open_time] = (
                        inst_id, bar, open_time, float(row[1]), float(row[2]),
                        float(row[3]), float(row[4]), float(row[5] or 0),
                        float(row[6] or 0))
                except (IndexError, TypeError, ValueError):
                    continue
            received = len(unique)
            oldest = min(int(row[0]) for row in page)
            if oldest <= since or oldest == cursor:
                break
            cursor = oldest
        return inst_id, bar, since, target_rows, requests, received, list(unique.values())

    try:
        tasks = ([(inst_id, bar) for inst_id in symbols
                  for bar in ("1m", "15m", "1H", "4H")]
                 if include_klines else [])
        with ThreadPoolExecutor(max_workers=max(1, min(int(threads), 16))) as pool:
            futures = [pool.submit(download_series, inst_id, bar)
                       for inst_id, bar in tasks]
            for future in as_completed(futures):
                try:
                    inst_id, bar, since, target_rows, requests, received, parsed = future.result()
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
                    continue
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?,?)", parsed)
                conn.commit()
                inserted = conn.total_changes - before
                totals["requests"] += requests
                totals["received"] += received
                totals["inserted"] += inserted
                count = conn.execute(
                    "SELECT COUNT(*) FROM klines WHERE inst_id=? AND bar=? "
                    "AND open_time>=? AND open_time<?",
                    [inst_id, bar, since, now]).fetchone()[0]
                per_series[f"{inst_id}:{bar}"] = {
                    "received": received, "inserted": inserted,
                    "available": int(count), "expected": int(target_rows),
                    "coverage": min(1.0, int(count) / max(1, int(target_rows)))}
                if progress:
                    print(f"[backfill] complete {inst_id}:{bar} "
                          f"coverage={per_series[f'{inst_id}:{bar}']['coverage']:.2%}",
                          file=sys.stderr, flush=True)
        if include_funding:
            fetch_funding = funding_fetch or _fetch_funding_page
            since = now - int(days) * 86_400_000
            for inst_id in symbols:
                cursor = None
                unique = {}
                requests = 0
                try:
                    while True:
                        last_error = None
                        page = None
                        for attempt in range(max(1, int(retries))):
                            if request_delay > 0:
                                with rate_lock:
                                    wait = max(0.0, next_request_at[0] -
                                               time.monotonic())
                                    if wait:
                                        time.sleep(wait)
                                    next_request_at[0] = time.monotonic() + request_delay
                            try:
                                page = fetch_funding(inst_id, cursor)
                                break
                            except Exception as exc:
                                last_error = exc
                                if attempt + 1 < max(1, int(retries)):
                                    time.sleep(min(2.0, .25 * (2 ** attempt)))
                        if page is None:
                            raise last_error or RuntimeError("funding fetch failed")
                        requests += 1
                        if not page:
                            break
                        for row in page:
                            try:
                                funding_time = int(row["fundingTime"])
                                if since <= funding_time < now:
                                    unique[funding_time] = (
                                        inst_id, funding_time,
                                        float(row.get("fundingRate", 0) or 0),
                                        float(row.get("realizedRate", 0) or 0))
                            except (KeyError, TypeError, ValueError):
                                continue
                        oldest = min(int(row["fundingTime"]) for row in page)
                        if oldest <= since or oldest == cursor or len(page) < 100:
                            break
                        cursor = oldest
                    before = conn.total_changes
                    conn.executemany(
                        "INSERT OR IGNORE INTO funding_rates VALUES (?,?,?,?)",
                        list(unique.values()))
                    conn.commit()
                    inserted = conn.total_changes - before
                    totals["funding_requests"] += requests
                    totals["funding_received"] += len(unique)
                    totals["funding_inserted"] += inserted
                    count = conn.execute(
                        "SELECT COUNT(*) FROM funding_rates WHERE inst_id=? "
                        "AND funding_time>=? AND funding_time<?",
                        [inst_id, since, now]).fetchone()[0]
                    per_funding[inst_id] = {
                        "received": len(unique), "inserted": inserted,
                        "available": int(count)}
                except Exception as exc:
                    errors.append(
                        f"{inst_id}:funding:{type(exc).__name__}: {str(exc)[:140]}")
        return {"market_db": os.path.abspath(market_db), "symbols": symbols,
                "days": days, "context_days": context_days,
                "totals": totals, "series": per_series,
                "funding": per_funding,
                "errors": errors, "complete": not errors and
                len(per_series) == len(tasks) and
                (not include_funding or len(per_funding) == len(symbols))}
    finally:
        conn.close()


def _closed_prefix(rows: list[list[float]], bar: str, event_ms: int,
                   limit: int | None = None) -> list[list[float]]:
    times = [int(row[0]) for row in rows]
    end = bisect.bisect_right(times, event_ms - BAR_MS[bar])
    start = max(0, end - limit) if limit else 0
    return rows[start:end]


def _trend(rows: list[list[float]], bar: str, event_ms: int) -> tuple[int, list[float]]:
    prefix = _closed_prefix(rows, bar, event_ms, 60)
    closes = [float(row[4]) for row in prefix]
    if len(closes) < 50:
        return 0, closes
    e20, e50 = ema(closes, 20), ema(closes, 50)
    return (1 if e20[-1] > e50[-1] else -1), closes


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[idx] / closes[idx - 1])
            for idx in range(1, len(closes))
            if closes[idx] > 0 and closes[idx - 1] > 0]


def _factor_snapshot(window: list[dict[str, float]], setup: dict[str, Any],
                     ema20_val: float, ema50_val: float, atr_val: float,
                     event_ts: float, closes_4h: list[float],
                     funding_rate: float | None = None,
                     funding_change: float | None = None,
                     funding_percentile: float | None = None,
                     vol5: dict | None = None,
                     cross: dict | None = None) -> tuple[float, dict, dict, dict]:
    last = window[-1]
    volumes = [row["volume"] for row in
               window[-config.SHADOW_VOL_LOOKBACK - 1:-1]]
    vol_avg = sum(volumes) / len(volumes) if volumes else 0.0
    score, scored_dims = compute_shadow_score(
        setup["wick"], setup["body"], setup["touch"], ema20_val,
        ema50_val, atr_val, last["volume"], vol_avg, funding_rate, None,
        setup["direction"])
    # 0.5 只用于现役分数降级；研究证据仍必须诚实标记数据源缺失。
    dims = dict(scored_dims or {})
    if funding_rate is None:
        dims["funding"] = None
    dims["book"] = None
    closes = [row["close"] for row in window]
    returns = _log_returns(closes[-25:])
    last4 = returns[-4:]
    tm = time.gmtime(event_ts)
    vol5 = dict(vol5 or {})
    cross = dict(cross or {})
    factors = {name: None for name in REGISTRY}
    factors.update({
        "wick_ratio": (setup["wick"] / setup["body"]
                       if setup["body"] > 0 else None),
        "pullback_depth_atr": (abs(setup["touch"] - ema20_val) / atr_val
                               if atr_val > 0 else None),
        "trend_band_atr": ((ema20_val - ema50_val) / atr_val
                           if atr_val > 0 else None),
        "volume_ratio": (last["volume"] / vol_avg if vol_avg > 0 else None),
        "funding_rate": funding_rate,
        "funding_change": funding_change,
        "funding_percentile": funding_percentile,
        "realized_vol_1h": (math.sqrt(sum(value * value for value in last4))
                            if len(last4) == 4 else None),
        "realized_vol_5m": vol5.get("realized_vol_5m"),
        "vol_of_vol": vol5.get("vol_of_vol"),
        "har_rv": vol5.get("har_rv"),
        "downside_semivol_1h": (
            math.sqrt(sum(value * value for value in last4 if value < 0))
            if len(last4) == 4 else None),
        "atr_pct": atr_val / last["close"] if last["close"] else None,
        "momentum_1h": sum(last4) if len(last4) == 4 else None,
        "momentum_4h": sum(returns[-16:]) if len(returns) >= 16 else None,
        "hour_sin": math.sin(2 * math.pi * tm.tm_hour / 24),
        "hour_cos": math.cos(2 * math.pi * tm.tm_hour / 24),
        "weekend": 1.0 if tm.tm_wday >= 5 else 0.0,
        "source_latency_ms": 0.0,
        "cross_sectional_rank": cross.get("cross_sectional_rank"),
        "btc_residual_momentum": cross.get("btc_residual_momentum"),
        "btc_beta": cross.get("btc_beta"),
        "market_breadth": cross.get("market_breadth"),
        "correlation_concentration": cross.get(
            "correlation_concentration"),
    })
    factors.update(technical_regime_features(window))
    factors = materialize_derived_features(factors, dims)
    denominator = max(1, len(REGISTRY) - 1)
    factors["feature_missing_rate"] = (
        sum(value is None for name, value in factors.items()
            if name != "feature_missing_rate") / denominator)
    regime = compute_regime(window, closes_4h)
    return float(score or 0.0), dims, regime, factors


def _outcome_bars(rows_1m: list[list[float]], event_ms: int,
                  horizon_hours: int) -> list[list[float]]:
    times = [int(row[0]) for row in rows_1m]
    start = bisect.bisect_left(times, event_ms)
    end = bisect.bisect_left(times, event_ms + horizon_hours * 3_600_000)
    return rows_1m[start:end]


def _validate_output(market_db: str, output_db: str) -> None:
    market = os.path.realpath(market_db)
    output = os.path.realpath(output_db)
    if market == output:
        raise ValueError("输出研究库不能覆盖行情输入库")
    if os.path.basename(output) in RUNTIME_DB_NAMES:
        raise ValueError("拒绝把历史重放写入运行数据库")


def replay_symbol(reader: MarketReader, inst_id: str, output_db: str,
                  funding_universe: list[str] | None = None,
                  start_ms: int | None = None,
                  end_ms: int | None = None,
                  strategy_ids: list[str] | None = None) -> dict[str, Any]:
    if not inst_id.endswith("-USDT-SWAP"):
        raise ValueError("研究重放只接受 OKX USDT SWAP，不接受现货代理")
    base = inst_id.removesuffix("-USDT-SWAP")
    rows_15m = reader.series(inst_id, "15m")
    rows_1m = reader.series(inst_id, "1m")
    rows_1h = reader.series(inst_id, "1H")
    rows_4h = reader.series(inst_id, "4H")
    strategy_ids = list(strategy_ids or [config.ENTRY_SIGNAL_STRATEGY_ID])
    stats = {"bars": 0, "signals": 0, "created": 0, "settled": 0,
             "missing_path": 0, "funding_available": 0,
             "five_minute_available": 0, "cross_section_available": 0,
             "by_strategy": {
                 strategy_id: {"signals": 0, "created": 0, "settled": 0,
                               "missing_path": 0}
                 for strategy_id in strategy_ids}}
    for idx in range(59, len(rows_15m)):
        row = rows_15m[idx]
        event_ms = int(row[0]) + BAR_MS["15m"]
        if start_ms is not None and event_ms < start_ms:
            continue
        if end_ms is not None and event_ms > end_ms:
            continue
        stats["bars"] += 1
        raw_window = rows_15m[max(0, idx + 1 - config.SIGNAL_LOOKBACK_BARS):idx + 1]
        window = [{"open": item[1], "high": item[2], "low": item[3],
                   "close": item[4], "volume": item[5]} for item in raw_window]
        closes = [item["close"] for item in window]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        atr_val = atr(window, 14)
        if not e20 or not e50 or not atr_val or atr_val <= 0:
            continue
        trend_1h, _ = _trend(rows_1h, "1H", event_ms)
        trend_4h, closes_4h = _trend(rows_4h, "4H", event_ms)
        setup_a = detect_pullback_setup(
            window[-1], e20[-1], e50[-1], config.REJECT_WICK_RATIO,
            trend_1h, trend_4h, config.MTF_ENABLED)
        candidates: list[tuple[str, dict[str, Any]]] = []
        if (config.ENTRY_SIGNAL_STRATEGY_ID in strategy_ids and setup_a):
            candidates.append((config.ENTRY_SIGNAL_STRATEGY_ID, setup_a))
        if config.BREAKOUT_SIGNAL_STRATEGY_ID in strategy_ids:
            strategy_b_window = [item[:6] for item in raw_window]
            setup_b = breakout_signal(strategy_b_window)
            if setup_b:
                candidates.append((config.BREAKOUT_SIGNAL_STRATEGY_ID, setup_b))
        if not candidates:
            continue
        stats["signals"] += len(candidates)
        event_ts = event_ms / 1000.0
        funding_rate, funding_change = reader.funding_context_asof(
            inst_id, event_ms)
        funding_percentile = reader.funding_percentile_asof(
            inst_id, event_ms, funding_universe or [inst_id])
        vol5 = volatility_5m_features(reader.five_minute_closes_asof(
            inst_id, event_ms, config.FACTOR_5M_LOOKBACK_BARS))
        cross_snapshot = reader.cross_section_asof(
            event_ms, funding_universe or [inst_id])
        cross = {"market_breadth": cross_snapshot.get("market_breadth"),
                 "correlation_concentration": cross_snapshot.get(
                     "correlation_concentration"),
                 **((cross_snapshot.get("by_symbol") or {}).get(base, {}))}
        availability_n = len(candidates)
        stats["funding_available"] += availability_n * int(
            funding_rate is not None)
        stats["five_minute_available"] += availability_n * int(
            vol5.get("realized_vol_5m") is not None)
        stats["cross_section_available"] += availability_n * int(
            cross.get("market_breadth") is not None)
        for strategy_id, setup in candidates:
            direction = str(setup.get("direction") or setup.get("dir"))
            entry = float(setup.get("entry") or window[-1]["close"])
            swing = (max(item["high"] for item in window[-21:-1])
                     if direction == "long" else
                     min(item["low"] for item in window[-21:-1]))
            if strategy_id == config.ENTRY_SIGNAL_STRATEGY_ID:
                score, dims, regime, factors = _factor_snapshot(
                    window, setup, e20[-1], e50[-1], atr_val, event_ts,
                    closes_4h, funding_rate, funding_change,
                    funding_percentile, vol5, cross)
                market_regime = classify_market_regime(regime, factors)
                strategy_route = route_strategy(
                    market_regime,
                    available=config.MARKET_REGIME_IMPLEMENTED_STRATEGIES)
                regime = dict(regime or {})
                regime["market_state"] = market_regime
                regime["strategy_route"] = strategy_route
                sig = {
                    "strategy_id": strategy_id,
                    "dir": direction, "entry": entry,
                    "stop": (entry - config.STOP_ATR_MULT * atr_val
                             if direction == "long" else
                             entry + config.STOP_ATR_MULT * atr_val),
                    "tp": (entry + config.TP_ATR_MULT * atr_val
                           if direction == "long" else
                           entry - config.TP_ATR_MULT * atr_val),
                    "atr": atr_val, "shadow_score": score,
                    "shadow_dims": dims, "factor_features": factors,
                    "targets": compute_targets(
                        entry, atr_val, direction, swing),
                    "forecast": None, "regime": regime,
                    "market_regime": market_regime,
                    "strategy_route": strategy_route,
                    "kline_ts": int(row[0]),
                }
            else:
                sig = enrich_shadow_signal(
                    setup, strategy_b_window, cross=cross,
                    closes_4h=closes_4h,
                    funding_rate=funding_rate,
                    funding_change=funding_change,
                    funding_percentile=funding_percentile, vol5=vol5,
                    event_ts=event_ts, source_latency_ms=0.0)
                sig["targets"] = compute_targets(
                    entry, atr_val, direction, swing)
                sig["forecast"] = None
            from decision.forecast import forecast_for_trade
            seed = int(hashlib.sha256(
                f"{config.FORECAST_REPLAY_SEED_VERSION}|{inst_id}|"
                f"{int(row[0])}|{direction}".encode()
            ).hexdigest()[:16], 16)
            sig["forecast"] = forecast_for_trade(
                sig, base, window, db_path=output_db,
                empirical_enabled=False, as_of_ts=event_ts, seed=seed)
            signal_id, created = record_signal_sample(
                base, sig, "swap", db_path=output_db, event_ts=event_ts)
            stats["created"] += int(created)
            stats["by_strategy"][strategy_id]["signals"] += 1
            stats["by_strategy"][strategy_id]["created"] += int(created)
            update_signal_decision(
                signal_id, db_path=output_db,
                rule_decision="historical_candidate",
                final_decision="research_only")
            merge_sample_features(signal_id, {
                "provenance": {
                    "kind": "historical_replay", "version": REPLAY_VERSION,
                    "strategy_id": strategy_id,
                    "source_inst_id": inst_id,
                    "entry_proxy": "closed_15m_close",
                    "forecast_mode": "bootstrap_no_empirical",
                    "funding_source": (
                        "latest_completed_funding_asof" if
                        funding_rate is not None else "unavailable"),
                    "five_minute_source": "causal_1m_to_5m",
                    "cross_section_source": "same_time_15m_universe",
                    "unavailable": (["book", "oi"] if
                                    funding_rate is not None else
                                    ["funding", "book", "oi"])},
                }, db_path=output_db)
            import storage.db as sdb
            sample = sdb.q1("SELECT * FROM signal_samples WHERE signal_id=?",
                            [signal_id], db_path=output_db)
            outcome = settle_path(
                sample, _outcome_bars(
                    rows_1m, event_ms,
                    config.SIGNAL_OUTCOME_HORIZON_HOURS),
                bar_resolution="1m",
                label_version=config.SIGNAL_OUTCOME_LABEL_VERSION)
            if outcome is None:
                stats["missing_path"] += 1
                stats["by_strategy"][strategy_id]["missing_path"] += 1
                continue
            persist_outcome(outcome, db_path=output_db)
            stats["settled"] += 1
            stats["by_strategy"][strategy_id]["settled"] += 1
    return stats


def replay_market(market_db: str, output_db: str,
                  symbols: list[str] | None = None,
                  start_ms: int | None = None,
                  end_ms: int | None = None,
                  strategy_ids: list[str] | None = None) -> dict[str, Any]:
    _validate_output(market_db, output_db)
    reader = MarketReader(market_db)
    try:
        available = reader.symbols()
        requested = symbols or available
        strategy_ids = list(strategy_ids or
                            [config.ENTRY_SIGNAL_STRATEGY_ID])
        supported = {config.ENTRY_SIGNAL_STRATEGY_ID,
                     config.BREAKOUT_SIGNAL_STRATEGY_ID}
        unknown = sorted(set(strategy_ids) - supported)
        if unknown:
            raise ValueError("不支持的研究策略: " + ",".join(unknown))
        invalid = sorted(set(requested) - set(available))
        if invalid:
            raise ValueError("缺少完整 SWAP 周期数据: " + ",".join(invalid))
        totals = {"symbols": len(requested), "bars": 0, "signals": 0,
                  "created": 0, "settled": 0, "missing_path": 0,
                  "funding_available": 0, "five_minute_available": 0,
                  "cross_section_available": 0,
                  "by_strategy": {
                      strategy_id: {"signals": 0, "created": 0,
                                    "settled": 0, "missing_path": 0}
                      for strategy_id in strategy_ids}}
        per_symbol = {}
        for inst_id in requested:
            result = replay_symbol(reader, inst_id, output_db, requested,
                                   start_ms, end_ms, strategy_ids)
            per_symbol[inst_id] = result
            for name, value in result.items():
                if name == "by_strategy":
                    for strategy_id, counts in value.items():
                        for metric, count in counts.items():
                            totals[name][strategy_id][metric] += count
                else:
                    totals[name] += value
        metadata = {"replay_version": REPLAY_VERSION, "research_only": True,
                    "forecast_seed_version": config.FORECAST_REPLAY_SEED_VERSION,
                    "market_db": os.path.abspath(market_db),
                    "source_venue": "OKX SWAP", "created_at": time.time(),
                    "strategy_ids": strategy_ids,
                    "totals": totals, "per_symbol": per_symbol}
        import storage.db as sdb
        sdb.x("INSERT OR REPLACE INTO kv (key,value,updated_at) VALUES (?,?,?)",
              ["research.15m_replay.latest",
               json.dumps(metadata, ensure_ascii=False, sort_keys=True),
               time.time()], db_path=output_db)
        return metadata
    finally:
        reader.close()


def inventory(market_db: str) -> dict[str, Any]:
    reader = MarketReader(market_db)
    try:
        symbols = reader.symbols()
        return {"market_db": os.path.abspath(market_db),
                "eligible_swap_symbols": len(symbols), "symbols": symbols}
    finally:
        reader.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真实 OKX SWAP 15m 候选/4h 路径独立研究重放")
    parser.add_argument("--market-db", default=str(ROOT / "data" / "market.db"))
    parser.add_argument("--output-db")
    parser.add_argument("--symbols", help="逗号分隔的完整 instId")
    parser.add_argument(
        "--strategies", default=config.ENTRY_SIGNAL_STRATEGY_ID,
        help="逗号分隔策略身份；可选 A_pullback,B_breakout，默认只重放 A")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--backfill-market", action="store_true",
                        help="从 OKX 公共 history-candles 写独立行情库")
    parser.add_argument("--funding-only", action="store_true",
                        help="配合 --backfill-market，仅补资金费历史，不重抓 K 线")
    parser.add_argument("--days", type=int, default=8)
    parser.add_argument("--context-days", type=int, default=30)
    parser.add_argument("--request-delay", type=float, default=.12)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    symbols = ([item.strip() for item in args.symbols.split(",") if item.strip()]
               if args.symbols else None)
    strategy_ids = [item.strip() for item in args.strategies.split(",")
                    if item.strip()]
    if args.backfill_market:
        if not symbols:
            parser.error("--backfill-market 必须显式提供 --symbols")
        result = backfill_swap_market(
            args.market_db, symbols, days=args.days,
            context_days=args.context_days, request_delay=args.request_delay,
            threads=args.threads, retries=args.retries,
            include_klines=not args.funding_only, progress=True)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["complete"] else 1
    if not args.apply:
        print(json.dumps(inventory(args.market_db), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.output_db:
        parser.error("--apply 必须显式提供 --output-db（独立研究库）")
    result = replay_market(args.market_db, args.output_db, symbols,
                           args.start_ms, args.end_ms, strategy_ids)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
